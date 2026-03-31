"""
WebRTC peer using aiortc.
Receives audio tracks from Jitsi Videobridge.
"""

import asyncio
import logging
from typing import Callable, Optional

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaBlackhole

logger = logging.getLogger(__name__)


class WebRTCPeer:
    def __init__(
        self,
        on_ice_candidate: Callable[[dict], None],
        on_track: Callable[[MediaStreamTrack], None],
    ):
        self._pc: Optional[RTCPeerConnection] = None
        self._on_ice_candidate = on_ice_candidate
        self._on_track = on_track
        self._ice_gathering_done = asyncio.Event()
        self._connected = asyncio.Event()

    def _create_pc(self, ice_servers: list = None):
        from aiortc import RTCConfiguration, RTCIceServer
        config = None
        if ice_servers:
            rtc_servers = []
            for s in ice_servers:
                urls = s.get("urls", "")
                username = s.get("username")
                credential = s.get("credential")
                if username and credential:
                    rtc_servers.append(RTCIceServer(urls=urls, username=username, credential=credential))
                else:
                    rtc_servers.append(RTCIceServer(urls=urls))
            if rtc_servers:
                config = RTCConfiguration(iceServers=rtc_servers)
                logger.info(f"Using {len(rtc_servers)} ICE servers")
        self._pc = RTCPeerConnection(configuration=config)

        @self._pc.on("icecandidate")
        def on_ice_candidate(candidate):
            if candidate is None:
                logger.debug("ICE gathering complete (null candidate)")
                self._ice_gathering_done.set()
                return
            logger.debug(f"New local ICE candidate: {candidate.candidate}")
            self._on_ice_candidate(candidate)

        @self._pc.on("icegatheringstatechange")
        def on_gathering_state():
            state = self._pc.iceGatheringState
            logger.debug(f"ICE gathering state: {state}")
            if state == "complete":
                self._ice_gathering_done.set()

        @self._pc.on("iceconnectionstatechange")
        def on_ice_state():
            state = self._pc.iceConnectionState
            logger.info(f"ICE connection state: {state}")
            if state in ("connected", "completed"):
                self._connected.set()
            elif state == "failed":
                logger.error("ICE connection failed!")

        @self._pc.on("connectionstatechange")
        def on_connection_state():
            state = self._pc.connectionState
            logger.info(f"Connection state: {state}")

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack):
            logger.info(f"Received remote track: kind={track.kind} id={track.id}")
            if track.kind == "audio":
                self._on_track(track)
            else:
                # Discard video/data
                sink = MediaBlackhole()
                sink.addTrack(track)
                asyncio.ensure_future(sink.start())

        @self._pc.on("signalingstatechange")
        def on_signaling_state():
            logger.debug(f"Signaling state: {self._pc.signalingState}")

    async def create_answer(self, offer_sdp: str, ice_servers: list = None) -> str:
        """
        Accept an SDP offer and return an SDP answer.
        """
        self._create_pc(ice_servers=ice_servers)

        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
        logger.debug(f"Setting remote description (offer):\n{offer_sdp[:500]}...")

        await self._pc.setRemoteDescription(offer)

        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        logger.debug(f"Local description (answer):\n{self._pc.localDescription.sdp[:500]}...")
        return self._pc.localDescription.sdp

    async def add_ice_candidate(self, candidate_dict: dict):
        """Add a remote ICE candidate."""
        from aiortc.sdp import candidate_from_sdp
        try:
            from aiortc import RTCIceCandidate
            candidate_str = candidate_dict.get("candidate", "")
            if not candidate_str:
                return
            # candidate_str may or may not have "candidate:" prefix
            if not candidate_str.startswith("candidate:"):
                candidate_str = "candidate:" + candidate_str

            # Parse using aiortc's internal parser
            sdp_mid = candidate_dict.get("sdpMid", "0")
            sdp_mline_index = int(candidate_dict.get("sdpMLineIndex", 0))

            candidate = candidate_from_sdp(candidate_str)
            candidate.sdpMid = sdp_mid
            candidate.sdpMLineIndex = sdp_mline_index

            if self._pc:
                await self._pc.addIceCandidate(candidate)
                logger.debug(f"Added ICE candidate: {candidate_str[:80]}")
        except Exception as e:
            logger.warning(f"Failed to add ICE candidate: {e}")

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        """Wait until ICE is connected."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.error(f"ICE connection timeout after {timeout}s")
            return False

    async def close(self):
        if self._pc:
            await self._pc.close()
            self._pc = None
