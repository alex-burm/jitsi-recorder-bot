"""
XMPP over WebSocket client for Jitsi signaling.
Implements RFC 7590 (XMPP over WebSocket) and Jingle signaling.
Uses websockets library directly for full control.
"""

import asyncio
import base64
import logging
import random
import re
import string
import uuid
import xml.etree.ElementTree as ET
from typing import Callable, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

try:
    from urllib.request import urlopen, Request
except ImportError:
    pass

logger = logging.getLogger(__name__)

# XMPP namespaces
NS_FRAMING = "urn:ietf:params:xml:ns:xmpp-framing"
NS_SASL = "urn:ietf:params:xml:ns:xmpp-sasl"
NS_BIND = "urn:ietf:params:xml:ns:xmpp-bind"
NS_CLIENT = "jabber:client"
NS_JINGLE = "urn:xmpp:jingle:1"
NS_MUC = "http://jabber.org/protocol/muc"
NS_FOCUS = "http://jitsi.org/protocol/focus"
NS_DISCO_INFO = "http://jabber.org/protocol/disco#info"


def _rand_id(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class XMPPWebSocketClient:
    """
    Minimal XMPP-over-WebSocket client.
    Supports anonymous auth, MUC join, Jingle IQ handling.
    """

    # Known anonymous domains for public Jitsi servers
    ANONYMOUS_DOMAINS = {
        "meet.jit.si": "guest.meet.jit.si",
    }

    def __init__(
        self,
        server: str,
        room_name: str,
        on_session_initiate: Callable,
        on_transport_info: Callable,
        on_session_terminate: Callable = None,
        token: Optional[str] = None,
        nick: str = "recorder",
        xmpp_domain: Optional[str] = None,
        conference_domain: Optional[str] = None,
    ):
        self.server = server
        self.room_name = room_name
        self.on_session_initiate = on_session_initiate
        self.on_transport_info = on_transport_info
        self.on_session_terminate = on_session_terminate
        self.token = token
        self.nick = nick

        self.ws_url = f"wss://{server}/xmpp-websocket"

        # For anonymous access, use guest domain if known (e.g. meet.jit.si → guest.meet.jit.si)
        if xmpp_domain:
            self.xmpp_domain = xmpp_domain
        elif not token:
            self.xmpp_domain = self.ANONYMOUS_DOMAINS.get(server, server)
        else:
            self.xmpp_domain = server

        self.conference_domain = conference_domain or f"conference.{server}"
        self.muc_jid = f"{room_name}@{self.conference_domain}/{nick}"
        self.room_jid = f"{room_name}@{self.conference_domain}"
        self.focus_target_jid = f"focus.{server}"

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._my_jid: Optional[str] = None
        self._session_sid: Optional[str] = None
        self._focus_jid: Optional[str] = None

        # Events are created lazily in connect() to bind to the running event loop
        self._connected_event: Optional[asyncio.Event] = None
        self._joined_event: Optional[asyncio.Event] = None
        self._running = False
        self._join_error: Optional[str] = None
        self.ice_servers: list = []  # STUN/TURN servers from service discovery

        # Buffer for partial XML (XMPP over WebSocket sends complete stanzas)
        self._recv_task: Optional[asyncio.Task] = None
        self._pending_iq: dict = {}

    def _resolve_ws_url(self) -> str:
        """
        For meet.jit.si, the WebSocket URL is shard-specific.
        Fetch the room page to get the correct shard URL.
        Falls back to default if not detectable.
        """
        if self.server != "meet.jit.si":
            return self.ws_url
        try:
            url = f"https://{self.server}/{self.room_name}"
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=5) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            # Extract shard WebSocket URL
            m = re.search(r'wss://[^\s\'"]*xmpp-websocket[^\s\'"`]*', html)
            if m:
                ws = m.group(0).split("`")[0].split("'")[0].split('"')[0]
                # Remove template literal variable
                ws = re.sub(r'\$\{[^}]+\}', '', ws).rstrip("?&")
                logger.info(f"Resolved shard WS URL: {ws}")
                return ws
        except Exception as e:
            logger.debug(f"Could not resolve shard URL: {e}")
        return self.ws_url

    async def connect(self):
        """Connect, authenticate, and join the MUC room."""
        # Create events here so they bind to the running event loop
        self._connected_event = asyncio.Event()
        self._joined_event = asyncio.Event()

        # Resolve correct shard URL (important for meet.jit.si)
        ws_url = self._resolve_ws_url()
        logger.info(f"Connecting to {ws_url}")

        self._ws = await websockets.connect(
            ws_url,
            subprotocols=["xmpp"],
            additional_headers={
                "Origin": f"https://{self.server}",
            },
            ping_interval=30,
            ping_timeout=10,
            open_timeout=15,
        )
        self._running = True

        # Start receive loop
        self._recv_task = asyncio.ensure_future(self._receive_loop())

        # 1. Send stream open
        stream_id = _rand_id(12)
        await self._send_raw(
            f'<open xmlns="{NS_FRAMING}" '
            f'to="{self.xmpp_domain}" '
            f'version="1.0"/>'
        )

        # Wait for auth + resource bind
        await asyncio.wait_for(self._connected_event.wait(), timeout=30)
        logger.info("XMPP authenticated, waiting for MUC join...")

        # Generous timeout: bot waits for the room to be created by someone else
        await asyncio.wait_for(self._joined_event.wait(), timeout=300)

        if self._join_error and "restricted" in self._join_error.lower():
            raise ConnectionError(f"Cannot join room (still restricted): {self._join_error}")

        logger.info(f"Joined MUC: {self.muc_jid}")

    async def _send_raw(self, data: str):
        if self._ws:
            logger.debug(f"SEND: {data[:300]}")
            await self._ws.send(data)

    async def _receive_loop(self):
        """Continuously receive and dispatch XMPP stanzas."""
        try:
            async for message in self._ws:
                logger.debug(f"RECV: {str(message)[:400]}")
                await self._dispatch(str(message))
        except ConnectionClosed as e:
            logger.warning(f"WebSocket closed: {e}")
        except Exception as e:
            logger.error(f"Receive loop error: {e}", exc_info=True)
        finally:
            self._running = False

    async def _dispatch(self, data: str):
        """Parse and dispatch an XMPP stanza."""
        try:
            # XMPP over WebSocket: each message is a complete element
            # But <open> and <close> are self-closing stream-level elements
            if data.startswith("<open "):
                await self._handle_stream_open(data)
                return
            if data.startswith("<close"):
                logger.info("Stream closed by server")
                return

            elem = ET.fromstring(data)
            tag = elem.tag
            local = tag.split("}", 1)[1] if "}" in tag else tag

            if local == "features":
                await self._handle_features(elem)
            elif local == "success":
                await self._handle_sasl_success()
            elif local == "failure":
                logger.error(f"SASL failure: {ET.tostring(elem, encoding='unicode')}")
            elif local == "iq":
                await self._handle_iq(elem)
            elif local == "presence":
                await self._handle_presence(elem)
            elif local == "message":
                self._parse_services(elem)
            else:
                logger.debug(f"Unhandled stanza: {local}")

        except ET.ParseError as e:
            logger.debug(f"XML parse error (fragment?): {e} — data: {data[:200]}")
        except Exception as e:
            logger.error(f"Dispatch error: {e}", exc_info=True)

    async def _handle_stream_open(self, data: str):
        """Handle <open> stream element."""
        logger.debug("Stream opened by server")
        # Features will arrive as next stanza

    async def _handle_features(self, elem: ET.Element):
        """Handle <stream:features> element."""
        # Check for SASL mechanisms
        mechanisms_elem = elem.find(f"{{{NS_SASL}}}mechanisms")
        if mechanisms_elem is not None:
            mechs = [m.text for m in mechanisms_elem.findall(f"{{{NS_SASL}}}mechanism")]
            logger.info(f"SASL mechanisms: {mechs}")

            if self.token:
                # JWT auth: use PLAIN with token as password
                # JID: "token@domain" or just send JWT in correct format
                # Jitsi uses: user@domain with token as password
                local = _rand_id(12)
                auth_jid = f"{local}@{self.xmpp_domain}"
                # PLAIN: \0user\0password
                plain = base64.b64encode(
                    f"\0{local}\0{self.token}".encode()
                ).decode()
                await self._send_raw(
                    f'<auth xmlns="{NS_SASL}" mechanism="PLAIN">{plain}</auth>'
                )
            elif "ANONYMOUS" in mechs:
                logger.info("Authenticating with SASL ANONYMOUS")
                await self._send_raw(
                    f'<auth xmlns="{NS_SASL}" mechanism="ANONYMOUS"/>'
                )
            else:
                logger.error(f"No supported SASL mechanism. Available: {mechs}")
                return

        else:
            # Post-auth features: bind resource
            bind_elem = elem.find(f"{{{NS_BIND}}}bind")
            if bind_elem is not None:
                await self._bind_resource()

    async def _handle_sasl_success(self):
        """SASL auth succeeded. Restart stream."""
        logger.info("SASL authentication successful")
        # Restart stream
        await self._send_raw(
            f'<open xmlns="{NS_FRAMING}" '
            f'to="{self.xmpp_domain}" '
            f'version="1.0"/>'
        )

    async def _bind_resource(self):
        """Bind XMPP resource."""
        iq_id = _rand_id()
        resource = f"recorder-{_rand_id(6)}"
        await self._send_raw(
            f'<iq xmlns="{NS_CLIENT}" type="set" id="{iq_id}">'
            f'<bind xmlns="{NS_BIND}">'
            f'<resource>{resource}</resource>'
            f'</bind>'
            f'</iq>'
        )

    async def _handle_iq(self, elem: ET.Element):
        """Handle incoming IQ stanzas."""
        iq_type = elem.get("type")
        iq_id = elem.get("id")
        from_jid = elem.get("from", "")
        to_jid = elem.get("to", "")

        # Resolve any pending IQ request waiter.
        if iq_id and iq_type in ("result", "error"):
            fut = self._pending_iq.pop(iq_id, None)
            if fut is not None and not fut.done():
                fut.set_result(elem)

        # Bind result
        if iq_type == "result":
            bind_elem = elem.find(f"{{{NS_BIND}}}bind")
            if bind_elem is not None:
                jid_elem = bind_elem.find(f"{{{NS_BIND}}}jid")
                if jid_elem is not None and jid_elem.text:
                    self._my_jid = jid_elem.text
                    logger.info(f"Bound JID: {self._my_jid}")
                    # Connection established
                    self._connected_event.set()
                    # Request conference allocation from focus before joining MUC.
                    await self._request_conference()
                    # Join MUC
                    await self._join_muc()
                    return

        # Jingle IQ
        if iq_type == "set":
            jingle_elem = None
            for child in elem:
                local = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
                if local == "jingle":
                    jingle_elem = child
                    break

            if jingle_elem is not None:
                # ACK immediately
                await self._send_raw(
                    f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" to="{from_jid}" type="result"/>'
                )
                action = jingle_elem.get("action")
                sid = jingle_elem.get("sid")
                logger.info(f"Jingle action={action} sid={sid} from={from_jid}")

                # Detect P2P vs JVB: if from_jid resource is not 'focus', it's P2P
                resource = from_jid.split("/")[-1] if "/" in from_jid else from_jid
                is_p2p = resource != "focus"

                if action == "session-initiate":
                    # Accept any session-initiate: P2P (from participant) or JVB (from focus)
                    logger.info(f"Accepting {'P2P' if is_p2p else 'JVB'} session {sid} from {from_jid}")
                    self._session_sid = sid
                    self._focus_jid = from_jid
                    asyncio.ensure_future(
                        self.on_session_initiate(jingle_elem, sid, from_jid)
                    )
                elif action == "transport-info":
                    # Only process transport-info for current active session
                    if sid == self._session_sid:
                        asyncio.ensure_future(
                            self.on_transport_info(jingle_elem, sid)
                        )
                elif action == "session-terminate":
                    logger.info(f"Session terminated: sid={sid} from={from_jid}")
                    if self.on_session_terminate:
                        asyncio.ensure_future(
                            self.on_session_terminate(sid, from_jid)
                        )
                return

            # Unhandled set IQ — send service-unavailable
            await self._send_raw(
                f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" to="{from_jid}" type="error">'
                f'<error type="cancel">'
                f'<service-unavailable xmlns="urn:ietf:params:xml:ns:xmpp-stanzas"/>'
                f'</error>'
                f'</iq>'
            )
            return

        # Reply to basic disco#info queries so other participants/focus can discover client capabilities.
        if iq_type == "get":
            query = None
            for child in elem:
                local = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
                if local == "query":
                    query = child
                    break
            if query is not None and query.tag.startswith(f"{{{NS_DISCO_INFO}}}"):
                node_attr = query.get("node", "")
                node_xml = f' node="{node_attr}"' if node_attr else ""
                result = (
                    f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" to="{from_jid}" type="result">'
                    f'<query xmlns="{NS_DISCO_INFO}"{node_xml}>'
                    f'<identity category="client" type="pc" name="jitsi-recorder-bot"/>'
                    f'<feature var="urn:xmpp:jingle:1"/>'
                    f'<feature var="urn:xmpp:jingle:apps:rtp:1"/>'
                    f'<feature var="urn:xmpp:jingle:apps:rtp:audio"/>'
                    f'<feature var="urn:xmpp:jingle:transports:ice-udp:1"/>'
                    f'<feature var="urn:xmpp:jingle:apps:dtls:0"/>'
                    f'<feature var="urn:ietf:rfc:5761"/>'
                    f'<feature var="urn:ietf:rfc:5888"/>'
                    f'</query>'
                    f'</iq>'
                )
                await self._send_raw(result)
                return

            # Unknown iq/get
            await self._send_raw(
                f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" to="{from_jid}" type="error">'
                f'<error type="cancel">'
                f'<service-unavailable xmlns="urn:ietf:params:xml:ns:xmpp-stanzas"/>'
                f'</error>'
                f'</iq>'
            )
            return

    async def _send_iq_wait(self, iq_xml: str, iq_id: str, timeout: float = 10.0):
        """Send IQ stanza and wait for matching result/error by id."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_iq[iq_id] = fut
        try:
            await self._send_raw(iq_xml)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_iq.pop(iq_id, None)

    async def _request_conference(self, timeout: float = 1.5):
        """Request conference allocation from focus (Jicofo)."""
        iq_id = _rand_id()
        machine_uid = uuid.uuid4().hex
        token_attr = f' token="{self.token}"' if self.token else ""
        iq = (
            f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" to="{self.focus_target_jid}" type="set">'
            f'<conference xmlns="{NS_FOCUS}" room="{self.room_jid}" machine-uid="{machine_uid}"{token_attr}>'
            f'<property name="rtcstatsEnabled" value="false"/>'
            f'</conference>'
            f'</iq>'
        )

        try:
            logger.info(f"Sending conference request to {self.focus_target_jid}")
            result = await self._send_iq_wait(iq, iq_id, timeout=timeout)
            if result.get("type") == "error":
                logger.warning("Conference request returned IQ error; continuing anyway")
                return

            conference_elem = None
            for child in result:
                local = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
                if local == "conference":
                    conference_elem = child
                    break
            if conference_elem is not None:
                focus_jid = conference_elem.get("focusjid")
                ready = conference_elem.get("ready")
                if focus_jid:
                    logger.info(f"Focus JID from conference request: {focus_jid}")
                    self._focus_jid = focus_jid
                logger.info(f"Conference request ready={ready}")
        except asyncio.TimeoutError:
            logger.info(
                f"Conference request timeout after {timeout:.1f}s; continuing with MUC join"
            )
        except Exception as e:
            logger.warning(f"Conference request failed: {e}")

    async def _handle_presence(self, elem: ET.Element):
        """Handle presence stanzas."""
        from_jid = elem.get("from", "")
        ptype = elem.get("type", "available")

        # MUC join confirmation: presence from our own nick in the room
        if (
            self.conference_domain in from_jid
            and self.room_name in from_jid
            and self.nick in from_jid
        ):
            if ptype == "available":
                logger.debug(f"MUC presence from {from_jid} — joined!")
                self._join_error = None
                self._joined_event.set()
            elif ptype == "error":
                # Extract error text
                try:
                    text_elem = elem.find(".//{urn:ietf:params:xml:ns:xmpp-stanzas}text")
                    error_text = text_elem.text if text_elem is not None else "unknown error"
                except Exception:
                    error_text = "unknown error"
                logger.warning(f"MUC join error: {error_text}")
                self._join_error = error_text
                if "restricted" in error_text.lower() or "not allowed" in error_text.lower():
                    logger.info("Room not available yet, retrying in 3s...")
                    asyncio.ensure_future(self._retry_join(delay=3.0))
                else:
                    self._joined_event.set()  # unblock with error

    def _parse_services(self, elem: ET.Element):
        """Parse STUN/TURN servers from Jitsi service discovery message."""
        for services in elem.iter("{urn:xmpp:extdisco:2}services"):
            for svc in services:
                stype = svc.get("type", "")
                host = svc.get("host", "")
                port = svc.get("port", "3478")
                username = svc.get("username")
                password = svc.get("password")
                transport = svc.get("transport", "udp")

                if stype == "stun":
                    url = f"stun:{host}:{port}"
                    if not any(s.get("urls") == url for s in self.ice_servers):
                        self.ice_servers.append({"urls": url})
                        logger.info(f"STUN server: {url}")
                elif stype in ("turn", "turns"):
                    url = f"{stype}:{host}:{port}?transport={transport}"
                    if username and password:
                        entry = {"urls": url, "username": username, "credential": password}
                        if not any(s.get("urls") == url for s in self.ice_servers):
                            self.ice_servers.append(entry)
                            logger.info(f"TURN server: {url} (user={username})")

    async def _send_session_terminate(self, sid: str, to_jid: str):
        """Send Jingle session-terminate to reject a P2P session."""
        iq_id = _rand_id()
        iq = (
            f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" '
            f'to="{to_jid}" from="{self._my_jid}" type="set">'
            f'<jingle xmlns="{NS_JINGLE}" action="session-terminate" sid="{sid}">'
            f'<reason><decline/></reason>'
            f'</jingle>'
            f'</iq>'
        )
        await self._send_raw(iq)

    def send_session_terminate(self, sid: str, to_jid: str):
        """Public wrapper for async session-terminate sender."""
        logger.info(f"Sending session-terminate sid={sid} to {to_jid}")
        asyncio.ensure_future(self._send_session_terminate(sid, to_jid))

    async def _retry_join(self, delay: float = 3.0):
        """Retry joining the MUC after a delay."""
        await asyncio.sleep(delay)
        if self._running and not self._joined_event.is_set():
            await self._join_muc()

    async def _join_muc(self):
        """Send presence to join the MUC room."""
        logger.info(f"Joining MUC: {self.muc_jid}")
        await self._send_raw(
            f'<presence xmlns="{NS_CLIENT}" to="{self.muc_jid}">'
            f'<x xmlns="{NS_MUC}">'
            f'<history maxstanzas="0"/>'
            f'</x>'
            f'<nick xmlns="http://jabber.org/protocol/nick">{self.nick}</nick>'
            f'<c xmlns="http://jabber.org/protocol/caps" '
            f'hash="sha-1" '
            f'node="https://jitsi.org/jitsi-meet" '
            f'ver=""/>'
            f'</presence>'
        )

    def send_session_accept(self, jingle_xml: str, to_jid: str):
        """Send session-accept IQ."""
        iq_id = _rand_id()
        iq = (
            f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" '
            f'to="{to_jid}" from="{self._my_jid}" type="set">'
            f"{jingle_xml}"
            f"</iq>"
        )
        logger.info(f"Sending session-accept to {to_jid}")
        asyncio.ensure_future(self._send_raw(iq))

    def send_transport_info(
        self, candidates: list, sid: str, to_jid: str, mid: str = "0"
    ):
        """Send transport-info IQ with ICE candidates."""
        if not candidates:
            return

        cands_xml = ""
        for cand in candidates:
            attrs = " ".join(f'{k}="{v}"' for k, v in cand.items())
            cands_xml += f"<candidate {attrs}/>"

        iq_id = _rand_id()
        iq = (
            f'<iq xmlns="{NS_CLIENT}" id="{iq_id}" '
            f'to="{to_jid}" from="{self._my_jid}" type="set">'
            f'<jingle xmlns="{NS_JINGLE}" action="transport-info" sid="{sid}">'
            f'<content creator="responder" name="{mid}">'
            f'<transport xmlns="urn:xmpp:jingle:transports:ice-udp:1">'
            f"{cands_xml}"
            f"</transport>"
            f"</content>"
            f"</jingle>"
            f"</iq>"
        )
        logger.debug(f"Sending transport-info ({len(candidates)} candidates)")
        asyncio.ensure_future(self._send_raw(iq))

    def _ws_is_open(self) -> bool:
        """Check if WebSocket is still open (compatible with websockets 15.x)."""
        if not self._ws:
            return False
        try:
            # websockets 15.x uses close_code; older uses .closed
            return getattr(self._ws, "close_code", None) is None and not getattr(self._ws, "closed", False)
        except Exception:
            return False

    async def disconnect(self):
        """Send leave presence and close WebSocket."""
        if self._ws_is_open():
            try:
                leave = (
                    f'<presence xmlns="{NS_CLIENT}" '
                    f'to="{self.muc_jid}" type="unavailable"/>'
                )
                await self._send_raw(leave)
                await asyncio.sleep(0.3)
                await self._send_raw(f'<close xmlns="{NS_FRAMING}"/>')
                await self._ws.close()
            except Exception as e:
                logger.debug(f"Disconnect error (ignored): {e}")

        if self._recv_task:
            self._recv_task.cancel()


# Alias for import compatibility
JitsiXMPPClient = XMPPWebSocketClient
