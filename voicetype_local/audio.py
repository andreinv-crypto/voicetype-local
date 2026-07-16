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
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._sample_rate = 16_000
        self._started_at = 0.0

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
            if candidate >= 0:
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
        del frames, time_info, status
        with self._lock:
            self._frames.append(bytes(indata))

    def start(self, preferred_device: str = "default") -> None:
        if self._stream is not None:
            raise RuntimeError("Recording is already active")
        device = self._resolve_device(preferred_device)
        candidates = [device]
        if preferred_device in {"", "default"} and device is not None:
            # If the preferred low-latency WASAPI endpoint is unavailable,
            # fall back to Windows' configured default input endpoint.
            candidates.append(None)
        last_error: Exception | None = None
        for candidate in candidates:
            with self._lock:
                self._frames = []
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
                try:
                    stream.start()
                except Exception:
                    stream.close()
                    raise
            except Exception as exc:
                last_error = exc
                continue
            self._sample_rate = sample_rate
            self._stream = stream
            self._started_at = time.monotonic()
            return
        if last_error is not None:
            raise last_error
        raise RuntimeError("No microphone input device is available")

    @property
    def duration(self) -> float:
        return max(0.0, time.monotonic() - self._started_at) if self._stream else 0.0

    def stop(self) -> io.BytesIO:
        stream = self._stream
        if stream is None:
            raise RuntimeError("Recording is not active")
        self._stream = None
        try:
            stream.stop()
        finally:
            stream.close()
        with self._lock:
            frames = self._frames
            self._frames = []
        if not frames:
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
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
        with self._lock:
            self._frames = []

    @property
    def is_recording(self) -> bool:
        return self._stream is not None
