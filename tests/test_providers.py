from __future__ import annotations

import pytest

from voicetype_local.config import Settings
from voicetype_local.correction import CorrectionContext, LocalContextualCorrector
from voicetype_local.providers import build_cloud_transcriber, build_correction_pipeline


class _Secrets:
    def get(self, _name: str) -> str | None:
        return None


@pytest.mark.parametrize(
    ("source", "language", "expected"),
    (
        ("Открой гит хаб репозиторий.", "ru", "Открой GitHub репозиторий."),
        ("Open the git hub repository.", "en", "Open the GitHub repository."),
        ("Abre el repositorio git hub.", "es", "Abre el repositorio GitHub."),
    ),
)
def test_local_basic_applies_only_exact_context_gated_spellings(
    source: str,
    language: str,
    expected: str,
) -> None:
    pipeline = build_correction_pipeline(Settings(correction_mode="local_basic"))

    result = pipeline.correct(source, CorrectionContext(language=language))

    assert result.text == expected
    assert isinstance(pipeline.provider, LocalContextualCorrector)
    assert pipeline.provider.memory is None


def test_local_basic_does_not_replace_without_the_required_context() -> None:
    pipeline = build_correction_pipeline(Settings(correction_mode="local_basic"))

    result = pipeline.correct(
        "Мне нравится гит хаб.",
        CorrectionContext(language="ru"),
    )

    assert result.text == "Мне нравится гит хаб."


def test_cloud_text_without_consent_or_key_falls_back_locally() -> None:
    settings = Settings(correction_mode="cloud_text", cloud_text_consent=False)
    result = build_correction_pipeline(
        settings, secret_store=_Secrets()  # type: ignore[arg-type]
    ).correct("Исходный текст", CorrectionContext())
    assert result.text == "Исходный текст"
    assert result.fallback_reason is not None


def test_cloud_transcription_needs_its_own_consent() -> None:
    settings = Settings(
        transcription_mode="cloud_transcription",
        cloud_text_consent=True,
        cloud_transcription_consent=False,
    )
    assert build_cloud_transcriber(settings, secret_store=_Secrets()) is None  # type: ignore[arg-type]
