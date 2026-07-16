from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .paths import state_dir


SUPPORTED_LANGUAGES = {
    "auto": None,
    "ru": "ru",
    "es": "es",
    "en": "en",
}


@dataclass(slots=True)
class Settings:
    model_name: str = "small"
    language: str = "auto"
    microphone: str = "default"
    compute_type: str = "int8"
    cpu_threads: int = min(8, max(4, (os.cpu_count() or 6) // 2))
    max_record_seconds: int = 5 * 60
    sounds: bool = True

    def validate(self) -> None:
        if self.model_name != "small":
            self.model_name = "small"
        if self.language not in SUPPORTED_LANGUAGES:
            self.language = "auto"
        if self.compute_type not in {"int8", "float32"}:
            self.compute_type = "int8"
        self.cpu_threads = min(16, max(1, int(self.cpu_threads)))
        self.max_record_seconds = min(5 * 60, max(5, int(self.max_record_seconds)))


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
