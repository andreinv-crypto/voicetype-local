from __future__ import annotations

import pytest

from voicetype_local.voice_commands import (
    CommandIntent,
    CommandLanguage,
    CommandRisk,
    ExecutionPolicy,
    ParseDisposition,
    ScrollDirection,
    VoiceCommandParser,
    VoiceMode,
    normalize_spoken_text,
    parse_voice_input,
)


def _command(text: str, **kwargs: object):
    result = parse_voice_input(text, mode=VoiceMode.COMMANDS, **kwargs)
    assert result.disposition is ParseDisposition.COMMAND, result
    assert result.command is not None
    return result.command


def test_normalization_is_case_and_punctuation_insensitive() -> None:
    assert normalize_spoken_text("  ¡COMANDO, Abre PÁGINA!  ") == "comando abre pagina"


def test_normalization_preserves_russian_short_i_and_folds_yo() -> None:
    assert normalize_spoken_text("Й Ё") == "й е"


def test_empty_input_has_no_execution_policy() -> None:
    result = parse_voice_input("  ... ", mode="commands")

    assert result.disposition is ParseDisposition.EMPTY
    assert result.risk is None
    assert result.execution_policy is None
    assert not result.is_executable


@pytest.mark.parametrize(
    "text",
    [
        "открой Chrome",
        "command open Chrome",
        "comando abre Chrome",
        "нажми удалить",
    ],
)
def test_dictation_mode_never_creates_windows_action(text: str) -> None:
    result = parse_voice_input(text, mode=VoiceMode.DICTATION)

    assert result.disposition is ParseDisposition.DICTATION
    assert result.command is None
    assert result.execution_policy is None


@pytest.mark.parametrize(
    "text",
    [
        "открой Chrome",
        "open Chrome",
        "abre Chrome",
        "я думаю открой тему подробнее",
        "commandment is an English word",
        "командовать парадом сложно",
        "comandante es una palabra",
    ],
)
def test_mixed_mode_without_exact_prefix_is_dictation(text: str) -> None:
    result = parse_voice_input(text, mode=VoiceMode.MIXED)

    assert result.disposition is ParseDisposition.DICTATION
    assert result.command is None


@pytest.mark.parametrize(
    ("text", "intent", "language"),
    [
        ("Команда, открой Chrome.", CommandIntent.OPEN_APP, CommandLanguage.RU),
        ("Command: open Chrome!", CommandIntent.OPEN_APP, CommandLanguage.EN),
        ("Comando, abre Chrome.", CommandIntent.OPEN_APP, CommandLanguage.ES),
    ],
)
def test_mixed_mode_accepts_only_explicit_multilingual_prefix(
    text: str, intent: CommandIntent, language: CommandLanguage
) -> None:
    result = parse_voice_input(text, mode=VoiceMode.MIXED)

    assert result.disposition is ParseDisposition.COMMAND
    assert result.command is not None
    assert result.command.intent is intent
    assert result.command.language is language
    assert result.command.app == "chrome"


def test_prefix_without_command_is_rejected_and_denied() -> None:
    result = parse_voice_input("команда", mode="mixed")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == "missing_command"
    assert result.risk is CommandRisk.UNSUPPORTED
    assert result.execution_policy is ExecutionPolicy.DENY
    assert not result.requires_confirmation
    assert not result.is_executable


def test_commands_mode_optionally_accepts_prefix() -> None:
    command = _command("command show commands")

    assert command.intent is CommandIntent.HELP
    assert command.language is CommandLanguage.EN


def test_explicit_language_limits_grammar_and_mixed_prefix() -> None:
    commands_result = parse_voice_input(
        "open Chrome", mode="commands", language=CommandLanguage.RU
    )
    mixed_result = parse_voice_input(
        "command open Chrome", mode="mixed", language=CommandLanguage.RU
    )

    assert commands_result.disposition is ParseDisposition.REJECTED
    assert commands_result.reason_code == "unknown_command"
    assert mixed_result.disposition is ParseDisposition.DICTATION


@pytest.mark.parametrize(
    ("text", "intent", "language"),
    [
        ("отмена", CommandIntent.CANCEL, CommandLanguage.RU),
        ("подтверждаю", CommandIntent.CONFIRM, CommandLanguage.RU),
        ("confirm command", CommandIntent.CONFIRM, CommandLanguage.EN),
        ("confirmo", CommandIntent.CONFIRM, CommandLanguage.ES),
        ("what can I say", CommandIntent.HELP, CommandLanguage.EN),
        ("ayuda", CommandIntent.HELP, CommandLanguage.ES),
        ("покажи номера", CommandIntent.SHOW_NUMBERS, CommandLanguage.RU),
        ("number the controls", CommandIntent.SHOW_NUMBERS, CommandLanguage.EN),
        ("muestra los números", CommandIntent.SHOW_NUMBERS, CommandLanguage.ES),
        ("повтори последнее", CommandIntent.REPEAT_LAST, CommandLanguage.RU),
        ("copy last text", CommandIntent.COPY_LAST, CommandLanguage.EN),
        ("repite lo último", CommandIntent.REPEAT_LAST, CommandLanguage.ES),
    ],
)
def test_exact_commands_are_canonical(
    text: str, intent: CommandIntent, language: CommandLanguage
) -> None:
    command = _command(text)

    assert command.intent is intent
    assert command.language is language
    assert command.risk is CommandRisk.SAFE
    assert command.policy is ExecutionPolicy.IMMEDIATE


@pytest.mark.parametrize(
    ("text", "keys", "risk"),
    (
        ("выдели всё", ("ctrl", "a"), CommandRisk.SAFE),
        ("select all", ("ctrl", "a"), CommandRisk.SAFE),
        ("selecciona todo", ("ctrl", "a"), CommandRisk.SAFE),
        ("copy selection", ("ctrl", "c"), CommandRisk.SAFE),
        ("undo", ("ctrl", "z"), CommandRisk.DESTRUCTIVE),
        ("rehacer", ("ctrl", "y"), CommandRisk.DESTRUCTIVE),
    ),
)
def test_natural_editing_shortcuts_are_exact_and_never_named_clicks(
    text: str, keys: tuple[str, ...], risk: CommandRisk
) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.PRESS_KEYS
    assert command.keys == keys
    assert command.risk is risk
    assert command.target is None


def test_confirmation_is_only_a_structured_intent_not_an_execution() -> None:
    result = parse_voice_input("command confirm", mode="mixed")

    assert result.disposition is ParseDisposition.COMMAND
    assert result.command is not None
    assert result.command.intent is CommandIntent.CONFIRM
    assert result.command.as_payload()["intent"] == "confirm"
    # Pending-command lookup and TTL enforcement intentionally belong to the executor.
    assert "pending" not in result.command.as_payload()


@pytest.mark.parametrize(
    "text",
    [
        "команда пронумеруй элементы",
        "command show numbers",
        "comando numera los elementos",
    ],
)
def test_show_numbers_is_safe_and_immediate_in_mixed_mode(text: str) -> None:
    result = parse_voice_input(text, mode="mixed")

    assert result.disposition is ParseDisposition.COMMAND
    assert result.command is not None
    assert result.command.intent is CommandIntent.SHOW_NUMBERS
    assert result.command.risk is CommandRisk.SAFE
    assert result.command.policy is ExecutionPolicy.IMMEDIATE


@pytest.mark.parametrize(
    ("text", "intent", "app", "language"),
    [
        ("запусти блокнот", CommandIntent.OPEN_APP, "блокнот", CommandLanguage.RU),
        ("switch to Visual Studio Code", CommandIntent.SWITCH_APP, "visual studio code", CommandLanguage.EN),
        ("cambia a Chrome", CommandIntent.SWITCH_APP, "chrome", CommandLanguage.ES),
        ("сверни Chrome", CommandIntent.MINIMIZE_APP, "chrome", CommandLanguage.RU),
        ("maximize window Chrome", CommandIntent.MAXIMIZE_APP, "chrome", CommandLanguage.EN),
        ("restaura la ventana Chrome", CommandIntent.MAXIMIZE_APP, "chrome", CommandLanguage.ES),
    ],
)
def test_app_commands_have_typed_target(
    text: str,
    intent: CommandIntent,
    app: str,
    language: CommandLanguage,
) -> None:
    command = _command(text)

    assert command.intent is intent
    assert command.app == app
    assert command.language is language


@pytest.mark.parametrize("text", ["сверни окно", "minimize window", "minimiza la ventana"])
def test_minimize_current_window_has_no_app_target(text: str) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.MINIMIZE_APP
    assert command.app is None


@pytest.mark.parametrize(
    ("text", "direction", "steps", "language"),
    [
        ("прокрути страницу вниз", ScrollDirection.DOWN, 1, CommandLanguage.RU),
        ("scroll up 3 times", ScrollDirection.UP, 3, CommandLanguage.EN),
        ("desplaza la página hacia abajo 2 veces", ScrollDirection.DOWN, 2, CommandLanguage.ES),
    ],
)
def test_scroll_commands_are_bounded_and_typed(
    text: str,
    direction: ScrollDirection,
    steps: int,
    language: CommandLanguage,
) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.SCROLL
    assert command.direction is direction
    assert command.scroll_steps == steps
    assert command.language is language


def test_excessive_scroll_is_rejected() -> None:
    result = parse_voice_input("scroll down 100 times", mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == "invalid_scroll_amount"
    assert result.execution_policy is ExecutionPolicy.DENY


@pytest.mark.parametrize(
    ("text", "number", "language"),
    [
        ("нажми номер 12", 12, CommandLanguage.RU),
        ("click number 7", 7, CommandLanguage.EN),
        ("haz clic en el número 4", 4, CommandLanguage.ES),
    ],
)
def test_number_overlay_selection(text: str, number: int, language: CommandLanguage) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.CLICK_NUMBER
    assert command.number == number
    assert command.language is language


@pytest.mark.parametrize("text", ["нажми номер 0", "click number 1000"])
def test_overlay_number_must_be_in_range(text: str) -> None:
    result = parse_voice_input(text, mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == "invalid_number"


@pytest.mark.parametrize(
    ("text", "target", "language"),
    [
        ("нажми на кнопку Сохранить", "сохранить", CommandLanguage.RU),
        ("click on Continue", "continue", CommandLanguage.EN),
        ("pulsa el botón Siguiente", "siguiente", CommandLanguage.ES),
    ],
)
def test_named_click_is_structured(text: str, target: str, language: CommandLanguage) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.CLICK_NAMED
    assert command.target == target
    assert command.language is language


@pytest.mark.parametrize(
    ("text", "risk"),
    [
        ("click Send", CommandRisk.SENSITIVE),
        ("нажми Оплатить", CommandRisk.SENSITIVE),
        ("pulsa Confirmar", CommandRisk.SENSITIVE),
        ("click Delete account", CommandRisk.DESTRUCTIVE),
        ("нажми Удалить", CommandRisk.DESTRUCTIVE),
        ("pulsa Borrar", CommandRisk.DESTRUCTIVE),
    ],
)
def test_sensitive_and_destructive_clicks_require_confirmation(
    text: str, risk: CommandRisk
) -> None:
    command = _command(text)

    assert command.risk is risk
    assert command.requires_confirmation
    assert command.policy is ExecutionPolicy.REQUIRE_CONFIRMATION


@pytest.mark.parametrize(
    ("text", "keys", "language"),
    [
        ("нажми клавиши контрол си", ("ctrl", "c"), CommandLanguage.RU),
        ("press Control plus C", ("ctrl", "c"), CommandLanguage.EN),
        ("pulsa las teclas Alt Tabulador", ("alt", "tab"), CommandLanguage.ES),
        ("press ctrl shift tab", ("ctrl", "shift", "tab"), CommandLanguage.EN),
    ],
)
def test_allowlisted_keys_are_canonical_and_immediate(
    text: str, keys: tuple[str, ...], language: CommandLanguage
) -> None:
    command = _command(text)

    assert command.intent is CommandIntent.PRESS_KEYS
    assert command.keys == keys
    assert command.language is language
    assert command.risk is CommandRisk.SAFE
    assert not command.requires_confirmation


@pytest.mark.parametrize(
    ("text", "keys"),
    (
        ("press Control Y", ("ctrl", "y")),
        ("pulsa Control Y", ("ctrl", "y")),
        ("press Control why", ("ctrl", "y")),
        ("pulsa Control i griega", ("ctrl", "y")),
        ("нажми клавиши контрол а", ("ctrl", "a")),
    ),
)
def test_spoken_letter_aliases_keep_redo_and_select_all_reachable(
    text: str, keys: tuple[str, ...]
) -> None:
    command = _command(text)

    assert command.keys == keys


@pytest.mark.parametrize(
    ("text", "risk"),
    [
        ("press Enter", CommandRisk.SENSITIVE),
        ("нажми клавишу пробел", CommandRisk.SENSITIVE),
        ("press Space", CommandRisk.SENSITIVE),
        ("pulsa Espacio", CommandRisk.SENSITIVE),
        ("pulsa Control V", CommandRisk.SENSITIVE),
        ("нажми клавишу бэкспейс", CommandRisk.DESTRUCTIVE),
        ("press Alt F4", CommandRisk.DESTRUCTIVE),
        ("press Control Z", CommandRisk.DESTRUCTIVE),
        ("нажми клавиши контрол игрек", CommandRisk.DESTRUCTIVE),
    ],
)
def test_risky_key_actions_require_confirmation(text: str, risk: CommandRisk) -> None:
    command = _command(text)

    assert command.risk is risk
    assert command.policy is ExecutionPolicy.REQUIRE_CONFIRMATION


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("press Windows R", "restricted_shortcut"),
        ("press Control Alt Delete", "restricted_shortcut"),
        ("press Control Q", "unsupported_key_combo"),
        ("нажми клавишу ж", "unsupported_key_combo"),
    ],
)
def test_non_allowlisted_shortcuts_are_denied(text: str, reason: str) -> None:
    result = parse_voice_input(text, mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == reason
    assert result.risk is CommandRisk.UNSUPPORTED
    assert result.execution_policy is ExecutionPolicy.DENY
    assert not result.is_executable


@pytest.mark.parametrize(
    "text",
    [
        "open PowerShell",
        "open Windows Terminal",
        "запусти командную строку",
        "abre el símbolo del sistema",
        "run command whoami",
        "открой настройки от имени администратора",
        "abre Regedit",
        "bypass UAC",
        "выключи компьютер",
    ],
)
def test_terminal_admin_uac_registry_and_system_requests_are_never_commands(
    text: str,
) -> None:
    result = parse_voice_input(text, mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == "restricted_operation"
    assert result.risk is CommandRisk.UNSUPPORTED
    assert result.execution_policy is ExecutionPolicy.DENY
    assert result.command is None


@pytest.mark.parametrize(
    "text",
    [
        r"open C:\Windows\notepad.exe",
        "open https://example.com",
        "open app && whoami",
        "open %COMSPEC%",
    ],
)
def test_paths_urls_and_shell_syntax_are_rejected(text: str) -> None:
    result = parse_voice_input(text, mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.reason_code == "restricted_syntax"
    assert result.execution_policy is ExecutionPolicy.DENY


@pytest.mark.parametrize(
    "text",
    [
        "please maybe open Chrome",
        "delete all files",
        "write a poem",
        "когда откроешь окно скажи",
    ],
)
def test_unknown_or_unanchored_phrases_do_not_fall_through_to_actions(text: str) -> None:
    result = parse_voice_input(text, mode="commands")

    assert result.disposition is ParseDisposition.REJECTED
    assert result.risk is CommandRisk.UNSUPPORTED
    assert result.execution_policy is ExecutionPolicy.DENY
    assert result.command is None


def test_command_payload_contains_only_canonical_fields_not_spoken_text() -> None:
    command = _command("open Chrome")
    payload = command.as_payload()

    assert payload["intent"] == "open_app"
    assert payload["app"] == "chrome"
    assert "original_text" not in payload
    assert "spoken_text" not in payload


def test_parser_is_stateless_and_reusable() -> None:
    parser = VoiceCommandParser()

    first = parser.parse("command help", mode="mixed")
    second = parser.parse("ordinary dictated text", mode="mixed")

    assert first.disposition is ParseDisposition.COMMAND
    assert second.disposition is ParseDisposition.DICTATION


def test_non_string_input_is_rejected_early() -> None:
    with pytest.raises(TypeError):
        parse_voice_input(None)  # type: ignore[arg-type]
