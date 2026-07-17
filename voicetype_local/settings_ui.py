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
    activation_key: str = "right_ctrl"
    activation_mode: str = "toggle"
    overlay_size: str = "large"
    overlay_contrast: str = "high"
    overlay_position: str = "top"
    interaction_mode: str = "dictation"
    dictation_commands_enabled: bool = True
    remove_fillers: bool = False
    automatic_spacing: bool = False
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
            automatic_spacing=_setting_bool(settings, "automatic_spacing", False),
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
        has_last_text: bool = False,
        has_raw_text: bool = False,
        status_text: str = "Готово к работе",
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
        self._has_last_text = bool(has_last_text)
        self._has_raw_text = bool(has_raw_text)
        self._main_status_text = str(status_text)[:120]
        self._closed = False
        self._delete_secret_pending = False

        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("VoiceType Local — Настройки")
        self.window.transient(parent.winfo_toplevel())
        self.window.minsize(900, 620)
        self.window.geometry("1060x680")
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", self._cancel_event)
        self.window.bind("<Return>", self._save_event)

        self._configure_styles()
        self._create_variables()
        self._field_widgets: dict[str, tk.Misc] = {}
        self._field_tabs: dict[str, tk.Misc] = {}
        self._build()

        self.window.update_idletasks()
        self.window.deiconify()
        self.window.grab_set()
        self.window.after_idle(self._language_widget.focus_set)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.window)
        style.configure("VoiceType.Settings.TFrame", background="#F8FAFC")
        style.configure(
            "VoiceType.Settings.Title.TLabel",
            font=("Segoe UI", 20, "bold"),
            background="#F8FAFC",
            foreground="#0F172A",
        )
        style.configure(
            "VoiceType.Settings.TLabel",
            font=("Segoe UI", 13),
            background="#F8FAFC",
            foreground="#0F172A",
        )
        style.configure(
            "VoiceType.Settings.Warning.TLabel",
            font=("Segoe UI", 11),
            foreground="#9A3412",
        )
        style.configure(
            "VoiceType.Settings.Status.TLabel",
            font=("Segoe UI", 12, "bold"),
            background="#F8FAFC",
            foreground="#B91C1C",
        )
        style.configure(
            "VoiceType.Settings.TLabelframe.Label", font=("Segoe UI", 15, "bold")
        )
        style.configure("VoiceType.Settings.TCheckbutton", font=("Segoe UI", 13))
        style.configure(
            "VoiceType.Settings.TButton", font=("Segoe UI", 14, "bold"), padding=(18, 10)
        )

    def _create_variables(self) -> None:
        values = self.model.values
        self._language_var = tk.StringVar(
            self.window, _label_for(values.language, LANGUAGE_OPTIONS)
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

    def _combo(
        self,
        parent: tk.Misc,
        row: int,
        title: str,
        variable: tk.StringVar,
        options: tuple[tuple[str, str], ...],
    ) -> ttk.Combobox:
        ttk.Label(parent, text=title, style="VoiceType.Settings.TLabel").grid(
            row=row, column=0, sticky="w", padx=14, pady=(10, 4)
        )
        widget = ttk.Combobox(
            parent,
            textvariable=variable,
            values=_labels(options),
            state="readonly",
            takefocus=True,
            font=("Segoe UI", 14),
        )
        widget.grid(row=row + 1, column=0, sticky="ew", padx=14, pady=(0, 8))
        return widget

    def _build(self) -> None:
        """Build the selected five-section, accessibility-first interface."""

        root = ttk.Frame(
            self.window,
            padding=(20, 16, 20, 14),
            style="VoiceType.Settings.TFrame",
        )
        root.grid(row=0, column=0, sticky="nsew")
        self.window.rowconfigure(0, weight=1)
        self.window.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.columnconfigure(0, weight=1)

        ttk.Label(
            root,
            text="VoiceType Local",
            style="VoiceType.Settings.Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            root,
            text="Крупные понятные настройки · Tab — дальше · Enter — сохранить · Esc — отменить",
            style="VoiceType.Settings.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

        notebook = ttk.Notebook(root, takefocus=True)
        notebook.grid(row=2, column=0, sticky="nsew")
        notebook.enable_traversal()
        self._notebook = notebook

        home = ttk.Frame(notebook, padding=20)
        speech = ttk.Frame(notebook, padding=20)
        text_tab = ttk.Frame(notebook, padding=20)
        privacy = ttk.Frame(notebook, padding=20)
        accessibility = ttk.Frame(notebook, padding=20)
        for frame in (home, speech, text_tab, privacy, accessibility):
            frame.columnconfigure(0, weight=1)
            frame.columnconfigure(1, weight=1)
        notebook.add(home, text="Главная")
        notebook.add(speech, text="Речь")
        notebook.add(text_tab, text="Текст")
        notebook.add(privacy, text="Приватность")
        notebook.add(accessibility, text="Доступность")

        # Главная: only the controls needed every day.
        status_card = ttk.LabelFrame(
            home,
            text="Состояние",
            padding=16,
            style="VoiceType.Settings.TLabelframe",
        )
        status_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        status_card.columnconfigure(0, weight=1)
        ttk.Label(
            status_card,
            text=f"● {self._main_status_text}",
            font=("Segoe UI", 17, "bold"),
            foreground="#15803D",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            status_card,
            text="По умолчанию: правый Ctrl — начать, правый Ctrl ещё раз — закончить.",
            font=("Segoe UI", 12),
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        home_settings = ttk.LabelFrame(
            home,
            text="Как слушать речь",
            padding=10,
            style="VoiceType.Settings.TLabelframe",
        )
        home_settings.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        home_settings.columnconfigure(0, weight=1)
        self._language_widget = self._combo(
            home_settings,
            0,
            "Язык",
            self._language_var,
            LANGUAGE_OPTIONS,
        )
        self._interaction_mode_widget = self._combo(
            home_settings,
            2,
            "Режим",
            self._interaction_mode_var,
            INTERACTION_MODE_OPTIONS,
        )
        ttk.Label(
            home_settings,
            text="В смешанном режиме управление начинается только со слова «команда», «command» или «comando».",
            wraplength=390,
            justify="left",
            font=("Segoe UI", 11),
        ).grid(row=4, column=0, sticky="w", padx=14, pady=(2, 10))

        quick = ttk.LabelFrame(
            home,
            text="Быстрые действия",
            padding=14,
            style="VoiceType.Settings.TLabelframe",
        )
        quick.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        quick.columnconfigure(0, weight=1)

        def button(
            parent: tk.Misc,
            row: int,
            title: str,
            callback: Callable[[], object] | None,
            *,
            enabled: bool = True,
            leave_window: bool = False,
        ) -> ttk.Button:
            command = (
                (lambda: self._leave_for_action(callback))
                if leave_window
                else (lambda: self._call_optional(callback))
            )
            widget = ttk.Button(
                parent,
                text=title,
                command=command,
                takefocus=True,
                style="VoiceType.Settings.TButton",
            )
            widget.grid(row=row, column=0, sticky="ew", pady=5)
            if callback is None or not enabled:
                widget.state(["disabled"])
            return widget

        repeat_button = button(
            quick,
            0,
            "Повторить последнюю вставку",
            self._on_repeat_last,
            enabled=self._has_last_text,
            leave_window=True,
        )
        copy_button = button(
            quick,
            1,
            "Скопировать итог",
            self._on_copy_last,
            enabled=self._has_last_text,
        )
        copy_raw_button = button(
            quick,
            2,
            "Скопировать исходный текст",
            self._on_copy_raw,
            enabled=self._has_raw_text,
        )
        button(
            quick,
            3,
            "Что можно сказать",
            self._on_help,
            leave_window=True,
        )

        # Речь.
        local_speech = ttk.LabelFrame(
            speech,
            text="Распознавание",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        local_speech.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        local_speech.columnconfigure(0, weight=1)
        speech_language = self._combo(
            local_speech, 0, "Язык распознавания", self._language_var, LANGUAGE_OPTIONS
        )
        transcription = self._combo(
            local_speech,
            2,
            "Где распознавать",
            self._transcription_var,
            TRANSCRIPTION_MODE_OPTIONS,
        )
        ttk.Label(
            local_speech,
            text="Локальная модель: Whisper small · CPU int8\nМикрофон: системный по умолчанию\nБольшие модели не скачиваются автоматически.",
            justify="left",
            wraplength=390,
            font=("Segoe UI", 12),
        ).grid(row=4, column=0, sticky="w", padx=14, pady=12)

        speech_cloud = ttk.LabelFrame(
            speech,
            text="Добровольное облачное распознавание",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        speech_cloud.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        speech_cloud.columnconfigure(0, weight=1)
        ttk.Label(
            speech_cloud,
            text="Модель распознавания аудио",
            style="VoiceType.Settings.TLabel",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        transcription_model = ttk.Entry(
            speech_cloud,
            textvariable=self._transcription_model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        transcription_model.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        ttk.Label(
            speech_cloud,
            text="Работает только вместе с отдельным разрешением на отправку аудио во вкладке «Приватность».",
            style="VoiceType.Settings.Warning.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=2, column=0, sticky="w", padx=14, pady=(2, 10))

        # Текст.
        correction_box = ttk.LabelFrame(
            text_tab,
            text="Коррекция",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        correction_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        correction_box.columnconfigure(0, weight=1)
        correction = self._combo(
            correction_box,
            0,
            "Режим коррекции",
            self._correction_var,
            CORRECTION_MODE_OPTIONS,
        )
        ttk.Label(
            correction_box,
            text="Бережный локальный режим исправляет пробелы, регистр, пунктуацию и очевидные повторы — без перефразирования.",
            justify="left",
            wraplength=390,
            font=("Segoe UI", 11),
        ).grid(row=2, column=0, sticky="w", padx=14, pady=(2, 8))
        ttk.Label(
            correction_box,
            text="Локальная модель Ollama (только для compact / full)",
            style="VoiceType.Settings.TLabel",
        ).grid(row=3, column=0, sticky="w", padx=14, pady=(8, 4))
        local_model = ttk.Entry(
            correction_box,
            textvariable=self._local_model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        local_model.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 8))

        memory_box = ttk.LabelFrame(
            text_tab,
            text="Словарь и текущий результат",
            padding=14,
            style="VoiceType.Settings.TLabelframe",
        )
        memory_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        memory_box.columnconfigure(0, weight=1)
        button(
            memory_box,
            0,
            "Открыть личный словарь",
            self._on_memory,
            leave_window=True,
        )
        button(
            memory_box,
            1,
            "Скопировать исправленный текст",
            self._on_copy_last,
            enabled=self._has_last_text,
        )
        button(
            memory_box,
            2,
            "Скопировать исходный текст",
            self._on_copy_raw,
            enabled=self._has_raw_text,
        )
        ttk.Label(
            memory_box,
            text="Исходный, словарный и итоговый варианты хранятся только в RAM до закрытия VoiceType.",
            wraplength=390,
            justify="left",
            font=("Segoe UI", 11),
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

        dictation_box = ttk.LabelFrame(
            text_tab,
            text="Команды внутри диктовки",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        dictation_box.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(14, 0),
        )
        dictation_box.columnconfigure(0, weight=1)
        dictation_commands = ttk.Checkbutton(
            dictation_box,
            text="Понимать «точка», «новая строка», «табуляция»",
            variable=self._dictation_commands_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        dictation_commands.grid(row=0, column=0, sticky="w", padx=14, pady=(6, 3))
        remove_fillers = ttk.Checkbutton(
            dictation_box,
            text="Убирать безопасные слова-паразиты: «эм», «ээ», “um”, “uh”, “eh”",
            variable=self._remove_fillers_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        remove_fillers.grid(row=1, column=0, sticky="w", padx=14, pady=3)
        automatic_spacing = ttk.Checkbutton(
            dictation_box,
            text="Добавлять пробел между диктовками в том же поле (до 30 секунд)",
            variable=self._automatic_spacing_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        automatic_spacing.grid(row=2, column=0, sticky="w", padx=14, pady=(3, 6))

        # Приватность.
        consent_box = ttk.LabelFrame(
            privacy,
            text="Разрешения",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        consent_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        consent_box.columnconfigure(0, weight=1)
        text_consent = ttk.Checkbutton(
            consent_box,
            text="Разрешить отправку текста в облако",
            variable=self._cloud_text_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        text_consent.grid(row=0, column=0, sticky="w", padx=14, pady=(8, 2))
        ttk.Label(
            consent_box,
            text=CLOUD_TEXT_WARNING,
            style="VoiceType.Settings.Warning.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=36, pady=(0, 8))
        audio_consent = ttk.Checkbutton(
            consent_box,
            text="Разрешить отправку аудио в облако",
            variable=self._cloud_audio_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        audio_consent.grid(row=2, column=0, sticky="w", padx=14, pady=(6, 2))
        ttk.Label(
            consent_box,
            text=CLOUD_TRANSCRIPTION_WARNING,
            style="VoiceType.Settings.Warning.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=3, column=0, sticky="ew", padx=36, pady=(0, 8))
        clear_button = button(
            consent_box,
            4,
            "Очистить текст текущей сессии",
            self._on_clear_session,
            enabled=self._has_last_text or self._has_raw_text,
        )

        provider_box = ttk.LabelFrame(
            privacy,
            text="Провайдер и ключ",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        provider_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        provider_box.columnconfigure(0, weight=1)
        provider = self._combo(
            provider_box, 0, "Провайдер", self._provider_var, PROVIDER_OPTIONS
        )
        ttk.Label(
            provider_box,
            text="Модель облачной коррекции текста",
            style="VoiceType.Settings.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=14, pady=(8, 4))
        model = ttk.Entry(
            provider_box,
            textvariable=self._model_var,
            font=("Segoe UI", 14),
            takefocus=True,
        )
        model.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        ttk.Label(
            provider_box,
            text="Новый API-ключ (пусто — не менять; существующий не показывается)",
            style="VoiceType.Settings.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=4, column=0, sticky="w", padx=14, pady=(8, 4))
        api_key = ttk.Entry(
            provider_box,
            textvariable=self._api_key_var,
            show="●",
            font=("Segoe UI", 14),
            takefocus=True,
        )
        api_key.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 8))
        delete_key = ttk.Button(
            provider_box,
            text="Удалить сохранённый ключ (два нажатия)",
            command=self.delete_saved_secret,
            takefocus=True,
        )
        delete_key.grid(row=6, column=0, sticky="ew", padx=14, pady=(4, 8))

        # Доступность.
        activation_box = ttk.LabelFrame(
            accessibility,
            text="Способ запуска",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        activation_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        activation_box.columnconfigure(0, weight=1)
        self._activation_widget = self._combo(
            activation_box,
            0,
            "Клавиша или комбинация",
            self._activation_var,
            ACTIVATION_KEY_OPTIONS,
        )
        activation_mode = self._combo(
            activation_box,
            2,
            "Как включать запись",
            self._activation_mode_var,
            ACTIVATION_MODE_OPTIONS,
        )
        ttk.Label(
            activation_box,
            text=(
                "Стандарт: правый Ctrl и режим переключателя. Другую клавишу "
                "или комбинацию можно выбрать здесь. Pause работает только "
                "как переключатель."
            ),
            justify="left",
            wraplength=390,
            font=("Segoe UI", 11),
        ).grid(row=4, column=0, sticky="w", padx=14, pady=(2, 8))
        sounds = ttk.Checkbutton(
            activation_box,
            text="Воспроизводить звуковые сигналы",
            variable=self._sounds_var,
            takefocus=True,
            style="VoiceType.Settings.TCheckbutton",
        )
        sounds.grid(row=5, column=0, sticky="w", padx=14, pady=(6, 8))
        reset_accessibility = ttk.Button(
            activation_box,
            text="Вернуть настройки доступности по умолчанию",
            command=self._reset_accessibility_defaults,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        )
        reset_accessibility.grid(row=6, column=0, sticky="ew", padx=14, pady=(4, 10))

        appearance_box = ttk.LabelFrame(
            accessibility,
            text="Индикатор записи",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        appearance_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        appearance_box.columnconfigure(0, weight=1)
        overlay_size = self._combo(
            appearance_box,
            0,
            "Размер",
            self._overlay_size_var,
            OVERLAY_SIZE_OPTIONS,
        )
        overlay_contrast = self._combo(
            appearance_box,
            2,
            "Контраст",
            self._overlay_contrast_var,
            OVERLAY_CONTRAST_OPTIONS,
        )
        overlay_position = self._combo(
            appearance_box,
            4,
            "Положение на экране",
            self._overlay_position_var,
            OVERLAY_POSITION_OPTIONS,
        )
        ttk.Label(
            appearance_box,
            text="Изменения применятся после сохранения. Настройки не влияют на распознавание речи.",
            wraplength=390,
            justify="left",
            font=("Segoe UI", 11),
        ).grid(row=6, column=0, sticky="w", padx=14, pady=(4, 10))

        access_help = ttk.LabelFrame(
            accessibility,
            text="Голосовое управление",
            padding=12,
            style="VoiceType.Settings.TLabelframe",
        )
        access_help.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(14, 0),
        )
        access_help.columnconfigure(0, weight=1)
        ttk.Label(
            access_help,
            text="Команды работают на русском, испанском и английском. Обычные действия выполняются сразу; опасные требуют подтверждения. UAC не обходится.",
            wraplength=390,
            justify="left",
            font=("Segoe UI", 12),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))
        button(
            access_help,
            1,
            "Открыть справку команд",
            self._on_help,
            leave_window=True,
        )

        self._field_widgets.update(
            {
                "language": self._language_widget,
                "activation_key": self._activation_widget,
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
                "language": home,
                "interaction_mode": home,
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

        bottom = ttk.Frame(root, style="VoiceType.Settings.TFrame")
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(
            bottom,
            textvariable=self._status_var,
            style="VoiceType.Settings.Status.TLabel",
            wraplength=680,
        ).grid(row=0, column=0, sticky="w")
        save_button = ttk.Button(
            bottom,
            text="Сохранить (Enter)",
            command=self.save,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        )
        save_button.grid(row=0, column=1, padx=(10, 8))
        ttk.Button(
            bottom,
            text="Отмена (Esc)",
            command=self.cancel,
            takefocus=True,
            style="VoiceType.Settings.TButton",
        ).grid(row=0, column=2)

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
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def _save_event(self, _event: tk.Event[tk.Misc]) -> str:
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
    has_last_text: bool = False,
    has_raw_text: bool = False,
    status_text: str = "Готово к работе",
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
        has_last_text=has_last_text,
        has_raw_text=has_raw_text,
        status_text=status_text,
    )
