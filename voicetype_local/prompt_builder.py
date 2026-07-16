from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .memory import MemoryContext, MemoryStore, StylePreference, TermRecord, normalize_key


PromptStage = Literal["pre_asr", "post_asr"]


@dataclass(frozen=True, slots=True)
class PromptSlice:
    stage: PromptStage
    text: str
    terms: tuple[TermRecord, ...]
    styles: tuple[StylePreference, ...]
    token_count: int
    token_budget: int
    truncated: bool

    @property
    def term_ids(self) -> tuple[int, ...]:
        return tuple(term.id for term in self.terms)

    def as_untrusted_data(self) -> dict[str, object]:
        """Small structured payload for an optional corrector, never instructions."""

        return {
            "language_terms": [term.canonical_text for term in self.terms],
            "style_preferences": [
                {"key": style.key, "value": style.value} for style in self.styles
            ],
        }

    def as_untrusted_json(self) -> str:
        return json.dumps(
            self.as_untrusted_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def approximate_token_count(text: str) -> int:
    """Conservative dependency-free fallback; production may inject a tokenizer."""

    if not text:
        return 0
    # Words, punctuation, and CJK characters are each charged separately.  The
    # extra UTF-8 allowance intentionally overestimates rather than overruns.
    pieces = re.findall(r"[\w]+|[^\w\s]", text, flags=re.UNICODE)
    utf8_allowance = sum(max(0, len(piece.encode("utf-8")) - 4) // 4 for piece in pieces)
    return len(pieces) + utf8_allowance


def whisper_token_counter(tokenizer_path: Path) -> Callable[[str], int]:
    """Load the bundled Whisper tokenizer, with a safe dependency-free fallback."""

    try:
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    except Exception:
        return approximate_token_count

    def count(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=False).ids)

    return count


class PromptBuilder:
    """Build deterministic compact memory slices within a measured token cap."""

    def __init__(
        self,
        token_counter: Callable[[str], int] = approximate_token_count,
        *,
        max_tokens: int = 224,
        reserve_tokens: int = 32,
        max_terms: int = 64,
        max_candidates: int = 128,
        max_styles: int = 16,
        global_min_priority: int = 50,
        separator: str = "; ",
    ) -> None:
        if not callable(token_counter):
            raise TypeError("token_counter must be callable")
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if type(reserve_tokens) is not int or not 0 <= reserve_tokens < max_tokens:
            raise ValueError("reserve_tokens must be smaller than max_tokens")
        if type(max_terms) is not int or not 1 <= max_terms <= 256:
            raise ValueError("max_terms must be from 1 to 256")
        if type(max_candidates) is not int or not max_terms <= max_candidates <= 1000:
            raise ValueError("max_candidates must be between max_terms and 1000")
        if type(max_styles) is not int or not 0 <= max_styles <= 64:
            raise ValueError("max_styles must be from 0 to 64")
        if not isinstance(separator, str) or not separator:
            raise ValueError("separator cannot be empty")
        self._token_counter = token_counter
        self.max_tokens = max_tokens
        self.reserve_tokens = reserve_tokens
        self.max_terms = max_terms
        self.max_candidates = max_candidates
        self.max_styles = max_styles
        self.global_min_priority = global_min_priority
        self.separator = separator

    @property
    def token_budget(self) -> int:
        return self.max_tokens - self.reserve_tokens

    def count_tokens(self, text: str) -> int:
        count = self._token_counter(text)
        if type(count) is not int or count < 0:
            raise ValueError("token_counter must return a non-negative integer")
        return count

    def build_pre_asr(
        self,
        store: MemoryStore,
        context: MemoryContext | None = None,
    ) -> PromptSlice:
        """Use scoped terms plus only explicitly high-priority global terms."""

        context = context or MemoryContext()
        candidates = store.select_terms(
            context,
            limit=self.max_candidates,
            global_min_priority=self.global_min_priority,
        )
        styles = tuple(store.select_styles(context)[: self.max_styles])
        return self.build_from_terms(candidates, stage="pre_asr", styles=styles)

    def build_post_asr(
        self,
        store: MemoryStore,
        draft: str,
        context: MemoryContext | None = None,
    ) -> PromptSlice:
        """Use only terms relevant to aliases/tokens present in the first draft."""

        if not isinstance(draft, str):
            raise TypeError("draft must be a string")
        context = context or MemoryContext()
        candidates = store.search_relevant(
            draft,
            context,
            limit=self.max_candidates,
        )
        styles = tuple(store.select_styles(context)[: self.max_styles])
        return self.build_from_terms(candidates, stage="post_asr", styles=styles)

    def build_initial_prompt(
        self,
        store: MemoryStore,
        context: MemoryContext | None = None,
    ) -> str:
        return self.build_pre_asr(store, context).text

    def build_from_terms(
        self,
        terms: Iterable[TermRecord],
        *,
        stage: PromptStage,
        styles: Sequence[StylePreference] = (),
    ) -> PromptSlice:
        if stage not in {"pre_asr", "post_asr"}:
            raise ValueError("invalid prompt stage")
        all_candidates = list(terms)
        candidates = all_candidates[: self.max_candidates]
        selected: list[TermRecord] = []
        selected_keys: set[str] = set()
        prompt = ""
        omitted = len(all_candidates) > self.max_candidates
        for term in candidates:
            # Defensive filtering makes PromptBuilder safe even if a caller
            # constructs TermRecord values rather than using MemoryStore.
            if not term.confirmed or not term.enabled:
                omitted = True
                continue
            key = normalize_key(term.canonical_text)
            if not key or key in selected_keys:
                omitted = True
                continue
            safe_term = self._serialize_term(term.canonical_text)
            if not safe_term:
                omitted = True
                continue
            candidate_prompt = safe_term if not prompt else f"{prompt}{self.separator}{safe_term}"
            candidate_count = self.count_tokens(candidate_prompt)
            if candidate_count > self.token_budget:
                omitted = True
                continue
            prompt = candidate_prompt
            selected.append(term)
            selected_keys.add(key)
            if len(selected) >= self.max_terms:
                omitted = omitted or len(selected) < len(candidates)
                break
        measured = self.count_tokens(prompt)
        if measured > self.token_budget:
            # A stateful or non-deterministic counter is unsafe; fail closed.
            raise ValueError("token counter produced an inconsistent result")
        return PromptSlice(
            stage=stage,
            text=prompt,
            terms=tuple(selected),
            styles=tuple(styles[: self.max_styles]),
            token_count=measured,
            token_budget=self.token_budget,
            truncated=omitted,
        )

    @staticmethod
    def _serialize_term(value: str) -> str:
        # Terms are data, not instructions.  Strip line/control separators and
        # neutralize the compact vocabulary delimiter without rewriting spelling.
        cleaned = " ".join(value.split())
        cleaned = "".join(character for character in cleaned if character.isprintable())
        return cleaned.replace(";", "；").strip()
