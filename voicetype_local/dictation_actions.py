from __future__ import annotations

"""Strict end-of-dictation actions for the everyday interaction mode.

The parser is deliberately small and deterministic.  It recognizes only an
explicit action at the *end* of a non-empty dictation, returns the text that
must be inserted first, and never performs desktop input itself.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .voice_commands import CommandLanguage, normalize_spoken_text


class DictationEndAction(StrEnum):
    PRESS_ENTER = "press_enter"
    SEND_MESSAGE = "send_message"


@dataclass(frozen=True, slots=True)
class DictationEndActionMatch:
    text: str
    action: DictationEndAction
    language: CommandLanguage


_PHRASES: Final[
    dict[CommandLanguage, tuple[tuple[DictationEndAction, tuple[str, ...]], ...]]
] = {
    CommandLanguage.RU: (
        (DictationEndAction.SEND_MESSAGE, ("отправить", "сообщение")),
        (DictationEndAction.SEND_MESSAGE, ("отправь", "сообщение")),
        (DictationEndAction.PRESS_ENTER, ("нажать", "enter")),
        (DictationEndAction.PRESS_ENTER, ("нажми", "enter")),
        (DictationEndAction.PRESS_ENTER, ("нажать", "энтер")),
        (DictationEndAction.PRESS_ENTER, ("нажми", "энтер")),
        (DictationEndAction.PRESS_ENTER, ("нажать", "ввод")),
        (DictationEndAction.PRESS_ENTER, ("нажми", "ввод")),
    ),
    CommandLanguage.EN: (
        (DictationEndAction.SEND_MESSAGE, ("send", "the", "message")),
        (DictationEndAction.SEND_MESSAGE, ("send", "message")),
        (DictationEndAction.PRESS_ENTER, ("press", "enter")),
        (DictationEndAction.PRESS_ENTER, ("hit", "enter")),
    ),
    CommandLanguage.ES: (
        (DictationEndAction.SEND_MESSAGE, ("envia", "el", "mensaje")),
        (DictationEndAction.SEND_MESSAGE, ("enviar", "el", "mensaje")),
        (DictationEndAction.SEND_MESSAGE, ("envia", "mensaje")),
        (DictationEndAction.SEND_MESSAGE, ("enviar", "mensaje")),
        (DictationEndAction.PRESS_ENTER, ("pulsa", "intro")),
        (DictationEndAction.PRESS_ENTER, ("presiona", "intro")),
        (DictationEndAction.PRESS_ENTER, ("pulsa", "enter")),
        (DictationEndAction.PRESS_ENTER, ("presiona", "enter")),
    ),
}

_LANGUAGE_ORDER: Final[tuple[CommandLanguage, ...]] = (
    CommandLanguage.RU,
    CommandLanguage.EN,
    CommandLanguage.ES,
)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[^\W_]+", re.UNICODE)
_TRAILING_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"[\s,;:—–-]+$")


def _token_spans(text: str) -> list[tuple[str, int, int]]:
    tokens: list[tuple[str, int, int]] = []
    for match in _TOKEN_RE.finditer(text):
        normalized = normalize_spoken_text(match.group(0))
        if normalized:
            tokens.append((normalized, match.start(), match.end()))
    return tokens


def extract_dictation_end_action(
    text: str,
    *,
    language: CommandLanguage | str = CommandLanguage.AUTO,
) -> DictationEndActionMatch | None:
    """Return an exact final action while preserving the preceding text.

    A standalone command is intentionally not accepted here: everyday mode
    end actions are scoped to a successful text insertion.  Standalone
    commands continue through the normal command parser and confirmation
    policy.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    try:
        selected_language = CommandLanguage(language)
    except ValueError:
        selected_language = CommandLanguage.AUTO
    tokens = _token_spans(text)
    if not tokens:
        return None
    languages = (
        _LANGUAGE_ORDER
        if selected_language is CommandLanguage.AUTO
        else (selected_language,)
    )
    normalized_tokens = tuple(item[0] for item in tokens)
    for candidate_language in languages:
        for action, phrase in _PHRASES[candidate_language]:
            if len(normalized_tokens) <= len(phrase):
                continue
            if normalized_tokens[-len(phrase) :] != phrase:
                continue
            command_start = tokens[-len(phrase)][1]
            body = _TRAILING_SEPARATOR_RE.sub("", text[:command_start]).rstrip()
            if not body:
                continue
            return DictationEndActionMatch(body, action, candidate_language)
    return None


__all__ = [
    "DictationEndAction",
    "DictationEndActionMatch",
    "extract_dictation_end_action",
]
