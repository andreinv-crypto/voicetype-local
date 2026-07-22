from __future__ import annotations

"""Explicit, keyboard-accessible settings window for VoiceType Local.

Importing this module never creates a Tk root or opens a window.  The pure
``SettingsFormModel`` contains validation and submission logic so it can be
tested without a display.  API secrets are accepted only at submit time and
are passed directly to ``save_secret``; they are never part of form settings.
"""

import tkinter as tk
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

from .config import is_supported_activation_pair


LANGUAGE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("auto", "Автоматически"),
    ("ru", "Русский"),
    ("es", "Español"),
    ("en", "English"),
)

MICROPHONE_DEFAULT_LABEL = "По умолчанию"


def _microphone_options(
    current: object,
    available: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Return a bounded, stable value/label list for the microphone picker."""

    current_value = str(current).strip()[:256] or "default"
    values: list[tuple[str, str]] = [("default", MICROPHONE_DEFAULT_LABEL)]
    seen = {"default"}
    candidates = (current_value, *available)
    for candidate in candidates:
        value = str(candidate).strip()[:256]
        if not value or value == "default" or value in seen:
            continue
        seen.add(value)
        values.append((value, value))
    return tuple(values)


def _visible_toplevel_owner(parent: tk.Misc) -> tk.Misc | None:
    """Return a visible Tk owner, never a withdrawn tray root.

    On Windows, making Settings transient to VoiceType's permanently withdrawn
    root also hides the Settings window and prevents a taskbar button.  A
    visible preview/test host can still own the window normally.
    """

    try:
        owner = parent.winfo_toplevel()
        return owner if bool(owner.winfo_viewable()) else None
    except (AttributeError, tk.TclError):
        return None

ACTIVATION_KEY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("right_ctrl", "Правый Ctrl"),
    ("f8", "F8"),
    ("f9", "F9"),
    ("f10", "F10"),
    ("pause", "Pause"),
    ("ctrl+f8", "Ctrl + F8"),
    ("ctrl+f9", "Ctrl + F9"),
    ("ctrl+f10", "Ctrl + F10"),
    ("alt+f8", "Alt + F8"),
    ("alt+f9", "Alt + F9"),
    ("alt+f10", "Alt + F10"),
    ("shift+f8", "Shift + F8"),
    ("shift+f9", "Shift + F9"),
    ("shift+f10", "Shift + F10"),
)

ACTIVATION_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("toggle", "Нажал — включил, нажал ещё раз — выключил"),
    ("hold", "Запись идёт, пока удерживаю клавишу"),
)

OVERLAY_SIZE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("compact", "Компактный"),
    ("large", "Крупный"),
    ("extra_large", "Очень крупный"),
)

OVERLAY_CONTRAST_OPTIONS: tuple[tuple[str, str], ...] = (
    ("standard", "Стандартный"),
    ("high", "Высокий контраст"),
)

OVERLAY_POSITION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("top", "Сверху"),
    ("center", "По центру"),
    ("bottom", "Снизу"),
)

INTERACTION_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("dictation", "Диктовка — печатать текст"),
    ("commands", "Команды — управлять Windows"),
    ("mixed", "Смешанный — команды со словом «команда»"),
)

CORRECTION_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("off", "Без коррекции"),
    ("local_basic", "Локальная базовая"),
    ("local_compact", "Локальная компактная"),
    ("local_full", "Локальная полная"),
    ("cloud_text", "Облачная коррекция текста"),
)

TRANSCRIPTION_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("local", "Локальное распознавание"),
    ("cloud_transcription", "Облачное распознавание"),
)

PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("openai", "OpenAI"),
    ("ollama", "Ollama / локальный сервер"),
)

CLOUD_TEXT_WARNING = (
    "Это разрешение позволяет отправлять распознанный текст, подходящие "
    "предпочтительные написания и стили провайдеру для коррекции. Аудио по "
    "этому согласию не отправляется."
)

CLOUD_TRANSCRIPTION_WARNING = (
    "Отдельное разрешение: запись голоса будет отправлена провайдеру для "
    "распознавания. Личный словарь остаётся локальным. Не включайте режим, "
    "если хотите полностью локальную обработку."
)


__all__ = [
    "ACTIVATION_MODE_OPTIONS",
    "ACTIVATION_KEY_OPTIONS",
    "CLOUD_TEXT_WARNING",
    "CLOUD_TRANSCRIPTION_WARNING",
    "CORRECTION_MODE_OPTIONS",
    "INTERACTION_MODE_OPTIONS",
    "LANGUAGE_OPTIONS",
    "OVERLAY_CONTRAST_OPTIONS",
    "OVERLAY_POSITION_OPTIONS",
    "OVERLAY_SIZE_OPTIONS",
    "PROVIDER_OPTIONS",
    "SettingsFormModel",
    "SettingsFormValues",
    "SettingsValidationError",
    "SettingsWindow",
    "TRANSCRIPTION_MODE_OPTIONS",
    "open_settings_window",
]


def _setting_value(settings: Mapping[str, Any] | object, name: str, default: Any) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _setting_bool(
    settings: Mapping[str, Any] | object, name: str, default: bool
) -> bool:
    value = _setting_value(settings, name, default)
    return value if isinstance(value, bool) else default


def _valid_initial_choice(value: Any, options: tuple[tuple[str, str], ...], default: str) -> str:
    normalized = str(value)
    return normalized if normalized in {item[0] for item in options} else default


@dataclass(slots=True)
class SettingsFormValues:
    """Non-secret values edited by the settings form."""

    language: str = "auto"
    microphone: str = "default"
    activation_key: str = "right_ctrl"
    activation_mode: str = "toggle"
    overlay_size: str = "large"
    overlay_contrast: str = "high"
    overlay_position: str = "top"
    interaction_mode: str = "dictation"
    dictation_commands_enabled: bool = True
    remove_fillers: bool = False
    automatic_spacing: bool = True
    sounds: bool = True
    correction_mode: str = "local_basic"
    transcription_mode: str = "local"
    cloud_text_consent: bool = False
    cloud_transcription_consent: bool = False
    cloud_provider: str = "openai"
    cloud_model: str = ""
    cloud_transcription_model: str = ""
    local_model: str = ""

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, Any] | object
    ) -> SettingsFormValues:
        return cls(
            language=_valid_initial_choice(
                _setting_value(settings, "language", "auto"),
                LANGUAGE_OPTIONS,
                "auto",
            ),
            microphone=(
                str(_setting_value(settings, "microphone", "default")).strip()[:256]
                or "default"
            ),
            activation_key=_valid_initial_choice(
                _setting_value(settings, "activation_key", "right_ctrl"),
                ACTIVATION_KEY_OPTIONS,
                "right_ctrl",
            ),
            activation_mode=_valid_initial_choice(
                _setting_value(settings, "activation_mode", "toggle"),
                ACTIVATION_MODE_OPTIONS,
                "toggle",
            ),
            overlay_size=_valid_initial_choice(
                _setting_value(settings, "overlay_size", "large"),
                OVERLAY_SIZE_OPTIONS,
                "large",
            ),
            overlay_contrast=_valid_initial_choice(
                _setting_value(settings, "overlay_contrast", "high"),
                OVERLAY_CONTRAST_OPTIONS,
                "high",
            ),
            overlay_position=_valid_initial_choice(
                _setting_value(settings, "overlay_position", "top"),
                OVERLAY_POSITION_OPTIONS,
                "top",
            ),
            interaction_mode=_valid_initial_choice(
                _setting_value(settings, "interaction_mode", "dictation"),
                INTERACTION_MODE_OPTIONS,
                "dictation",
            ),
            dictation_commands_enabled=_setting_bool(
                settings, "dictation_commands_enabled", True
            ),
            remove_fillers=_setting_bool(settings, "remove_fillers", False),
            automatic_spacing=_setting_bool(settings, "automatic_spacing", True),
            sounds=_setting_bool(settings, "sounds", True),
            correction_mode=_valid_initial_choice(
                _setting_value(settings, "correction_mode", "local_basic"),
                CORRECTION_MODE_OPTIONS,
                "local_basic",
            ),
            transcription_mode=_valid_initial_choice(
                _setting_value(settings, "transcription_mode", "local"),
                TRANSCRIPTION_MODE_OPTIONS,
                "local",
            ),
            cloud_text_consent=_setting_bool(
                settings, "cloud_text_consent", False
            ),
            cloud_transcription_consent=_setting_bool(
                settings, "cloud_transcription_consent", False
            ),
            cloud_provider=_valid_initial_choice(
                _setting_value(settings, "cloud_provider", "openai"),
                PROVIDER_OPTIONS,
                "openai",
            ),
            cloud_model=str(_setting_value(settings, "cloud_model", "")).strip()[:128],
            cloud_transcription_model=str(
                _setting_value(settings, "cloud_transcription_model", "")
            ).strip()[:128],
            local_model=str(_setting_value(settings, "local_model", "")).strip()[:128],
        )


class SettingsValidationError(ValueError):
    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(next(iter(self.errors.values()), "Некорректные настройки"))


class SettingsFormModel:
    """Display-independent settings validation and callback orchestration."""

    _FIELDS = frozenset(SettingsFormValues.__dataclass_fields__)

    def __init__(self, settings: Mapping[str, Any] | object) -> None:
        self.values = SettingsFormValues.from_settings(settings)

    def update(self, **changes: Any) -> None:
        unknown = set(changes) - self._FIELDS
        if unknown:
            raise KeyError(f"Unknown settings fields: {', '.join(sorted(unknown))}")
        for name, value in changes.items():
            setattr(self.values, name, value)

    def validation_errors(
        self,
        *,
        api_key_supplied: bool = False,
        can_save_secret: bool = True,
        has_saved_secret: bool = False,
        require_cloud_secret: bool = False,
    ) -> dict[str, str]:
        values = self.values
        errors: dict[str, str] = {}
        if not str(values.microphone).strip():
            errors["microphone"] = "Выберите микрофон или системное устройство по умолчанию."
        elif len(str(values.microphone)) > 256:
            errors["microphone"] = "Название микрофона слишком длинное."
        option_sets = (
            ("language", LANGUAGE_OPTIONS, "Выберите язык из списка."),
            (
                "activation_key",
                ACTIVATION_KEY_OPTIONS,
                "Выберите клавишу или безопасную комбинацию из списка.",
            ),
            (
                "activation_mode",
                ACTIVATION_MODE_OPTIONS,
                "Выберите переключение или удержание.",
            ),
            (
                "overlay_size",
                OVERLAY_SIZE_OPTIONS,
                "Выберите размер индикатора.",
            ),
            (
                "overlay_contrast",
                OVERLAY_CONTRAST_OPTIONS,
                "Выберите контраст индикатора.",
            ),
            (
                "overlay_position",
                OVERLAY_POSITION_OPTIONS,
                "Выберите положение индикатора.",
            ),
            (
                "interaction_mode",
                INTERACTION_MODE_OPTIONS,
                "Выберите: диктовка, команды или смешанный режим.",
            ),
            (
                "correction_mode",
                CORRECTION_MODE_OPTIONS,
                "Выберите режим коррекции из списка.",
            ),
            (
                "transcription_mode",
                TRANSCRIPTION_MODE_OPTIONS,
                "Выберите режим распознавания из списка.",
            ),
            ("cloud_provider", PROVIDER_OPTIONS, "Выберите провайдера из списка."),
        )
        for field, options, message in option_sets:
            if str(getattr(values, field)) not in {item[0] for item in options}:
                errors[field] = message

        if not is_supported_activation_pair(
            values.activation_key, values.activation_mode
        ) and values.activation_key == "pause" and values.activation_mode == "hold":
            errors["activation_mode"] = (
                "Pause работает только как переключатель: Windows не гарантирует "
                "событие отпускания этой клавиши. Выберите «нажал — включил» или "
                "другую клавишу для удержания."
            )

        if values.correction_mode == "cloud_text" and not values.cloud_text_consent:
            errors["cloud_text_consent"] = (
                "Для облачной коррекции нужно отдельно разрешить отправку текста."
            )
        if (
            values.correction_mode == "cloud_text"
            or values.transcription_mode == "cloud_transcription"
        ) and values.cloud_provider != "openai":
            errors["cloud_provider"] = (
                "Облачные режимы сейчас поддерживают OpenAI; Ollama выбирается локальным режимом."
            )
        if (
            values.transcription_mode == "cloud_transcription"
            and not values.cloud_transcription_consent
        ):
            errors["cloud_transcription_consent"] = (
                "Для облачного распознавания нужно отдельно разрешить отправку аудио."
            )

        if values.correction_mode == "cloud_text" and not str(
            values.cloud_model
        ).strip():
            errors["cloud_model"] = (
                "Для облачной коррекции текста укажите отдельную модель."
            )
        if values.transcription_mode == "cloud_transcription" and not str(
            values.cloud_transcription_model
        ).strip():
            errors["cloud_transcription_model"] = (
                "Для облачного распознавания аудио укажите отдельную модель."
            )
        if values.correction_mode in {"local_compact", "local_full"} and not str(
            values.local_model
        ).strip():
            errors["local_model"] = (
                "Для локальной AI-коррекции укажите модель Ollama."
            )
        if api_key_supplied and not can_save_secret:
            errors["api_key"] = (
                "Хранилище секретов не подключено: ключ не может быть сохранён безопасно."
            )
        cloud_selected = (
            values.correction_mode == "cloud_text"
            or values.transcription_mode == "cloud_transcription"
        )
        if (
            require_cloud_secret
            and cloud_selected
            and not api_key_supplied
            and not has_saved_secret
        ):
            errors["api_key"] = (
                "Для облачного режима сначала введите API-ключ. "
                "Он сохранится только в защищённом хранилище Windows."
            )
        return errors

    def changes(self) -> dict[str, object]:
        errors = self.validation_errors()
        if errors:
            raise SettingsValidationError(errors)
        values = self.values
        return {
            "language": str(values.language),
            "microphone": str(values.microphone).strip()[:256] or "default",
            "activation_key": str(values.activation_key),
            "activation_mode": str(values.activation_mode),
            "overlay_size": str(values.overlay_size),
            "overlay_contrast": str(values.overlay_contrast),
            "overlay_position": str(values.overlay_position),
            "interaction_mode": str(values.interaction_mode),
            "dictation_commands_enabled": bool(
                values.dictation_commands_enabled
            ),
            "remove_fillers": bool(values.remove_fillers),
            "automatic_spacing": bool(values.automatic_spacing),
            "sounds": bool(values.sounds),
            "correction_mode": str(values.correction_mode),
            "transcription_mode": str(values.transcription_mode),
            "cloud_text_consent": bool(values.cloud_text_consent),
            "cloud_transcription_consent": bool(
                values.cloud_transcription_consent
            ),
            "cloud_provider": str(values.cloud_provider),
            "cloud_model": str(values.cloud_model).strip()[:128],
            "cloud_transcription_model": str(
                values.cloud_transcription_model
            ).strip()[:128],
            "local_model": str(values.local_model).strip()[:128],
        }

    def submit(
        self,
        *,
        api_key: str,
        on_save: Callable[[dict[str, object]], object],
        save_secret: Callable[[str], object] | None = None,
        has_saved_secret: bool = False,
    ) -> dict[str, object]:
        """Validate and save without ever returning or retaining the secret."""

        secret = str(api_key).strip()
        errors = self.validation_errors(
            api_key_supplied=bool(secret),
            can_save_secret=save_secret is not None,
            has_saved_secret=has_saved_secret,
            require_cloud_secret=True,
        )
        if errors:
            raise SettingsValidationError(errors)
        changes = self.changes()
        rollback_secret: Callable[[], object] | None = None
        if secret and save_secret is not None:
            rollback = save_secret(secret)
            if callable(rollback):
                rollback_secret = rollback
        try:
            on_save(dict(changes))
        except Exception:
            if rollback_secret is not None:
                try:
                    rollback_secret()
                except Exception:
                    # Preserve the original settings error and never surface a
                    # secret, encrypted bytes, or rollback path in the UI.
                    pass
            raise
        return dict(changes)


def _labels(options: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(label for _, label in options)


def _label_for(value: str, options: tuple[tuple[str, str], ...]) -> str:
    return next((label for item, label in options if item == value), options[0][1])


def _value_for(label: str, options: tuple[tuple[str, str], ...]) -> str:
    return next((item for item, text in options if text == label), options[0][0])


class SettingsWindow:
    """Large, keyboard-first settings ``Toplevel`` created only explicitly."""

    def __init__(
        self,
        parent: tk.Misc,
        settings: Mapping[str, Any] | object,
        *,
        on_save: Callable[[dict[str, object]], object],
        save_secret: Callable[[str], object] | None = None,
        has_saved_secret: bool = False,
        delete_secret: Callable[[], object] | None = None,
        on_cancel: Callable[[], object] | None = None,
        on_repeat_last: Callable[[], object] | None = None,
        on_copy_last: Callable[[], object] | None = None,
        on_copy_raw: Callable[[], object] | None = None,
        on_clear_session: Callable[[], object] | None = None,
        on_memory: Callable[[], object] | None = None,
        on_help: Callable[[], object] | None = None,
        on_quick_test: Callable[[], object] | None = None,
        has_last_text: bool = False,
        has_raw_text: bool = False,
        status_text: str = "Готово к работе",
        microphone_options: tuple[str, ...] = (),
        last_text: str = "",
        last_raw_text: str = "",
        runtime_feedback: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self.model = SettingsFormModel(settings)
        self._on_save_callback = on_save
        self._save_secret_callback = save_secret
        self._has_saved_secret = bool(has_saved_secret)
        self._delete_secret_callback = delete_secret
        self._on_cancel_callback = on_cancel
        self._on_repeat_last = on_repeat_last
        self._on_copy_last = on_copy_last
        self._on_copy_raw = on_copy_raw
        self._on_clear_session = on_clear_session
        self._on_memory = on_memory
        self._on_help = on_help
        self._on_quick_test = on_quick_test
        self._has_last_text = bool(has_last_text)
        self._has_raw_text = bool(has_raw_text)
        self._main_status_text = str(status_text)[:120]
        self._last_text = str(last_text)[:4000]
        self._last_raw_text = str(last_raw_text)[:4000]
        self._runtime_feedback = runtime_feedback
        self._microphone_options = _microphone_options(
            self.model.values.microphone,
            tuple(microphone_options),
        )
        self._closed = False
        self._delete_secret_pending = False
        self._feedback_after_id: str | None = None
        self._dirty = False
        self._suppress_dirty = True
        self._last_text_action_buttons: list[tk.Button] = []
        self._raw_text_action_buttons: list[tk.Button] = []
        self._clear_session_button: tk.Button | None = None

        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("VoiceType Local — Настройки")
        visible_owner = _visible_toplevel_owner(parent)
        if visible_owner is not None:
            self.window.transient(visible_owner)
        self.window.configure(background="#071827")
        self.window.minsize(1040, 680)
        screen_width = max(1040, self.window.winfo_screenwidth())
        screen_height = max(680, self.window.winfo_screenheight())
        window_width = max(1040, min(1280, screen_width - 60))
        window_height = max(680, min(820, screen_height - 80))
        left = max(0, (screen_width - window_width) // 2)
        top = max(0, (screen_height - window_height) // 2)
        self.window.geometry(f"{window_width}x{window_height}+{left}+{top}")
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", self._cancel_event)
        self.window.bind("<Return>", self._save_event)

        self._configure_styles()
        self._create_variables()
        self._field_widgets: dict[str, tk.Misc] = {}
        self._field_tabs: dict[str, tk.Misc] = {}
        self._build()
        self._bind_dirty_tracking()
        self._suppress_dirty = False
        self._status_var.set("Все изменения сохранены")
        self._poll_runtime_feedback()

        self.window.update_idletasks()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.window.after_idle(self._language_widget.focus_set)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        # These styles are deliberately scoped. Changing the active ttk theme
        # here would also restyle the memory and command-help windows.
        style.configure("VoiceType.Settings.TFrame", background="#071827")
        style.configure("VoiceType.Settings.Panel.TFrame", background="#0B2032")
        style.configure(
            "VoiceType.Settings.Title.TLabel",
            font=("Segoe UI", 20, "bold"),
            background="#071827",
            foreground="#F8FAFC",
        )
        style.configure(
            "VoiceType.Settings.TLabel",
            font=("Segoe UI", 13),
            background="#071827",
            foreground="#E5EDF5",
        )
        style.configure(
            "VoiceType.Settings.Panel.TLabel",
            font=("Segoe UI", 12),
            background="#0B2032",
            foreground="#E5EDF5",
        )
        style.configure(
            "VoiceType.Settings.Warning.TLabel",
            font=("Segoe UI", 11),
            background="#0B2032",
            foreground="#F59E0B",
        )
        style.configure(
            "VoiceType.Settings.Status.TLabel",
            font=("Segoe UI", 12, "bold"),
            background="#0A1D2D",
            foreground="#A9BAC9",
        )
        style.configure(
            "VoiceType.Settings.TLabelframe",
            background="#0B2032",
            foreground="#F8FAFC",
            bordercolor="#294356",
            relief="solid",
        )
        style.configure(
            "VoiceType.Settings.TLabelframe.Label",
            font=("Segoe UI", 15, "bold"),
            background="#0B2032",
            foreground="#F8FAFC",
        )
        style.configure(
            "VoiceType.Settings.TCheckbutton",
            font=("Segoe UI", 12),
            background="#0B2032",
            foreground="#E5EDF5",
        )
        style.map(
            "VoiceType.Settings.TCheckbutton",
            background=[("active", "#0B2032")],
            foreground=[("disabled", "#708396")],
        )
        style.configure(
            "VoiceType.Settings.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(14, 9),
            background="#123149",
            foreground="#F8FAFC",
        )
        style.map(
            "VoiceType.Settings.TButton",
            background=[("active", "#17405F"), ("pressed", "#0B69D1")],
            foreground=[("disabled", "#718398")],
        )
        style.configure(
            "VoiceType.Settings.Primary.TButton",
            font=("Segoe UI", 12, "bold"),
            padding=(18, 10),
            background="#0969DA",
            foreground="#FFFFFF",
        )
        style.map(
            "VoiceType.Settings.Primary.TButton",
            background=[("active", "#0B7CE5"), ("pressed", "#0759B8")],
        )
        style.configure(
            "VoiceType.Settings.TCombobox",
            padding=(8, 7),
            fieldbackground="#132A3D",
            background="#132A3D",
            foreground="#F8FAFC",
            arrowcolor="#D9E4EE",
        )
        style.map(
            "VoiceType.Settings.TCombobox",
            fieldbackground=[("readonly", "#132A3D")],
            foreground=[("readonly", "#F8FAFC")],
            selectbackground=[("readonly", "#132A3D")],
            selectforeground=[("readonly", "#F8FAFC")],
        )
        style.configure(
            "VoiceType.Settings.TEntry",
            padding=(8, 7),
            fieldbackground="#132A3D",
            foreground="#F8FAFC",
            insertcolor="#FFFFFF",
        )
        style.configure(
            "VoiceType.Settings.Vertical.TScrollbar",
            background="#123149",
            darkcolor="#123149",
            lightcolor="#123149",
            troughcolor="#071827",
            bordercolor="#294356",
            arrowcolor="#A9BAC9",
            relief="flat",
        )
        style.map(
            "VoiceType.Settings.Vertical.TScrollbar",
            background=[("active", "#17405F"), ("pressed", "#0B69D1")],
        )
        style.configure(
            "VoiceType.Dark.TNotebook",
            background="#071827",
            borderwidth=0,
            tabmargins=0,
        )
        style.layout("VoiceType.Dark.TNotebook.Tab", [])

    def _create_variables(self) -> None:
        values = self.model.values
        self._language_var = tk.StringVar(
            self.window, _label_for(values.language, LANGUAGE_OPTIONS)
        )
        self._microphone_var = tk.StringVar(
            self.window,
            _label_for(values.microphone, self._microphone_options),
        )
        self._activation_var = tk.StringVar(
            self.window,
            _label_for(values.activation_key, ACTIVATION_KEY_OPTIONS),
        )
        self._activation_mode_var = tk.StringVar(
            self.window,
            _label_for(values.activation_mode, ACTIVATION_MODE_OPTIONS),
        )
        self._overlay_size_var = tk.StringVar(
            self.window,
            _label_for(values.overlay_size, OVERLAY_SIZE_OPTIONS),
        )
        self._overlay_contrast_var = tk.StringVar(
            self.window,
            _label_for(values.overlay_contrast, OVERLAY_CONTRAST_OPTIONS),
        )
        self._overlay_position_var = tk.StringVar(
            self.window,
            _label_for(values.overlay_position, OVERLAY_POSITION_OPTIONS),
        )
        self._interaction_mode_var = tk.StringVar(
            self.window,
            _label_for(values.interaction_mode, INTERACTION_MODE_OPTIONS),
        )
        self._dictation_commands_var = tk.BooleanVar(
            self.window, values.dictation_commands_enabled
        )
        self._remove_fillers_var = tk.BooleanVar(
            self.window, values.remove_fillers
        )
        self._automatic_spacing_var = tk.BooleanVar(
            self.window, values.automatic_spacing
        )
        self._sounds_var = tk.BooleanVar(self.window, values.sounds)
        self._correction_var = tk.StringVar(
            self.window,
            _label_for(values.correction_mode, CORRECTION_MODE_OPTIONS),
        )
        self._transcription_var = tk.StringVar(
            self.window,
            _label_for(values.transcription_mode, TRANSCRIPTION_MODE_OPTIONS),
        )
        self._cloud_text_var = tk.BooleanVar(
            self.window, values.cloud_text_consent
        )
        self._cloud_audio_var = tk.BooleanVar(
            self.window, values.cloud_transcription_consent
        )
        self._provider_var = tk.StringVar(
            self.window, _label_for(values.cloud_provider, PROVIDER_OPTIONS)
        )
        self._model_var = tk.StringVar(self.window, values.cloud_model)
        self._transcription_model_var = tk.StringVar(
            self.window, values.cloud_transcription_model
        )
        self._local_model_var = tk.StringVar(self.window, values.local_model)
        # There is intentionally no parameter or getter for an existing key.
        self._api_key_var = tk.StringVar(self.window, "")
        self._status_var = tk.StringVar(self.window, "")
        initial_home_status = self._main_status_text
        if "готов" in initial_home_status.casefold():
            key_label = self._activation_var.get()
            key_label = key_label[:1].lower() + key_label[1:]
            initial_home_status = f"Готово — нажмите {key_label}"
        self._home_status_var = tk.StringVar(self.window, initial_home_status)
        self._last_result_var = tk.StringVar(
            self.window,
            self._feedback_value(
                self._last_text,
                "Пока ничего не продиктовано",
                limit=96,
            ),
        )
        self._feedback_vars = {
            "heard": tk.StringVar(
                self.window,
                self._feedback_value(self._last_raw_text, "Пока нет данных"),
            ),
            "text": tk.StringVar(
                self.window,
                self._feedback_value(self._last_text, "Пока нет данных"),
            ),
            "command": tk.StringVar(self.window, "Нет команды"),
            "outcome": tk.StringVar(self.window, "Ожидает первой диктовки"),
        }

    def _dropdown(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        options: tuple[tuple[str, str], ...],
        *,
        font_size: int = 12,
        direction: str = "below",
    ) -> tk.Menubutton:
        """Create a native dark read-only picker without global theme changes."""

        widget = tk.Menubutton(
            parent,
            textvariable=variable,
            width=1,
            takefocus=True,
            anchor="w",
            indicatoron=True,
            direction=direction,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#294356",
            highlightcolor="#2D9CFF",
            bg="#132A3D",
            activebackground="#17405F",
            fg="#F8FAFC",
            activeforeground="#FFFFFF",
            font=("Segoe UI", font_size),
            padx=11,
            pady=8,
            cursor="hand2",
        )
        menu = tk.Menu(
            widget,
            tearoff=False,
            bg="#132A3D",
            fg="#F8FAFC",
            activebackground="#075DBD",
            activeforeground="#FFFFFF",
            selectcolor="#38A7FF",
            font=("Segoe UI", 11),
        )
        for _value, label in options:
            menu.add_radiobutton(label=label, variable=variable, value=label)
        widget.configure(menu=menu)
        widget.bind(
            "<Return>",
            lambda _event, target=widget: self._open_dropdown_from_keyboard(target),
        )
        return widget

    def _combo(
        self,
        parent: tk.Misc,
        row: int,
        title: str,
        variable: tk.StringVar,
        options: tuple[tuple[str, str], ...],
    ) -> tk.Menubutton:
        ttk.Label(parent, text=title, style="VoiceType.Settings.Panel.TLabel").grid(
            row=row, column=0, sticky="w", padx=14, pady=(10, 4)
        )
        widget = self._dropdown(parent, variable, options, font_size=13)
        widget.grid(row=row + 1, column=0, sticky="ew", padx=14, pady=(0, 8))
        return widget

    @staticmethod
    def _replace_dropdown_options(
        widget: tk.Menubutton,
        variable: tk.StringVar,
        options: tuple[tuple[str, str], ...],
    ) -> None:
        menu = widget.nametowidget(str(widget.cget("menu")))
        assert isinstance(menu, tk.Menu)
        menu.delete(0, "end")
        for _value, label in options:
            menu.add_radiobutton(label=label, variable=variable, value=label)

    def set_microphone_options(self, available: tuple[str, ...]) -> None:
        """Refresh both microphone pickers after a background device scan."""

        if self._closed:
            return
        previous = self._microphone_options
        selected_value = _value_for(self._microphone_var.get(), previous)
        options = _microphone_options(selected_value, tuple(available))
        suppress_dirty = self._suppress_dirty
        self._suppress_dirty = True
        try:
            self._microphone_options = options
            self._microphone_var.set(_label_for(selected_value, options))
            for widget in (
                self._microphone_widget,
                self._speech_microphone_widget,
            ):
                self._replace_dropdown_options(
                    widget,
                    self._microphone_var,
                    options,
                )
        except tk.TclError:
            return
        finally:
            self._suppress_dirty = suppress_dirty

    def _build(self) -> None:
        """Build the selected dark, keyboard-first application shell."""

        bg = "#071827"
        sidebar_bg = "#081B2B"
        panel_bg = "#0B2032"
        field_bg = "#132A3D"
        border = "#294356"
        text = "#F8FAFC"
        muted = "#A9BAC9"
        blue = "#0B69D1"
        blue_hover = "#0B7CE5"
        green = "#16813A"

        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        root = tk.Frame(self.window, bg=bg, highlightthickness=0)
        root.grid(row=0, column=0, sticky="nsew")
        root.rowconfigure(1, weight=1)
        root.columnconfigure(0, weight=1)

        header = tk.Frame(root, bg="#082038", height=48)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header,
            text="\ue720",
            font=("Segoe MDL2 Assets", 17),
            bg=blue,
            fg="#FFFFFF",
            width=2,
            pady=5,
        ).pack(side="left", padx=(18, 10), pady=6)
        tk.Label(
            header,
            text="VoiceType Local",
            font=("Segoe UI", 15),
            bg="#082038",
            fg=text,
        ).pack(side="left")

        body = tk.Frame(root, bg=bg)
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        sidebar = tk.Frame(
            body,
            bg=sidebar_bg,
            width=220,
            highlightbackground=border,
            highlightthickness=1,
        )
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)

        notebook = ttk.Notebook(
            body,
            takefocus=False,
            style="VoiceType.Dark.TNotebook",
        )
        notebook.grid(row=0, column=1, sticky="nsew")
        self._notebook = notebook

        home_page = tk.Frame(notebook, bg=bg)
        home_page.rowconfigure(0, weight=1)
        home_page.columnconfigure(0, weight=1)
        home_canvas = tk.Canvas(
            home_page,
            background=bg,
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=24,
        )
        home_scrollbar = tk.Canvas(
            home_page,
            width=12,
            background=bg,
            highlightthickness=0,
            borderwidth=0,
            takefocus=True,
            cursor="hand2",
        )
        home_scroll_thumb = home_scrollbar.create_rectangle(
            2,
            0,
            10,
            48,
            fill="#294356",
            outline="",
        )
        home_canvas.configure(yscrollcommand=self._set_home_scrollbar)
        home_canvas.grid(row=0, column=0, sticky="nsew")
        home_scrollbar.grid(row=0, column=1, sticky="ns")
        home = tk.Frame(home_canvas, bg=bg, padx=18, pady=10)
        home_canvas_window = home_canvas.create_window(
            (0, 0),
            window=home,
            anchor="nw",
        )
        self._home_page = home_page
        self._home_canvas = home_canvas
        self._home_content = home
        self._home_scrollbar = home_scrollbar
        self._home_scroll_thumb = home_scroll_thumb
        self._home_canvas_window = home_canvas_window
        self._home_scroll_drag_offset = 0.0
        home.bind("<Configure>", self._update_home_scroll, add="+")
        home_canvas.bind("<Configure>", self._update_home_scroll, add="+")
        home_scrollbar.bind("<Button-1>", self._press_home_scrollbar)
        home_scrollbar.bind("<B1-Motion>", self._drag_home_scrollbar)
        home_scrollbar.bind(
            "<Up>",
            lambda _event: self._scroll_home_by_units(-3),
        )
        home_scrollbar.bind(
            "<Down>",
            lambda _event: self._scroll_home_by_units(3),
        )
        home_scrollbar.bind(
            "<Prior>",
            lambda _event: self._scroll_home_by_units(-12),
        )
        home_scrollbar.bind(
            "<Next>",
            lambda _event: self._scroll_home_by_units(12),
        )
        self.window.bind("<MouseWheel>", self._scroll_home, add="+")
        speech = tk.Frame(notebook, bg=bg, padx=22, pady=18)
        text_tab = tk.Frame(notebook, bg=bg, padx=22, pady=18)
        privacy = tk.Frame(notebook, bg=bg, padx=22, pady=18)
        accessibility = tk.Frame(notebook, bg=bg, padx=22, pady=18)
        about = tk.Frame(notebook, bg=bg, padx=22, pady=18)
        notebook.add(home_page, text="Главная")
        notebook.add(speech, text="Речь")
        notebook.add(text_tab, text="Текст")
        notebook.add(privacy, text="Приватность")
        notebook.add(accessibility, text="Доступность")
        notebook.add(about, text="О программе")
        self._pages = {
            "Главная": home_page,
            "Речь": speech,
            "Текст": text_tab,
            "Приватность": privacy,
            "Доступность": accessibility,
            "О программе": about,
        }

        nav_icons = {
            "Главная": "\ue80f",
            "Речь": "\ue720",
            "Текст": "\ue8a5",
            "Приватность": "\ue72e",
            "Доступность": "\ue776",
            "О программе": "\ue946",
        }
        self._nav_buttons: dict[str, tuple[tk.Frame, tk.Label, tk.Button]] = {}
        for row, (title, page) in enumerate(self._pages.items()):
            if title == "О программе":
                sidebar.rowconfigure(row, weight=1)
                row += 1
            nav_row = tk.Frame(sidebar, bg=sidebar_bg)
            nav_row.grid(row=row, column=0, sticky="ew", padx=9, pady=3)
            nav_row.columnconfigure(1, weight=1)
            icon = tk.Label(
                nav_row,
                text=nav_icons[title],
                font=("Segoe MDL2 Assets", 17),
                bg=sidebar_bg,
                fg=text,
                width=2,
            )
            icon.grid(row=0, column=0, padx=(10, 4), pady=13)
            nav_button = tk.Button(
                nav_row,
                text=title,
                command=lambda target=page: self._select_page(target),
                takefocus=True,
                anchor="w",
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
                font=("Segoe UI", 13),
                bg=sidebar_bg,
                activebackground="#123B59",
                activeforeground=text,
                fg=text,
                cursor="hand2",
            )
            nav_button.grid(row=0, column=1, sticky="ew", pady=3)
            nav_button.bind(
                "<Return>",
                lambda _event, target=page: self._activate_page_from_keyboard(target),
            )
            for clickable in (nav_row, icon):
                clickable.bind(
                    "<Button-1>",
                    lambda _event, target=page: self._select_page(target),
                )
            self._nav_buttons[title] = (nav_row, icon, nav_button)
        notebook.bind("<<NotebookTabChanged>>", self._refresh_navigation, add="+")

        def heading(parent: tk.Misc, title: str, subtitle: str = "") -> None:
            tk.Label(
                parent,
                text=title,
                font=("Segoe UI", 22, "bold"),
                bg=bg,
                fg=text,
            ).grid(row=0, column=0, sticky="w", columnspan=2)
            if subtitle:
                tk.Label(
                    parent,
                    text=subtitle,
                    font=("Segoe UI", 12),
                    bg=bg,
                    fg=muted,
                ).grid(row=1, column=0, sticky="w", columnspan=2, pady=(2, 10))

        def panel(parent: tk.Misc, title: str) -> tk.Frame:
            frame = tk.Frame(
                parent,
                bg=panel_bg,
                highlightbackground=border,
                highlightcolor="#2D8CFF",
                highlightthickness=1,
                padx=15,
                pady=13,
            )
            frame.columnconfigure(0, weight=1)
            tk.Label(
                frame,
                text=title,
                font=("Segoe UI", 15, "bold"),
                bg=panel_bg,
                fg=text,
            ).grid(row=0, column=0, sticky="w", pady=(0, 8))
            return frame

        def note(
            parent: tk.Misc,
            value: str,
            row: int,
            *,
            wraplength: int = 420,
            color: str = muted,
            padx: tuple[int, int] | int = 0,
        ) -> tk.Label:
            label = tk.Label(
                parent,
                text=value,
                font=("Segoe UI", 10),
                bg=panel_bg,
                fg=color,
                justify="left",
                anchor="w",
                wraplength=wraplength,
            )
            label.grid(row=row, column=0, sticky="ew", padx=padx, pady=(3, 8))
            return label

        def field_label(parent: tk.Misc, title: str, row: int) -> None:
            tk.Label(
                parent,
                text=title,
                font=("Segoe UI", 11),
                bg=panel_bg,
                fg=text,
            ).grid(row=row, column=0, sticky="w", pady=(7, 4))

        def entry(parent: tk.Misc, variable: tk.StringVar, row: int, **kwargs: Any) -> tk.Entry:
            widget = tk.Entry(
                parent,
                textvariable=variable,
                takefocus=True,
                font=("Segoe UI", 12),
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=border,
                highlightcolor="#2D9CFF",
                bg=field_bg,
                fg=text,
                insertbackground="#FFFFFF",
                disabledbackground="#102536",
                disabledforeground="#708396",
                **kwargs,
            )
            widget.grid(row=row, column=0, sticky="ew", ipady=8, pady=(0, 7))
            return widget

        def check(
            parent: tk.Misc,
            title: str,
            variable: tk.BooleanVar,
        ) -> tk.Checkbutton:
            widget = tk.Checkbutton(
                parent,
                text=title,
                variable=variable,
                takefocus=True,
                anchor="w",
                justify="left",
                font=("Segoe UI", 11),
                bg=panel_bg,
                fg=text,
                activebackground=panel_bg,
                activeforeground="#FFFFFF",
                selectcolor=field_bg,
                disabledforeground="#708396",
                highlightthickness=1,
                highlightbackground=panel_bg,
                highlightcolor="#2D9CFF",
                borderwidth=0,
            )
            widget.bind(
                "<Return>",
                lambda _event, target=widget: self._invoke_button_from_keyboard(target),
            )
            return widget

        def button(
            parent: tk.Misc,
            row: int,
            title: str,
            callback: Callable[[], object] | None,
            *,
            enabled: bool = True,
            leave_window: bool = False,
            column: int = 0,
        ) -> tk.Button:
            command = (
                (lambda: self._leave_for_action(callback))
                if leave_window
                else (lambda: self._call_optional(callback))
            )
            widget = tk.Button(
                parent,
                text=title,
                command=command,
                takefocus=True,
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=border,
                highlightcolor="#2D9CFF",
                font=("Segoe UI", 11, "bold"),
                bg="#123149",
                activebackground="#17405F",
                fg=text,
                activeforeground="#FFFFFF",
                disabledforeground="#708396",
                cursor="hand2",
            )
            widget.grid(row=row, column=column, sticky="ew", padx=3, pady=2, ipady=6)
            widget.bind(
                "<Return>",
                lambda _event, target=widget: self._invoke_button_from_keyboard(target),
            )
            if callback is None or not enabled:
                widget.configure(state="disabled", cursor="arrow")
            return widget

        # Главная.
        home.rowconfigure(2, weight=1)
        home.columnconfigure(0, weight=1)
        heading(home, "Главное", "Режим работы и ежедневные настройки")
        home_workspace = tk.Frame(home, bg=bg)
        home_workspace.grid(row=2, column=0, sticky="nsew")
        home_workspace.rowconfigure(0, weight=1)
        home_workspace.columnconfigure(0, weight=1)
        home_workspace.columnconfigure(1, minsize=285)
        center = tk.Frame(home_workspace, bg=bg)
        center.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        for column in range(3):
            center.columnconfigure(column, weight=1, uniform="mode")

        tk.Label(
            center,
            text="Режим работы",
            font=("Segoe UI", 12),
            bg=bg,
            fg=text,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._mode_card_buttons: dict[str, tk.Button] = {}
        mode_specs = (
            ("mixed", "Обычный\nТекст + точные команды"),
            ("dictation", "Диктовка\nТолько текст"),
            ("commands", "Управление\nТолько команды"),
        )
        for column, (value, title) in enumerate(mode_specs):
            widget = tk.Button(
                center,
                text=title,
                command=lambda selected=value: self._choose_interaction_mode(selected),
                takefocus=True,
                font=("Segoe UI", 13, "bold"),
                justify="center",
                wraplength=190,
                height=3,
                relief="flat",
                borderwidth=0,
                highlightthickness=2,
                highlightbackground=border,
                highlightcolor="#2D8CFF",
                bg=panel_bg,
                activebackground=blue_hover,
                activeforeground="#FFFFFF",
                fg=text,
                cursor="hand2",
            )
            widget.grid(row=1, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 2 else 5), pady=(0, 10))
            widget.bind(
                "<Return>",
                lambda _event, selected=value: self._activate_mode_from_keyboard(selected),
            )
            self._mode_card_buttons[value] = widget
        self._interaction_mode_widget = self._mode_card_buttons["mixed"]
        self._refresh_mode_cards()

        status_banner = tk.Frame(
            center,
            bg="#0E542A" if "ошиб" not in self._main_status_text.casefold() else "#6E2020",
            highlightbackground="#1C9A4A",
            highlightthickness=1,
            padx=14,
            pady=11,
        )
        status_banner.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        tk.Label(
            status_banner,
            textvariable=self._home_status_var,
            font=("Segoe UI", 15, "bold"),
            bg=status_banner.cget("bg"),
            fg="#FFFFFF",
        ).pack()

        daily = tk.Frame(center, bg=bg)
        daily.grid(row=3, column=0, columnspan=3, sticky="ew")
        for column in range(2):
            daily.columnconfigure(column, weight=1, uniform="daily")
        language_panel = panel(daily, "Язык")
        language_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        language_segments = tk.Frame(language_panel, bg=panel_bg)
        language_segments.grid(row=1, column=0, sticky="ew")
        for column in range(4):
            language_segments.columnconfigure(column, weight=1, uniform="lang")
        self._language_buttons: dict[str, tk.Button] = {}
        short_labels = {"auto": "Auto", "ru": "RU", "en": "EN", "es": "ES"}
        for column, value in enumerate(("auto", "ru", "en", "es")):
            language_button = tk.Button(
                language_segments,
                text=short_labels[value],
                command=lambda selected=value: self._choose_language(selected),
                takefocus=True,
                font=("Segoe UI", 11),
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=border,
                bg=field_bg,
                activebackground=blue_hover,
                activeforeground="#FFFFFF",
                fg=text,
                cursor="hand2",
            )
            language_button.grid(row=0, column=column, sticky="ew", ipady=8)
            language_button.bind(
                "<Return>",
                lambda _event, selected=value: self._activate_language_from_keyboard(selected),
            )
            self._language_buttons[value] = language_button
        self._language_widget = self._language_buttons["auto"]
        self._refresh_language_buttons()
        note(
            language_panel,
            "Auto — для смешанной речи. RU / EN / ES — обычно быстрее.",
            2,
            wraplength=340,
        )

        activation_panel = panel(daily, "Способ записи")
        activation_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        activation_segments = tk.Frame(activation_panel, bg=panel_bg)
        activation_segments.grid(row=1, column=0, sticky="ew")
        for column in range(2):
            activation_segments.columnconfigure(column, weight=1, uniform="activation")
        self._activation_mode_buttons: dict[str, tk.Button] = {}
        for column, (value, title) in enumerate((("toggle", "Нажать ещё раз"), ("hold", "Удерживать"))):
            activation_button = tk.Button(
                activation_segments,
                text=title,
                command=lambda selected=value: self._choose_activation_mode(selected),
                takefocus=True,
                font=("Segoe UI", 10),
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=border,
                bg=field_bg,
                activebackground=blue_hover,
                activeforeground="#FFFFFF",
                fg=text,
                cursor="hand2",
            )
            activation_button.grid(row=0, column=column, sticky="ew", ipady=8, padx=(0, 3) if column == 0 else (3, 0))
            activation_button.bind(
                "<Return>",
                lambda _event, selected=value: self._activate_activation_from_keyboard(selected),
            )
            self._activation_mode_buttons[value] = activation_button
        self._refresh_activation_mode_buttons()

        key_panel = panel(daily, "Клавиша")
        key_panel.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self._activation_widget = self._dropdown(
            key_panel,
            self._activation_var,
            ACTIVATION_KEY_OPTIONS,
            direction="above",
        )
        self._activation_widget.grid(row=1, column=0, sticky="ew")

        microphone_panel = panel(daily, "Микрофон")
        microphone_panel.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        self._microphone_widget = self._dropdown(
            microphone_panel,
            self._microphone_var,
            self._microphone_options,
            direction="above",
        )
        self._microphone_widget.grid(row=1, column=0, sticky="ew")

        result_panel = panel(center, "Последний результат")
        result_panel.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        tk.Label(
            result_panel,
            textvariable=self._last_result_var,
            font=("Segoe UI", 11),
            bg=panel_bg,
            fg=muted,
            justify="left",
            anchor="w",
            wraplength=650,
            height=2,
        ).grid(row=1, column=0, sticky="ew")
        quick_actions = tk.Frame(result_panel, bg=panel_bg)
        quick_actions.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        for column in range(3):
            quick_actions.columnconfigure(column, weight=1, uniform="quick")
        self._last_text_action_buttons.append(
            button(quick_actions, 0, "Повторить", self._on_repeat_last, enabled=self._has_last_text, leave_window=True, column=0)
        )
        self._last_text_action_buttons.append(
            button(quick_actions, 0, "Копировать итог", self._on_copy_last, enabled=self._has_last_text, column=1)
        )
        button(quick_actions, 0, "Справка команд", self._on_help, leave_window=True, column=2)

        feedback = tk.Frame(
            home_workspace,
            bg="#091D2C",
            highlightbackground=border,
            highlightthickness=1,
            padx=14,
            pady=14,
        )
        feedback.grid(row=0, column=1, sticky="nsew")
        feedback.columnconfigure(0, weight=1)
        tk.Label(
            feedback,
            text="Что программа поняла",
            font=("Segoe UI", 15, "bold"),
            bg="#091D2C",
            fg=text,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))
        feedback_titles = (
            ("heard", "Услышано", "#38A7FF"),
            ("text", "Текст", "#38A7FF"),
            ("command", "Команда", "#38A7FF"),
            ("outcome", "Результат", "#38C75B"),
        )
        self._feedback_value_labels: dict[str, tk.Label] = {}
        for row, (key, title, color) in enumerate(feedback_titles, start=1):
            card = tk.Frame(
                feedback,
                bg=panel_bg,
                highlightbackground=border,
                highlightthickness=1,
                padx=11,
                pady=9,
            )
            card.grid(row=row, column=0, sticky="ew", pady=4)
            card.columnconfigure(0, weight=1)
            tk.Label(
                card,
                text=title,
                font=("Segoe UI", 10, "bold"),
                bg=panel_bg,
                fg=color,
            ).grid(row=0, column=0, sticky="w")
            value_label = tk.Label(
                card,
                textvariable=self._feedback_vars[key],
                font=("Segoe UI", 10),
                bg=panel_bg,
                fg=text if key != "outcome" else "#38C75B",
                justify="left",
                anchor="w",
                wraplength=245,
            )
            value_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))
            self._feedback_value_labels[key] = value_label
        button(
            feedback,
            5,
            "Быстрая проверка",
            self._on_quick_test,
            leave_window=True,
        )
        tk.Label(
            feedback,
            text="3 голосовых шага подряд · результат проверяется автоматически.",
            font=("Segoe UI", 9),
            bg="#091D2C",
            fg="#A9BAC9",
            justify="left",
            wraplength=255,
        ).grid(row=6, column=0, sticky="sw", pady=(8, 0))
        tk.Label(
            feedback,
            text="В режиме проверки остальные голосовые действия заблокированы.",
            font=("Segoe UI", 9),
            bg="#091D2C",
            fg="#F59E0B",
            justify="left",
            wraplength=255,
        ).grid(row=7, column=0, sticky="sw", pady=(4, 0))

        # Речь.
        for column in range(2):
            speech.columnconfigure(column, weight=1, uniform="speech")
        heading(speech, "Речь", "Распознавание, язык и источник звука")
        local_speech = panel(speech, "Локальное распознавание")
        local_speech.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        speech_language = self._combo(local_speech, 1, "Язык распознавания", self._language_var, LANGUAGE_OPTIONS)
        self._speech_microphone_widget = self._combo(
            local_speech,
            3,
            "Микрофон",
            self._microphone_var,
            self._microphone_options,
        )
        transcription = self._combo(local_speech, 5, "Где распознавать", self._transcription_var, TRANSCRIPTION_MODE_OPTIONS)
        note(local_speech, "Локальная модель: Whisper small · CPU int8. Большие модели не скачиваются автоматически.", 7)

        speech_cloud = panel(speech, "Добровольное облачное распознавание")
        speech_cloud.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        field_label(speech_cloud, "Модель распознавания аудио", 1)
        transcription_model = entry(speech_cloud, self._transcription_model_var, 2)
        note(speech_cloud, "Работает только вместе с отдельным разрешением на отправку аудио на странице «Приватность».", 3, color="#F59E0B")

        # Текст.
        for column in range(2):
            text_tab.columnconfigure(column, weight=1, uniform="text")
        heading(text_tab, "Текст", "Коррекция, словарь и команды внутри диктовки")
        correction_box = panel(text_tab, "Коррекция")
        correction_box.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        correction = self._combo(correction_box, 1, "Режим коррекции", self._correction_var, CORRECTION_MODE_OPTIONS)
        note(correction_box, "Бережный локальный режим исправляет пробелы, регистр, пунктуацию, очевидные повторы и несколько подтверждённых написаний по контексту — без сети и перефразирования.", 3)
        field_label(correction_box, "Локальная модель Ollama (для compact / full)", 4)
        local_model = entry(correction_box, self._local_model_var, 5)

        memory_box = panel(text_tab, "Словарь и текущий результат")
        memory_box.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        button(memory_box, 1, "Открыть личный словарь", self._on_memory, leave_window=True)
        self._last_text_action_buttons.append(
            button(memory_box, 2, "Скопировать исправленный текст", self._on_copy_last, enabled=self._has_last_text)
        )
        self._raw_text_action_buttons.append(
            button(memory_box, 3, "Скопировать исходный текст", self._on_copy_raw, enabled=self._has_raw_text)
        )
        note(memory_box, "Исходный, словарный и итоговый варианты хранятся только в RAM до закрытия VoiceType.", 4)

        dictation_box = panel(text_tab, "Команды внутри диктовки")
        dictation_box.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        dictation_commands = check(dictation_box, "Понимать «точка», «новая строка», «табуляция»", self._dictation_commands_var)
        dictation_commands.grid(row=1, column=0, sticky="w", pady=3)
        remove_fillers = check(dictation_box, "Убирать безопасные слова-паразиты: «эм», «ээ», “um”, “uh”, “eh”", self._remove_fillers_var)
        remove_fillers.grid(row=2, column=0, sticky="w", pady=3)
        automatic_spacing = check(dictation_box, "Добавлять пробел между диктовками в том же поле (до 30 секунд)", self._automatic_spacing_var)
        automatic_spacing.grid(row=3, column=0, sticky="w", pady=3)

        # Приватность.
        for column in range(2):
            privacy.columnconfigure(column, weight=1, uniform="privacy")
        heading(privacy, "Приватность", "Локальная работа по умолчанию и отдельные облачные разрешения")
        consent_box = panel(privacy, "Разрешения")
        consent_box.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        text_consent = check(consent_box, "Разрешить отправку текста в облако", self._cloud_text_var)
        text_consent.grid(row=1, column=0, sticky="w", pady=(5, 1))
        note(consent_box, CLOUD_TEXT_WARNING, 2, color="#F59E0B")
        audio_consent = check(consent_box, "Разрешить отправку аудио в облако", self._cloud_audio_var)
        audio_consent.grid(row=3, column=0, sticky="w", pady=(8, 1))
        note(consent_box, CLOUD_TRANSCRIPTION_WARNING, 4, color="#F59E0B")
        self._clear_session_button = button(
            consent_box,
            5,
            "Очистить текст текущей сессии",
            self._clear_session_text,
            enabled=(
                self._on_clear_session is not None
                and (self._has_last_text or self._has_raw_text)
            ),
        )

        provider_box = panel(privacy, "Провайдер и ключ")
        provider_box.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        provider = self._combo(provider_box, 1, "Провайдер", self._provider_var, PROVIDER_OPTIONS)
        field_label(provider_box, "Модель облачной коррекции текста", 3)
        model = entry(provider_box, self._model_var, 4)
        field_label(provider_box, "Новый API-ключ (пусто — не менять)", 5)
        api_key = entry(provider_box, self._api_key_var, 6, show="●")
        delete_key = tk.Button(
            provider_box,
            text="Удалить сохранённый ключ (два нажатия)",
            command=self.delete_saved_secret,
            takefocus=True,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor="#2D9CFF",
            font=("Segoe UI", 11, "bold"),
            bg="#123149",
            activebackground="#17405F",
            fg=text,
            activeforeground="#FFFFFF",
            cursor="hand2",
        )
        delete_key.grid(row=7, column=0, sticky="ew", pady=(5, 0), ipady=6)
        delete_key.bind(
            "<Return>",
            lambda _event, target=delete_key: self._invoke_button_from_keyboard(target),
        )
        note(provider_box, "Существующий ключ не показывается и хранится в защищённом хранилище Windows.", 8)

        # Доступность.
        for column in range(2):
            accessibility.columnconfigure(column, weight=1, uniform="access")
        heading(accessibility, "Доступность", "Клавиши, звуки и крупный индикатор записи")
        activation_box = panel(accessibility, "Способ запуска")
        activation_box.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        access_activation = self._combo(activation_box, 1, "Клавиша или комбинация", self._activation_var, ACTIVATION_KEY_OPTIONS)
        activation_mode = self._combo(activation_box, 3, "Как включать запись", self._activation_mode_var, ACTIVATION_MODE_OPTIONS)
        note(activation_box, "Стандарт: правый Ctrl и режим переключателя. Pause работает только как переключатель.", 5)
        sounds = check(activation_box, "Воспроизводить звуковые сигналы", self._sounds_var)
        sounds.grid(row=6, column=0, sticky="w", pady=(5, 8))
        reset_accessibility = tk.Button(
            activation_box,
            text="Вернуть настройки по умолчанию",
            command=self._reset_accessibility_defaults,
            takefocus=True,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor="#2D9CFF",
            font=("Segoe UI", 11, "bold"),
            bg="#123149",
            activebackground="#17405F",
            fg=text,
            activeforeground="#FFFFFF",
            cursor="hand2",
        )
        reset_accessibility.grid(row=7, column=0, sticky="ew", ipady=6)
        reset_accessibility.bind(
            "<Return>",
            lambda _event, target=reset_accessibility: self._invoke_button_from_keyboard(target),
        )

        appearance_box = panel(accessibility, "Индикатор записи")
        appearance_box.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        overlay_size = self._combo(appearance_box, 1, "Размер", self._overlay_size_var, OVERLAY_SIZE_OPTIONS)
        overlay_contrast = self._combo(appearance_box, 3, "Контраст", self._overlay_contrast_var, OVERLAY_CONTRAST_OPTIONS)
        overlay_position = self._combo(appearance_box, 5, "Положение на экране", self._overlay_position_var, OVERLAY_POSITION_OPTIONS)
        note(appearance_box, "Изменения применятся после сохранения и не влияют на качество распознавания.", 7)

        access_help = panel(accessibility, "Голосовое управление")
        access_help.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        note(access_help, "Команды работают на русском, испанском и английском. Опасные действия требуют подтверждения; UAC не обходится.", 1, wraplength=760)
        button(access_help, 2, "Открыть справку команд", self._on_help, leave_window=True)

        # О программе — фактическая информация, без неработающих элементов.
        for column in range(2):
            about.columnconfigure(column, weight=1, uniform="about")
        heading(about, "О программе", "VoiceType Local — локальная диктовка и безопасное голосовое управление Windows")
        local_about = panel(about, "Как работает")
        local_about.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        note(local_about, "Голос записывается выбранным микрофоном, распознаётся Whisper small и вставляется в поле, где стоял курсор. По умолчанию аудио остаётся на компьютере.", 1)
        note(local_about, "Правый Ctrl запускает и останавливает запись. Другую безопасную клавишу можно выбрать на странице «Доступность».", 2)
        safety_about = panel(about, "Безопасность и помощь")
        safety_about.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        note(safety_about, "Текст текущей сессии не превращается в постоянную историю. Облачная обработка включается только отдельными разрешениями.", 1)
        button(safety_about, 2, "Что можно сказать", self._on_help, leave_window=True)
        button(safety_about, 3, "Открыть личный словарь", self._on_memory, leave_window=True)

        self._field_widgets.update(
            {
                "language": self._language_widget,
                "microphone": self._microphone_widget,
                "activation_key": access_activation,
                "activation_mode": activation_mode,
                "overlay_size": overlay_size,
                "overlay_contrast": overlay_contrast,
                "overlay_position": overlay_position,
                "interaction_mode": self._interaction_mode_widget,
                "dictation_commands_enabled": dictation_commands,
                "remove_fillers": remove_fillers,
                "automatic_spacing": automatic_spacing,
                "sounds": sounds,
                "correction_mode": correction,
                "transcription_mode": transcription,
                "cloud_text_consent": text_consent,
                "cloud_transcription_consent": audio_consent,
                "cloud_provider": provider,
                "cloud_model": model,
                "cloud_transcription_model": transcription_model,
                "local_model": local_model,
                "api_key": api_key,
                "delete_api_key": delete_key,
            }
        )
        self._field_tabs.update(
            {
                "language": home_page,
                "microphone": home_page,
                "interaction_mode": home_page,
                "transcription_mode": speech,
                "cloud_transcription_model": speech,
                "correction_mode": text_tab,
                "local_model": text_tab,
                "dictation_commands_enabled": text_tab,
                "remove_fillers": text_tab,
                "automatic_spacing": text_tab,
                "activation_key": accessibility,
                "activation_mode": accessibility,
                "overlay_size": accessibility,
                "overlay_contrast": accessibility,
                "overlay_position": accessibility,
                "sounds": accessibility,
                "cloud_text_consent": privacy,
                "cloud_transcription_consent": privacy,
                "cloud_provider": privacy,
                "cloud_model": privacy,
                "api_key": privacy,
                "delete_api_key": privacy,
            }
        )

        footer = tk.Frame(
            root,
            bg="#0A1D2D",
            highlightbackground=border,
            highlightthickness=1,
            padx=18,
            pady=6,
        )
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            textvariable=self._status_var,
            style="VoiceType.Settings.Status.TLabel",
            wraplength=650,
        ).grid(row=0, column=0, sticky="w")
        save_button = tk.Button(
            footer,
            text="Сохранить (Enter)",
            command=self.save,
            takefocus=True,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#2D9CFF",
            highlightcolor="#8CCBFF",
            font=("Segoe UI", 11, "bold"),
            bg=blue,
            activebackground=blue_hover,
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            cursor="hand2",
        )
        save_button.grid(row=0, column=1, padx=(10, 8), ipadx=14, ipady=5)
        save_button.bind(
            "<Return>",
            lambda _event, target=save_button: self._invoke_button_from_keyboard(target),
        )
        cancel_button = tk.Button(
            footer,
            text="Отмена (Esc)",
            command=self.cancel,
            takefocus=True,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor="#2D9CFF",
            font=("Segoe UI", 11, "bold"),
            bg="#123149",
            activebackground="#17405F",
            fg=text,
            activeforeground="#FFFFFF",
            cursor="hand2",
        )
        cancel_button.grid(row=0, column=2, ipadx=12, ipady=5)
        cancel_button.bind(
            "<Return>",
            lambda _event, target=cancel_button: self._invoke_button_from_keyboard(target),
        )

        self._select_page(home_page)

    def _set_home_scrollbar(self, first: str, last: str) -> None:
        """Render a dark, high-contrast scrollbar thumb for the daily page."""

        if self._closed or not hasattr(self, "_home_scrollbar"):
            return
        start = max(0.0, min(1.0, float(first)))
        end = max(start, min(1.0, float(last)))
        track_height = max(1, self._home_scrollbar.winfo_height())
        visible_fraction = max(0.0, min(1.0, end - start))
        thumb_height = min(
            track_height,
            max(48, int(track_height * visible_fraction)),
        )
        travel = max(0, track_height - thumb_height)
        denominator = max(0.0001, 1.0 - visible_fraction)
        top = int(travel * (start / denominator)) if travel else 0
        self._home_scrollbar.coords(
            self._home_scroll_thumb,
            2,
            top,
            10,
            top + thumb_height,
        )

    def _home_scrollbar_target(self, y: float, drag_offset: float) -> float:
        track_height = max(1, self._home_scrollbar.winfo_height())
        bounds = self._home_scrollbar.coords(self._home_scroll_thumb)
        thumb_height = max(1.0, bounds[3] - bounds[1]) if len(bounds) == 4 else 48.0
        travel = max(1.0, track_height - thumb_height)
        return max(0.0, min(1.0, (float(y) - drag_offset) / travel))

    def _press_home_scrollbar(self, event: tk.Event[tk.Misc]) -> str:
        bounds = self._home_scrollbar.coords(self._home_scroll_thumb)
        if len(bounds) == 4 and bounds[1] <= event.y <= bounds[3]:
            self._home_scroll_drag_offset = float(event.y) - bounds[1]
        else:
            thumb_height = max(1.0, bounds[3] - bounds[1]) if len(bounds) == 4 else 48.0
            self._home_scroll_drag_offset = thumb_height / 2
            self._home_canvas.yview_moveto(
                self._home_scrollbar_target(event.y, self._home_scroll_drag_offset)
            )
        self._home_scrollbar.focus_set()
        return "break"

    def _drag_home_scrollbar(self, event: tk.Event[tk.Misc]) -> str:
        self._home_canvas.yview_moveto(
            self._home_scrollbar_target(event.y, self._home_scroll_drag_offset)
        )
        return "break"

    def _scroll_home_by_units(self, units: int) -> str:
        self._home_canvas.yview_scroll(int(units), "units")
        return "break"

    def _update_home_scroll(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        """Keep the daily page usable at high Windows scaling and 680px height."""

        if self._closed or not hasattr(self, "_home_canvas"):
            return
        canvas = self._home_canvas
        content = self._home_content
        viewport_width = max(1, canvas.winfo_width())
        viewport_height = max(1, canvas.winfo_height())
        content_height = max(1, content.winfo_reqheight())
        needs_scroll = content_height > viewport_height + 24
        rendered_height = content_height if needs_scroll else viewport_height
        canvas.itemconfigure(
            self._home_canvas_window,
            width=viewport_width,
            height=rendered_height,
        )
        canvas.configure(
            scrollregion=(0, 0, viewport_width, rendered_height),
        )
        if needs_scroll:
            self._home_scrollbar.grid()
        else:
            self._home_scrollbar.grid_remove()
            canvas.yview_moveto(0.0)

    def _scroll_home(self, event: tk.Event[tk.Misc]) -> str | None:
        """Scroll only the selected daily page; leave menus and other pages alone."""

        if self._closed or not hasattr(self, "_home_canvas"):
            return None
        try:
            selected = self._notebook.select()
        except tk.TclError:
            return None
        if str(selected) != str(self._home_page):
            return None
        first, last = self._home_canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return None
        delta = int(getattr(event, "delta", 0))
        if delta == 0:
            return None
        self._home_canvas.yview_scroll(-3 if delta > 0 else 3, "units")
        return "break"

    def _select_page(self, page: tk.Misc) -> None:
        """Select a real settings page from the custom left navigation."""

        if self._closed:
            return
        try:
            self._notebook.select(page)
        except tk.TclError:
            return
        if hasattr(self, "_home_page") and str(page) == str(self._home_page):
            self._home_canvas.yview_moveto(0.0)
            self.window.after_idle(self._update_home_scroll)
        self._refresh_navigation()

    def _activate_page_from_keyboard(self, page: tk.Misc) -> str:
        self._select_page(page)
        return "break"

    @staticmethod
    def _invoke_button_from_keyboard(button: tk.Button | tk.Checkbutton) -> str:
        """Make Enter activate focused native controls without saving the form."""

        button.invoke()
        return "break"

    @staticmethod
    def _open_dropdown_from_keyboard(widget: tk.Menubutton) -> str:
        """Post a focused native picker with Enter, matching mouse activation."""

        widget.event_generate("<space>")
        return "break"

    def _refresh_navigation(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        if self._closed:
            return
        try:
            selected = self._notebook.select()
        except tk.TclError:
            return
        for title, page in self._pages.items():
            row, icon, button = self._nav_buttons[title]
            active = str(page) == str(selected)
            background = "#123B59" if active else "#081B2B"
            foreground = "#FFFFFF" if active else "#E5EDF5"
            row.configure(bg=background)
            icon.configure(bg=background, fg="#38A7FF" if active else foreground)
            button.configure(bg=background, fg=foreground)

    def _choose_interaction_mode(self, value: str) -> None:
        self._interaction_mode_var.set(_label_for(value, INTERACTION_MODE_OPTIONS))
        self._refresh_mode_cards()

    def _activate_mode_from_keyboard(self, value: str) -> str:
        self._choose_interaction_mode(value)
        return "break"

    def _refresh_mode_cards(self) -> None:
        current = _value_for(self._interaction_mode_var.get(), INTERACTION_MODE_OPTIONS)
        for value, button in self._mode_card_buttons.items():
            selected = value == current
            button.configure(
                bg="#075DBD" if selected else "#0B2032",
                fg="#FFFFFF" if selected else "#F8FAFC",
                highlightbackground="#2D9CFF" if selected else "#294356",
            )

    def _choose_language(self, value: str) -> None:
        self._language_var.set(_label_for(value, LANGUAGE_OPTIONS))
        self._refresh_language_buttons()

    def _activate_language_from_keyboard(self, value: str) -> str:
        self._choose_language(value)
        return "break"

    def _refresh_language_buttons(self) -> None:
        current = _value_for(self._language_var.get(), LANGUAGE_OPTIONS)
        for value, button in self._language_buttons.items():
            selected = value == current
            button.configure(
                bg="#075DBD" if selected else "#132A3D",
                fg="#FFFFFF" if selected else "#E5EDF5",
                highlightbackground="#2D9CFF" if selected else "#294356",
            )

    def _choose_activation_mode(self, value: str) -> None:
        self._activation_mode_var.set(_label_for(value, ACTIVATION_MODE_OPTIONS))
        self._refresh_activation_mode_buttons()

    def _activate_activation_from_keyboard(self, value: str) -> str:
        self._choose_activation_mode(value)
        return "break"

    def _refresh_activation_mode_buttons(self) -> None:
        current = _value_for(
            self._activation_mode_var.get(), ACTIVATION_MODE_OPTIONS
        )
        for value, button in self._activation_mode_buttons.items():
            selected = value == current
            button.configure(
                bg="#075DBD" if selected else "#132A3D",
                fg="#FFFFFF" if selected else "#E5EDF5",
                highlightbackground="#2D9CFF" if selected else "#294356",
            )

    def _bind_dirty_tracking(self) -> None:
        variables = (
            self._language_var,
            self._microphone_var,
            self._activation_var,
            self._activation_mode_var,
            self._overlay_size_var,
            self._overlay_contrast_var,
            self._overlay_position_var,
            self._interaction_mode_var,
            self._dictation_commands_var,
            self._remove_fillers_var,
            self._automatic_spacing_var,
            self._sounds_var,
            self._correction_var,
            self._transcription_var,
            self._cloud_text_var,
            self._cloud_audio_var,
            self._provider_var,
            self._model_var,
            self._transcription_model_var,
            self._local_model_var,
            self._api_key_var,
        )
        for variable in variables:
            variable.trace_add("write", self._on_form_changed)
        self._language_var.trace_add(
            "write", lambda *_args: self._refresh_language_buttons()
        )
        self._interaction_mode_var.trace_add(
            "write", lambda *_args: self._refresh_mode_cards()
        )
        self._activation_mode_var.trace_add(
            "write", lambda *_args: self._refresh_activation_mode_buttons()
        )
        self._activation_var.trace_add(
            "write", lambda *_args: self._refresh_home_status()
        )

    def _refresh_home_status(self) -> None:
        if self._closed or not hasattr(self, "_home_status_var"):
            return
        if "готов" not in self._main_status_text.casefold():
            self._home_status_var.set(self._main_status_text)
            return
        key_label = self._activation_var.get()
        key_label = key_label[:1].lower() + key_label[1:]
        self._home_status_var.set(f"Готово — нажмите {key_label}")

    def _on_form_changed(self, *_args: object) -> None:
        if self._closed or self._suppress_dirty:
            return
        self._dirty = True
        self._status_var.set("Есть несохранённые изменения")

    @staticmethod
    def _feedback_value(
        value: object,
        fallback: str,
        *,
        limit: int = 160,
    ) -> str:
        normalized = " ".join(str(value).split())
        if not normalized:
            return fallback
        safe_limit = max(4, int(limit))
        if len(normalized) > safe_limit:
            return f"{normalized[: safe_limit - 1].rstrip()}…"
        return normalized

    def _poll_runtime_feedback(self) -> None:
        """Poll an optional RAM-only presenter; no spoken text is persisted here."""

        if self._closed or self._runtime_feedback is None:
            return
        try:
            feedback = self._runtime_feedback()
        except Exception:
            feedback = None
        if isinstance(feedback, Mapping):
            fallbacks = {
                "heard": "Пока нет данных",
                "text": "Пока нет данных",
                "command": "Нет команды",
                "outcome": "Ожидает первой диктовки",
            }
            for key, fallback in fallbacks.items():
                if key in feedback:
                    self._feedback_vars[key].set(
                        self._feedback_value(feedback.get(key), fallback)
                    )
            if "text" in feedback:
                rendered_text = str(feedback.get("text", "")).strip()[:4000]
                self._last_result_var.set(
                    self._feedback_value(
                        rendered_text,
                        "Пока ничего не продиктовано",
                        limit=96,
                    )
                )
            tone = str(feedback.get("tone", "")).casefold()
            color = {
                "success": "#38C75B",
                "warning": "#F59E0B",
                "error": "#F87171",
                "pending": "#F59E0B",
            }.get(tone, "#E5EDF5")
            self._feedback_value_labels["outcome"].configure(fg=color)
        self._feedback_after_id = self.window.after(
            300, self._poll_runtime_feedback
        )

    def _clear_session_text(self) -> None:
        """Clear session-only text in the app and in this already-open window."""

        if self._closed or self._on_clear_session is None:
            return
        try:
            self._on_clear_session()
        except Exception:
            self._status_var.set(
                "Не удалось выполнить действие. Попробуйте ещё раз."
            )
            return

        self._last_text = ""
        self._last_raw_text = ""
        self._has_last_text = False
        self._has_raw_text = False
        self._last_result_var.set("Пока ничего не продиктовано")
        for action_button in (
            *getattr(self, "_last_text_action_buttons", ()),
            *getattr(self, "_raw_text_action_buttons", ()),
        ):
            action_button.configure(state="disabled", cursor="arrow")
        clear_button = getattr(self, "_clear_session_button", None)
        if clear_button is not None:
            clear_button.configure(state="disabled", cursor="arrow")

    def _call_optional(self, callback: Callable[[], object] | None) -> None:
        """Run an in-window action without letting callback errors break Tk."""

        if self._closed or callback is None:
            return
        try:
            callback()
        except Exception:
            self._status_var.set(
                "Не удалось выполнить действие. Попробуйте ещё раз."
            )

    def _reset_accessibility_defaults(self) -> None:
        """Reset accessibility controls in the form without saving them."""

        if self._closed:
            return
        self._activation_var.set(_label_for("right_ctrl", ACTIVATION_KEY_OPTIONS))
        self._activation_mode_var.set(
            _label_for("toggle", ACTIVATION_MODE_OPTIONS)
        )
        self._overlay_size_var.set(_label_for("large", OVERLAY_SIZE_OPTIONS))
        self._overlay_contrast_var.set(
            _label_for("high", OVERLAY_CONTRAST_OPTIONS)
        )
        self._overlay_position_var.set(
            _label_for("top", OVERLAY_POSITION_OPTIONS)
        )
        self._sounds_var.set(True)
        self._status_var.set(
            "Настройки доступности возвращены к стандартным. Нажмите «Сохранить», чтобы применить."
        )

    def _leave_for_action(self, callback: Callable[[], object] | None) -> None:
        """Release the modal settings window before opening another surface."""

        if self._closed or callback is None:
            return
        # A typed secret must never survive after leaving the settings window.
        self._api_key_var.set("")
        try:
            if self._on_cancel_callback is not None:
                self._on_cancel_callback()
        except Exception:
            # Cleanup bookkeeping must not leave a modal grab behind or block
            # the explicitly requested destination action.
            pass
        finally:
            self.close()
        callback()

    def _build_legacy(self) -> None:
        shell = ttk.Frame(self.window, style="VoiceType.Settings.TFrame")
        shell.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)

        self._scroll_canvas = tk.Canvas(
            shell,
            background="#F8FAFC",
            highlightthickness=0,
            takefocus=False,
        )
        scrollbar = ttk.Scrollbar(
            shell, orient="vertical", command=self._scroll_canvas.yview
        )
        self._scroll_canvas.configure(yscrollcommand=scrollbar.set)
        self._scroll_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        root = ttk.Frame(
            self._scroll_canvas,
            padding=20,
            style="VoiceType.Settings.TFrame",
        )
        self._scroll_content = root
        self._scroll_window = self._scroll_canvas.create_window(
            (0, 0), window=root, anchor="nw"
        )
        root.bind("<Configure>", self._update_scroll_region)
        self._scroll_canvas.bind("<Configure>", self._resize_scroll_content)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(2, weight=1)

        ttk.Label(
            root,
            text="Настройки VoiceType Local",
            style="VoiceType.Settings.Title.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(
            root,
            text="Tab — следующий элемент · Enter — сохранить · Esc — отменить",
            style="VoiceType.Settings.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 14))

        basic = ttk.LabelFrame(
            root,
            text="Основное",
            padding=8,
            style="VoiceType.Settings.TLabelframe",
        )
        cloud = ttk.LabelFrame(
            root,
            text="Облако и приватность",
            padding=8,
            style="VoiceType.Settings.TLabelframe",
        )
        basic.grid(row=2, column=0, sticky="nsew", padx=(0, 9))
        cloud.grid(row=2, column=1, sticky="nsew", padx=(9, 0))
        basic.columnconfigure(0, weight=1)
        cloud.columnconfigure(0, weight=1)

        self._language_widget = self._combo(
            basic, 0, "Язык распознавания", self._language_var, LANGUAGE_OPTIONS
        )
        self._activation_widget = self._combo(
            basic,
            4,
            "Единственная клавиша включения / выключения",
            self._activation_var,
            ACTIVATION_KEY_OPTIONS,
        )
        self._interaction_mode_widget = self._combo(
            basic,
            2,
            "Что делать с распознанной речью",
            self._interaction_mode_var,
            INTERACTION_MODE_OPTIONS,
        )
        sounds = ttk.Checkbutton(
            basic,
            text="Воспроизводить звуковые сигналы",
            variable=self._sounds_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        sounds.grid(row=6, column=0, sticky="w", padx=14, pady=10)
        correction = self._combo(
            basic,
            7,
            "Коррекция текста",
            self._correction_var,
            CORRECTION_MODE_OPTIONS,
        )
        transcription = self._combo(
            basic,
            9,
            "Распознавание речи",
            self._transcription_var,
            TRANSCRIPTION_MODE_OPTIONS,
        )

        text_consent = ttk.Checkbutton(
            cloud,
            text="Разрешить отправку текста в облако",
            variable=self._cloud_text_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        text_consent.grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))
        ttk.Label(
            cloud,
            text=CLOUD_TEXT_WARNING,
            style="VoiceType.Settings.Warning.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=36, pady=(0, 8))

        audio_consent = ttk.Checkbutton(
            cloud,
            text="Разрешить отправку аудио в облако",
            variable=self._cloud_audio_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        audio_consent.grid(row=2, column=0, sticky="w", padx=14, pady=(6, 2))
        ttk.Label(
            cloud,
            text=CLOUD_TRANSCRIPTION_WARNING,
            style="VoiceType.Settings.Warning.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=3, column=0, sticky="ew", padx=36, pady=(0, 8))

        provider = self._combo(
            cloud, 4, "Провайдер", self._provider_var, PROVIDER_OPTIONS
        )
        ttk.Label(
            cloud,
            text="Модель облачной коррекции текста",
            style="VoiceType.Settings.TLabel",
        ).grid(row=6, column=0, sticky="w", padx=14, pady=(8, 4))
        model = ttk.Entry(
            cloud,
            textvariable=self._model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        model.grid(row=7, column=0, sticky="ew", padx=14, pady=(0, 8))

        ttk.Label(
            cloud,
            text="Модель облачного распознавания аудио",
            style="VoiceType.Settings.TLabel",
        ).grid(row=8, column=0, sticky="w", padx=14, pady=(8, 4))
        transcription_model = ttk.Entry(
            cloud,
            textvariable=self._transcription_model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        transcription_model.grid(
            row=9, column=0, sticky="ew", padx=14, pady=(0, 8)
        )

        ttk.Label(
            cloud,
            text="Локальная модель Ollama (для compact / full)",
            style="VoiceType.Settings.TLabel",
        ).grid(row=10, column=0, sticky="w", padx=14, pady=(8, 4))
        local_model = ttk.Entry(
            cloud,
            textvariable=self._local_model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        local_model.grid(row=11, column=0, sticky="ew", padx=14, pady=(0, 8))

        ttk.Label(
            cloud,
            text="Новый API-ключ (пусто — не менять; существующий ключ не показывается)",
            style="VoiceType.Settings.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=12, column=0, sticky="w", padx=14, pady=(8, 4))
        api_key = ttk.Entry(
            cloud,
            textvariable=self._api_key_var,
            show="●",
            font=("Segoe UI", 14),
            takefocus=True,
        )
        api_key.grid(row=13, column=0, sticky="ew", padx=14, pady=(0, 8))
        delete_key = ttk.Button(
            cloud,
            text="Удалить сохранённый API-ключ (два нажатия)",
            command=self.delete_saved_secret,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        )
        delete_key.grid(row=14, column=0, sticky="w", padx=14, pady=(4, 12))

        self._field_widgets.update(
            {
                "language": self._language_widget,
                "activation_key": self._activation_widget,
                "interaction_mode": self._interaction_mode_widget,
                "sounds": sounds,
                "correction_mode": correction,
                "transcription_mode": transcription,
                "cloud_text_consent": text_consent,
                "cloud_transcription_consent": audio_consent,
                "cloud_provider": provider,
                "cloud_model": model,
                "cloud_transcription_model": transcription_model,
                "local_model": local_model,
                "api_key": api_key,
                "delete_api_key": delete_key,
            }
        )

        ttk.Label(
            root,
            textvariable=self._status_var,
            style="VoiceType.Settings.Status.TLabel",
            wraplength=850,
        ).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 4))

        buttons = ttk.Frame(root, style="VoiceType.Settings.TFrame")
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))
        save_button = ttk.Button(
            buttons,
            text="Сохранить (Enter)",
            command=self.save,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        )
        save_button.grid(row=0, column=0, padx=(0, 10))
        cancel_button = ttk.Button(
            buttons,
            text="Отмена (Esc)",
            command=self.cancel,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        )
        cancel_button.grid(row=0, column=1)
        self._bind_focus_scrolling(root)

    def _update_scroll_region(self, _event: tk.Event[tk.Misc]) -> None:
        bounds = self._scroll_canvas.bbox("all")
        if bounds is not None:
            self._scroll_canvas.configure(scrollregion=bounds)

    def _resize_scroll_content(self, event: tk.Event[tk.Misc]) -> None:
        self._scroll_canvas.itemconfigure(self._scroll_window, width=event.width)

    def _bind_focus_scrolling(self, parent: tk.Misc) -> None:
        for child in parent.winfo_children():
            child.bind("<FocusIn>", self._focus_into_view, add="+")
            self._bind_focus_scrolling(child)

    def _focus_into_view(self, event: tk.Event[tk.Misc]) -> None:
        widget = event.widget

        def scroll() -> None:
            if self._closed or not widget.winfo_exists():
                return
            self._scroll_canvas.update_idletasks()
            top = float(self._scroll_canvas.canvasy(0))
            viewport = max(1, self._scroll_canvas.winfo_height())
            content_height = max(1, self._scroll_content.winfo_reqheight())
            y = widget.winfo_rooty() - self._scroll_content.winfo_rooty()
            bottom = y + max(1, widget.winfo_height())
            if y < top:
                self._scroll_canvas.yview_moveto(max(0.0, y / content_height))
            elif bottom > top + viewport:
                target = max(0, bottom - viewport + 18)
                self._scroll_canvas.yview_moveto(
                    min(1.0, target / content_height)
                )

        self.window.after_idle(scroll)

    def _sync_model(self) -> None:
        self.model.update(
            language=_value_for(self._language_var.get(), LANGUAGE_OPTIONS),
            microphone=_value_for(
                self._microphone_var.get(), self._microphone_options
            ),
            activation_key=_value_for(
                self._activation_var.get(), ACTIVATION_KEY_OPTIONS
            ),
            activation_mode=_value_for(
                self._activation_mode_var.get(), ACTIVATION_MODE_OPTIONS
            ),
            overlay_size=_value_for(
                self._overlay_size_var.get(), OVERLAY_SIZE_OPTIONS
            ),
            overlay_contrast=_value_for(
                self._overlay_contrast_var.get(), OVERLAY_CONTRAST_OPTIONS
            ),
            overlay_position=_value_for(
                self._overlay_position_var.get(), OVERLAY_POSITION_OPTIONS
            ),
            interaction_mode=_value_for(
                self._interaction_mode_var.get(), INTERACTION_MODE_OPTIONS
            ),
            dictation_commands_enabled=self._dictation_commands_var.get(),
            remove_fillers=self._remove_fillers_var.get(),
            automatic_spacing=self._automatic_spacing_var.get(),
            sounds=self._sounds_var.get(),
            correction_mode=_value_for(
                self._correction_var.get(), CORRECTION_MODE_OPTIONS
            ),
            transcription_mode=_value_for(
                self._transcription_var.get(), TRANSCRIPTION_MODE_OPTIONS
            ),
            cloud_text_consent=self._cloud_text_var.get(),
            cloud_transcription_consent=self._cloud_audio_var.get(),
            cloud_provider=_value_for(self._provider_var.get(), PROVIDER_OPTIONS),
            cloud_model=self._model_var.get(),
            cloud_transcription_model=self._transcription_model_var.get(),
            local_model=self._local_model_var.get(),
        )

    def _show_validation_error(self, error: SettingsValidationError) -> None:
        field, message = next(iter(error.errors.items()))
        self._status_var.set(message)
        widget = self._field_widgets.get(field)
        tab = self._field_tabs.get(field)
        if tab is not None:
            try:
                self._notebook.select(tab)
            except tk.TclError:
                pass
        if widget is not None:
            widget.focus_set()

    def save(self) -> None:
        if self._closed:
            return
        self._sync_model()
        try:
            self.model.submit(
                api_key=self._api_key_var.get(),
                on_save=self._on_save_callback,
                save_secret=self._save_secret_callback,
                has_saved_secret=self._has_saved_secret,
            )
        except SettingsValidationError as exc:
            self._show_validation_error(exc)
            return
        except Exception:
            # A secret-storage exception could contain the submitted key in
            # its message. Keep the UI deliberately generic and never echo it.
            self._status_var.set(
                "Не удалось сохранить настройки. Проверьте безопасное хранилище и повторите."
            )
            return
        self._api_key_var.set("")
        self.close()

    def delete_saved_secret(self) -> None:
        if self._closed:
            return
        if self._delete_secret_callback is None:
            self._status_var.set("Безопасное хранилище ключа сейчас недоступно.")
            return
        if not self._delete_secret_pending:
            self._delete_secret_pending = True
            self._status_var.set(
                "Подтвердите удаление: нажмите эту же кнопку ещё раз."
            )
            return
        try:
            self._delete_secret_callback()
        except Exception:
            self._status_var.set("Не удалось удалить сохранённый API-ключ.")
            return
        self._delete_secret_pending = False
        self._api_key_var.set("")
        self._status_var.set(
            "Ключ удалён, новые облачные запросы отключены. Уже начатый запрос отозвать нельзя."
        )

    def cancel(self) -> None:
        if self._closed:
            return
        self._api_key_var.set("")
        try:
            if self._on_cancel_callback is not None:
                self._on_cancel_callback()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._feedback_after_id is not None:
            try:
                self.window.after_cancel(self._feedback_after_id)
            except tk.TclError:
                pass
            self._feedback_after_id = None
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def _save_event(self, event: tk.Event[tk.Misc]) -> str:
        # Enter activates the focused control. It remains a global Save shortcut
        # only for text fields and non-interactive surfaces.
        if isinstance(
            event.widget,
            (tk.Button, tk.Checkbutton, tk.Menubutton, ttk.Button, ttk.Checkbutton),
        ):
            return "break"
        self.save()
        return "break"

    def _cancel_event(self, _event: tk.Event[tk.Misc]) -> str:
        self.cancel()
        return "break"


def open_settings_window(
    parent: tk.Misc,
    settings: Mapping[str, Any] | object,
    *,
    on_save: Callable[[dict[str, object]], object],
    save_secret: Callable[[str], object] | None = None,
    has_saved_secret: bool = False,
    delete_secret: Callable[[], object] | None = None,
    on_cancel: Callable[[], object] | None = None,
    on_repeat_last: Callable[[], object] | None = None,
    on_copy_last: Callable[[], object] | None = None,
    on_copy_raw: Callable[[], object] | None = None,
    on_clear_session: Callable[[], object] | None = None,
    on_memory: Callable[[], object] | None = None,
    on_help: Callable[[], object] | None = None,
    on_quick_test: Callable[[], object] | None = None,
    has_last_text: bool = False,
    has_raw_text: bool = False,
    status_text: str = "Готово к работе",
    microphone_options: tuple[str, ...] = (),
    last_text: str = "",
    last_raw_text: str = "",
    runtime_feedback: Callable[[], Mapping[str, str]] | None = None,
) -> SettingsWindow:
    """Explicitly create the window; importing the module has no UI effect."""

    return SettingsWindow(
        parent,
        settings,
        on_save=on_save,
        save_secret=save_secret,
        has_saved_secret=has_saved_secret,
        delete_secret=delete_secret,
        on_cancel=on_cancel,
        on_repeat_last=on_repeat_last,
        on_copy_last=on_copy_last,
        on_copy_raw=on_copy_raw,
        on_clear_session=on_clear_session,
        on_memory=on_memory,
        on_help=on_help,
        on_quick_test=on_quick_test,
        has_last_text=has_last_text,
        has_raw_text=has_raw_text,
        status_text=status_text,
        microphone_options=microphone_options,
        last_text=last_text,
        last_raw_text=last_raw_text,
        runtime_feedback=runtime_feedback,
    )
