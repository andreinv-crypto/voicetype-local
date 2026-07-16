from __future__ import annotations

import re
from typing import Any


PROTOCOL_VERSION = 1

MSG_READY = "ready"
MSG_TRANSCRIBE = "transcribe"
MSG_RESULT = "result"
MSG_ERROR = "error"
MSG_CLOSE = "close"
MSG_CLOSED = "closed"


def normalize_transcript(parts: list[str]) -> str:
    text = "".join(parts).strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text


def message(message_type: str, **values: Any) -> dict[str, Any]:
    return {"version": PROTOCOL_VERSION, "type": message_type, **values}


def is_message(value: object, expected_type: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("version") == PROTOCOL_VERSION
        and value.get("type") == expected_type
    )


def safe_error_text(error: BaseException, limit: int = 800) -> str:
    text = " ".join(str(error).split()) or error.__class__.__name__
    return text[:limit]
