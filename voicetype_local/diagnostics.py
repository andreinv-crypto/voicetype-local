from __future__ import annotations

import json
import logging
import math
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any

from .paths import state_dir


ALLOWED_FIELDS = {
    "app_version",
    "backend",
    "code",
    "count",
    "duration_ms",
    "error_type",
    "operation",
    "retry_count",
    "state",
    "worker_pid",
}

ALLOWED_EVENTS = {
    "app_exiting",
    "app_initialized",
    "cloud_transcription_fallback",
    "correction_completed",
    "dictation_transform_completed",
    "heartbeat",
    "memory_pack_failed",
    "memory_postprocess_failed",
    "memory_prompt_failed",
    "memory_recovered",
    "processing_cancelled",
    "recording_start_failed",
    "recording_start_requested",
    "recording_start_timeout",
    "recording_started",
    "recording_stop_failed",
    "recording_stop_requested",
    "recording_stop_timeout",
    "settings_updated",
    "text_inserted",
    "transcription_completed",
    "transcription_failed",
    "voice_command_blocked",
    "voice_command_completed",
}

_TECHNICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:+-]{1,160}\Z")
_TOKEN_FIELDS = frozenset(
    {"app_version", "backend", "code", "error_type", "operation", "state"}
)
_NUMERIC_LIMITS = {
    "count": (0, 10_000),
    "duration_ms": (0, 3_600_000),
    "retry_count": (0, 1_000),
    "worker_pid": (0, 2_147_483_647),
}


class TechnicalLogger:
    """Bounded diagnostics that deliberately cannot accept dictated content."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = 512 * 1024,
        backup_count: int = 3,
    ) -> None:
        self.path = path or state_dir() / "logs" / "voicetype.log"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._logger = logging.getLogger(f"voicetype.technical.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(
            self.path,
            maxBytes=max(4096, int(max_bytes)),
            backupCount=max(1, int(backup_count)),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._logger.addHandler(handler)
        self._handler = handler

    @staticmethod
    def _safe_token(value: Any) -> str | None:
        if not isinstance(value, str) or not _TECHNICAL_TOKEN_RE.fullmatch(value):
            return None
        return value

    @staticmethod
    def _safe_numeric(
        value: Any, *, minimum: int, maximum: int
    ) -> bool | int | float | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return min(maximum, max(minimum, value))
        if not isinstance(value, float) or not math.isfinite(value):
            return None
        return min(maximum, max(minimum, value))

    def event(self, event: str, **fields: Any) -> None:
        safe_event = str(event) if event in ALLOWED_EVENTS else "unknown_event"
        payload: dict[str, Any] = {"event": safe_event}
        for key, value in fields.items():
            if key not in ALLOWED_FIELDS:
                continue
            safe_value: str | bool | int | float | None
            if key in _TOKEN_FIELDS:
                safe_value = self._safe_token(value)
            else:
                minimum, maximum = _NUMERIC_LIMITS[key]
                safe_value = self._safe_numeric(
                    value, minimum=minimum, maximum=maximum
                )
            if safe_value is not None:
                payload[key] = safe_value
        with self._lock:
            self._logger.info(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            )

    def close(self) -> None:
        with self._lock:
            self._handler.flush()
            self._handler.close()
            self._logger.removeHandler(self._handler)


class NullTechnicalLogger:
    def event(self, event: str, **fields: Any) -> None:
        del event, fields

    def close(self) -> None:
        return
