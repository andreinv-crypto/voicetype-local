from __future__ import annotations

from voicetype_local.config import Settings


def test_malformed_cloud_consents_fail_closed() -> None:
    settings = Settings(
        cloud_text_consent="false",  # type: ignore[arg-type]
        cloud_transcription_consent=1,  # type: ignore[arg-type]
    )

    settings.validate()

    assert settings.cloud_text_consent is False
    assert settings.cloud_transcription_consent is False


def test_real_json_booleans_are_preserved() -> None:
    settings = Settings(
        cloud_text_consent=True,
        cloud_transcription_consent=True,
        app_scoped_memory=False,
    )

    settings.validate()

    assert settings.cloud_text_consent is True
    assert settings.cloud_transcription_consent is True
    assert settings.app_scoped_memory is False
