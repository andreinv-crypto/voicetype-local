from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from voicetype_local.config import Settings
from voicetype_local.settings_ui import (
    ACTIVATION_KEY_OPTIONS,
    CLOUD_TEXT_WARNING,
    CLOUD_TRANSCRIPTION_WARNING,
    SettingsFormModel,
    SettingsWindow,
    SettingsValidationError,
    open_settings_window,
)


def test_form_loads_non_secret_settings_without_tk() -> None:
    settings = SimpleNamespace(
        language="es",
        activation_key="f9",
        sounds=False,
        correction_mode="local_full",
        transcription_mode="local",
        cloud_provider="ollama",
        cloud_model="qwen",
        cloud_transcription_model="whisper-1",
        local_model="qwen2.5:3b",
    )

    values = SettingsFormModel(settings).values

    assert values.language == "es"
    assert values.activation_key == "f9"
    assert values.sounds is False
    assert values.correction_mode == "local_full"
    assert values.cloud_provider == "ollama"
    assert values.cloud_model == "qwen"
    assert values.cloud_transcription_model == "whisper-1"
    assert values.local_model == "qwen2.5:3b"
    assert not hasattr(values, "api_key")


def test_form_malformed_consents_fail_closed_without_store_wrapper() -> None:
    values = SettingsFormModel(
        {
            "cloud_text_consent": "false",
            "cloud_transcription_consent": 1,
        }
    ).values

    assert values.cloud_text_consent is False
    assert values.cloud_transcription_consent is False


def test_activation_key_is_exactly_one_supported_choice() -> None:
    assert tuple(value for value, _ in ACTIVATION_KEY_OPTIONS) == (
        "right_ctrl",
        "f8",
        "f9",
        "f10",
        "pause",
    )


def test_invalid_initial_choices_fall_back_safely() -> None:
    model = SettingsFormModel(
        {
            "language": "invalid",
            "activation_key": "escape",
            "correction_mode": "unsafe",
            "transcription_mode": "remote_unknown",
            "cloud_provider": "unknown",
        }
    )

    assert model.values.language == "auto"
    assert model.values.activation_key == "right_ctrl"
    assert model.values.correction_mode == "local_basic"
    assert model.values.transcription_mode == "local"
    assert model.values.cloud_provider == "openai"


def test_text_and_audio_cloud_consents_are_independent() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        correction_mode="cloud_text",
        transcription_mode="cloud_transcription",
        cloud_text_consent=True,
        cloud_transcription_consent=False,
        cloud_model="configured-model",
        cloud_transcription_model="audio-model",
    )

    errors = model.validation_errors()

    assert "cloud_text_consent" not in errors
    assert "cloud_transcription_consent" in errors

    model.update(
        cloud_text_consent=False,
        cloud_transcription_consent=True,
    )
    errors = model.validation_errors()
    assert "cloud_text_consent" in errors
    assert "cloud_transcription_consent" not in errors


def test_cloud_mode_requires_model_and_matching_consent() -> None:
    model = SettingsFormModel(Settings())
    model.update(correction_mode="cloud_text", cloud_model="")

    with pytest.raises(SettingsValidationError) as captured:
        model.changes()

    assert set(captured.value.errors) == {"cloud_text_consent", "cloud_model"}


def test_cloud_transcription_requires_its_own_model() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        transcription_mode="cloud_transcription",
        cloud_transcription_consent=True,
        cloud_model="text-only-model",
        cloud_transcription_model="",
    )

    with pytest.raises(SettingsValidationError) as captured:
        model.changes()

    assert set(captured.value.errors) == {"cloud_transcription_model"}


@pytest.mark.parametrize("mode", ["local_compact", "local_full"])
def test_local_ai_modes_require_ollama_model(mode: str) -> None:
    model = SettingsFormModel(Settings())
    model.update(correction_mode=mode, local_model="")

    with pytest.raises(SettingsValidationError) as captured:
        model.changes()

    assert set(captured.value.errors) == {"local_model"}

    model.update(local_model="qwen2.5:3b")
    assert model.changes()["local_model"] == "qwen2.5:3b"


def test_local_modes_allow_no_cloud_consent_or_model() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        correction_mode="local_basic",
        transcription_mode="local",
        cloud_text_consent=False,
        cloud_transcription_consent=False,
        cloud_model="",
        cloud_transcription_model="",
        local_model="",
    )

    changes = model.changes()

    assert changes["cloud_text_consent"] is False
    assert changes["cloud_transcription_consent"] is False
    assert changes["cloud_model"] == ""
    assert changes["cloud_transcription_model"] == ""
    assert changes["local_model"] == ""


def test_cloud_modes_do_not_mislabel_local_ollama_as_cloud_provider() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        correction_mode="cloud_text",
        cloud_text_consent=True,
        cloud_provider="ollama",
        cloud_model="configured",
    )

    assert "cloud_provider" in model.validation_errors()


def test_submit_passes_new_secret_only_to_secret_callback() -> None:
    model = SettingsFormModel(Settings(language="ru", activation_key="f8"))
    saved: list[dict[str, object]] = []
    secrets: list[str] = []

    result = model.submit(
        api_key="  sk-new-secret  ",
        on_save=saved.append,
        save_secret=secrets.append,
    )

    assert secrets == ["sk-new-secret"]
    assert saved == [result]
    assert result["language"] == "ru"
    assert result["activation_key"] == "f8"
    assert "api_key" not in result
    assert "secret" not in result
    assert "sk-new-secret" not in repr(model.values)


def test_empty_secret_never_calls_secret_callback() -> None:
    model = SettingsFormModel(Settings())
    secrets: list[str] = []

    model.submit(api_key="   ", on_save=lambda _changes: None, save_secret=secrets.append)

    assert secrets == []


def test_nonempty_secret_requires_safe_callback() -> None:
    model = SettingsFormModel(Settings())

    with pytest.raises(SettingsValidationError) as captured:
        model.submit(api_key="secret", on_save=lambda _changes: None)

    assert "api_key" in captured.value.errors


def test_cloud_warnings_clearly_distinguish_text_and_audio() -> None:
    assert "текст" in CLOUD_TEXT_WARNING.casefold()
    assert "аудио" in CLOUD_TEXT_WARNING.casefold()
    assert "не отправляется" in CLOUD_TEXT_WARNING.casefold()
    assert "запись голоса" in CLOUD_TRANSCRIPTION_WARNING.casefold()
    assert "отдельное разрешение" in CLOUD_TRANSCRIPTION_WARNING.casefold()


def test_public_open_api_cannot_receive_existing_secret() -> None:
    parameters = inspect.signature(open_settings_window).parameters

    assert "api_key" not in parameters
    assert "existing_secret" not in parameters
    assert "save_secret" in parameters
    assert "delete_secret" in parameters


def test_saved_key_deletion_requires_two_explicit_actions() -> None:
    class _Var:
        def __init__(self, value="") -> None:
            self.value = value

        def set(self, value) -> None:
            self.value = value

    calls: list[bool] = []
    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._delete_secret_pending = False
    window._delete_secret_callback = lambda: calls.append(True)
    window._status_var = _Var()
    window._api_key_var = _Var("new-secret")

    window.delete_saved_secret()
    assert calls == []
    assert "ещё раз" in window._status_var.value

    window.delete_saved_secret()
    assert calls == [True]
    assert window._api_key_var.value == ""
