from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .memory import ReplacementCandidate, normalize_key


@dataclass(frozen=True, slots=True)
class ReplacementRule:
    alias: str
    replacement: str
    match_type: str = "token_sequence"
    term_id: int | None = None
    precedence: tuple[int, int, int, str] = (0, 0, 0, "")


@dataclass(frozen=True, slots=True)
class AppliedReplacement:
    start: int
    end: int
    original: str
    replacement: str
    term_id: int | None


@dataclass(frozen=True, slots=True)
class RuleApplication:
    text: str
    applied: tuple[AppliedReplacement, ...]


@dataclass(frozen=True, slots=True)
class ProtectedToken:
    kind: str
    text: str
    start: int
    end: int

    @property
    def comparison_key(self) -> tuple[str, str]:
        # Compatibility normalization is safe for comparing presentation forms,
        # but case and diacritics remain significant for protected values.
        return self.kind, unicodedata.normalize("NFKC", self.text)


@dataclass(frozen=True, slots=True)
class ProtectedTokenValidation:
    valid: bool
    missing: tuple[tuple[str, str], ...]
    added: tuple[tuple[str, str], ...]

    def __bool__(self) -> bool:
        return self.valid


class SafeNormalizer:
    """Stateless facade for deterministic local post-ASR normalization."""

    def normalize(
        self,
        text: str,
        rules: Iterable[ReplacementRule | ReplacementCandidate],
        *,
        protected_terms: Iterable[str] = (),
    ) -> RuleApplication:
        return apply_replacement_rules(
            text,
            rules,
            protected_terms=protected_terms,
        )

    def normalize_text(
        self,
        text: str,
        rules: Iterable[ReplacementRule | ReplacementCandidate],
        *,
        protected_terms: Iterable[str] = (),
    ) -> str:
        return self.normalize(
            text,
            rules,
            protected_terms=protected_terms,
        ).text


@dataclass(frozen=True, slots=True)
class _NormalizedView:
    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Match:
    start: int
    end: int
    rule_index: int
    rule: ReplacementRule


_URL_RE = re.compile(
    r"(?i)(?<![\w@])(?:https?://|www\.)[^\s<>\[\]{}\"']+"
)
_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?![\w.-])"
)
_DATE_RE = re.compile(
    r"(?<!\w)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\w)"
)
_AMOUNT_RE = re.compile(
    r"(?<!\w)(?:[$€£¥]\s?\d(?:[\d .,'’]*\d)?|"
    r"\d(?:[\d .,'’]*\d)?\s?(?:€|EUR|USD|GBP|RUB|₽|%))(?!\w)",
    flags=re.IGNORECASE,
)
_HYPHEN_CODE_RE = re.compile(
    r"(?<!\w)(?=[A-Za-z0-9-]{4,}(?!\w))"
    r"(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)"
    r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+(?!\w)"
)
_UPPER_CODE_RE = re.compile(
    r"(?<!\w)(?=[A-Z0-9]{5,}(?!\w))(?=[A-Z0-9]*[A-Z])"
    r"(?=[A-Z0-9]*\d)[A-Z0-9]+(?!\w)"
)
_NUMBER_RE = re.compile(
    r"(?<![\w@])[-+]?\d+(?:[.,]\d+)*(?:e[-+]?\d+)?(?![\w@])",
    flags=re.IGNORECASE,
)


def _is_word_character(character: str) -> bool:
    if not character:
        return False
    return unicodedata.category(character)[0] in {"L", "N", "M"} or character == "_"


def _normalized_view(value: str) -> _NormalizedView:
    """NFKC+casefold view with a lossless map back to original spans."""

    normalized: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    index = 0
    while index < len(value):
        start = index
        if value[index].isspace():
            index += 1
            while index < len(value) and value[index].isspace():
                index += 1
            if normalized and normalized[-1] != " ":
                normalized.append(" ")
                starts.append(start)
                ends.append(index)
            continue
        index += 1
        while index < len(value) and unicodedata.combining(value[index]):
            index += 1
        cluster = unicodedata.normalize("NFKC", value[start:index]).casefold()
        for character in cluster:
            normalized.append(character)
            starts.append(start)
            ends.append(index)
    if normalized and normalized[-1] == " ":
        normalized.pop()
        starts.pop()
        ends.pop()
    return _NormalizedView("".join(normalized), tuple(starts), tuple(ends))


def _whole_token_span(value: str, start: int, end: int) -> bool:
    before = value[start - 1] if start > 0 else ""
    after = value[end] if end < len(value) else ""
    return not _is_word_character(before) and not _is_word_character(after)


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start < protected_end and end > protected_start for protected_start, protected_end in spans)


def rules_from_candidates(
    candidates: Iterable[ReplacementCandidate],
) -> tuple[ReplacementRule, ...]:
    return tuple(
        ReplacementRule(
            alias=item.alias,
            replacement=item.canonical_text,
            match_type=item.match_type,
            term_id=item.term_id,
            precedence=item.precedence,
        )
        for item in candidates
    )


def apply_replacement_rules(
    text: str,
    rules: Iterable[ReplacementRule | ReplacementCandidate],
    *,
    protected_terms: Iterable[str] = (),
) -> RuleApplication:
    """Apply confirmed deterministic replacements at Unicode token boundaries.

    The returned text is otherwise byte-for-byte identical to the input: NFKC
    and casefold are used only for matching. URLs, email addresses, dates,
    amounts, numbers, and codes are never rewritten as a side effect.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    prepared: list[ReplacementRule] = []
    for raw in rules:
        if isinstance(raw, ReplacementCandidate):
            rule = ReplacementRule(
                raw.alias,
                raw.canonical_text,
                raw.match_type,
                raw.term_id,
                raw.precedence,
            )
        elif isinstance(raw, ReplacementRule):
            rule = raw
        else:
            raise TypeError("rules must contain ReplacementRule values")
        alias_key = normalize_key(rule.alias)
        if not alias_key or not rule.replacement:
            continue
        prepared.append(rule)
    grouped: dict[str, list[ReplacementRule]] = {}
    for rule in prepared:
        grouped.setdefault(normalize_key(rule.alias), []).append(rule)
    resolved: list[ReplacementRule] = []
    for alias_key, candidates in grouped.items():
        best_precedence = max(candidate.precedence for candidate in candidates)
        best = [candidate for candidate in candidates if candidate.precedence == best_precedence]
        # Equal-precedence disagreement is ambiguous and must fail closed.
        if len({candidate.replacement for candidate in best}) > 1:
            continue
        best.sort(
            key=lambda item: (
                item.replacement,
                item.match_type,
                item.term_id if item.term_id is not None else -1,
            )
        )
        resolved.append(best[0])
    prepared = resolved
    prepared.sort(
        key=lambda item: (
            -len(normalize_key(item.alias)),
            normalize_key(item.alias),
            item.replacement,
            item.term_id if item.term_id is not None else -1,
        )
    )
    if not text or not prepared:
        return RuleApplication(text, ())

    protected = extract_protected_tokens(text, protected_terms=protected_terms)
    protected_spans = tuple((item.start, item.end) for item in protected)
    view = _normalized_view(text)
    matches: list[_Match] = []
    for rule_index, rule in enumerate(prepared):
        needle = normalize_key(rule.alias)
        search_at = 0
        while search_at <= len(view.text) - len(needle):
            found = view.text.find(needle, search_at)
            if found < 0:
                break
            normalized_end = found + len(needle)
            original_start = view.starts[found]
            original_end = view.ends[normalized_end - 1]
            if (
                _whole_token_span(text, original_start, original_end)
                and not _overlaps(original_start, original_end, protected_spans)
            ):
                matches.append(_Match(original_start, original_end, rule_index, rule))
            search_at = found + 1

    matches.sort(key=lambda item: (item.start, -(item.end - item.start), item.rule_index))
    accepted: list[_Match] = []
    cursor = 0
    for match in matches:
        if match.start < cursor:
            continue
        accepted.append(match)
        cursor = match.end
    if not accepted:
        return RuleApplication(text, ())

    pieces: list[str] = []
    applied: list[AppliedReplacement] = []
    cursor = 0
    for match in accepted:
        pieces.append(text[cursor : match.start])
        pieces.append(match.rule.replacement)
        applied.append(
            AppliedReplacement(
                match.start,
                match.end,
                text[match.start : match.end],
                match.rule.replacement,
                match.rule.term_id,
            )
        )
        cursor = match.end
    pieces.append(text[cursor:])
    return RuleApplication("".join(pieces), tuple(applied))


def normalize_transcript(
    text: str,
    rules: Iterable[ReplacementRule | ReplacementCandidate],
    *,
    protected_terms: Iterable[str] = (),
) -> str:
    return apply_replacement_rules(
        text,
        rules,
        protected_terms=protected_terms,
    ).text


def _dictionary_token_spans(text: str, terms: Iterable[str]) -> list[ProtectedToken]:
    view = _normalized_view(text)
    found_tokens: list[ProtectedToken] = []
    unique_terms = sorted(
        {term for term in terms if isinstance(term, str) and normalize_key(term)},
        key=lambda term: (-len(normalize_key(term)), normalize_key(term)),
    )
    for term in unique_terms:
        needle = normalize_key(term)
        search_at = 0
        while search_at <= len(view.text) - len(needle):
            found = view.text.find(needle, search_at)
            if found < 0:
                break
            end_index = found + len(needle)
            start = view.starts[found]
            end = view.ends[end_index - 1]
            if _whole_token_span(text, start, end):
                found_tokens.append(ProtectedToken("dictionary", text[start:end], start, end))
            search_at = found + 1
    return found_tokens


def extract_protected_tokens(
    text: str,
    *,
    protected_terms: Iterable[str] = (),
) -> tuple[ProtectedToken, ...]:
    """Extract non-negotiable values before an optional AI correction step."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    candidates: list[tuple[int, int, int, str]] = []
    patterns = (
        (0, "url", _URL_RE),
        (1, "email", _EMAIL_RE),
        (2, "date", _DATE_RE),
        (3, "amount", _AMOUNT_RE),
        (4, "code", _HYPHEN_CODE_RE),
        (5, "code", _UPPER_CODE_RE),
        (6, "number", _NUMBER_RE),
    )
    for priority, kind, pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            # URLs commonly end a sentence; punctuation is not part of the URL.
            if kind == "url":
                while end > start and text[end - 1] in ".,;:!?)]}":
                    end -= 1
            if end > start:
                candidates.append((start, end, priority, kind))
    for item in _dictionary_token_spans(text, protected_terms):
        candidates.append((item.start, item.end, 7, item.kind))
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))
    accepted: list[ProtectedToken] = []
    for start, end, _priority, kind in candidates:
        if any(start < item.end and end > item.start for item in accepted):
            continue
        accepted.append(ProtectedToken(kind, text[start:end], start, end))
    accepted.sort(key=lambda item: (item.start, item.end, item.kind))
    return tuple(accepted)


def validate_protected_tokens(
    before: str,
    after: str,
    *,
    protected_terms: Iterable[str] = (),
) -> ProtectedTokenValidation:
    """Require an optional corrector to preserve every protected value exactly."""

    before_counter = Counter(
        token.comparison_key
        for token in extract_protected_tokens(before, protected_terms=protected_terms)
    )
    after_counter = Counter(
        token.comparison_key
        for token in extract_protected_tokens(after, protected_terms=protected_terms)
    )
    missing_counter = before_counter - after_counter
    added_counter = after_counter - before_counter
    missing = tuple(sorted(item for item, count in missing_counter.items() for _ in range(count)))
    added = tuple(sorted(item for item, count in added_counter.items() for _ in range(count)))
    return ProtectedTokenValidation(not missing and not added, missing, added)


def accept_corrected_text_or_fallback(
    local_text: str,
    corrected_text: str,
    *,
    protected_terms: Iterable[str] = (),
) -> str:
    """Return the cloud/local-AI result only if protected values survived."""

    validation = validate_protected_tokens(
        local_text,
        corrected_text,
        protected_terms=protected_terms,
    )
    return corrected_text if validation.valid else local_text
