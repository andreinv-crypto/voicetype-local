"""Deterministic, side-effect-free transforms for completed dictation text.

This module has no UI, clipboard, keyboard, shell, network, model, or storage
access.  It recognizes only a closed RU/EN/ES phrase list.  The caller decides
when the resulting text is inserted.

Templates are intentionally stricter than embedded formatting commands: a
template expands only when the complete normalized utterance is its name, or
when the complete utterance is ``шаблон|template|plantilla`` plus its name.
Template contents are returned byte-for-byte and are never recursively parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Mapping


class DictationLanguage(str, Enum):
    AUTO = "auto"
    RU = "ru"
    EN = "en"
    ES = "es"


class DictationCommand(str, Enum):
    NEW_LINE = "new_line"
    NEW_PARAGRAPH = "new_paragraph"
    TAB = "tab"
    PERIOD = "period"
    COMMA = "comma"
    QUESTION_MARK = "question_mark"
    EXCLAMATION_MARK = "exclamation_mark"
    COLON = "colon"
    SEMICOLON = "semicolon"


@dataclass(frozen=True, slots=True)
class AppliedDictationCommand:
    """Metadata-only record; it deliberately contains no source phrase."""

    command: DictationCommand
    language: DictationLanguage


@dataclass(frozen=True, slots=True)
class DictationTransformResult:
    text: str
    language: DictationLanguage
    applied_commands: tuple[AppliedDictationCommand, ...] = ()
    applied_template: str | None = None
    fillers_removed: int = 0
    changed: bool = False


@dataclass(frozen=True, slots=True)
class _PhraseSpec:
    phrase: str
    command: DictationCommand
    replacement: str
    language: DictationLanguage


@dataclass(frozen=True, slots=True)
class _NormalizedView:
    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Operation:
    start: int
    end: int
    replacement: str
    command: DictationCommand | None = None
    language: DictationLanguage | None = None
    is_filler: bool = False


_ACCENT_TRANSLATION: Final[dict[int, str]] = str.maketrans(
    {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
        "ё": "е",
    }
)

_PHRASES: Final[tuple[_PhraseSpec, ...]] = (
    # Russian
    _PhraseSpec("новая строка", DictationCommand.NEW_LINE, "\n", DictationLanguage.RU),
    _PhraseSpec(
        "новый абзац", DictationCommand.NEW_PARAGRAPH, "\n\n", DictationLanguage.RU
    ),
    _PhraseSpec("табуляция", DictationCommand.TAB, "\t", DictationLanguage.RU),
    _PhraseSpec("таб", DictationCommand.TAB, "\t", DictationLanguage.RU),
    _PhraseSpec("точка", DictationCommand.PERIOD, ".", DictationLanguage.RU),
    _PhraseSpec("запятая", DictationCommand.COMMA, ",", DictationLanguage.RU),
    _PhraseSpec(
        "вопросительный знак",
        DictationCommand.QUESTION_MARK,
        "?",
        DictationLanguage.RU,
    ),
    _PhraseSpec(
        "знак вопроса", DictationCommand.QUESTION_MARK, "?", DictationLanguage.RU
    ),
    _PhraseSpec(
        "восклицательный знак",
        DictationCommand.EXCLAMATION_MARK,
        "!",
        DictationLanguage.RU,
    ),
    _PhraseSpec(
        "знак восклицания",
        DictationCommand.EXCLAMATION_MARK,
        "!",
        DictationLanguage.RU,
    ),
    _PhraseSpec("двоеточие", DictationCommand.COLON, ":", DictationLanguage.RU),
    _PhraseSpec(
        "точка с запятой", DictationCommand.SEMICOLON, ";", DictationLanguage.RU
    ),
    # English
    _PhraseSpec("new line", DictationCommand.NEW_LINE, "\n", DictationLanguage.EN),
    _PhraseSpec(
        "new paragraph", DictationCommand.NEW_PARAGRAPH, "\n\n", DictationLanguage.EN
    ),
    _PhraseSpec("tab", DictationCommand.TAB, "\t", DictationLanguage.EN),
    _PhraseSpec("period", DictationCommand.PERIOD, ".", DictationLanguage.EN),
    _PhraseSpec("full stop", DictationCommand.PERIOD, ".", DictationLanguage.EN),
    _PhraseSpec("comma", DictationCommand.COMMA, ",", DictationLanguage.EN),
    _PhraseSpec(
        "question mark", DictationCommand.QUESTION_MARK, "?", DictationLanguage.EN
    ),
    _PhraseSpec(
        "exclamation mark",
        DictationCommand.EXCLAMATION_MARK,
        "!",
        DictationLanguage.EN,
    ),
    _PhraseSpec(
        "exclamation point",
        DictationCommand.EXCLAMATION_MARK,
        "!",
        DictationLanguage.EN,
    ),
    _PhraseSpec("colon", DictationCommand.COLON, ":", DictationLanguage.EN),
    _PhraseSpec("semicolon", DictationCommand.SEMICOLON, ";", DictationLanguage.EN),
    # Spanish
    _PhraseSpec("nueva linea", DictationCommand.NEW_LINE, "\n", DictationLanguage.ES),
    _PhraseSpec(
        "nuevo parrafo", DictationCommand.NEW_PARAGRAPH, "\n\n", DictationLanguage.ES
    ),
    _PhraseSpec("tabulacion", DictationCommand.TAB, "\t", DictationLanguage.ES),
    _PhraseSpec("punto", DictationCommand.PERIOD, ".", DictationLanguage.ES),
    _PhraseSpec("coma", DictationCommand.COMMA, ",", DictationLanguage.ES),
    _PhraseSpec(
        "signo de interrogacion",
        DictationCommand.QUESTION_MARK,
        "?",
        DictationLanguage.ES,
    ),
    _PhraseSpec(
        "signo de exclamacion",
        DictationCommand.EXCLAMATION_MARK,
        "!",
        DictationLanguage.ES,
    ),
    _PhraseSpec("dos puntos", DictationCommand.COLON, ":", DictationLanguage.ES),
    _PhraseSpec(
        "punto y coma", DictationCommand.SEMICOLON, ";", DictationLanguage.ES
    ),
)

_FILLERS: Final[dict[DictationLanguage, tuple[str, ...]]] = {
    # Keep the list deliberately small. Words such as «ну», "like", "pues" or
    # "este" can carry meaning and therefore are not removed.
    DictationLanguage.RU: ("эм", "ээ", "э э", "мм"),
    DictationLanguage.EN: ("um", "uh", "erm"),
    DictationLanguage.ES: ("eh", "em", "mmm"),
}

_TEMPLATE_PREFIXES: Final[dict[DictationLanguage, tuple[str, ...]]] = {
    DictationLanguage.RU: ("шаблон",),
    DictationLanguage.EN: ("template",),
    DictationLanguage.ES: ("plantilla",),
}

_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?![\w.-])"
)
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![\w@])(?:"
    r"(?:https?://|www\.)[^\s<>\[\]{}\"']+|"
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|org|net|edu|gov|io|ai|app|dev|co|me|info|biz|cloud|"
    r"online|site|tech|xyz|us|uk|es|ru|de|fr|it|pt|eu)"
    r"(?:/[^\s<>\[\]{}\"']*)?"
    r")"
)
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w@])(?:[$€£]\s*)?[+-]?\d+"
    r"(?:[ .,'’:/-]\d+)*(?:\s*(?:%|[$€£]))?(?![\w@])"
)
_SEPARATED_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\w)[A-Za-zА-Яа-яЁё0-9]+(?:[_-][A-Za-zА-Яа-яЁё0-9]+)+(?!\w)"
)
_UPPER_CODE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\w)(?:[A-ZА-ЯЁ]{2,}|"
    r"(?=[A-ZА-ЯЁ0-9]{3,}(?!\w))(?=[A-ZА-ЯЁ0-9]*[A-ZА-ЯЁ])"
    r"(?=[A-ZА-ЯЁ0-9]*\d)[A-ZА-ЯЁ0-9]+)(?!\w)"
)

_PUNCTUATION_REPLACEMENTS: Final[frozenset[str]] = frozenset(".,?!:;")
_CLOSING_PUNCTUATION: Final[frozenset[str]] = frozenset(".,?!:;%)]}»”’\"'")
_OPENING_PUNCTUATION: Final[frozenset[str]] = frozenset("([{«“‘/@#-\"'")


def _fold_char(character: str) -> str:
    return character.casefold().translate(_ACCENT_TRANSLATION)


def _protected_spans(text: str) -> tuple[tuple[int, int], ...]:
    candidates: list[tuple[int, int]] = []
    for pattern in (
        _URL_RE,
        _EMAIL_RE,
        _SEPARATED_CODE_RE,
        _UPPER_CODE_RE,
        _NUMBER_RE,
    ):
        for match in pattern.finditer(text):
            start, end = match.span()
            if pattern is _URL_RE:
                while end > start and text[end - 1] in ".,;:!?)]}":
                    end -= 1
            if end > start:
                candidates.append((start, end))

    selected: list[tuple[int, int]] = []
    for start, end in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < current_end and end > current_start for current_start, current_end in selected):
            continue
        selected.append((start, end))
    return tuple(sorted(selected))


def _normalized_view(text: str, protected: tuple[tuple[int, int], ...] = ()) -> _NormalizedView:
    mask = bytearray(len(text))
    for start, end in protected:
        mask[start:end] = b"\x01" * (end - start)

    characters: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, character in enumerate(text):
        folded = "\ue000" if mask[index] else _fold_char(character)
        for normalized in folded:
            if normalized.isspace():
                if characters and characters[-1] == " ":
                    ends[-1] = index + 1
                else:
                    characters.append(" ")
                    starts.append(index)
                    ends.append(index + 1)
            else:
                characters.append(normalized)
                starts.append(index)
                ends.append(index + 1)
    return _NormalizedView("".join(characters), tuple(starts), tuple(ends))


def _normalized_text(text: str) -> str:
    return _normalized_view(text).text.strip()


def _languages(language: DictationLanguage) -> tuple[DictationLanguage, ...]:
    if language is DictationLanguage.AUTO:
        return (DictationLanguage.RU, DictationLanguage.EN, DictationLanguage.ES)
    return (language,)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    normalized = _normalized_text(phrase)
    return re.compile(rf"(?<![\w]){re.escape(normalized)}(?![\w])")


def _original_span(view: _NormalizedView, start: int, end: int) -> tuple[int, int]:
    return view.starts[start], view.ends[end - 1]


def _command_candidates(
    text: str,
    view: _NormalizedView,
    language: DictationLanguage,
) -> list[_Operation]:
    allowed = set(_languages(language))
    candidates: list[_Operation] = []
    for spec in _PHRASES:
        if spec.language not in allowed:
            continue
        for match in _phrase_pattern(spec.phrase).finditer(view.text):
            start, end = _original_span(view, *match.span())
            candidates.append(
                _Operation(
                    start,
                    end,
                    spec.replacement,
                    command=spec.command,
                    language=spec.language,
                )
            )
    return candidates


def _filler_candidates(
    text: str,
    view: _NormalizedView,
    language: DictationLanguage,
) -> list[_Operation]:
    candidates: list[_Operation] = []
    for candidate_language in _languages(language):
        for filler in _FILLERS[candidate_language]:
            for match in _phrase_pattern(filler).finditer(view.text):
                start, end = _original_span(view, *match.span())
                # A common ASR rendering is "hello, um, world". The comma
                # following the hesitation belongs to the filler; the comma
                # before it remains valid punctuation.
                cursor = end
                while cursor < len(text) and text[cursor] in " \t":
                    cursor += 1
                if cursor < len(text) and text[cursor] == ",":
                    end = cursor + 1
                left_cursor = start - 1
                while left_cursor >= 0 and text[left_cursor] in " \t":
                    left_cursor -= 1
                right_cursor = end
                while right_cursor < len(text) and text[right_cursor] in " \t":
                    right_cursor += 1
                left = text[left_cursor] if left_cursor >= 0 else ""
                right = text[right_cursor] if right_cursor < len(text) else ""
                # Parenthesized/quoted tokens can be deliberate literal text.
                # Leaving them alone is safer than cleaning them as hesitation.
                if (left and left in "([{«“‘\"'") or (
                    right and right in ")]}»”’\"'"
                ):
                    continue
                candidates.append(
                    _Operation(start, end, "", language=candidate_language, is_filler=True)
                )
    return candidates


def _select_operations(candidates: list[_Operation]) -> list[_Operation]:
    # Longest match at a position wins (notably Spanish "punto y coma" over
    # "coma"). Command matches win a theoretical equal-span filler collision.
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.start,
            -(item.end - item.start),
            1 if item.is_filler else 0,
        ),
    )
    selected: list[_Operation] = []
    last_end = -1
    for operation in ordered:
        if operation.start < last_end:
            continue
        selected.append(operation)
        last_end = operation.end
    return selected


def _needs_joining_space(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    left = previous[-1]
    right = current[0]
    if left.isspace() or right.isspace():
        return False
    if right in _CLOSING_PUNCTUATION or left in _OPENING_PUNCTUATION:
        return False
    return True


def _render_operations(text: str, operations: list[_Operation]) -> str:
    output = ""
    cursor = 0
    trim_next: str | None = None
    space_after_punctuation = False
    join_after_filler = False

    def append_text(segment: str) -> None:
        nonlocal output, trim_next, space_after_punctuation, join_after_filler
        if trim_next == "horizontal":
            segment = segment.lstrip(" \t")
        elif trim_next == "all":
            segment = segment.lstrip(" \t\r\n")
        trim_next = None
        if not segment:
            return
        if space_after_punctuation:
            if _needs_joining_space(output, segment):
                output += " "
            space_after_punctuation = False
        if join_after_filler:
            if _needs_joining_space(output, segment):
                output += " "
            join_after_filler = False
        output += segment

    for operation in operations:
        append_text(text[cursor : operation.start])
        cursor = operation.end
        if operation.is_filler:
            output = output.rstrip(" \t")
            trim_next = "horizontal"
            join_after_filler = True
            continue

        # A visible formatting command supersedes pending spacing from an
        # adjacent removed filler or punctuation command.
        space_after_punctuation = False
        join_after_filler = False
        replacement = operation.replacement
        if replacement in _PUNCTUATION_REPLACEMENTS:
            output = output.rstrip(" \t") + replacement
            trim_next = "horizontal"
            space_after_punctuation = True
        elif replacement == "\n":
            output = output.rstrip(" \t") + "\n"
            trim_next = "horizontal"
        elif replacement == "\n\n":
            output = output.rstrip(" \t\r\n") + "\n\n"
            trim_next = "all"
        elif replacement == "\t":
            output = output.rstrip(" \t") + "\t"
            trim_next = "horizontal"
        else:  # pragma: no cover - closed table invariant
            output += replacement

    append_text(text[cursor:])
    return output


def _template_match(
    text: str,
    language: DictationLanguage,
    templates: Mapping[str, str],
) -> tuple[str, str] | None:
    normalized_names: dict[str, list[tuple[str, str]]] = {}
    for name, content in templates.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise TypeError("template names and contents must be str")
        normalized_name = _normalized_text(name)
        if not normalized_name:
            continue
        normalized_names.setdefault(normalized_name, []).append((name, content))

    def unique(name: str) -> tuple[str, str] | None:
        matches = normalized_names.get(name, ())
        return matches[0] if len(matches) == 1 else None

    utterance = _normalized_text(text)
    direct = unique(utterance)
    if direct is not None:
        return direct

    for candidate_language in _languages(language):
        for prefix in _TEMPLATE_PREFIXES[candidate_language]:
            normalized_prefix = _normalized_text(prefix)
            if not utterance.startswith(normalized_prefix + " "):
                continue
            requested_name = utterance[len(normalized_prefix) + 1 :].strip()
            return unique(requested_name)
    return None


def transform_dictation(
    text: str,
    *,
    language: DictationLanguage | str = DictationLanguage.AUTO,
    templates: Mapping[str, str] | None = None,
    commands_enabled: bool = True,
    remove_fillers: bool = False,
) -> DictationTransformResult:
    """Apply exact embedded formatting phrases to one completed utterance.

    Protected URLs, emails, numeric values, and code-like tokens are never
    searched for commands or fillers.  Template expansion happens first and is
    terminal: predefined content is not recursively interpreted. Embedded
    commands and conservative filler removal can be enabled independently.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    language = DictationLanguage(language)
    template_values: Mapping[str, str] = templates or {}
    matched_template = _template_match(text, language, template_values)
    if matched_template is not None:
        name, content = matched_template
        return DictationTransformResult(
            text=content,
            language=language,
            applied_template=name,
            changed=content != text,
        )

    protected = _protected_spans(text)
    view = _normalized_view(text, protected)
    candidates = (
        _command_candidates(text, view, language) if bool(commands_enabled) else []
    )
    if bool(remove_fillers):
        candidates.extend(_filler_candidates(text, view, language))
    operations = _select_operations(candidates)
    transformed = _render_operations(text, operations)
    applied = tuple(
        AppliedDictationCommand(operation.command, operation.language)
        for operation in operations
        if operation.command is not None and operation.language is not None
    )
    return DictationTransformResult(
        text=transformed,
        language=language,
        applied_commands=applied,
        fillers_removed=sum(operation.is_filler for operation in operations),
        changed=transformed != text,
    )


def apply_short_phrase_period_policy(
    text: str,
    *,
    no_final_period_short: bool = False,
    period_was_auto_added: bool = False,
    max_words: int = 8,
) -> str:
    """Remove a short phrase's final period only with explicit provenance.

    ``no_final_period_short=True`` alone is intentionally insufficient: a local
    rule cannot know whether the user dictated the period.  The caller must
    additionally prove ``period_was_auto_added=True``.  This keeps user-spoken
    punctuation intact by default.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    if not isinstance(max_words, int) or isinstance(max_words, bool) or not 1 <= max_words <= 50:
        raise ValueError("max_words must be an integer between 1 and 50")
    if not no_final_period_short or not period_was_auto_added:
        return text

    content_end = len(text.rstrip(" \t"))
    content = text[:content_end]
    trailing = text[content_end:]
    if not content or "\n" in content or "\r" in content:
        return text
    if not content.endswith(".") or content.endswith(".."):
        return text
    word_count = len(re.findall(r"(?u)\b\w+(?:[-'’]\w+)*\b", content[:-1]))
    if not 1 <= word_count <= max_words:
        return text
    return content[:-1] + trailing


def combine_insertions(previous_tail: str, current: str) -> str:
    """Join sequential dictations with one safe, deterministic separator.

    No space is added around existing whitespace, before closing punctuation,
    or after opening punctuation/path-like delimiters.  Both arguments are
    returned as supplied apart from the possible single separator.
    """

    if not isinstance(previous_tail, str) or not isinstance(current, str):
        raise TypeError("previous_tail and current must be str")
    if not previous_tail or not current:
        return previous_tail + current
    if _needs_joining_space(previous_tail, current):
        return previous_tail + " " + current
    return previous_tail + current


__all__ = [
    "AppliedDictationCommand",
    "DictationCommand",
    "DictationLanguage",
    "DictationTransformResult",
    "apply_short_phrase_period_policy",
    "combine_insertions",
    "transform_dictation",
]
