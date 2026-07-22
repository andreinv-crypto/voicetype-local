from __future__ import annotations

import atexit
import io
import wave
from pathlib import Path
from typing import BinaryIO, Any

from .config import SUPPORTED_LANGUAGES, Settings
from .paths import model_dir
from .worker import WhisperWorkerClient
from .worker_protocol import normalize_transcript


class OfflineTranscriber:
    def __init__(
        self,
        settings: Settings,
        *,
        startup_timeout: float = 90.0,
        transcription_timeout: float | None = None,
        worker_factory: Any = WhisperWorkerClient,
    ) -> None:
        self.settings = settings
        self._fixed_transcription_timeout = transcription_timeout
        self._worker = worker_factory(
            self.path,
            settings.compute_type,
            settings.cpu_threads,
            startup_timeout=startup_timeout,
        )
        self._closed = False
        atexit.register(self.close)

    @property
    def path(self) -> Path:
        return model_dir(self.settings.model_name)

    def load(self) -> None:
        required = self.path / "model.bin"
        if not required.exists():
            raise FileNotFoundError(
                f"Локальная модель не найдена: {self.path}. Запустите setup.ps1."
            )
        if self._closed:
            raise RuntimeError("Распознаватель уже закрыт")
        self._worker.start()

    @staticmethod
    def _read_audio(audio_source: Path | BinaryIO) -> bytes:
        if isinstance(audio_source, Path):
            return audio_source.read_bytes()
        original_position: int | None = None
        try:
            original_position = audio_source.tell()
        except (AttributeError, OSError):
            pass
        try:
            try:
                audio_source.seek(0)
            except (AttributeError, OSError):
                pass
            payload = audio_source.read()
        finally:
            if original_position is not None:
                try:
                    audio_source.seek(original_position)
                except (AttributeError, OSError):
                    pass
        if not isinstance(payload, bytes):
            payload = bytes(payload)
        if not payload:
            raise ValueError("Аудиозапись пуста")
        return payload

    def _timeout_for(self, payload: bytes) -> float:
        duration = 0.0
        try:
            with wave.open(io.BytesIO(payload), "rb") as source:
                rate = source.getframerate()
                if rate > 0:
                    duration = source.getnframes() / rate
        except (EOFError, wave.Error):
            pass
        # A short utterance should never hold the app indefinitely, while the
        # existing five-minute recording limit still gets enough time on CPU.
        duration_budget = min(600.0, max(30.0, 15.0 + duration * 2.0))
        if self._fixed_transcription_timeout is None:
            return duration_budget
        # The configured timeout is a ceiling, not a reason to make a short
        # phrase wait the full global limit.  WhisperWorkerClient applies this
        # as one total budget across recovery attempts.
        return max(
            0.1,
            min(float(self._fixed_transcription_timeout), duration_budget),
        )

    def transcribe(
        self,
        audio_source: Path | BinaryIO,
        language_setting: str,
        *,
        initial_prompt: str = "",
        hotwords: str = "",
    ) -> tuple[str, str, float]:
        if language_setting not in SUPPORTED_LANGUAGES:
            raise ValueError(f"Неподдерживаемый язык: {language_setting}")
        if self._closed:
            raise RuntimeError("Распознаватель уже закрыт")
        self.load()
        payload = self._read_audio(audio_source)
        return self._worker.transcribe(
            payload,
            SUPPORTED_LANGUAGES[language_setting],
            timeout=self._timeout_for(payload),
            initial_prompt=initial_prompt,
            hotwords=hotwords,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._worker.close()
