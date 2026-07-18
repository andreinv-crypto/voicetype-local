from __future__ import annotations

"""Strict, session-only commands for editing VoiceType's own last insertion.

This module is deliberately side-effect free.  It parses a small RU/EN/ES
grammar and produces an exact edit plan over text that VoiceType already keeps
in RAM.  Desktop verification and input are handled by :mod:`session_editor`.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .voice_commands import CommandLanguage, VoiceMode, normalize_spoken_text


MAX_EDIT_FRAGMENT_CHARS: Final[int] = 400
MAX_TRACKED_INSERTION_CHARS: Final[int] = 12_000


class SessionEditAction(StrEnum):
    DELETE_LAST_WORD = "delete_last_word"
    DELETE_LAST_SENTENCE = "delete_last_sentence"
    DELETE_LAST_DICTATION = "delete_last_dictation"
    REPLACE_LAST_WORD = "replace_last_word"
    REPLACE_LAST_SENTENCE = "replace_last_sentence"
    REPLACE_LAST_DICTATION = "replace_last_dictation"
    REPLACE_UNIQUE = "replace_unique"
    RESTORE_LAST_EDIT = "restore_last_edit"


@dataclass(frozen=True, slots=True)
class SessionEditRequest:
    action: SessionEditAction
    language: CommandLanguage
    old_text: str | None = None
    new_text: str | None = None

    def __post_init__(self) -> None:
        if self.old_text is not None:
            if not self.old_text or len(self.old_text) > MAX_EDIT_FRAGMENT_CHARS:
                raise ValueError("old_text is empty or too long")
        if self.new_text is not None:
            if not self.new_text or len(self.new_text) > MAX_EDIT_FRAGMENT_CHARS:
                raise ValueError("new_text is empty or too long")
        if self.action is SessionEditAction.REPLACE_UNIQUE:
            if self.old_text is None or self.new_text is None:
                raise ValueError("replace_unique requires old_text and new_text")
        elif self.action in {
            SessionEditAction.REPLACE_LAST_WORD,
            SessionEditAction.REPLACE_LAST_SENTENCE,
            SessionEditAction.REPLACE_LAST_DICTATION,
        }:
            if self.old_text is not None or self.new_text is None:
                raise ValueError("replacement of the last unit requires new_text only")
        elif self.old_text is not None or self.new_text is not None:
            raise ValueError("non-replacement actions cannot carry text")


@dataclass(frozen=True, slots=True)
class SessionEditParseResult:
    recognized: bool
    request: SessionEditRequest | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.request is not None and not self.recognized:
            raise ValueError("a parsed request must be recognized")
        if self.request is not None and self.reason_code is not None:
            raise ValueError("a successful parse cannot also contain an error")


@dataclass(frozen=True, slots=True)
class SessionEditPlan:
    action: SessionEditAction
    original_text: str
    result_text: str
    selected_text: str
    replacement_text: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if not 0 <= self.start <= self.end <= len(self.original_text):
            raise ValueError("edit range is outside the tracked insertion")
        if self.original_text[self.start : self.end] != self.selected_text:
            raise ValueError("selected_text does not match the edit range")
        expected = (
            self.original_text[: self.start]
            + self.replacement_text
            + self.original_text[self.end :]
        )
        if expected != self.result_text:
            raise ValueError("result_text does not match the edit operation")


class SessionEditPlanError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


_PREFIX_PATTERNS: Final[dict[CommandLanguage, re.Pattern[str]]] = {
    CommandLanguage.RU: re.compile(
        r"^\s*команда(?:\s+|\s*[,.;:!?…—–-]+\s*)(?P<body>.+?)\s*$",
        re.IGNORECASE,
    ),
    CommandLanguage.EN: re.compile(
        r"^\s*command(?:\s+|\s*[,.;:!?…—–-]+\s*)(?P<body>.+?)\s*$",
        re.IGNORECASE,
    ),
    CommandLanguage.ES: re.compile(
        r"^\s*comando(?:\s+|\s*[,.;:!?…—–-]+\s*)(?P<body>.+?)\s*$",
        re.IGNORECASE,
    ),
}

_LANGUAGE_ORDER: Final[tuple[CommandLanguage, ...]] = (
    CommandLanguage.RU,
    CommandLanguage.EN,
    CommandLanguage.ES,
)

_RU_POLITE_PREFIX: Final[re.Pattern[str]] = re.compile(
    r"^\s*пожалуйста(?:\s+|\s*[,.;:!?…—–-]+\s*)(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_RU_GRAMMAR_SEPARATOR: Final[str] = r"(?:\s+|\s*[,.;:!?…—–-]+\s*)"

_EXACT_ACTIONS: Final[
    dict[CommandLanguage, dict[str, SessionEditAction]]
] = {
    CommandLanguage.RU: {
        "удали последнее слово": SessionEditAction.DELETE_LAST_WORD,
        "удалить последнее слово": SessionEditAction.DELETE_LAST_WORD,
        "убери последнее слово": SessionEditAction.DELETE_LAST_WORD,
        "убрать последнее слово": SessionEditAction.DELETE_LAST_WORD,
        "удали последнее предложение": SessionEditAction.DELETE_LAST_SENTENCE,
        "удалить последнее предложение": SessionEditAction.DELETE_LAST_SENTENCE,
        "удали последнюю фразу": SessionEditAction.DELETE_LAST_SENTENCE,
        "удалить последнюю фразу": SessionEditAction.DELETE_LAST_SENTENCE,
        "убери последнее предложение": SessionEditAction.DELETE_LAST_SENTENCE,
        "убрать последнее предложение": SessionEditAction.DELETE_LAST_SENTENCE,
        "убери последнюю фразу": SessionEditAction.DELETE_LAST_SENTENCE,
        "убрать последнюю фразу": SessionEditAction.DELETE_LAST_SENTENCE,
        "удали последний текст": SessionEditAction.DELETE_LAST_DICTATION,
        "удалить последний текст": SessionEditAction.DELETE_LAST_DICTATION,
        "убери последний текст": SessionEditAction.DELETE_LAST_DICTATION,
        "убрать последний текст": SessionEditAction.DELETE_LAST_DICTATION,
        "удали последнюю диктовку": SessionEditAction.DELETE_LAST_DICTATION,
        "удалить последнюю диктовку": SessionEditAction.DELETE_LAST_DICTATION,
        "убери последнюю диктовку": SessionEditAction.DELETE_LAST_DICTATION,
        "убрать последнюю диктовку": SessionEditAction.DELETE_LAST_DICTATION,
        "верни последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "вернуть последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "верни последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "вернуть последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "верни последнюю правку": SessionEditAction.RESTORE_LAST_EDIT,
        "вернуть последнюю правку": SessionEditAction.RESTORE_LAST_EDIT,
        "отмени последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "отменить последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "восстанови последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "восстановить последнее исправление": SessionEditAction.RESTORE_LAST_EDIT,
        "восстанови последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "восстановить последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "отмени последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "отменить последнее изменение": SessionEditAction.RESTORE_LAST_EDIT,
        "отмени последнюю правку": SessionEditAction.RESTORE_LAST_EDIT,
        "отменить последнюю правку": SessionEditAction.RESTORE_LAST_EDIT,
    },
    CommandLanguage.EN: {
        "delete last word": SessionEditAction.DELETE_LAST_WORD,
        "delete the last word": SessionEditAction.DELETE_LAST_WORD,
        "remove last word": SessionEditAction.DELETE_LAST_WORD,
        "remove the last word": SessionEditAction.DELETE_LAST_WORD,
        "delete last sentence": SessionEditAction.DELETE_LAST_SENTENCE,
        "delete the last sentence": SessionEditAction.DELETE_LAST_SENTENCE,
        "remove last sentence": SessionEditAction.DELETE_LAST_SENTENCE,
        "remove the last sentence": SessionEditAction.DELETE_LAST_SENTENCE,
        "delete last dictation": SessionEditAction.DELETE_LAST_DICTATION,
        "delete the last dictation": SessionEditAction.DELETE_LAST_DICTATION,
        "delete last text": SessionEditAction.DELETE_LAST_DICTATION,
        "remove last dictation": SessionEditAction.DELETE_LAST_DICTATION,
        "remove last text": SessionEditAction.DELETE_LAST_DICTATION,
        "restore last voice edit": SessionEditAction.RESTORE_LAST_EDIT,
        "undo last voice edit": SessionEditAction.RESTORE_LAST_EDIT,
    },
    CommandLanguage.ES: {
        "borra la ultima palabra": SessionEditAction.DELETE_LAST_WORD,
        "borrar la ultima palabra": SessionEditAction.DELETE_LAST_WORD,
        "elimina la ultima palabra": SessionEditAction.DELETE_LAST_WORD,
        "eliminar la ultima palabra": SessionEditAction.DELETE_LAST_WORD,
        "borra la ultima frase": SessionEditAction.DELETE_LAST_SENTENCE,
        "borrar la ultima frase": SessionEditAction.DELETE_LAST_SENTENCE,
        "borrar la ultima oracion": SessionEditAction.DELETE_LAST_SENTENCE,
        "borra la ultima oracion": SessionEditAction.DELETE_LAST_SENTENCE,
        "elimina la ultima frase": SessionEditAction.DELETE_LAST_SENTENCE,
        "eliminar la ultima frase": SessionEditAction.DELETE_LAST_SENTENCE,
        "eliminar la ultima oracion": SessionEditAction.DELETE_LAST_SENTENCE,
        "borra el ultimo dictado": SessionEditAction.DELETE_LAST_DICTATION,
        "borrar el ultimo dictado": SessionEditAction.DELETE_LAST_DICTATION,
        "elimina el ultimo dictado": SessionEditAction.DELETE_LAST_DICTATION,
        "eliminar el ultimo dictado": SessionEditAction.DELETE_LAST_DICTATION,
        "borra el ultimo texto": SessionEditAction.DELETE_LAST_DICTATION,
        "borrar el ultimo texto": SessionEditAction.DELETE_LAST_DICTATION,
        "eliminar el ultimo texto": SessionEditAction.DELETE_LAST_DICTATION,
        "deshaz la ultima correccion": SessionEditAction.RESTORE_LAST_EDIT,
        "deshacer la ultima correccion": SessionEditAction.RESTORE_LAST_EDIT,
        "restaura la ultima correccion": SessionEditAction.RESTORE_LAST_EDIT,
        "restaurar la ultima correccion": SessionEditAction.RESTORE_LAST_EDIT,
    },
}

_EDIT_STARTS: Final[dict[CommandLanguage, tuple[str, ...]]] = {
    CommandLanguage.RU: ("удали ", "удалить ", "убери ", "убрать ", "замени ", "заменить ", "поменяй ", "поменять ", "исправь ", "исправить ", "верни ", "вернуть ", "восстанови ", "восстановить ", "отмени ", "отменить "),
    CommandLanguage.EN: ("delete ", "remove ", "replace ", "change ", "correct ", "restore ", "undo "),
    CommandLanguage.ES: ("borra ", "borrar ", "elimina ", "eliminar ", "reemplaza ", "reemplazar ", "cambia ", "cambiar ", "corrige ", "corregir ", "deshaz ", "restaura "),
}

_REPLACE_LAST_PATTERNS: Final[
    dict[CommandLanguage, tuple[tuple[re.Pattern[str], SessionEditAction], ...]]
] = {
    CommandLanguage.RU: (
        (
            re.compile(
                rf"^\s*(?:замени|заменить|поменяй|поменять|исправь|исправить){_RU_GRAMMAR_SEPARATOR}последнее\s+слово\s+на{_RU_GRAMMAR_SEPARATOR}(?P<new>.+?)\s*$",
                re.I,
            ),
            SessionEditAction.REPLACE_LAST_WORD,
        ),
        (
            re.compile(
                rf"^\s*(?:замени|заменить|поменяй|поменять|исправь|исправить){_RU_GRAMMAR_SEPARATOR}(?:последнее\s+предложение|последнюю\s+фразу)\s+на{_RU_GRAMMAR_SEPARATOR}(?P<new>.+?)\s*$",
                re.I,
            ),
            SessionEditAction.REPLACE_LAST_SENTENCE,
        ),
        (
            re.compile(
                rf"^\s*(?:замени|заменить|исправь|исправить){_RU_GRAMMAR_SEPARATOR}(?:последний\s+текст|последнюю\s+диктовку)\s+на{_RU_GRAMMAR_SEPARATOR}(?P<new>.+?)\s*$",
                re.I,
            ),
            SessionEditAction.REPLACE_LAST_DICTATION,
        ),
    ),
    CommandLanguage.EN: (
        (re.compile(r"^\s*(?:replace|change|correct)\s+(?:the\s+)?last\s+word\s+with\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_WORD),
        (re.compile(r"^\s*(?:replace|change|correct)\s+(?:the\s+)?last\s+sentence\s+with\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_SENTENCE),
        (re.compile(r"^\s*(?:replace|change|correct)\s+(?:the\s+)?last\s+(?:dictation|text)\s+with\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_DICTATION),
    ),
    CommandLanguage.ES: (
        (re.compile(r"^\s*(?:reemplaza|reemplazar|cambia|cambiar|corrige|corregir)\s+la\s+[úu]ltima\s+palabra\s+(?:por|con)\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_WORD),
        (re.compile(r"^\s*(?:reemplaza|reemplazar|cambia|cambiar|corrige|corregir)\s+la\s+[úu]ltima\s+(?:frase|oraci[oó]n)\s+(?:por|con)\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_SENTENCE),
        (re.compile(r"^\s*(?:reemplaza|reemplazar|cambia|cambiar|corrige|corregir)\s+el\s+[úu]ltimo\s+(?:dictado|texto)\s+(?:por|con)\s+(?P<new>.+?)\s*$", re.I), SessionEditAction.REPLACE_LAST_DICTATION),
    ),
}

_REPLACE_UNIQUE_PATTERNS: Final[dict[CommandLanguage, re.Pattern[str]]] = {
    CommandLanguage.RU: re.compile(
        rf"^\s*(?:замени|заменить){_RU_GRAMMAR_SEPARATOR}(?:в\s+последн(?:ем|ей)\s+(?:тексте|диктовке)\s+)?(?P<old>.+?)\s+на{_RU_GRAMMAR_SEPARATOR}(?P<new>.+?)\s*$",
        re.I,
    ),
    CommandLanguage.EN: re.compile(
        r"^\s*replace(?:\s+in\s+(?:the\s+)?last\s+(?:text|dictation))?\s+(?P<old>.+?)\s+with\s+(?P<new>.+?)\s*$",
        re.I,
    ),
    CommandLanguage.ES: re.compile(
        r"^\s*(?:reemplaza|reemplazar)(?:\s+en\s+el\s+[úu]ltimo\s+(?:texto|dictado))?\s+(?P<old>.+?)\s+(?:por|con)\s+(?P<new>.+?)\s*$",
        re.I,
    ),
}


def _languages(language: CommandLanguage) -> tuple[CommandLanguage, ...]:
    return _LANGUAGE_ORDER if language is CommandLanguage.AUTO else (language,)


def _strip_prefix(
    text: str,
    languages: tuple[CommandLanguage, ...],
) -> tuple[str, CommandLanguage] | None:
    for language in languages:
        match = _PREFIX_PATTERNS[language].fullmatch(text)
        if match:
            return match.group("body"), language
    return None


def _clean_fragment(value: str) -> str:
    # Remove only whitespace belonging to the command grammar.  In particular,
    # do not case-fold, normalize punctuation, collapse internal whitespace, or
    # strip quotation marks from the user's replacement payload.
    return value.strip()


class SessionEditCommandParser:
    """Parse only explicit editing commands; never reinterpret dictation."""

    def parse(
        self,
        text: str,
        *,
        mode: VoiceMode | str,
        language: CommandLanguage | str = CommandLanguage.AUTO,
    ) -> SessionEditParseResult:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        selected_mode = VoiceMode(mode)
        selected_language = CommandLanguage(language)
        if selected_mode is VoiceMode.DICTATION:
            return SessionEditParseResult(False)

        languages = _languages(selected_language)
        prefixed = _strip_prefix(text, languages)
        if selected_mode is VoiceMode.MIXED:
            if prefixed is None:
                return SessionEditParseResult(False)
            body, prefix_language = prefixed
            languages = (prefix_language,)
        elif prefixed is not None:
            body, prefix_language = prefixed
            languages = (prefix_language,)
        else:
            body = text

        # A single polite lead-in is accepted only after the explicit Russian
        # command prefix.  A repeated or unprefixed "пожалуйста" never widens
        # the grammar and therefore cannot turn ordinary dictation into an edit.
        if prefixed is not None and prefix_language is CommandLanguage.RU:
            polite = _RU_POLITE_PREFIX.fullmatch(body)
            if polite is not None:
                body = polite.group("body")

        normalized = normalize_spoken_text(body)
        if not normalized:
            return SessionEditParseResult(False)

        for candidate_language in languages:
            exact = _EXACT_ACTIONS[candidate_language].get(normalized)
            if exact is not None:
                return SessionEditParseResult(
                    True,
                    SessionEditRequest(exact, candidate_language),
                )

            for pattern, action in _REPLACE_LAST_PATTERNS[candidate_language]:
                match = pattern.fullmatch(body)
                if match is None:
                    continue
                new_text = _clean_fragment(match.group("new"))
                if not new_text or len(new_text) > MAX_EDIT_FRAGMENT_CHARS:
                    return SessionEditParseResult(True, reason_code="invalid_edit_text")
                return SessionEditParseResult(
                    True,
                    SessionEditRequest(action, candidate_language, new_text=new_text),
                )

            match = _REPLACE_UNIQUE_PATTERNS[candidate_language].fullmatch(body)
            if match is not None:
                old_text = _clean_fragment(match.group("old"))
                new_text = _clean_fragment(match.group("new"))
                if (
                    not old_text
                    or not new_text
                    or len(old_text) > MAX_EDIT_FRAGMENT_CHARS
                    or len(new_text) > MAX_EDIT_FRAGMENT_CHARS
                ):
                    return SessionEditParseResult(True, reason_code="invalid_edit_text")
                return SessionEditParseResult(
                    True,
                    SessionEditRequest(
                        SessionEditAction.REPLACE_UNIQUE,
                        candidate_language,
                        old_text=old_text,
                        new_text=new_text,
                    ),
                )

            if any(normalized.startswith(prefix) for prefix in _EDIT_STARTS[candidate_language]):
                return SessionEditParseResult(
                    True,
                    reason_code="invalid_edit_command",
                )
        return SessionEditParseResult(False)


_TRAILING_WORD_RE: Final[re.Pattern[str]] = re.compile(r"\S+\s*$", re.UNICODE)
_SENTENCE_ENDS: Final[frozenset[str]] = frozenset(".!?…")


def _last_word_span(text: str) -> tuple[int, int]:
    match = _TRAILING_WORD_RE.search(text)
    if match is None:
        raise SessionEditPlanError("last_word_not_found")
    start = match.start()
    while start > 0 and text[start - 1] in {" ", "\t"}:
        start -= 1
    return start, len(text)


def _last_sentence_span(text: str) -> tuple[int, int]:
    end = len(text)
    while end > 0 and text[end - 1].isspace():
        end -= 1
    if end <= 0:
        raise SessionEditPlanError("last_sentence_not_found")

    cursor = end
    while cursor > 0 and text[cursor - 1] in _SENTENCE_ENDS:
        cursor -= 1
    while cursor > 0:
        previous = text[cursor - 1]
        if previous in _SENTENCE_ENDS or previous in {"\n", "\r"}:
            break
        cursor -= 1
    start = cursor
    while start > 0 and text[start - 1] in {" ", "\t"}:
        start -= 1
    # Include trailing whitespace because it belongs to the final unit and
    # would otherwise remain as an invisible edit artifact.
    return start, len(text)


def _unique_span(text: str, needle: str) -> tuple[int, int]:
    parts = [re.escape(part) for part in needle.split() if part]
    if not parts:
        raise SessionEditPlanError("replacement_not_found")
    expression = r"\s+".join(parts)
    if needle[0].isalnum():
        expression = r"(?<!\w)" + expression
    if needle[-1].isalnum():
        expression += r"(?!\w)"
    matches = list(re.finditer(expression, text, re.IGNORECASE | re.UNICODE))
    if not matches:
        raise SessionEditPlanError("replacement_not_found")
    if len(matches) != 1:
        raise SessionEditPlanError("replacement_ambiguous")
    return matches[0].span()


def _preserve_leading_spacing(selected: str, replacement: str) -> str:
    leading_length = len(selected) - len(selected.lstrip(" \t"))
    return selected[:leading_length] + replacement.lstrip(" \t")


def plan_session_edit(text: str, request: SessionEditRequest) -> SessionEditPlan:
    """Build one bounded edit over the tracked last insertion."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not text:
        raise SessionEditPlanError("no_session_insertion")
    if len(text) > MAX_TRACKED_INSERTION_CHARS:
        raise SessionEditPlanError("tracked_insertion_too_large")

    action = request.action
    if action is SessionEditAction.RESTORE_LAST_EDIT:
        raise SessionEditPlanError("restore_requires_transaction")
    if action is SessionEditAction.DELETE_LAST_WORD:
        start, end = _last_word_span(text)
        replacement = ""
    elif action is SessionEditAction.DELETE_LAST_SENTENCE:
        start, end = _last_sentence_span(text)
        replacement = ""
    elif action is SessionEditAction.DELETE_LAST_DICTATION:
        start, end = 0, len(text)
        replacement = ""
    elif action is SessionEditAction.REPLACE_LAST_WORD:
        start, end = _last_word_span(text)
        assert request.new_text is not None
        replacement = _preserve_leading_spacing(text[start:end], request.new_text)
    elif action is SessionEditAction.REPLACE_LAST_SENTENCE:
        start, end = _last_sentence_span(text)
        assert request.new_text is not None
        replacement = _preserve_leading_spacing(text[start:end], request.new_text)
    elif action is SessionEditAction.REPLACE_LAST_DICTATION:
        start, end = 0, len(text)
        assert request.new_text is not None
        replacement = _preserve_leading_spacing(text, request.new_text)
    elif action is SessionEditAction.REPLACE_UNIQUE:
        assert request.old_text is not None and request.new_text is not None
        start, end = _unique_span(text, request.old_text)
        replacement = request.new_text
    else:  # pragma: no cover - StrEnum exhaustiveness guard
        raise SessionEditPlanError("unsupported_edit_action")

    selected = text[start:end]
    result = text[:start] + replacement + text[end:]
    if result == text:
        raise SessionEditPlanError("edit_would_not_change_text")
    return SessionEditPlan(
        action=action,
        original_text=text,
        result_text=result,
        selected_text=selected,
        replacement_text=replacement,
        start=start,
        end=end,
    )


__all__ = [
    "MAX_EDIT_FRAGMENT_CHARS",
    "MAX_TRACKED_INSERTION_CHARS",
    "SessionEditAction",
    "SessionEditCommandParser",
    "SessionEditParseResult",
    "SessionEditPlan",
    "SessionEditPlanError",
    "SessionEditRequest",
    "plan_session_edit",
]
