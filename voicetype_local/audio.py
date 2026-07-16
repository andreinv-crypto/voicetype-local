from __future__ import annotations

import io
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

import sounddevice as sd


class AudioRecorder:
    def __init__(self) -> None:
        self._cleanup_stale_audio()
        self._stream: sd.RawInputStream | None = None
        self._pending_stream: sd.RawInputStream | None = None
        self._closing_stream: sd.RawInputStream | None = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cancel_requested = False
        self._native_opening = False
        self._sample_rate = 16_000
        self._started_at = 0.0
        self._overflow_count = 0

    @staticmethod
    def _cleanup_stale_audio() -> None:
        for path in Path(tempfile.gettempdir()).glob("voicetype_*.wav"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def input_devices() -> list[str]:
        names: list[str] = []
        for device in sd.query_devices():
            if int(device.get("max_input_channels", 0)) > 0:
                name = str(device.get("name", "Microphone"))
                if name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _normalized_name(name: str) -> str:
        return "".join(character for character in name.casefold() if character.isalnum())

    @classmethod
    def _resolve_device(cls, preferred: str) -> int | None:
        devices = list(sd.query_devices())
        default_index: int | None = None
        try:
            candidate = int(sd.default.device[0])
            if 0 <= candidate < len(devices):
                default_index = candidate
        except (TypeError, ValueError, IndexError):
            pass

        if not preferred or preferred == "default":
            if default_index is None:
                return None
            target_name = str(devices[default_index].get("name", ""))
        else:
            target_name = preferred

        target = cls._normalized_name(target_name)
        if not target:
            return default_index
        try:
            host_apis = list(sd.query_hostapis())
        except Exception:
            host_apis = []

        matches: list[tuple[bool, bool, int]] = []
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels", 0)) <= 0:
                continue
            name = str(device.get("name", ""))
            normalized = cls._normalized_name(name)
            exact = normalized == target
            partial = normalized.startswith(target) or target.startswith(normalized)
            if not exact and not partial:
                continue
            is_wasapi = False
            try:
                host_name = str(host_apis[int(device.get("hostapi", -1))].get("name", ""))
                is_wasapi = "wasapi" in host_name.casefold()
            except (IndexError, TypeError, ValueError):
                pass
            matches.append((exact, is_wasapi, index))

        if matches:
            # WASAPI has much lower and more reliable latency than the legacy
            # MME default for the same physical USB microphone.
            matches.sort(key=lambda item: (item[1], item[0], item[2]), reverse=True)
            return matches[0][2]
        return default_index if preferred == "default" else None

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        with self._lock:
            if bool(getattr(status, "input_overflow", False)):
                self._overflow_count += 1
            self._frames.append(bytes(indata))

    @staticmethod
    def _abort_and_close(stream: Any) -> None:
        try:
            stream.abort()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def start(self, preferred_device: str = "default") -> None:
        with self._state_lock:
            if (
                self._stream is not None
                or self._pending_stream is not None
                or self._closing_stream is not None
            ):
                raise RuntimeError("Recording is already active")
            self._cancel_requested = False
        with self._state_lock:
            self._native_opening = True
        try:
            device = self._resolve_device(preferred_device)
        finally:
            with self._state_lock:
                self._native_opening = False
        with self._state_lock:
            if self._cancel_requested:
                raise RuntimeError("Recording start was cancelled")
        candidates = [device]
        if preferred_device in {"", "default"} and device is not None:
            # If the preferred low-latency WASAPI endpoint is unavailable,
            # fall back to Windows' configured default input endpoint.
            candidates.append(None)
        last_error: Exception | None = None
        for candidate in candidates:
            with self._lock:
                self._frames = []
                self._overflow_count = 0
            stream: sd.RawInputStream | None = None
            try:
                with self._state_lock:
                    self._native_opening = True
                try:
                    info = sd.query_devices(candidate, "input")
                    sample_rate = int(float(info["default_samplerate"]))
                    stream = sd.RawInputStream(
                        samplerate=sample_rate,
                        blocksize=0,
                        device=candidate,
                        channels=1,
                        dtype="int16",
                        callback=self._callback,
                        latency="high",
                    )
                finally:
                    with self._state_lock:
                        self._native_opening = False
                with self._state_lock:
                    if self._cancel_requested:
                        self._abort_and_close(stream)
                        raise RuntimeError("Recording start was cancelled")
                    self._pending_stream = stream
                try:
                    stream.start()
                except Exception:
                    self._abort_and_close(stream)
                    raise
                finally:
                    with self._state_lock:
                        if self._pending_stream is stream:
                            self._pending_stream = None
            except Exception as exc:
                last_error = exc
                with self._state_lock:
                    if self._cancel_requested:
                        raise RuntimeError("Recording start was cancelled") from exc
                continue
            with self._state_lock:
                if self._cancel_requested:
                    self._abort_and_close(stream)
                    raise RuntimeError("Recording start was cancelled")
                self._sample_rate = sample_rate
                self._stream = stream
                self._started_at = time.monotonic()
            return
        if last_error is not None:
            raise last_error
        raise RuntimeError("No microphone input device is available")

    @property
    def duration(self) -> float:
        with self._state_lock:
            active = self._stream is not None
            started_at = self._started_at
        return max(0.0, time.monotonic() - started_at) if active else 0.0

    def stop(self) -> io.BytesIO:
        with self._state_lock:
            stream = self._stream
            if stream is None:
                raise RuntimeError("Recording is not active")
            self._stream = None
            self._closing_stream = stream
        release_error: Exception | None = None
        try:
            stream.stop()
        except Exception as exc:
            release_error = exc
            try:
                stream.abort()
            except Exception:
                pass
        try:
            stream.close()
        except Exception as exc:
            if release_error is None:
                release_error = exc
        finally:
            with self._state_lock:
                if self._closing_stream is stream:
                    self._closing_stream = None
        with self._lock:
            frames = self._frames
            self._frames = []
        if not frames:
            if release_error is not None:
                raise RuntimeError(f"Microphone did not stop cleanly: {release_error}") from release_error
            raise RuntimeError("Microphone returned no audio")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(self._sample_rate)
            for frame in frames:
                output.writeframesraw(frame)
        buffer.seek(0)
        return buffer

    def cancel(self) -> None:
        with self._state_lock:
            self._cancel_requested = True
            pending = self._pending_stream
            active = self._stream
            closing = self._closing_stream
            self._pending_stream = None
            self._stream = None
        for stream in (pending, active):
            if stream is not None:
                self._abort_and_close(stream)
        if closing is not None and closing is not pending and closing is not active:
            # stop()/close() remains the owner; abort is enough to unblock it.
            try:
                closing.abort()
            except Exception:
                pass
        with self._lock:
            self._frames = []

    @property
    def is_recording(self) -> bool:
        with self._state_lock:
            return self._stream is not None

    @property
    def native_open_unabortable(self) -> bool:
        """True only while PortAudio has not returned a stream handle yet."""

        with self._state_lock:
            return self._native_opening and self._pending_stream is None
