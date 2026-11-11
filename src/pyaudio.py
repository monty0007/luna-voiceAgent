"""Minimal `pyaudio` drop-in backed by `sounddevice`.

Why this exists: the voice client (`audio.py` / `voice_client.py`) is derived
from the official Voice Live samples, which use PyAudio. PyAudio needs the system
PortAudio C library (via Homebrew/MacPorts), which requires sudo. `sounddevice`
ships a prebuilt PortAudio and works without sudo, so this shim maps the small
PyAudio API surface the client uses onto `sounddevice`.

Only the subset of the PyAudio API used by the client is implemented:
  - `pyaudio.PyAudio()` -> `.open(...)`, `.get_device_count()`,
    `.get_device_info_by_index()`, `.terminate()`
    (`open` also accepts sounddevice's `latency`, e.g. "low"; real PyAudio does
    not, so callers check `SUPPORTS_LATENCY` first)
  - stream objects with `.start_stream()`, `.stop_stream()`, `.close()`
  - constants: `paInt16`, `paContinue`, `paComplete`
  - `pyaudio.get_sample_size(pyaudio.paInt16)`

Stream callback contract (matching PyAudio):
  - input  callback(in_data=None, frame_count, time_info, status) -> (None, action)
  - output callback(None, frame_count, time_info, status) -> (bytes, action)
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import sounddevice as _sd

# --- Constants (values chosen to mirror PyAudio where relevant) -------------
paInt16 = 8  # PyAudio's paInt16 == 8
paContinue = 0
paComplete = 1
paAbort = 2

_DTYPE = "int16"
SUPPORTS_LATENCY = True  # shim-only extension; absent from real PyAudio


def get_sample_size(fmt: int) -> int:
    """Return the size in bytes of a single sample for the given format."""
    if fmt == paInt16:
        return 2
    raise ValueError(f"Unsupported format: {fmt}")


class _Stream:
    """Wraps a sounddevice Raw stream to look like a PyAudio stream."""

    def __init__(
        self,
        pa: "PyAudio",
        rate: int,
        channels: int,
        frames_per_buffer: int,
        is_input: bool,
        stream_callback: Optional[Callable[..., Any]],
        latency: Any = None,
    ) -> None:
        self._pa = pa
        self._rate = rate
        self._channels = channels
        self._frames_per_buffer = frames_per_buffer
        self._is_input = is_input
        self._callback = stream_callback
        self._latency = latency  # None = sounddevice default ("high")
        self._sd_stream: Optional[_sd.RawInputStream] = None
        self._active = False

    # -- sounddevice callback -> PyAudio-style callback --------------------
    def _on_input(self, indata, frames, time_info, status) -> None:
        if self._callback is None:
            return
        # indata is a memoryview of int16 PCM; PyAudio passes raw bytes.
        in_bytes = bytes(indata)
        self._callback(in_bytes, frames, time_info, status)

    def _on_output(self, outdata, frames, time_info, status) -> None:
        if self._callback is None:
            outdata[:] = bytes(len(outdata))
            return
        out_bytes, _action = self._callback(None, frames, time_info, status)
        needed = len(outdata)
        if out_bytes is not None and len(out_bytes) == needed:
            outdata[:] = out_bytes  # fast path: the client returns exact sizes
            return
        out_bytes = (out_bytes or b"")[:needed]
        # memoryview slice assignment requires exact length.
        outdata[:] = out_bytes + bytes(needed - len(out_bytes))

    # -- PyAudio-compatible lifecycle --------------------------------------
    def start_stream(self) -> None:
        if self._active:
            return
        if self._sd_stream is None:
            cls = _sd.RawInputStream if self._is_input else _sd.RawOutputStream
            cb = self._on_input if self._is_input else self._on_output
            self._sd_stream = cls(
                samplerate=self._rate,
                blocksize=self._frames_per_buffer,
                channels=self._channels,
                dtype=_DTYPE,
                callback=cb,
                **({"latency": self._latency} if self._latency is not None else {}),
            )
        self._sd_stream.start()
        self._active = True

    def stop_stream(self) -> None:
        if self._sd_stream is not None and self._active:
            try:
                self._sd_stream.stop()
            finally:
                self._active = False

    def close(self) -> None:
        if self._sd_stream is not None:
            try:
                self.stop_stream()
            finally:
                self._sd_stream.close()
                self._sd_stream = None

    def is_active(self) -> bool:
        return self._active


class PyAudio:
    """Subset of pyaudio.PyAudio backed by sounddevice."""

    def __init__(self) -> None:
        self._terminated = False

    def open(
        self,
        format: int,
        channels: int,
        rate: int,
        input: bool = False,
        output: bool = False,
        frames_per_buffer: int = 1024,
        stream_callback: Optional[Callable[..., Any]] = None,
        latency: Any = None,
        **_ignored: Any,
    ) -> _Stream:
        if format != paInt16:
            raise ValueError("This shim only supports paInt16")
        if not (input or output):
            raise ValueError("Stream must be input and/or output")
        stream = _Stream(
            pa=self,
            rate=rate,
            channels=channels,
            frames_per_buffer=frames_per_buffer,
            is_input=bool(input),
            stream_callback=stream_callback,
            latency=latency,
        )
        # PyAudio starts streams opened with a callback immediately.
        if stream_callback is not None:
            stream.start_stream()
        return stream

    # -- device enumeration -------------------------------------------------
    def get_device_count(self) -> int:
        return len(_sd.query_devices())

    def get_device_info_by_index(self, index: int) -> dict:
        d = _sd.query_devices()[index]
        return {
            "maxInputChannels": d.get("max_input_channels", 0),
            "maxOutputChannels": d.get("max_output_channels", 0),
            "name": d.get("name", ""),
        }

    def terminate(self) -> None:
        self._terminated = True
