from __future__ import annotations

from typing import Any

from voicetype_local.correction import (
    CorrectionContext,
    CorrectionPipeline,
    LocalBasicCorrector,
    OllamaCorrector,
    OpenAIResponsesCorrector,
    protected_tokens,
    validate_conservative_change,
)


class _Transport:
    def __init__(self, corrected: str) -> None:
        self.corrected = corrected
        self.payload: dict[str, Any] | None = None
        self.headers: dict[str, str] | None = None

    def post_json(self, url, payload, headers, timeout):
        assert url == "https://api.openai.com/v1/responses"
        assert timeout == 4.0
        self.payload = dict(payload)
        self.headers = dict(headers)
        body = {
            "corrected_text": self.corrected,
            "changed": True,
            "status": "ok",
        }
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": __import__("json").dumps(body)}
                    ],
                }
            ]
        }


def test_protected_tokens_cover_numbers_urls_emails_codes_and_terms() -> None:
    text = "Пиши WordPress, 1 250 €, https://example.com и a@b.es, код AB-123."
    tokens = protected_tokens(text, ("WordPress",))
    assert tokens["WordPress"] == 1
    assert tokens["1 250 €"] == 1
    assert tokens["https://example.com"] == 1
    assert tokens["a@b.es"] == 1
    assert tokens["AB-123"] == 1


def test_conservative_validation_rejects_changed_number() -> None:
    assert not validate_conservative_change("Цена 15 евро", "Цена 50 евро")


def test_conservative_validation_rejects_semantic_or_negation_changes() -> None:
    assert not validate_conservative_change("Я согласен", "Я не согласен")
    assert not validate_conservative_change("Не отправляй письмо", "Отправляй письмо")
    assert not validate_conservative_change("Оплатить завтра", "Отменить завтра")


def test_conservative_validation_allows_punctuation_case_and_repeat_cleanup() -> None:
    assert validate_conservative_change("привет мир", "Привет, мир!")
    assert validate_conservative_change("да да, готово", "Да, готово.")


def test_local_pipeline_falls_back_when_rule_damages_a_code() -> None:
    pipeline = CorrectionPipeline(
        LocalBasicCorrector(lambda text: text.replace("AB-123", "AB-124"))
    )
    result = pipeline.correct("Код AB-123", CorrectionContext(language="ru"))
    assert result.text == "Код AB-123"
    assert result.fallback_reason is not None


def test_openai_provider_sends_text_only_minimal_memory_and_store_false() -> None:
    transport = _Transport("Привет, мир!")
    provider = OpenAIResponsesCorrector(
        api_key="test-key",
        model="configured-model",
        timeout=4,
        transport=transport,
    )
    result = provider.correct(
        "привет мир",
        CorrectionContext(language="ru", terms=("VoiceType Local",)),
    )

    assert result.text == "Привет, мир!"
    assert transport.payload is not None
    assert transport.payload["store"] is False
    assert "audio" not in str(transport.payload).casefold()
    assert "VoiceType Local" in str(transport.payload)
    assert transport.headers is not None
    assert transport.headers["Authorization"] == "Bearer test-key"


def test_cloud_pipeline_falls_back_on_protected_token_change() -> None:
    transport = _Transport("Цена 50 евро")
    provider = OpenAIResponsesCorrector(
        api_key="test-key",
        model="configured-model",
        transport=transport,
    )
    result = CorrectionPipeline(provider).correct(
        "Цена 15 евро", CorrectionContext(language="ru")
    )
    assert result.text == "Цена 15 евро"
    assert result.fallback_reason is not None


def test_openai_correction_rejects_non_official_endpoint() -> None:
    try:
        OpenAIResponsesCorrector(
            api_key="test-key",
            model="configured-model",
            endpoint="http://example.test/v1/responses",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-official endpoint must be rejected")


class _OllamaTransport:
    def __init__(self) -> None:
        self.payload = None

    def post_json(self, url, payload, headers, timeout):
        assert url == "http://127.0.0.1:11434/api/generate"
        assert headers == {"Content-Type": "application/json"}
        self.payload = dict(payload)
        return {
            "response": __import__("json").dumps(
                {
                    "corrected_text": "Привет, мир!",
                    "changed": True,
                    "status": "ok",
                }
            )
        }


def test_ollama_adapter_is_local_structured_and_never_installs_models() -> None:
    transport = _OllamaTransport()
    provider = OllamaCorrector(
        model="user-selected-model", transport=transport, mode="local_compact"
    )
    result = provider.correct("привет мир", CorrectionContext(language="ru"))
    assert result.text == "Привет, мир!"
    assert transport.payload is not None
    assert transport.payload["stream"] is False
    assert transport.payload["format"]["additionalProperties"] is False
    assert transport.payload["model"] == "user-selected-model"
