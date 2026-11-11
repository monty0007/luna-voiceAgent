"""Audio capture and playback for the voice client.

Design notes (these are the fixes for the "lag / choppy / stuck" symptoms):

- Playback uses a jitter buffer. A reply only starts playing once PREBUFFER_MS of
  audio has arrived (or the reply has finished), and after an underrun it
  re-buffers instead of stuttering through silence between tiny network chunks.
- Barge-in clears the whole buffer at once, so no tail of the old reply keeps
  playing after you start talking.
- Mic audio goes through one bounded queue drained by a single sender task. If the
  network or event loop stalls, any backlog is sent as one coalesced message, and
  beyond ~2 s the oldest chunks are dropped instead of building an ever-growing
  backlog (which is what made the agent feel seconds behind).
- Both streams use 20 ms blocks and PortAudio's "low" latency setting, which
  trims tens of milliseconds off every turn compared with 50 ms / "high".
- Send failures are logged instead of being lost inside fire-and-forget futures.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

import pyaudio

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

logger = logging.getLogger(__name__)


class AudioDeviceError(RuntimeError):
    """Mic/speaker could not be opened. Reconnecting won't fix it, so it is fatal."""

RATE = 24000
CHANNELS = 1
BYTES_PER_FRAME = 2  # PCM16 mono
MIC_BACKLOG_MS = 2000  # older mic chunks are dropped beyond this much backlog
# Ask PortAudio for its smallest safe device buffer (sounddevice defaults to
# "high", which adds ~10-50 ms per direction depending on the device).
# Only the sounddevice shim understands this; real PyAudio's open() would reject it.
_LATENCY_KW = {"latency": "low"} if getattr(pyaudio, "SUPPORTS_LATENCY", False) else {}


def _env_ms(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def _prebuffer_ms() -> int:
    return _env_ms("AUDIO_PREBUFFER_MS", 100)


# 20 ms blocks (WebRTC's frame size): the server sees the end of your speech
# sooner, and playback starts within one small block instead of a 50 ms one.
# Raise AUDIO_CHUNK_MS (e.g. 40-50) if audio crackles on a slow/Bluetooth device.
CHUNK_MS = _env_ms("AUDIO_CHUNK_MS", 20, minimum=10)
CHUNK_FRAMES = RATE * CHUNK_MS // 1000
MIC_QUEUE_MAX = max(1, MIC_BACKLOG_MS // CHUNK_MS)


class AudioProcessor:
    """Real-time mic capture and speaker playback over PCM16 / 24 kHz / mono.

    Threads: PortAudio runs the capture and playback callbacks on its own threads.
    Capture hands audio to the asyncio loop with `call_soon_threadsafe`; playback
    reads from a lock-protected byte buffer filled by the event loop.
    """

    # Half-duplex echo guard (for paths without server echo cancellation, i.e. the
    # AI gateway): while the speaker is playing, and for `echo_tail_s` after, mic
    # audio is not sent, so the agent never hears itself and interrupts itself.
    # Trade-off: you cannot barge in over the agent. Use headphones + half_duplex=False.
    half_duplex = False
    echo_tail_s = 0.5
    _speaking_until = 0.0

    def __init__(self, connection: "VoiceLiveConnection", prebuffer_ms: Optional[int] = None) -> None:
        self.connection = connection
        self.audio = pyaudio.PyAudio()
        self.format = pyaudio.paInt16
        self.channels = CHANNELS
        self.rate = RATE
        self.chunk_size = CHUNK_FRAMES

        self.input_stream = None
        self.output_stream = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Playback jitter buffer.
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._primed = False
        self._flush = False
        ms = _prebuffer_ms() if prebuffer_ms is None else prebuffer_ms
        self._prebuffer_bytes = int(self.rate * BYTES_PER_FRAME * ms / 1000)

        # Mic send path.
        self._mic_queue: Optional[asyncio.Queue[bytes]] = None
        self._sender_task: Optional[asyncio.Task] = None
        self.dropped_mic_chunks = 0

        logger.info("AudioProcessor initialized (24kHz PCM16 mono, %d ms blocks, prebuffer=%d ms)",
                    CHUNK_MS, ms)

    # ------------------------------------------------------------------ capture
    def start_capture(self) -> None:
        """Start capturing audio from the microphone."""
        if self.input_stream:
            return

        self.loop = asyncio.get_running_loop()
        self._mic_queue = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
        self._sender_task = self.loop.create_task(self._mic_sender())

        def _capture_callback(in_data, _frame_count, _time_info, _status_flags):
            # PortAudio real-time thread: only hand the raw bytes over; encoding
            # happens on the event loop so this callback never risks a glitch.
            try:
                self.loop.call_soon_threadsafe(self._enqueue_mic, in_data)
            except RuntimeError:
                pass  # loop already closed during shutdown
            return (None, pyaudio.paContinue)

        try:
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_capture_callback,
                **_LATENCY_KW,
            )
            logger.info("Started audio capture")
        except Exception as exc:
            logger.exception("Failed to start audio capture")
            raise AudioDeviceError(f"cannot open microphone ({exc})") from exc

    def _enqueue_mic(self, pcm: bytes) -> None:
        q = self._mic_queue
        if q is None:
            return
        if self.half_duplex and time.monotonic() < self._speaking_until:
            return  # speaker is playing: this is mostly our own echo
        if q.full():
            try:
                q.get_nowait()  # drop the oldest chunk, keep the stream current
                self.dropped_mic_chunks += 1
                if self.dropped_mic_chunks % 20 == 1:
                    logger.warning("Mic backlog: dropped %d chunks so far", self.dropped_mic_chunks)
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(pcm)

    async def _mic_sender(self) -> None:
        q = self._mic_queue
        assert q is not None
        while True:
            pcm = await q.get()
            if not q.empty():
                # Behind (slow network / busy loop): coalesce the backlog into
                # one append instead of one websocket message per chunk.
                parts = [pcm]
                while not q.empty():
                    parts.append(q.get_nowait())
                pcm = b"".join(parts)
            try:
                await self.connection.input_audio_buffer.append(
                    audio=base64.b64encode(pcm).decode("ascii"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to send mic audio; stopping sender")
                return

    # ----------------------------------------------------------------- playback
    def start_playback(self) -> None:
        """Open the speaker stream."""
        if self.output_stream:
            return

        def _playback_callback(_in_data, frame_count, _time_info, _status_flags):
            return (self.read_playback(frame_count * BYTES_PER_FRAME), pyaudio.paContinue)

        try:
            self.output_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                output=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_playback_callback,
                **_LATENCY_KW,
            )
            logger.info("Audio playback system ready")
        except Exception as exc:
            logger.exception("Failed to initialize audio playback")
            raise AudioDeviceError(f"cannot open speaker ({exc})") from exc

    def read_playback(self, nbytes: int) -> bytes:
        """Return exactly `nbytes` of audio for the speaker (silence if none ready)."""
        with self._lock:
            if not self._primed:
                if self._buf and (len(self._buf) >= self._prebuffer_bytes or self._flush):
                    self._primed = True
                else:
                    if not self._buf:
                        self._flush = False
                    return bytes(nbytes)
            chunk = bytes(self._buf[:nbytes])
            del self._buf[:nbytes]
            if chunk:
                self._speaking_until = time.monotonic() + self.echo_tail_s
            if not self._buf:
                # Drained (end of reply or underrun): re-buffer before resuming.
                self._primed = False
                self._flush = False
        if len(chunk) < nbytes:
            chunk += bytes(nbytes - len(chunk))
        return chunk

    def queue_audio(self, audio_data: Optional[bytes]) -> None:
        """Append a chunk of reply audio to the playback buffer."""
        if not audio_data:
            return
        with self._lock:
            self._buf.extend(audio_data)

    def finish_response(self) -> None:
        """The reply is complete: play whatever is buffered even if below the prebuffer."""
        with self._lock:
            self._flush = True

    def skip_pending_audio(self) -> None:
        """Barge-in: drop everything not yet played."""
        with self._lock:
            self._buf.clear()
            self._primed = False
            self._flush = False

    @property
    def buffered_ms(self) -> float:
        with self._lock:
            return len(self._buf) / (self.rate * BYTES_PER_FRAME) * 1000

    # ----------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Clean up audio resources."""
        if self._sender_task:
            self._sender_task.cancel()
            self._sender_task = None

        # Best effort per step: a device that vanished (unplugged headset) must
        # not stop the rest of the cleanup or mask the error that ended the session.
        self.skip_pending_audio()
        for attr in ("input_stream", "output_stream"):
            stream = getattr(self, attr)
            setattr(self, attr, None)
            if stream is None:
                continue
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001
                logger.warning("Error closing %s", attr, exc_info=True)

        if self.audio:
            try:
                self.audio.terminate()
            except Exception:  # noqa: BLE001
                logger.warning("Error terminating audio backend", exc_info=True)
            self.audio = None
        logger.info("Audio processor cleaned up")
