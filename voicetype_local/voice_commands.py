"""Strict, deterministic parsing for VoiceType Windows voice commands.

This module deliberately stops at a typed command boundary.  It never performs
UI Automation, keyboard input, process launches, shell execution, or logging.
The executor must honour :class:`ExecutionPolicy` before acting on a parsed
command.

Safety properties:

* dictation mode never turns speech into an operating-system action;
* mixed mode requires an exact ``команда`` / ``command`` / ``comando`` prefix;
* accepted commands use anchored, finite grammars -- there is no LLM fallback;
* keyboard shortcuts are allow-listed;
* shell, terminal, administrator, UAC, registry, and executable-path requests
  are denied rather than converted into a generic action;
* sensitive and destructive UI actions are marked for confirmation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Final


class VoiceMode(str, Enum):
    """How a completed transcription should be interpreted."""

    DICTATION = "dictation"
    COMMANDS = "commands"
    MIXED = "mixed"


class CommandLanguage(str, Enum):
    AUTO = "auto"
    RU = "ru"
    EN = "en"
    ES = "es"


class ParseDisposition(str, Enum):
    EMPTY = "empty"
    DICTATION = "dictation"
    COMMAND = "command"
    REJECTED = "rejected"


class CommandIntent(str, Enum):
    CANCEL = "cancel"
    CONFIRM = "confirm"
    HELP = "help"
    OPEN_APP = "open_app"
    SWITCH_APP = "switch_app"
    MINIMIZE_APP = "minimize_app"
    MAXIMIZE_APP = "maximize_app"
    SCROLL = "scroll"
    CLICK_NAMED = "click_named"
    CLICK_NUMBER = "click_number"
    SHOW_NUMBERS = "show_numbers"
    PRESS_KEYS = "press_keys"
    REPEAT_LAST = "repeat_last"
    COPY_LAST = "copy_last"


class ScrollDirection(str, Enum):
    UP = "up"
    DOWN = "down"


class CommandRisk(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"
    UNSUPPORTED = "unsupported"


class ExecutionPolicy(str, Enum):
    IMMEDIATE = "immediate"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    """Canonical command consumed by a separate, safety-aware executor."""

    intent: CommandIntent
    language: CommandLanguage
    app: str | None = None
    target: str | None = None
    number: int | None = None
    direction: ScrollDirection | None = None
    scroll_steps: int = 1
    keys: tuple[str, ...] = ()
    risk: CommandRisk = CommandRisk.SAFE
    policy: ExecutionPolicy = ExecutionPolicy.IMMEDIATE

    def __post_init__(self) -> None:
        if self.risk is CommandRisk.UNSUPPORTED:
            raise ValueError("unsupported input must not become a VoiceCommand")
        if self.policy is ExecutionPolicy.DENY:
            raise ValueError("denied input must not become a VoiceCommand")
        if self.risk in {CommandRisk.SENSITIVE, CommandRisk.DESTRUCTIVE}:
            if self.policy is not ExecutionPolicy.REQUIRE_CONFIRMATION:
                raise ValueError("sensitive and destructive commands require confirmation")
        if self.scroll_steps < 1 or self.scroll_steps > 10:
            raise ValueError("scroll_steps must be between 1 and 10")

    @property
    def requires_confirmation(self) -> bool:
        return self.policy is ExecutionPolicy.REQUIRE_CONFIRMATION

    def as_payload(self) -> dict[str, object]:
        """Return a serialization-friendly payload without spoken source text."""

        return {
            "intent": self.intent.value,
            "language": self.language.value,
            "app": self.app,
            "target": self.target,
            "number": self.number,
            "direction": self.direction.value if self.direction else None,
            "scroll_steps": self.scroll_steps,
            "keys": list(self.keys),
            "risk": self.risk.value,
            "policy": self.policy.value,
        }


@dataclass(frozen=True, slots=True)
class CommandParseResult:
    """Result of parsing speech; rejected input is never executable."""

    disposition: ParseDisposition
    mode: VoiceMode
    original_text: str
    normalized_text: str
    command: VoiceCommand | None = None
    reason_code: str | None = None

    @property
    def risk(self) -> CommandRisk | None:
        if self.command is not None:
            return self.command.risk
        if self.disposition is ParseDisposition.REJECTED:
            return CommandRisk.UNSUPPORTED
        return None

    @property
    def execution_policy(self) -> ExecutionPolicy | None:
        if self.command is not None:
            return self.command.policy
        if self.disposition is ParseDisposition.REJECTED:
            return ExecutionPolicy.DENY
        return None

    @property
    def requires_confirmation(self) -> bool:
        return bool(self.command and self.command.requires_confirmation)

    @property
    def is_executable(self) -> bool:
        return self.disposition is ParseDisposition.COMMAND and self.command is not None


_ACCENT_TRANSLATION: Final[dict[int, str]] = str.maketrans(
    {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n",
        "ё": "е",
    }
)


def normalize_spoken_text(text: str) -> str:
    """Normalize command matching while preserving Russian ``й`` correctly."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    value = unicodedata.normalize("NFC", text).casefold().translate(_ACCENT_TRANSLATION)
    return re.sub(r"[\W_]+", " ", value, flags=re.UNICODE).strip()


_PREFIXES: Final[dict[CommandLanguage, tuple[str, ...]]] = {
    CommandLanguage.RU: ("команда",),
    CommandLanguage.EN: ("command",),
    CommandLanguage.ES: ("comando",),
}

_LANGUAGE_ORDER: Final[tuple[CommandLanguage, ...]] = (
    CommandLanguage.RU,
    CommandLanguage.EN,
    CommandLanguage.ES,
)

_EXACT_INTENTS: Final[
    dict[CommandLanguage, dict[str, CommandIntent]]
] = {
    CommandLanguage.RU: {
        "отмена": CommandIntent.CANCEL,
        "отмени": CommandIntent.CANCEL,
        "стоп": CommandIntent.CANCEL,
        "ничего не делай": CommandIntent.CANCEL,
        "подтверждаю": CommandIntent.CONFIRM,
        "подтвердить": CommandIntent.CONFIRM,
        "подтверждаю команду": CommandIntent.CONFIRM,
        "подтвердить команду": CommandIntent.CONFIRM,
        "помощь": CommandIntent.HELP,
        "справка": CommandIntent.HELP,
        "что я могу сказать": CommandIntent.HELP,
        "покажи команды": CommandIntent.HELP,
        "какие команды": CommandIntent.HELP,
        "покажи номера": CommandIntent.SHOW_NUMBERS,
        "пронумеруй элементы": CommandIntent.SHOW_NUMBERS,
        "повтори последнее": CommandIntent.REPEAT_LAST,
        "повтори последний текст": CommandIntent.REPEAT_LAST,
        "вставь последнее еще раз": CommandIntent.REPEAT_LAST,
        "скопируй последнее": CommandIntent.COPY_LAST,
        "скопируй последний текст": CommandIntent.COPY_LAST,
    },
    CommandLanguage.EN: {
        "cancel": CommandIntent.CANCEL,
        "stop": CommandIntent.CANCEL,
        "do nothing": CommandIntent.CANCEL,
        "confirm": CommandIntent.CONFIRM,
        "confirm command": CommandIntent.CONFIRM,
        "help": CommandIntent.HELP,
        "show commands": CommandIntent.HELP,
        "what can i say": CommandIntent.HELP,
        "show numbers": CommandIntent.SHOW_NUMBERS,
        "number the controls": CommandIntent.SHOW_NUMBERS,
        "repeat last": CommandIntent.REPEAT_LAST,
        "repeat last text": CommandIntent.REPEAT_LAST,
        "insert last again": CommandIntent.REPEAT_LAST,
        "copy last": CommandIntent.COPY_LAST,
        "copy last text": CommandIntent.COPY_LAST,
    },
    CommandLanguage.ES: {
        "cancelar": CommandIntent.CANCEL,
        "cancela": CommandIntent.CANCEL,
        "para": CommandIntent.CANCEL,
        "no hagas nada": CommandIntent.CANCEL,
        "confirmar": CommandIntent.CONFIRM,
        "confirmo": CommandIntent.CONFIRM,
        "confirma el comando": CommandIntent.CONFIRM,
        "ayuda": CommandIntent.HELP,
        "muestra los comandos": CommandIntent.HELP,
        "que puedo decir": CommandIntent.HELP,
        "muestra los numeros": CommandIntent.SHOW_NUMBERS,
        "numera los elementos": CommandIntent.SHOW_NUMBERS,
        "repite lo ultimo": CommandIntent.REPEAT_LAST,
        "repite el ultimo texto": CommandIntent.REPEAT_LAST,
        "inserta lo ultimo otra vez": CommandIntent.REPEAT_LAST,
        "copia lo ultimo": CommandIntent.COPY_LAST,
        "copia el ultimo texto": CommandIntent.COPY_LAST,
    },
}

_FORBIDDEN_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:powershell|pwsh|cmd|shell|terminal|windows terminal|command prompt|regedit|registry editor|sudo|uac|administrator|admin)\b",
        r"\b(?:run|execute) (?:a |the )?command\b",
        r"\b(?:as administrator|as admin|administrator privileges|elevate privileges|bypass uac|disable uac)\b",
        r"\b(?:format (?:the )?disk|factory reset|shut down (?:the )?(?:computer|pc|windows)|restart (?:the )?(?:computer|pc|windows))\b",
        r"\b(?:командн(?:ая|ую) строк(?:а|у)|терминал|консоль|реестр|редактор реестра|судо|uac|администратор(?:а|ом)?)\b",
        r"\b(?:выполни|выполнить|исполни|исполнить|запусти|запустить) команду\b",
        r"\b(?:от имени администратора|права администратора|обойти uac|отключи uac|отключить uac)\b",
        r"\b(?:форматируй|форматировать) диск\b",
        r"\b(?:выключи|выключить|перезагрузи|перезагрузить) (?:компьютер|windows)\b",
        r"\b(?:simbolo del sistema|linea de comandos|editor del registro|administrador|uac)\b",
        r"\b(?:ejecuta|ejecutar) (?:el )?comando\b",
        r"\b(?:como administrador|permisos de administrador|omitir uac|desactivar uac)\b",
        r"\b(?:formatea|formatear) (?:el )?disco\b",
        r"\b(?:apaga|apagar|reinicia|reiniciar) (?:el )?(?:ordenador|computador|windows)\b",
    )
)

_UNSAFE_RAW_SYNTAX: Final[re.Pattern[str]] = re.compile(
    r"(?:https?://|[a-zA-Z]:[\\/]|\\\\|\.(?:exe|bat|cmd|ps1)\b|"
    r"\$\(|`|&&|\|\||[|<>]|%[^%\r\n]+%)",
    flags=re.IGNORECASE,
)

_DESTRUCTIVE_TARGET_TERMS: Final[tuple[str, ...]] = (
    "delete",
    "remove",
    "uninstall",
    "erase",
    "discard",
    "reset",
    "format",
    "удалить",
    "удали",
    "стереть",
    "очистить",
    "сбросить",
    "деинсталлировать",
    "eliminar",
    "borrar",
    "desinstalar",
    "restablecer",
    "descartar",
    "formatear",
)

_SENSITIVE_TARGET_TERMS: Final[tuple[str, ...]] = (
    "send",
    "submit",
    "pay",
    "buy",
    "purchase",
    "order",
    "checkout",
    "confirm",
    "authorize",
    "share",
    "publish",
    "post",
    "sign in",
    "log in",
    "log out",
    "отправить",
    "оплатить",
    "купить",
    "заказать",
    "подтвердить",
    "авторизовать",
    "поделиться",
    "опубликовать",
    "войти",
    "выйти",
    "enviar",
    "pagar",
    "comprar",
    "confirmar",
    "autorizar",
    "compartir",
    "publicar",
    "iniciar sesion",
    "cerrar sesion",
)

_MODIFIER_ALIASES: Final[dict[CommandLanguage, dict[str, str]]] = {
    CommandLanguage.RU: {
        "ctrl": "ctrl",
        "control": "ctrl",
        "контрол": "ctrl",
        "контроль": "ctrl",
        "alt": "alt",
        "альт": "alt",
        "shift": "shift",
        "шифт": "shift",
        "win": "win",
        "windows": "win",
        "виндовс": "win",
    },
    CommandLanguage.EN: {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "win": "win",
        "windows": "win",
    },
    CommandLanguage.ES: {
        "ctrl": "ctrl",
        "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "mayus": "shift",
        "mayusculas": "shift",
        "win": "win",
        "windows": "win",
    },
}

_COMMON_BASE_KEYS: Final[dict[str, str]] = {
    "escape": "escape",
    "esc": "escape",
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "home": "home",
    "end": "end",
    "page up": "page_up",
    "page down": "page_down",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "f1": "f1",
    "f4": "f4",
    "a": "a",
    "c": "c",
    "f": "f",
    "l": "l",
    "n": "n",
    "p": "p",
    "r": "r",
    "s": "s",
    "t": "t",
    "v": "v",
    "w": "w",
    "x": "x",
    "y": "y",
    "z": "z",
}

_BASE_KEY_ALIASES: Final[dict[CommandLanguage, dict[str, str]]] = {
    CommandLanguage.RU: {
        **_COMMON_BASE_KEYS,
        "эскейп": "escape",
        "энтер": "enter",
        "ввод": "enter",
        "таб": "tab",
        "пробел": "space",
        "бэкспейс": "backspace",
        "делит": "delete",
        "хоум": "home",
        "энд": "end",
        "страница вверх": "page_up",
        "страница вниз": "page_down",
        "стрелка вверх": "up",
        "стрелка вниз": "down",
        "стрелка влево": "left",
        "стрелка вправо": "right",
        "си": "c",
        "с": "c",
        "ви": "v",
        "вэ": "v",
        "зет": "z",
        "игрек": "y",
    },
    CommandLanguage.EN: {
        **_COMMON_BASE_KEYS,
        "up arrow": "up",
        "arrow up": "up",
        "down arrow": "down",
        "arrow down": "down",
        "left arrow": "left",
        "arrow left": "left",
        "right arrow": "right",
        "arrow right": "right",
    },
    CommandLanguage.ES: {
        **_COMMON_BASE_KEYS,
        "intro": "enter",
        "entrar": "enter",
        "tabulador": "tab",
        "espacio": "space",
        "retroceso": "backspace",
        "suprimir": "delete",
        "inicio": "home",
        "fin": "end",
        "pagina arriba": "page_up",
        "pagina abajo": "page_down",
        "flecha arriba": "up",
        "flecha abajo": "down",
        "flecha izquierda": "left",
        "flecha derecha": "right",
    },
}

_IMMEDIATE_KEY_COMBOS: Final[set[tuple[str, ...]]] = {
    ("escape",),
    ("tab",),
    ("space",),
    ("home",),
    ("end",),
    ("page_up",),
    ("page_down",),
    ("up",),
    ("down",),
    ("left",),
    ("right",),
    ("f1",),
    ("ctrl", "a"),
    ("ctrl", "c"),
    ("ctrl", "f"),
    ("ctrl", "l"),
    ("ctrl", "n"),
    ("ctrl", "t"),
    ("alt", "tab"),
    ("ctrl", "tab"),
    ("ctrl", "shift", "tab"),
    ("shift", "tab"),
    ("win", "d"),
    ("win", "tab"),
    ("alt", "left"),
    ("alt", "right"),
}

_SENSITIVE_KEY_COMBOS: Final[set[tuple[str, ...]]] = {
    ("enter",),
    ("ctrl", "p"),
    ("ctrl", "s"),
    ("ctrl", "v"),
}

_DESTRUCTIVE_KEY_COMBOS: Final[set[tuple[str, ...]]] = {
    ("backspace",),
    ("delete",),
    ("alt", "f4"),
    ("ctrl", "f4"),
    ("ctrl", "w"),
    ("ctrl", "y"),
    ("ctrl", "z"),
}

_DENIED_KEY_COMBOS: Final[set[tuple[str, ...]]] = {
    ("win", "r"),
    ("win", "x"),
    ("ctrl", "alt", "delete"),
}

_MODIFIER_ORDER: Final[dict[str, int]] = {
    "ctrl": 0,
    "alt": 1,
    "shift": 2,
    "win": 3,
}


def _languages(language: CommandLanguage) -> tuple[CommandLanguage, ...]:
    return _LANGUAGE_ORDER if language is CommandLanguage.AUTO else (language,)


def _strip_command_prefix(
    normalized: str, languages: tuple[CommandLanguage, ...]
) -> tuple[str, CommandLanguage] | None:
    for language in languages:
        for prefix in _PREFIXES[language]:
            if normalized == prefix:
                return "", language
            if normalized.startswith(prefix + " "):
                return normalized[len(prefix) + 1 :].strip(), language
    return None


def _make_command(
    intent: CommandIntent,
    language: CommandLanguage,
    *,
    app: str | None = None,
    target: str | None = None,
    number: int | None = None,
    direction: ScrollDirection | None = None,
    scroll_steps: int = 1,
    keys: tuple[str, ...] = (),
    risk: CommandRisk = CommandRisk.SAFE,
) -> VoiceCommand:
    policy = (
        ExecutionPolicy.REQUIRE_CONFIRMATION
        if risk in {CommandRisk.SENSITIVE, CommandRisk.DESTRUCTIVE}
        else ExecutionPolicy.IMMEDIATE
    )
    return VoiceCommand(
        intent=intent,
        language=language,
        app=app,
        target=target,
        number=number,
        direction=direction,
        scroll_steps=scroll_steps,
        keys=keys,
        risk=risk,
        policy=policy,
    )


def _target_risk(target: str) -> CommandRisk:
    padded = f" {target} "
    if any(f" {term} " in padded for term in _DESTRUCTIVE_TARGET_TERMS):
        return CommandRisk.DESTRUCTIVE
    if any(f" {term} " in padded for term in _SENSITIVE_TARGET_TERMS):
        return CommandRisk.SENSITIVE
    return CommandRisk.SAFE


def _clean_target(target: str, language: CommandLanguage) -> str:
    value = target.strip()
    generic_targets = {
        CommandLanguage.RU: {"окно", "приложение", "кнопка", "кнопку"},
        CommandLanguage.EN: {"the window", "window", "the app", "app", "the button", "button"},
        CommandLanguage.ES: {
            "la ventana",
            "ventana",
            "la aplicacion",
            "aplicacion",
            "el boton",
            "boton",
        },
    }
    if value in generic_targets[language]:
        return ""
    generic_prefixes = {
        CommandLanguage.RU: ("окно ", "приложение ", "кнопку ", "кнопка "),
        CommandLanguage.EN: ("the window ", "window ", "the app ", "app ", "the button ", "button "),
        CommandLanguage.ES: (
            "la ventana ",
            "ventana ",
            "la aplicacion ",
            "aplicacion ",
            "el boton ",
            "boton ",
        ),
    }
    for prefix in generic_prefixes[language]:
        if value.startswith(prefix):
            return value[len(prefix) :].strip()
    return value


def _canonicalize_keys(
    phrase: str, language: CommandLanguage
) -> tuple[str, ...] | None:
    value = re.sub(r"\b(?:plus|and|и|mas|y)\b", " ", phrase)
    value = re.sub(r"\s+", " ", value).strip()
    aliases = _MODIFIER_ALIASES[language]
    modifiers: list[str] = []

    while value:
        matched = False
        for alias in sorted(aliases, key=len, reverse=True):
            if value == alias or value.startswith(alias + " "):
                canonical = aliases[alias]
                if canonical in modifiers:
                    return None
                modifiers.append(canonical)
                value = value[len(alias) :].strip()
                matched = True
                break
        if not matched:
            break

    base = _BASE_KEY_ALIASES[language].get(value)
    if base is None:
        return None
    modifiers.sort(key=_MODIFIER_ORDER.__getitem__)
    return (*modifiers, base)


def _parse_key_command(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    patterns = {
        CommandLanguage.RU: re.compile(
            r"^(?:нажми|нажать)(?: (?P<marker>клавишу|клавиши|сочетание(?: клавиш)?))? (?P<keys>.+)$"
        ),
        CommandLanguage.EN: re.compile(
            r"^press(?: the)?(?: (?P<marker>key|keys|shortcut))? (?P<keys>.+)$"
        ),
        CommandLanguage.ES: re.compile(
            r"^(?:pulsa|presiona)(?: la| las)?(?: (?P<marker>tecla|teclas|combinacion(?: de teclas)?))? (?P<keys>.+)$"
        ),
    }
    match = patterns[language].fullmatch(text)
    if not match:
        return None, None
    key_phrase = match.group("keys").strip()
    if key_phrase.startswith(("button ", "boton ", "кнопку ", "кнопка ")):
        return None, None
    keys = _canonicalize_keys(key_phrase, language)
    if keys is None:
        aliases = _MODIFIER_ALIASES[language]
        looks_like_shortcut = any(
            key_phrase == alias or key_phrase.startswith(alias + " ")
            for alias in aliases
        )
        return (
            (None, "unsupported_key_combo")
            if match.group("marker") or looks_like_shortcut
            else (None, None)
        )
    if keys in _DENIED_KEY_COMBOS:
        return None, "restricted_shortcut"
    if keys in _IMMEDIATE_KEY_COMBOS:
        return _make_command(CommandIntent.PRESS_KEYS, language, keys=keys), None
    if keys in _SENSITIVE_KEY_COMBOS:
        return (
            _make_command(
                CommandIntent.PRESS_KEYS,
                language,
                keys=keys,
                risk=CommandRisk.SENSITIVE,
            ),
            None,
        )
    if keys in _DESTRUCTIVE_KEY_COMBOS:
        return (
            _make_command(
                CommandIntent.PRESS_KEYS,
                language,
                keys=keys,
                risk=CommandRisk.DESTRUCTIVE,
            ),
            None,
        )
    return None, "unsupported_key_combo"


def _parse_exact(text: str, language: CommandLanguage) -> VoiceCommand | None:
    intent = _EXACT_INTENTS[language].get(text)
    return _make_command(intent, language) if intent is not None else None


def _parse_scroll(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    patterns = {
        CommandLanguage.RU: re.compile(
            r"^(?:(?:прокрути|пролистай|листай|скролл)(?: (?:экран|страницу))? )?"
            r"(?P<direction>вверх|вниз)(?: (?:на )?(?P<count>\d+)(?: раз(?:а|ов)?)?)?$"
        ),
        CommandLanguage.EN: re.compile(
            r"^(?:scroll(?: the)?(?: page| screen)? )?(?P<direction>up|down)"
            r"(?: (?P<count>\d+)(?: times?)?)?$"
        ),
        CommandLanguage.ES: re.compile(
            r"^(?:(?:desplaza|desplazate|mueve|baja|sube)(?: la)?(?: pagina| pantalla)?(?: hacia)? )?"
            r"(?P<direction>arriba|abajo)(?: (?P<count>\d+)(?: veces)?)?$"
        ),
    }
    match = patterns[language].fullmatch(text)
    if not match:
        return None, None
    steps = int(match.group("count") or "1")
    if steps < 1 or steps > 10:
        return None, "invalid_scroll_amount"
    direction = (
        ScrollDirection.UP
        if match.group("direction") in {"вверх", "up", "arriba"}
        else ScrollDirection.DOWN
    )
    return (
        _make_command(
            CommandIntent.SCROLL,
            language,
            direction=direction,
            scroll_steps=steps,
        ),
        None,
    )


def _parse_app_command(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    missing_argument = {
        CommandLanguage.RU: {"открой", "открыть", "запусти", "запустить", "переключись", "перейди"},
        CommandLanguage.EN: {"open", "start", "launch", "switch", "switch to", "go to", "focus"},
        CommandLanguage.ES: {"abre", "abrir", "inicia", "iniciar", "cambia", "cambia a", "ve a"},
    }
    if text in missing_argument[language]:
        return None, "missing_argument"

    patterns: dict[
        CommandLanguage, tuple[tuple[CommandIntent, re.Pattern[str]], ...]
    ] = {
        CommandLanguage.RU: (
            (CommandIntent.SWITCH_APP, re.compile(r"^(?:переключись|перейди) (?:на|в) (?P<app>.+)$")),
            (CommandIntent.MINIMIZE_APP, re.compile(r"^(?:сверни|свернуть)(?: (?P<app>.+))?$")),
            (CommandIntent.MAXIMIZE_APP, re.compile(r"^(?:разверни|развернуть|максимизируй|восстанови)(?: (?P<app>.+))?$")),
            (CommandIntent.OPEN_APP, re.compile(r"^(?:открой|открыть|запусти|запустить) (?P<app>.+)$")),
        ),
        CommandLanguage.EN: (
            (CommandIntent.SWITCH_APP, re.compile(r"^(?:switch to|go to|focus) (?P<app>.+)$")),
            (CommandIntent.MINIMIZE_APP, re.compile(r"^minimi[sz]e(?: (?P<app>.+))?$")),
            (CommandIntent.MAXIMIZE_APP, re.compile(r"^(?:maximi[sz]e|restore)(?: (?P<app>.+))?$")),
            (CommandIntent.OPEN_APP, re.compile(r"^(?:open|start|launch) (?P<app>.+)$")),
        ),
        CommandLanguage.ES: (
            (CommandIntent.SWITCH_APP, re.compile(r"^(?:cambia a|ve a|muestra) (?P<app>.+)$")),
            (CommandIntent.MINIMIZE_APP, re.compile(r"^minimiza(?: (?P<app>.+))?$")),
            (CommandIntent.MAXIMIZE_APP, re.compile(r"^(?:maximiza|restaura)(?: (?P<app>.+))?$")),
            (CommandIntent.OPEN_APP, re.compile(r"^(?:abre|abrir|inicia|iniciar) (?P<app>.+)$")),
        ),
    }

    for intent, pattern in patterns[language]:
        match = pattern.fullmatch(text)
        if not match:
            continue
        app = match.groupdict().get("app")
        if app is not None:
            app = _clean_target(app, language)
            if not app:
                app = None
            elif len(app) > 80:
                return None, "argument_too_long"
        if intent in {CommandIntent.OPEN_APP, CommandIntent.SWITCH_APP} and not app:
            return None, "missing_argument"
        return _make_command(intent, language, app=app), None
    return None, None


def _parse_click_number(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    patterns = {
        CommandLanguage.RU: re.compile(
            r"^(?:нажми|выбери|кликни)(?: на)?(?: кнопку)?(?: номер| по номеру)? (?P<number>\d+)$"
        ),
        CommandLanguage.EN: re.compile(
            r"^(?:click|select|choose|press)(?: the)?(?: button)?(?: number)? (?P<number>\d+)$"
        ),
        CommandLanguage.ES: re.compile(
            r"^(?:haz clic en|selecciona|elige|pulsa|presiona)(?: el)?(?: boton)?(?: numero)? (?P<number>\d+)$"
        ),
    }
    match = patterns[language].fullmatch(text)
    if not match:
        return None, None
    number = int(match.group("number"))
    if number < 1 or number > 999:
        return None, "invalid_number"
    return _make_command(CommandIntent.CLICK_NUMBER, language, number=number), None


def _parse_click_named(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    missing_argument = {
        CommandLanguage.RU: {"нажми", "нажми на", "кликни", "выбери"},
        CommandLanguage.EN: {"click", "click on", "select", "choose", "press button"},
        CommandLanguage.ES: {"haz clic", "haz clic en", "selecciona", "elige", "pulsa", "presiona"},
    }
    if text in missing_argument[language]:
        return None, "missing_argument"
    patterns = {
        CommandLanguage.RU: re.compile(r"^(?:нажми(?: на)?|кликни(?: на)?|выбери) (?P<target>.+)$"),
        CommandLanguage.EN: re.compile(r"^(?:click(?: on)?|select|choose|press(?: the)? button) (?P<target>.+)$"),
        CommandLanguage.ES: re.compile(r"^(?:haz clic(?: en)?|selecciona|elige|pulsa|presiona) (?P<target>.+)$"),
    }
    match = patterns[language].fullmatch(text)
    if not match:
        return None, None
    target = _clean_target(match.group("target"), language)
    if not target:
        return None, "missing_argument"
    if len(target) > 128:
        return None, "argument_too_long"
    if target.isdecimal() or re.fullmatch(r"(?:номер|number|numero) \d+", target):
        return None, "invalid_number"
    risk = _target_risk(target)
    return (
        _make_command(
            CommandIntent.CLICK_NAMED,
            language,
            target=target,
            risk=risk,
        ),
        None,
    )


def _parse_language(
    text: str, language: CommandLanguage
) -> tuple[VoiceCommand | None, str | None]:
    exact = _parse_exact(text, language)
    if exact is not None:
        return exact, None
    for parser in (
        _parse_scroll,
        _parse_app_command,
        _parse_click_number,
        _parse_key_command,
        _parse_click_named,
    ):
        command, reason = parser(text, language)
        if command is not None or reason is not None:
            return command, reason
    return None, None


class VoiceCommandParser:
    """Stateless strict parser suitable for reuse by UI and worker code."""

    def parse(
        self,
        text: str,
        *,
        mode: VoiceMode | str = VoiceMode.MIXED,
        language: CommandLanguage | str = CommandLanguage.AUTO,
    ) -> CommandParseResult:
        if not isinstance(text, str):
            raise TypeError("text must be str")
        mode = VoiceMode(mode)
        language = CommandLanguage(language)
        normalized = normalize_spoken_text(text)
        if not normalized:
            return CommandParseResult(
                ParseDisposition.EMPTY, mode, text, normalized, reason_code="empty_input"
            )
        if mode is VoiceMode.DICTATION:
            return CommandParseResult(ParseDisposition.DICTATION, mode, text, normalized)

        candidate_languages = _languages(language)
        command_text = normalized
        prefix = _strip_command_prefix(command_text, candidate_languages)
        if mode is VoiceMode.MIXED:
            if prefix is None:
                return CommandParseResult(
                    ParseDisposition.DICTATION, mode, text, normalized
                )
            command_text, _prefix_language = prefix
            if not command_text:
                return CommandParseResult(
                    ParseDisposition.REJECTED,
                    mode,
                    text,
                    normalized,
                    reason_code="missing_command",
                )
        elif prefix is not None:
            command_text, _prefix_language = prefix
            if not command_text:
                return CommandParseResult(
                    ParseDisposition.REJECTED,
                    mode,
                    text,
                    normalized,
                    reason_code="missing_command",
                )

        if _UNSAFE_RAW_SYNTAX.search(text):
            return CommandParseResult(
                ParseDisposition.REJECTED,
                mode,
                text,
                normalized,
                reason_code="restricted_syntax",
            )
        if any(pattern.search(command_text) for pattern in _FORBIDDEN_PATTERNS):
            return CommandParseResult(
                ParseDisposition.REJECTED,
                mode,
                text,
                normalized,
                reason_code="restricted_operation",
            )

        first_reason: str | None = None
        for candidate_language in candidate_languages:
            command, reason = _parse_language(command_text, candidate_language)
            if command is not None:
                return CommandParseResult(
                    ParseDisposition.COMMAND,
                    mode,
                    text,
                    normalized,
                    command=command,
                )
            if first_reason is None and reason is not None:
                first_reason = reason

        return CommandParseResult(
            ParseDisposition.REJECTED,
            mode,
            text,
            normalized,
            reason_code=first_reason or "unknown_command",
        )


_DEFAULT_PARSER = VoiceCommandParser()


def parse_voice_input(
    text: str,
    *,
    mode: VoiceMode | str = VoiceMode.MIXED,
    language: CommandLanguage | str = CommandLanguage.AUTO,
) -> CommandParseResult:
    """Parse one completed transcription with the shared strict parser."""

    return _DEFAULT_PARSER.parse(text, mode=mode, language=language)
