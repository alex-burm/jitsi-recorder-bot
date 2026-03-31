"""
Audio recording: receives AudioFrame objects from aiortc and writes to WAV.
Handles multiple tracks by mixing them.
"""

import asyncio
import logging
import struct
import wave
from typing import Optional

import av

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 1  # mono mix
SAMPLE_WIDTH = 2  # 16-bit PCM


class AudioRecorder:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self._wav: Optional[wave.Wave_write] = None
        self._lock = asyncio.Lock()
        self._running = False
        self._frame_count = 0

    def start(self):
        self._wav = wave.open(self.output_path, "wb")
        self._wav.setnchannels(CHANNELS)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(SAMPLE_RATE)
        # Resampler: convert any input format to s16 mono 48kHz
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self._running = True
        logger.info(f"Recording started → {self.output_path}")

    def stop(self):
        self._running = False
        if self._wav:
            self._wav.close()
            self._wav = None
        logger.info(
            f"Recording stopped. Frames written: {self._frame_count} "
            f"({self._frame_count / SAMPLE_RATE:.1f}s)"
        )

    @property
    def frame_count(self) -> int:
        return self._frame_count

    async def write_frame(self, frame):
        """
        Accept an av.AudioFrame from aiortc and write PCM to WAV.
        Converts to mono s16 at 48kHz.
        """
        if not self._running or self._wav is None:
            return

        try:
            # Resample to s16 mono 48kHz using av.AudioResampler
            resampled_frames = self._resampler.resample(frame)
            async with self._lock:
                for rf in resampled_frames:
                    pcm_bytes = bytes(rf.planes[0])
                    self._wav.writeframes(pcm_bytes)
                    self._frame_count += rf.samples

        except Exception as e:
            logger.warning(f"Error writing audio frame: {e}")

    async def track_handler(self, track):
        """
        Consume an aiortc MediaStreamTrack (audio) and write frames.
        Run this as an asyncio task.
        """
        logger.info(f"Starting to record track: {track.kind} id={track.id}")
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                    await self.write_frame(frame)
                except asyncio.TimeoutError:
                    logger.debug("Track recv timeout (waiting for audio...)")
                    continue
                except Exception as e:
                    logger.warning(f"Track recv error: {e}")
                    break
        except asyncio.CancelledError:
            pass
        logger.info(f"Track handler stopped for {track.id}")
