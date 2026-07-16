from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import BinaryIO

from faster_whisper import WhisperModel

from .config import SUPPORTED_LANGUAGES, Settings
from .paths import model_dir


def normalize_transcript(parts: list[str]) -> str:
    text = "".join(parts).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text


class OfflineTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return model_dir(self.settings.model_name)

    def load(self) -> None:
        if self._model is not None:
            return
        required = self.path / "model.bin"
        if not required.exists():
            raise FileNotFoundError(
                f"Локальная модель не найдена: {self.path}. Запустите setup.ps1."
            )
        self._model = WhisperModel(
            str(self.path),
            device="cpu",
            compute_type=self.settings.compute_type,
            cpu_threads=self.settings.cpu_threads,
            local_files_only=True,
        )

    def transcribe(
        self, audio_source: Path | BinaryIO, language_setting: str
    ) -> tuple[str, str, float]:
        with self._lock:
            self.load()
            assert self._model is not None
            language = SUPPORTED_LANGUAGES.get(language_setting)
            audio_input = str(audio_source) if isinstance(audio_source, Path) else audio_source
            segments, info = self._model.transcribe(
                audio_input,
                language=language,
                task="transcribe",
                beam_size=1,
                best_of=1,
                temperature=0,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False,
            )
            text = normalize_transcript([segment.text for segment in segments])
            return text, info.language, float(info.language_probability)
