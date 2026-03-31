"""
JitsiBot: orchestrates XMPP signaling and WebRTC media.
"""

import asyncio
import os
import logging
from typing import Optional
from xml.etree import ElementTree as ET

from audio_recorder import AudioRecorder
from jingle_sdp import jingle_to_sdp, sdp_to_jingle, candidate_to_jingle
from webrtc_peer import WebRTCPeer
from xmpp_client import JitsiXMPPClient

logger = logging.getLogger(__name__)


class JitsiBot:
    def __init__(
        self,
        server: str,
        room_name: str,
        output_path: str,
        duration: Optional[float] = None,
        token: Optional[str] = None,
    ):
        self.server = server
        self.room_name = room_name
        self.output_path = output_path
        self.duration = duration
        self.token = token

        self._recorder = AudioRecorder(output_path)
        self._peer: Optional[WebRTCPeer] = None
        self._xmpp: Optional[JitsiXMPPClient] = None

        self._session_sid: Optional[str] = None
        self._focus_jid: Optional[str] = None
        self._pending_candidates: list = []      # local candidates buffered before session ready
        self._remote_candidates_buffer: list = []  # remote candidates buffered before PC ready
        self._pc_ready = False
        self._track_tasks: list = []

        self._stop_event: Optional[asyncio.Event] = None
        self._ice_mode_sequence = self._build_ice_mode_sequence()
        self._ice_mode_index = 0

    async def run(self):
        """Main entry point. Connects, records, stops."""
        logger.info(f"Starting JitsiBot: server={self.server} room={self.room_name}")

        # Create event in running loop
        self._stop_event = asyncio.Event()

        # Setup recorder
        self._recorder.start()

        # Setup WebRTC peer
        self._peer = WebRTCPeer(
            on_ice_candidate=self._on_local_ice_candidate,
            on_track=self._on_remote_track,
        )

        # Setup XMPP client
        self._xmpp = JitsiXMPPClient(
            server=self.server,
            room_name=self.room_name,
            on_session_initiate=self._on_session_initiate,
            on_transport_info=self._on_transport_info,
            on_session_terminate=self._on_session_terminate,
            token=self.token,
            nick="recorder",
        )

        try:
            # Connect XMPP and join room
            await self._xmpp.connect()

            logger.info("Waiting for Jingle session-initiate from Jicofo...")

            # Wait for recording duration or manual stop
            if self.duration:
                logger.info(f"Will record for {self.duration}s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.duration)
                except asyncio.TimeoutError:
                    logger.info(f"Recording duration ({self.duration}s) reached")
            else:
                logger.info("Recording until Ctrl+C...")
                await self._stop_event.wait()

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Error during session: {e}", exc_info=True)
        finally:
            await self._cleanup()

    def _on_local_ice_candidate(self, candidate):
        """Called when aiortc generates a local ICE candidate."""
        if not self._session_sid or not self._focus_jid:
            # Buffer candidates until session is established
            self._pending_candidates.append(candidate)
            return

        cand_dict = candidate_to_jingle(f"a={candidate.candidate}")
        if cand_dict:
            self._xmpp.send_transport_info(
                [cand_dict],
                self._session_sid,
                self._focus_jid,
                mid=candidate.sdpMid or "0",
            )

    def _on_remote_track(self, track):
        """Called when a remote audio track arrives."""
        logger.info(f"Starting to record audio track: {track.id}")
        task = asyncio.ensure_future(self._recorder.track_handler(track))
        self._track_tasks.append(task)

    def _build_ice_mode_sequence(self):
        """
        Build ICE fallback order.
        Default is host -> stun -> full for low-latency with reliability fallback.
        """
        valid = ["host", "stun", "full"]
        env_mode = os.getenv("JITSI_BOT_ICE_MODE", "").strip().lower()
        if env_mode in valid:
            start = valid.index(env_mode)
            return valid[start:]
        return valid

    def _current_ice_mode(self) -> str:
        return self._ice_mode_sequence[self._ice_mode_index]

    def _advance_ice_mode(self) -> bool:
        if self._ice_mode_index + 1 < len(self._ice_mode_sequence):
            self._ice_mode_index += 1
            return True
        return False

    def _ice_timeout_for_mode(self, mode: str) -> float:
        if mode == "host":
            return 2.5
        if mode == "stun":
            return 4.0
        return 8.0

    async def _reset_peer_for_new_session(self):
        # Cancel active track tasks
        for task in self._track_tasks:
            task.cancel()
        self._track_tasks.clear()

        if self._peer:
            await self._peer.close()
        self._peer = WebRTCPeer(
            on_ice_candidate=self._on_local_ice_candidate,
            on_track=self._on_remote_track,
        )

        self._session_sid = None
        self._focus_jid = None
        self._pc_ready = False
        self._pending_candidates.clear()
        self._remote_candidates_buffer.clear()

    async def _on_session_initiate(
        self, jingle_elem: ET.Element, sid: str, from_jid: str
    ):
        """Handle incoming Jingle session-initiate from Jicofo."""
        logger.info(f"Processing session-initiate: sid={sid}")

        try:
            resource = from_jid.split("/", 1)[1] if "/" in from_jid else ""
            is_p2p = resource != "focus"

            # P2P session from participant is currently unstable in our SDP<->Jingle bridge.
            # Reject it immediately to force JVB session without waiting for P2P timeout.
            if is_p2p:
                logger.info(f"Rejecting P2P session {sid} from {from_jid} to force JVB")
                self._xmpp.send_session_terminate(sid, from_jid)
                return

            self._session_sid = sid
            self._focus_jid = from_jid

            # Preserve original Jingle content names/order for session-accept.
            content_name_order = []
            for child in jingle_elem:
                local = child.tag.split("}", 1)[1] if "}" in child.tag else child.tag
                if local == "content":
                    name = child.get("name")
                    if name:
                        content_name_order.append(name)

            # Convert Jingle to SDP offer
            offer_sdp = jingle_to_sdp(jingle_elem)
            logger.debug(f"Converted offer SDP:\n{offer_sdp[:800]}")

            raw_ice_servers = self._xmpp.ice_servers if self._xmpp else []
            ice_mode = self._current_ice_mode()

            if ice_mode == "full":
                ice_servers = raw_ice_servers
            elif ice_mode == "host":
                ice_servers = []
            else:
                # Default low-latency mode: STUN-only.
                ice_mode = "stun"
                stun_only = [
                    s for s in raw_ice_servers
                    if str(s.get("urls", "")).startswith("stun:")
                ]
                ice_servers = stun_only if stun_only else []

            logger.info(
                f"Session type: JVB (ICE mode={ice_mode}, servers={len(ice_servers)})"
            )
            answer_sdp = await self._peer.create_answer(
                offer_sdp,
                ice_servers=ice_servers,
                fast_start=True,
            )
            logger.debug(f"Generated answer SDP:\n{answer_sdp[:800]}")

            # Convert answer SDP to Jingle session-accept
            my_jid = self._xmpp._my_jid or "recorder@guest.meet.jit.si/recorder"
            jingle_xml = sdp_to_jingle(
                sdp=answer_sdp,
                sid=sid,
                initiator=from_jid,
                responder=my_jid,
                content_name_order=content_name_order,
            )

            # Send session-accept
            self._xmpp.send_session_accept(jingle_xml, from_jid)

            # Mark PC as ready — allow remote ICE candidates to be added
            self._pc_ready = True

            # Add buffered remote ICE candidates that arrived before PC was ready
            for cand_info in self._remote_candidates_buffer:
                logger.debug(f"Adding buffered remote candidate: {cand_info['candidate'][:60]}")
                await self._peer.add_ice_candidate(cand_info)
            self._remote_candidates_buffer.clear()

            # Send any buffered local candidates
            for candidate in self._pending_candidates:
                cand_dict = candidate_to_jingle(f"a={candidate.candidate}")
                if cand_dict:
                    self._xmpp.send_transport_info(
                        [cand_dict],
                        sid,
                        from_jid,
                        mid=candidate.sdpMid or "0",
                    )
            self._pending_candidates.clear()

            # Wait for ICE to connect
            logger.info("Waiting for ICE connection...")
            connected = await self._peer.wait_connected(
                timeout=self._ice_timeout_for_mode(ice_mode)
            )
            if connected:
                logger.info("ICE connected! Recording audio...")
            else:
                # Auto-fallback only before any audio was actually captured.
                if self._recorder.frame_count == 0 and self._advance_ice_mode():
                    next_mode = self._current_ice_mode()
                    logger.warning(
                        f"ICE failed in mode={ice_mode}; switching to fallback mode={next_mode}"
                    )
                    self._xmpp.send_session_terminate(sid, from_jid)
                    await self._reset_peer_for_new_session()
                    return

                logger.error("ICE connection failed — no audio will be recorded")

        except Exception as e:
            logger.error(f"Error handling session-initiate: {e}", exc_info=True)

    async def _on_session_terminate(self, sid: str, from_jid: str):
        """Handle session-terminate (e.g. P2P ends, JVB session incoming)."""
        if sid != self._session_sid:
            return
        logger.info(f"Session {sid} terminated — resetting peer for next session")
        await self._reset_peer_for_new_session()

    async def _on_transport_info(self, jingle_elem: ET.Element, sid: str):
        """Handle incoming transport-info (remote ICE candidates)."""
        if sid != self._session_sid:
            logger.debug(f"Ignoring transport-info for stale sid={sid} (active={self._session_sid})")
            return

        from jingle_sdp import candidate_from_jingle
        for content in jingle_elem:
            if "content" not in (content.tag.split("}", 1)[1] if "}" in content.tag else content.tag):
                continue
            for transport in content:
                local = transport.tag.split("}", 1)[1] if "}" in transport.tag else transport.tag
                if local != "transport":
                    continue
                for cand_elem in transport:
                    cname = cand_elem.tag.split("}", 1)[1] if "}" in cand_elem.tag else cand_elem.tag
                    if cname != "candidate":
                        continue

                    cand_str = candidate_from_jingle(cand_elem)
                    mid = content.get("name", "0")
                    cand_info = {
                        "candidate": cand_str,
                        "sdpMid": mid,
                        "sdpMLineIndex": 0,
                    }

                    if self._pc_ready:
                        await self._peer.add_ice_candidate(cand_info)
                    else:
                        logger.debug(f"Buffering remote ICE candidate (PC not ready): {cand_str[:60]}")
                        self._remote_candidates_buffer.append(cand_info)

    async def _cleanup(self):
        """Stop recording and disconnect."""
        logger.info("Cleaning up...")

        # Cancel track tasks
        for task in self._track_tasks:
            task.cancel()

        # Stop recorder
        self._recorder.stop()

        # Close WebRTC
        if self._peer:
            await self._peer.close()

        # Disconnect XMPP
        if self._xmpp:
            await self._xmpp.disconnect()

        logger.info("Cleanup done.")
