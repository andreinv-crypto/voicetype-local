from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import state_dir


SUPPORTED_LANGUAGES = {
    "auto": None,
    "ru": "ru",
    "es": "es",
    "en": "en",
}

SUPPORTED_HOTKEYS = {"right_ctrl", "f8", "f9", "f10", "pause"}
CORRECTION_MODES = {"off", "local_basic", "cloud_text", "local_compact", "local_full"}
TRANSCRIPTION_MODES = {"local", "cloud_transcription"}


def _strict_bool(value: Any, *, default: bool) -> bool:
    """Accept JSON booleans only; malformed consent must never become truthy."""

    return value if isinstance(value, bool) else default


@dataclass(slots=True)
class Settings:
    model_name: str = "small"
    language: str = "auto"
    microphone: str = "default"
    compute_type: str = "int8"
    cpu_threads: int = min(8, max(4, (os.cpu_count() or 6) // 2))
    max_record_seconds: int = 5 * 60
    sounds: bool = True
    activation_key: str = "right_ctrl"
    microphone_start_timeout_seconds: float = 10.0
    microphone_stop_timeout_seconds: float = 10.0
    transcription_timeout_seconds: float = 90.0
    correction_timeout_seconds: float = 8.0
    worker_start_timeout_seconds: float = 45.0
    correction_mode: str = "local_basic"
    transcription_mode: str = "local"
    cloud_provider: str = "openai"
    cloud_model: str = ""
    cloud_transcription_model: str = ""
    local_model: str = ""
    cloud_text_consent: bool = False
    cloud_transcription_consent: bool = False
    active_domains: list[str] = field(default_factory=list)
    app_scoped_memory: bool = True
    memory_prompt_token_budget: int = 160

    def validate(self) -> None:
        if self.model_name != "small":
            self.model_name = "small"
        if self.language not in SUPPORTED_LANGUAGES:
            self.language = "auto"
        if self.compute_type not in {"int8", "float32"}:
            self.compute_type = "int8"
        self.cpu_threads = min(16, max(1, int(self.cpu_threads)))
        self.max_record_seconds = min(5 * 60, max(5, int(self.max_record_seconds)))
        if self.activation_key not in SUPPORTED_HOTKEYS:
            self.activation_key = "right_ctrl"
        self.microphone_start_timeout_seconds = min(
            60.0, max(2.0, float(self.microphone_start_timeout_seconds))
        )
        self.microphone_stop_timeout_seconds = min(
            60.0, max(2.0, float(self.microphone_stop_timeout_seconds))
        )
        self.transcription_timeout_seconds = min(
            10 * 60.0, max(10.0, float(self.transcription_timeout_seconds))
        )
        self.correction_timeout_seconds = min(
            60.0, max(1.0, float(self.correction_timeout_seconds))
        )
        self.worker_start_timeout_seconds = min(
            3 * 60.0, max(10.0, float(self.worker_start_timeout_seconds))
        )
        if self.correction_mode not in CORRECTION_MODES:
            self.correction_mode = "local_basic"
        if self.transcription_mode not in TRANSCRIPTION_MODES:
            self.transcription_mode = "local"
        if self.cloud_provider not in {"openai", "ollama"}:
            self.cloud_provider = "openai"
        self.cloud_model = str(self.cloud_model).strip()[:128]
        self.cloud_transcription_model = str(self.cloud_transcription_model).strip()[:128]
        self.local_model = str(self.local_model).strip()[:128]
        self.sounds = _strict_bool(self.sounds, default=True)
        self.cloud_text_consent = _strict_bool(
            self.cloud_text_consent, default=False
        )
        self.cloud_transcription_consent = _strict_bool(
            self.cloud_transcription_consent, default=False
        )
        self.app_scoped_memory = _strict_bool(
            self.app_scoped_memory, default=True
        )
        self.memory_prompt_token_budget = min(
            200, max(32, int(self.memory_prompt_token_budget))
        )
        if not isinstance(self.active_domains, list):
            self.active_domains = []
        self.active_domains = list(
            dict.fromkeys(
                value.strip()[:64]
                for value in self.active_domains[:16]
                if isinstance(value, str) and value.strip()
            )
        )


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_dir() / "settings.json"
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> Settings:
        settings = Settings()
        if not self.path.exists():
            return settings
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            allowed = {item.name for item in fields(Settings)}
            settings = Settings(**{key: value for key, value in raw.items() if key in allowed})
            settings.validate()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return Settings()
        return settings

    def get(self) -> Settings:
        with self._lock:
            return Settings(**asdict(self._settings))

    def update(self, **changes: Any) -> Settings:
        with self._lock:
            for key, value in changes.items():
                if hasattr(self._settings, key):
                    setattr(self._settings, key, value)
            self._settings.validate()
            self._save_unlocked()
            return Settings(**asdict(self._settings))

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self._settings), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(self.path)
