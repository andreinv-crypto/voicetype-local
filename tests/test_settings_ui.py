from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from voicetype_local.config import Settings
from voicetype_local.settings_ui import (
    ACTIVATION_MODE_OPTIONS,
    ACTIVATION_KEY_OPTIONS,
    CLOUD_TEXT_WARNING,
    CLOUD_TRANSCRIPTION_WARNING,
    INTERACTION_MODE_OPTIONS,
    OVERLAY_CONTRAST_OPTIONS,
    OVERLAY_POSITION_OPTIONS,
    OVERLAY_SIZE_OPTIONS,
    SettingsFormModel,
    SettingsWindow,
    SettingsValidationError,
    _microphone_options,
    _visible_toplevel_owner,
    open_settings_window,
)


def test_form_loads_non_secret_settings_without_tk() -> None:
    settings = SimpleNamespace(
        language="es",
        microphone="USB Speech Microphone",
        activation_key="f9",
        dictation_commands_enabled=False,
        remove_fillers=True,
        automatic_spacing=True,
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
    assert values.microphone == "USB Speech Microphone"
    assert values.activation_key == "f9"
    assert values.dictation_commands_enabled is False
    assert values.remove_fillers is True
    assert values.automatic_spacing is True
    assert values.sounds is False
    assert values.correction_mode == "local_full"
    assert values.cloud_provider == "ollama"
    assert values.cloud_model == "qwen"
    assert values.cloud_transcription_model == "whisper-1"
    assert values.local_model == "qwen2.5:3b"
    assert not hasattr(values, "api_key")


def test_microphone_round_trips_and_is_bounded_without_tk() -> None:
    model = SettingsFormModel(Settings(microphone="USB Speech Microphone"))

    assert model.values.microphone == "USB Speech Microphone"
    assert model.changes()["microphone"] == "USB Speech Microphone"

    model.update(microphone="x" * 257)
    assert "microphone" in model.validation_errors()


def test_microphone_choices_include_default_current_and_deduplicate() -> None:
    choices = _microphone_options(
        "Current microphone",
        ("Desk microphone", "Current microphone", "Desk microphone", ""),
    )

    assert choices == (
        ("default", "По умолчанию"),
        ("Current microphone", "Current microphone"),
        ("Desk microphone", "Desk microphone"),
    )


def test_hidden_tray_root_is_not_used_as_settings_owner() -> None:
    hidden_owner = SimpleNamespace(winfo_viewable=lambda: 0)
    visible_owner = SimpleNamespace(winfo_viewable=lambda: 1)

    assert (
        _visible_toplevel_owner(
            SimpleNamespace(winfo_toplevel=lambda: hidden_owner)
        )
        is None
    )
    assert (
        _visible_toplevel_owner(
            SimpleNamespace(winfo_toplevel=lambda: visible_owner)
        )
        is visible_owner
    )


def test_form_malformed_consents_fail_closed_without_store_wrapper() -> None:
    values = SettingsFormModel(
        {
            "cloud_text_consent": "false",
            "cloud_transcription_consent": 1,
        }
    ).values

    assert values.cloud_text_consent is False
    assert values.cloud_transcription_consent is False


def test_form_dictation_booleans_use_strict_defaults_and_round_trip() -> None:
    malformed = SettingsFormModel(
        {
            "dictation_commands_enabled": "false",
            "remove_fillers": 1,
            "automatic_spacing": None,
        }
    ).values
    assert malformed.dictation_commands_enabled is True
    assert malformed.remove_fillers is False
    assert malformed.automatic_spacing is True

    model = SettingsFormModel(Settings())
    model.update(
        dictation_commands_enabled=False,
        remove_fillers=True,
        automatic_spacing=True,
    )
    changes = model.changes()
    assert changes["dictation_commands_enabled"] is False
    assert changes["remove_fillers"] is True
    assert changes["automatic_spacing"] is True

    opt_out = SettingsFormModel({"automatic_spacing": False}).values
    assert opt_out.automatic_spacing is False


def test_activation_keys_are_an_explicit_safe_allowlist() -> None:
    assert tuple(value for value, _ in ACTIVATION_KEY_OPTIONS) == (
        "right_ctrl",
        "f8",
        "f9",
        "f10",
        "pause",
        "ctrl+f8",
        "ctrl+f9",
        "ctrl+f10",
        "alt+f8",
        "alt+f9",
        "alt+f10",
        "shift+f8",
        "shift+f9",
        "shift+f10",
    )


def test_accessibility_options_have_safe_explicit_values() -> None:
    assert tuple(value for value, _ in ACTIVATION_MODE_OPTIONS) == ("toggle", "hold")
    assert tuple(value for value, _ in OVERLAY_SIZE_OPTIONS) == (
        "compact",
        "large",
        "extra_large",
    )
    assert tuple(value for value, _ in OVERLAY_CONTRAST_OPTIONS) == (
        "standard",
        "high",
    )
    assert tuple(value for value, _ in OVERLAY_POSITION_OPTIONS) == (
        "top",
        "center",
        "bottom",
    )


def test_accessibility_defaults_and_changes_match_accessible_profile() -> None:
    model = SettingsFormModel({})
    assert model.values.activation_key == "right_ctrl"
    assert model.values.activation_mode == "toggle"
    assert model.values.overlay_size == "large"
    assert model.values.overlay_contrast == "high"
    assert model.values.overlay_position == "top"

    model.update(
        activation_key="ctrl+f9",
        activation_mode="hold",
        overlay_size="extra_large",
        overlay_contrast="standard",
        overlay_position="bottom",
    )
    changes = model.changes()
    assert changes["activation_key"] == "ctrl+f9"
    assert changes["activation_mode"] == "hold"
    assert changes["overlay_size"] == "extra_large"
    assert changes["overlay_contrast"] == "standard"
    assert changes["overlay_position"] == "bottom"


def test_invalid_accessibility_values_fall_back_to_safe_defaults() -> None:
    values = SettingsFormModel(
        {
            "activation_mode": "voice_only",
            "overlay_size": "tiny",
            "overlay_contrast": "invisible",
            "overlay_position": "outside",
        }
    ).values
    assert values.activation_mode == "toggle"
    assert values.overlay_size == "large"
    assert values.overlay_contrast == "high"
    assert values.overlay_position == "top"


def test_invalid_updated_accessibility_values_are_reported_by_validation() -> None:
    model = SettingsFormModel({})
    model.update(
        activation_key="ctrl+escape",
        activation_mode="voice_only",
        overlay_size="tiny",
        overlay_contrast="invisible",
        overlay_position="outside",
    )
    assert {
        "activation_key",
        "activation_mode",
        "overlay_size",
        "overlay_contrast",
        "overlay_position",
    } <= set(model.validation_errors())


def test_pause_hold_is_rejected_with_an_accessible_explanation() -> None:
    model = SettingsFormModel({})
    model.update(activation_key="pause", activation_mode="hold")

    errors = model.validation_errors()

    assert "activation_mode" in errors
    assert "Pause" in errors["activation_mode"]
    assert "отпускания" in errors["activation_mode"]


def test_reset_accessibility_defaults_updates_form_only() -> None:
    class _Var:
        def __init__(self, value) -> None:
            self.value = value

        def set(self, value) -> None:
            self.value = value

    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._activation_var = _Var("Alt + F9")
    window._activation_mode_var = _Var("Запись идёт, пока удерживаю клавишу")
    window._overlay_size_var = _Var("Компактный")
    window._overlay_contrast_var = _Var("Стандартный")
    window._overlay_position_var = _Var("Снизу")
    window._sounds_var = _Var(False)
    window._status_var = _Var("")

    window._reset_accessibility_defaults()

    assert window._activation_var.value == "Правый Ctrl"
    assert window._activation_mode_var.value.startswith("Нажал")
    assert window._overlay_size_var.value == "Крупный"
    assert window._overlay_contrast_var.value == "Высокий контраст"
    assert window._overlay_position_var.value == "Сверху"
    assert window._sounds_var.value is True
    assert "Сохранить" in window._status_var.value


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


def test_interaction_mode_is_explicit_and_defaults_to_dictation() -> None:
    model = SettingsFormModel(Settings())
    assert model.values.interaction_mode == "dictation"

    model.update(interaction_mode="mixed")
    assert model.changes()["interaction_mode"] == "mixed"


def test_unknown_interaction_mode_fails_closed_in_form() -> None:
    model = SettingsFormModel({"interaction_mode": "free_agent"})
    assert model.values.interaction_mode == "dictation"
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


def test_cloud_submit_requires_existing_or_new_secret() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        correction_mode="cloud_text",
        cloud_text_consent=True,
        cloud_model="configured-model",
    )

    with pytest.raises(SettingsValidationError) as captured:
        model.submit(api_key="", on_save=lambda _changes: None)

    assert "api_key" in captured.value.errors

    model.submit(
        api_key="",
        on_save=lambda _changes: None,
        has_saved_secret=True,
    )


def test_new_secret_satisfies_cloud_submit_requirement() -> None:
    model = SettingsFormModel(Settings())
    model.update(
        correction_mode="cloud_text",
        cloud_text_consent=True,
        cloud_model="configured-model",
    )
    saved: list[str] = []

    model.submit(
        api_key="new-secret",
        on_save=lambda _changes: None,
        save_secret=saved.append,
    )

    assert saved == ["new-secret"]


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


def test_submit_settings_failure_calls_secret_rollback_once() -> None:
    model = SettingsFormModel(Settings())
    events: list[str] = []

    def save_secret(_value: str):
        events.append("secret_saved")
        return lambda: events.append("secret_rolled_back")

    def fail_settings(_changes: dict[str, object]) -> None:
        events.append("settings_failed")
        raise OSError("simulated settings failure")

    with pytest.raises(OSError, match="simulated settings failure"):
        model.submit(
            api_key="temporary-secret",
            on_save=fail_settings,
            save_secret=save_secret,
        )

    assert events == ["secret_saved", "settings_failed", "secret_rolled_back"]


def test_submit_success_never_calls_secret_rollback() -> None:
    model = SettingsFormModel(Settings())
    events: list[str] = []

    def save_secret(_value: str):
        events.append("secret_saved")
        return lambda: events.append("secret_rolled_back")

    model.submit(
        api_key="temporary-secret",
        on_save=lambda _changes: events.append("settings_saved"),
        save_secret=save_secret,
    )

    assert events == ["secret_saved", "settings_saved"]


def test_submit_rollback_failure_preserves_original_safe_error() -> None:
    model = SettingsFormModel(Settings())

    def save_secret(_value: str):
        def fail_rollback() -> None:
            raise RuntimeError("rollback internals")

        return fail_rollback

    with pytest.raises(OSError, match="safe settings error") as captured:
        model.submit(
            api_key="temporary-secret",
            on_save=lambda _changes: (_ for _ in ()).throw(
                OSError("safe settings error")
            ),
            save_secret=save_secret,
        )

    assert "temporary-secret" not in str(captured.value)
    assert "rollback internals" not in str(captured.value)


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


def test_public_factory_and_window_constructor_keep_callback_contract_in_sync() -> None:
    factory = inspect.signature(open_settings_window).parameters
    constructor = inspect.signature(SettingsWindow.__init__).parameters
    optional_callbacks = {
        "on_repeat_last",
        "on_copy_last",
        "on_copy_raw",
        "on_clear_session",
        "on_memory",
        "on_help",
        "on_quick_test",
        "has_last_text",
        "has_raw_text",
        "status_text",
        "has_saved_secret",
        "microphone_options",
        "last_text",
        "last_raw_text",
        "runtime_feedback",
    }
    assert optional_callbacks <= set(factory)
    assert optional_callbacks <= set(constructor)
    assert set(factory) == set(constructor) - {"self"}


def test_constructor_uses_dark_side_navigation_build_not_legacy_builder() -> None:
    source = inspect.getsource(SettingsWindow.__init__)
    assert "self._build()" in source
    assert "self._build_legacy()" not in source
    build_source = inspect.getsource(SettingsWindow._build)
    for title in (
        "Главная",
        "Речь",
        "Текст",
        "Приватность",
        "Доступность",
        "О программе",
    ):
        assert f'text="{title}"' in build_source
    assert "self._nav_buttons" in build_source
    assert "self._select_page" in build_source
    assert 'style="VoiceType.Dark.TNotebook"' in build_source
    assert "theme_use" not in inspect.getsource(SettingsWindow._configure_styles)
    for variable in (
        "self._dictation_commands_var",
        "self._remove_fillers_var",
        "self._automatic_spacing_var",
    ):
        assert variable in build_source
    assert "Понимать «точка»" in build_source
    assert "Убирать безопасные слова-паразиты" in build_source
    assert "Добавлять пробел между диктовками" in build_source
    assert "Быстрая проверка" in build_source
    assert "3 голосовых шага подряд" in build_source


def test_quick_test_button_leaves_settings_before_opening_native_field() -> None:
    source = inspect.getsource(SettingsWindow._build)

    quick_test_call = source[source.index('"Быстрая проверка"') :]
    assert "self._on_quick_test" in quick_test_call[:180]
    assert "leave_window=True" in quick_test_call[:220]


def test_home_mode_cards_map_to_existing_interaction_values() -> None:
    source = inspect.getsource(SettingsWindow._build)

    assert '("mixed", "Обычный\\nТекст + точные команды")' in source
    assert '("dictation", "Диктовка\\nТолько текст")' in source
    assert '("commands", "Управление\\nТолько команды")' in source
    assert {value for value, _label in INTERACTION_MODE_OPTIONS} == {
        "dictation",
        "commands",
        "mixed",
    }


def test_home_language_hint_explains_auto_tradeoff_without_changing_default() -> None:
    source = inspect.getsource(SettingsWindow._build)

    assert "Auto — для смешанной речи. RU / EN / ES — обычно быстрее." in source
    assert Settings().language == "auto"


def test_mode_card_selection_uses_form_value_without_saving_immediately() -> None:
    class _Var:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    class _Button:
        def configure(self, **_values: object) -> None:
            return

    window = SettingsWindow.__new__(SettingsWindow)
    window._interaction_mode_var = _Var("Диктовка — печатать текст")
    window._mode_card_buttons = {
        "mixed": _Button(),
        "dictation": _Button(),
        "commands": _Button(),
    }

    window._choose_interaction_mode("mixed")

    assert window._interaction_mode_var.value == (
        "Смешанный — команды со словом «команда»"
    )


def test_runtime_feedback_poll_updates_only_ram_view_values() -> None:
    class _Var:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def set(self, value: str) -> None:
            self.value = value

    class _Label:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def configure(self, **values: object) -> None:
            self.values.update(values)

    class _Window:
        def after(self, milliseconds: int, _callback: object) -> str:
            assert milliseconds == 300
            return "feedback-job"

    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._runtime_feedback = lambda: {
        "heard": "напиши другу",
        "text": "Напиши другу",
        "command": "Нет команды",
        "outcome": "Текст готов",
        "tone": "success",
    }
    window._feedback_vars = {
        key: _Var()
        for key in ("heard", "text", "command", "outcome")
    }
    window._last_result_var = _Var()
    window._feedback_value_labels = {"outcome": _Label()}
    window.window = _Window()
    window._feedback_after_id = None

    window._poll_runtime_feedback()

    assert window._feedback_vars["heard"].value == "напиши другу"
    assert window._feedback_vars["text"].value == "Напиши другу"
    assert window._last_result_var.value == "Напиши другу"
    assert window._feedback_value_labels["outcome"].values["fg"] == "#38C75B"
    assert window._feedback_after_id == "feedback-job"


def test_runtime_feedback_poll_clears_stale_last_result_for_empty_text() -> None:
    class _Var:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def set(self, value: str) -> None:
            self.value = value

    class _Label:
        def configure(self, **_values: object) -> None:
            return

    class _Window:
        def after(self, _milliseconds: int, _callback: object) -> str:
            return "feedback-job"

    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._runtime_feedback = lambda: {"text": ""}
    window._feedback_vars = {
        key: _Var()
        for key in ("heard", "text", "command", "outcome")
    }
    window._last_result_var = _Var("Старый результат")
    window._feedback_value_labels = {"outcome": _Label()}
    window.window = _Window()
    window._feedback_after_id = None

    window._poll_runtime_feedback()

    assert window._feedback_vars["text"].value == "Пока нет данных"
    assert window._last_result_var.value == "Пока ничего не продиктовано"


def test_clear_session_text_updates_open_window_and_disables_actions() -> None:
    class _Var:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def set(self, value: str) -> None:
            self.value = value

    class _Button:
        def __init__(self) -> None:
            self.values: dict[str, object] = {}

        def configure(self, **values: object) -> None:
            self.values.update(values)

    calls: list[str] = []
    final_button = _Button()
    raw_button = _Button()
    clear_button = _Button()
    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._on_clear_session = lambda: calls.append("clear")
    window._status_var = _Var()
    window._last_text = "Исправленный текст"
    window._last_raw_text = "исходный текст"
    window._has_last_text = True
    window._has_raw_text = True
    window._last_result_var = _Var("Исправленный текст")
    window._last_text_action_buttons = [final_button]
    window._raw_text_action_buttons = [raw_button]
    window._clear_session_button = clear_button

    window._clear_session_text()

    assert calls == ["clear"]
    assert window._last_text == ""
    assert window._last_raw_text == ""
    assert window._has_last_text is False
    assert window._has_raw_text is False
    assert window._last_result_var.value == "Пока ничего не продиктовано"
    for action_button in (final_button, raw_button, clear_button):
        assert action_button.values == {"state": "disabled", "cursor": "arrow"}


def test_runtime_feedback_preview_is_bounded_without_losing_word_spacing() -> None:
    preview = SettingsWindow._feedback_value("слово  " * 80, "пусто")

    assert 150 <= len(preview) <= 160
    assert preview.endswith("…")
    assert "  " not in preview
    assert SettingsWindow._feedback_value("   ", "пусто") == "пусто"
    assert len(SettingsWindow._feedback_value("длинный текст" * 10, "пусто", limit=32)) <= 32


def test_optional_actions_and_leave_actions_have_safe_lifecycle() -> None:
    class _Var:
        def __init__(self, value="") -> None:
            self.value = value

        def set(self, value) -> None:
            self.value = value

    calls: list[str] = []
    window = SettingsWindow.__new__(SettingsWindow)
    window._closed = False
    window._status_var = _Var()
    window._api_key_var = _Var("temporary-secret")
    window._on_cancel_callback = lambda: calls.append("cancel")
    window.close = lambda: calls.append("close")  # type: ignore[method-assign]

    window._call_optional(lambda: calls.append("inline"))
    window._leave_for_action(lambda: calls.append("action"))

    assert calls == ["inline", "cancel", "close", "action"]
    assert window._api_key_var.value == ""


def test_validation_selects_the_tab_containing_the_invalid_field() -> None:
    class _Var:
        def __init__(self) -> None:
            self.value = ""

        def set(self, value) -> None:
            self.value = value

    class _Notebook:
        def __init__(self) -> None:
            self.selected = None

        def select(self, tab) -> None:
            self.selected = tab

    class _Widget:
        def __init__(self) -> None:
            self.focused = False

        def focus_set(self) -> None:
            self.focused = True

    window = SettingsWindow.__new__(SettingsWindow)
    window._status_var = _Var()
    window._notebook = _Notebook()
    widget = _Widget()
    privacy_tab = object()
    window._field_widgets = {"cloud_model": widget}
    window._field_tabs = {"cloud_model": privacy_tab}

    window._show_validation_error(
        SettingsValidationError({"cloud_model": "Укажите модель."})
    )

    assert window._notebook.selected is privacy_tab
    assert widget.focused
    assert window._status_var.value == "Укажите модель."


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
