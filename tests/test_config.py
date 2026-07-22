from __future__ import annotations

import pytest

from voicetype_local.config import SUPPORTED_HOTKEYS, Settings, SettingsStore


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


def test_dictation_feature_booleans_fail_closed_to_their_defaults() -> None:
    settings = Settings(
        dictation_commands_enabled="false",  # type: ignore[arg-type]
        remove_fillers=1,  # type: ignore[arg-type]
        automatic_spacing=None,  # type: ignore[arg-type]
    )

    settings.validate()

    assert settings.dictation_commands_enabled is True
    assert settings.remove_fillers is False
    assert settings.automatic_spacing is True


def test_existing_settings_file_migrates_dictation_defaults(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"language":"ru","sounds":false}\n', encoding="utf-8")

    settings = SettingsStore(path).get()

    assert settings.language == "ru"
    assert settings.dictation_commands_enabled is True
    assert settings.remove_fillers is False
    assert settings.automatic_spacing is True


def test_explicit_automatic_spacing_opt_out_is_preserved(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"automatic_spacing":false}\n', encoding="utf-8")

    settings = SettingsStore(path).get()

    assert settings.automatic_spacing is False


@pytest.mark.parametrize("document", ("[]", '"text"', "42", "null"))
def test_non_object_settings_json_fails_closed_to_defaults(
    tmp_path, document: str
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(document, encoding="utf-8")

    settings = SettingsStore(path).get()

    assert settings == Settings()


def test_oversized_settings_file_fails_closed_without_parsing(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")

    settings = SettingsStore(path).get()

    assert settings == Settings()


def test_interaction_mode_defaults_to_safe_dictation() -> None:
    settings = Settings()
    settings.validate()

    assert settings.interaction_mode == "dictation"


def test_unknown_interaction_mode_fails_closed_to_dictation() -> None:
    settings = Settings(interaction_mode="autonomous_agent")
    settings.validate()

    assert settings.interaction_mode == "dictation"


def test_supported_interaction_modes_are_preserved() -> None:
    for mode in ("dictation", "commands", "mixed"):
        settings = Settings(interaction_mode=mode)
        settings.validate()
        assert settings.interaction_mode == mode


def test_activation_defaults_and_closed_allowlist() -> None:
    settings = Settings()
    settings.validate()

    assert settings.activation_key == "right_ctrl"
    assert settings.activation_mode == "toggle"
    assert {"right_ctrl", "f8", "pause", "ctrl+f8", "alt+f9", "shift+f10"} <= set(
        SUPPORTED_HOTKEYS
    )
    assert "win+f8" not in SUPPORTED_HOTKEYS
    assert "ctrl+r" not in SUPPORTED_HOTKEYS


def test_supported_activation_modes_and_keys_are_preserved() -> None:
    for key in SUPPORTED_HOTKEYS:
        for mode in ("toggle", "hold"):
            settings = Settings(activation_key=key, activation_mode=mode)
            settings.validate()
            expected = (
                ("right_ctrl", "toggle")
                if (key, mode) == ("pause", "hold")
                else (key, mode)
            )
            assert (settings.activation_key, settings.activation_mode) == expected


def test_pause_hold_fails_closed_because_key_up_is_not_reliable() -> None:
    settings = Settings(activation_key="pause", activation_mode="hold")

    settings.validate()

    assert settings.activation_key == "right_ctrl"
    assert settings.activation_mode == "toggle"


def test_invalid_or_legacy_activation_pair_fails_closed() -> None:
    for key, mode in (
        ("ctrl+r", "toggle"),
        ("win+f8", "hold"),
        ("ctrl+f8", "push_to_talk"),
    ):
        settings = Settings(activation_key=key, activation_mode=mode)
        settings.validate()
        assert settings.activation_key == "right_ctrl"
        assert settings.activation_mode == "toggle"


def test_overlay_preferences_validate_independently() -> None:
    settings = Settings(
        overlay_size="extra_large",
        overlay_contrast="standard",
        overlay_position="bottom",
    )
    settings.validate()
    assert (
        settings.overlay_size,
        settings.overlay_contrast,
        settings.overlay_position,
    ) == ("extra_large", "standard", "bottom")

    invalid = Settings(
        overlay_size="giant",
        overlay_contrast="low",
        overlay_position="left",
    )
    invalid.validate()
    assert (
        invalid.overlay_size,
        invalid.overlay_contrast,
        invalid.overlay_position,
    ) == ("large", "high", "top")


def test_failed_settings_write_does_not_mutate_runtime_state(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    original = store.get()

    def fail_write(_candidate: Settings) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "_save_candidate_unlocked", fail_write)

    with pytest.raises(OSError, match="simulated disk failure"):
        store.update(language="es", interaction_mode="commands")

    assert store.get() == original
    assert not path.exists()
