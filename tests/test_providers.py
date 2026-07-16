from __future__ import annotations

from voicetype_local.config import Settings
from voicetype_local.correction import CorrectionContext
from voicetype_local.providers import build_cloud_transcriber, build_correction_pipeline


class _Secrets:
    def get(self, _name: str) -> str | None:
        return None


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
