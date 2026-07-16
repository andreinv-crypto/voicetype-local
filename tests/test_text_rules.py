from __future__ import annotations

from voicetype_local.text_rules import (
    ReplacementRule,
    SafeNormalizer,
    accept_corrected_text_or_fallback,
    apply_replacement_rules,
    extract_protected_tokens,
    normalize_transcript,
    validate_protected_tokens,
)


def test_whole_token_replacement_does_not_touch_substrings() -> None:
    rule = ReplacementRule("flow", "Wispr Flow")
    result = normalize_transcript("flow workflow flowing", [rule])
    assert result == "Wispr Flow workflow flowing"


def test_safe_normalizer_facade_returns_text_and_audit_metadata() -> None:
    result = SafeNormalizer().normalize(
        "chat g p t",
        [ReplacementRule("chat g p t", "ChatGPT", term_id=7)],
    )
    assert result.text == "ChatGPT"
    assert result.applied[0].term_id == 7


def test_nfkc_casefold_and_decomposed_diacritic_match_without_rewriting_other_text() -> None:
    rules = [
        ReplacementRule("WordPress", "WordPress"),
        ReplacementRule("sí", "SÍ"),
    ]
    result = apply_replacement_rules("ｗＯＲＤｐｒｅｓｓ and si\u0301 — untouched", rules)
    assert result.text == "WordPress and SÍ — untouched"
    assert [item.original for item in result.applied] == ["ｗＯＲＤｐｒｅｓｓ", "si\u0301"]


def test_longest_non_overlapping_alias_wins() -> None:
    rules = [
        ReplacementRule("chat", "CHAT"),
        ReplacementRule("chat g p t", "ChatGPT"),
    ]
    assert normalize_transcript("chat g p t", rules) == "ChatGPT"


def test_equal_precedence_conflict_is_not_applied() -> None:
    rules = [
        ReplacementRule("flow", "Flow A", precedence=(4, 1, 10, "same")),
        ReplacementRule("flow", "Flow B", precedence=(4, 1, 10, "same")),
    ]
    assert normalize_transcript("flow", rules) == "flow"


def test_replacements_skip_urls_email_dates_amounts_numbers_and_codes() -> None:
    rules = [
        ReplacementRule("openai", "BROKEN"),
        ReplacementRule("2026", "BROKEN"),
        ReplacementRule("123", "BROKEN"),
    ]
    text = (
        "openai https://openai.com a@openai.com 16/07/2026 "
        "€123 X1234567L plain 123"
    )
    result = normalize_transcript(text, rules)
    assert result.startswith("BROKEN https://openai.com a@openai.com")
    assert "16/07/2026" in result
    assert "€123" in result
    assert "X1234567L" in result
    assert result.endswith("plain 123")


def test_protected_token_extraction_and_validation() -> None:
    before = (
        "ChatGPT sent €1,250.50 on 16/07/2026 to me@example.com; "
        "see https://example.com/a and code X1234567L."
    )
    tokens = extract_protected_tokens(before, protected_terms=["ChatGPT"])
    kinds = {token.kind for token in tokens}
    assert {"dictionary", "amount", "date", "email", "url", "code"} <= kinds

    unchanged = validate_protected_tokens(before, before, protected_terms=["ChatGPT"])
    assert unchanged.valid
    changed = before.replace("€1,250.50", "€1,500.50")
    validation = validate_protected_tokens(before, changed, protected_terms=["ChatGPT"])
    assert not validation.valid
    assert accept_corrected_text_or_fallback(
        before,
        changed,
        protected_terms=["ChatGPT"],
    ) == before


def test_duplicate_protected_values_are_counted() -> None:
    before = "IDs 42 and 42"
    after = "IDs 42"
    result = validate_protected_tokens(before, after)
    assert not result.valid
    assert result.missing == (("number", "42"),)
