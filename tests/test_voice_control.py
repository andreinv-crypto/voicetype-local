from __future__ import annotations

from dataclasses import asdict

import pytest

from voicetype_local.session_editing import SessionEditAction
from voicetype_local.voice_commands import CommandRisk
from voicetype_local.voice_control import (
    VoiceControlRouter,
    VoiceRoute,
    VoiceRouteKind,
)
from voicetype_local.windows_control import CanonicalIntent, Sensitivity


def test_dictation_mode_never_creates_control_request() -> None:
    route = VoiceControlRouter().route(
        "открой Chrome", mode="dictation", language="ru"
    )

    assert route.kind is VoiceRouteKind.DICTATION
    assert route.dictation_text == "открой Chrome"
    assert route.request is None


def test_mixed_mode_requires_prefix_and_drops_command_transcript() -> None:
    router = VoiceControlRouter()
    ordinary = router.route("открой Chrome", mode="mixed", language="ru")
    command = router.route("команда открой Chrome", mode="mixed", language="ru")

    assert ordinary.kind is VoiceRouteKind.DICTATION
    assert command.kind is VoiceRouteKind.CONTROL
    assert command.dictation_text is None
    assert command.request is not None
    assert command.request.intent is CanonicalIntent.OPEN_APP
    assert command.request.app_id == "chrome"
    assert "команда" not in repr(asdict(command))


@pytest.mark.parametrize(
    ("text", "language", "body", "action_code"),
    (
        ("Напиши другу, нажать Enter", "ru", "Напиши другу", "press_enter"),
        ("See you soon, send message", "en", "See you soon", "send_message"),
        ("Nos vemos mañana, enviar mensaje", "es", "Nos vemos mañana", "send_message"),
        ("Текст press Enter", "auto", "Текст", "press_enter"),
    ),
)
def test_mixed_mode_supports_exact_action_only_at_dictation_end(
    text: str, language: str, body: str, action_code: str
) -> None:
    route = VoiceControlRouter().route(text, mode="mixed", language=language)

    assert route.kind is VoiceRouteKind.DICTATION_THEN_CONTROL
    assert route.dictation_text == body
    assert route.action_code == action_code
    assert route.request is not None
    assert route.request.intent is CanonicalIntent.SEND_KEY
    assert route.request.key == "enter"
    assert route.request.sensitivity is Sensitivity.SENSITIVE


def test_end_action_words_stay_text_in_dictation_mode_or_in_the_middle() -> None:
    router = VoiceControlRouter()

    dictation = router.route(
        "Напиши другу, нажать Enter", mode="dictation", language="ru"
    )
    middle = router.route(
        "Нажать Enter, а потом продолжить текст", mode="mixed", language="ru"
    )

    assert dictation.kind is VoiceRouteKind.DICTATION
    assert dictation.dictation_text == "Напиши другу, нажать Enter"
    assert middle.kind is VoiceRouteKind.DICTATION
    assert middle.dictation_text == "Нажать Enter, а потом продолжить текст"


def test_session_edit_requires_prefix_in_mixed_mode_and_is_typed() -> None:
    router = VoiceControlRouter()

    ordinary = router.route(
        "удали последнее предложение", mode="mixed", language="ru"
    )
    edit = router.route(
        "команда удали последнее предложение", mode="mixed", language="ru"
    )

    assert ordinary.kind is VoiceRouteKind.DICTATION
    assert edit.kind is VoiceRouteKind.SESSION_EDIT
    assert edit.edit_request is not None
    assert edit.edit_request.action is SessionEditAction.DELETE_LAST_SENTENCE
    assert edit.request is None
    assert edit.dictation_text is None


def test_whisper_sentence_break_after_prefix_routes_to_session_edit() -> None:
    route = VoiceControlRouter().route(
        "Команда. Удали последнее предложение.",
        mode="mixed",
        language="auto",
    )

    assert route.kind is VoiceRouteKind.SESSION_EDIT
    assert route.edit_request is not None
    assert route.edit_request.action is SessionEditAction.DELETE_LAST_SENTENCE


def test_polite_russian_edit_alias_routes_to_session_edit_without_transcript() -> None:
    route = VoiceControlRouter().route(
        "Команда, пожалуйста, убрать последнюю фразу.",
        mode="mixed",
        language="auto",
    )

    assert route.kind is VoiceRouteKind.SESSION_EDIT
    assert route.edit_request is not None
    assert route.edit_request.action is SessionEditAction.DELETE_LAST_SENTENCE
    assert route.dictation_text is None
    assert route.request is None


@pytest.mark.parametrize(
    "text",
    (
        "убрать последний текст",
        "пожалуйста поменяй последнее слово на готово",
    ),
)
def test_new_russian_edit_alias_stays_dictation_without_mixed_mode_prefix(
    text: str,
) -> None:
    route = VoiceControlRouter().route(text, mode="mixed", language="ru")

    assert route.kind is VoiceRouteKind.DICTATION
    assert route.dictation_text == text
    assert route.edit_request is None


def test_delete_everything_phrase_is_rejected_without_an_edit_request() -> None:
    route = VoiceControlRouter().route(
        "команда пожалуйста удали всё",
        mode="mixed",
        language="ru",
    )

    assert route.kind is VoiceRouteKind.REJECTED
    assert route.reason_code == "invalid_edit_command"
    assert route.edit_request is None


def test_malformed_session_edit_is_rejected_before_named_click_parser() -> None:
    route = VoiceControlRouter().route(
        "delete something somewhere", mode="commands", language="en"
    )

    assert route.kind is VoiceRouteKind.REJECTED
    assert route.reason_code == "invalid_edit_command"
    assert route.request is None


@pytest.mark.parametrize(
    ("text", "language"),
    (
        ("отправить сообщение", "ru"),
        ("send message", "en"),
        ("enviar mensaje", "es"),
    ),
)
def test_send_message_is_a_sensitive_standalone_control_command(
    text: str, language: str
) -> None:
    route = VoiceControlRouter().route(text, mode="commands", language=language)

    assert route.kind is VoiceRouteKind.CONTROL
    assert route.request is not None
    assert route.request.key == "enter"
    assert route.request.sensitivity is Sensitivity.SENSITIVE


@pytest.mark.parametrize(
    ("text", "intent", "field", "value"),
    (
        ("switch to calculator", CanonicalIntent.SWITCH_APP, "app_id", "calculator"),
        ("minimize window", CanonicalIntent.MINIMIZE_WINDOW, "app_id", None),
        ("scroll down 3 times", CanonicalIntent.SCROLL, "amount", 3),
        ("click Settings", CanonicalIntent.INVOKE_NAMED_ELEMENT, "element_name", "settings"),
        ("press Control plus C", CanonicalIntent.SEND_HOTKEY, "keys", ("ctrl", "c")),
        ("press Escape", CanonicalIntent.SEND_KEY, "key", "escape"),
    ),
)
def test_parser_intents_map_to_closed_executor_schema(
    text: str, intent: CanonicalIntent, field: str, value: object
) -> None:
    route = VoiceControlRouter().route(text, mode="commands", language="en")

    assert route.kind is VoiceRouteKind.CONTROL
    assert route.request is not None
    assert route.request.intent is intent
    assert getattr(route.request, field) == value


def test_click_number_requires_the_current_overlay_snapshot() -> None:
    router = VoiceControlRouter()

    stale = router.route("click number 7", mode="commands", language="en")
    current = router.route(
        "click number 7",
        mode="commands",
        language="en",
        snapshot_id="current-snapshot",
    )

    assert stale.kind is VoiceRouteKind.REJECTED
    assert stale.reason_code == "number_overlay_not_active"
    assert current.request is not None
    assert current.request.snapshot_id == "current-snapshot"
    assert current.request.element_number == 7


@pytest.mark.parametrize(
    ("text", "kind"),
    (
        ("help", VoiceRouteKind.HELP),
        ("show numbers", VoiceRouteKind.SHOW_NUMBERS),
        ("repeat last", VoiceRouteKind.REPEAT_LAST),
        ("copy last", VoiceRouteKind.COPY_LAST),
        ("cancel", VoiceRouteKind.CANCEL),
        ("confirm", VoiceRouteKind.CONFIRM),
    ),
)
def test_app_local_commands_have_no_desktop_request_or_spoken_text(
    text: str, kind: VoiceRouteKind
) -> None:
    route = VoiceControlRouter().route(text, mode="commands", language="en")

    assert route.kind is kind
    assert route.request is None
    assert route.dictation_text is None
    serialized = asdict(route)
    assert "original_text" not in serialized
    assert "normalized_text" not in serialized
    assert "spoken_text" not in serialized


def test_dangerous_command_retains_only_risk_and_typed_request() -> None:
    route = VoiceControlRouter().route(
        "press Control Z", mode="commands", language="en"
    )

    assert route.kind is VoiceRouteKind.CONTROL
    assert route.risk is CommandRisk.DESTRUCTIVE
    assert route.request is not None
    assert route.request.keys == ("ctrl", "z")
    assert route.request.sensitivity is Sensitivity.DESTRUCTIVE


@pytest.mark.parametrize(
    ("text", "risk", "sensitivity"),
    (
        ("open Chrome", CommandRisk.SAFE, None),
        ("click Send", CommandRisk.SENSITIVE, Sensitivity.SENSITIVE),
        (
            "click Delete account",
            CommandRisk.DESTRUCTIVE,
            Sensitivity.DESTRUCTIVE,
        ),
        ("press Enter", CommandRisk.SENSITIVE, Sensitivity.SENSITIVE),
    ),
)
def test_parser_risk_is_preserved_in_control_request_sensitivity(
    text: str,
    risk: CommandRisk,
    sensitivity: Sensitivity | None,
) -> None:
    route = VoiceControlRouter().route(text, mode="commands", language="en")

    assert route.kind is VoiceRouteKind.CONTROL
    assert route.risk is risk
    assert route.request is not None
    assert route.request.sensitivity is sensitivity


def test_rejected_shell_request_never_reaches_executor_schema() -> None:
    route = VoiceControlRouter().route(
        "open PowerShell", mode="commands", language="en"
    )

    assert route.kind is VoiceRouteKind.REJECTED
    assert route.request is None
    assert route.reason_code == "restricted_operation"


def test_route_invariants_reject_accidental_private_or_mixed_payloads() -> None:
    with pytest.raises(ValueError):
        VoiceRoute(VoiceRouteKind.HELP, dictation_text="private speech")
    with pytest.raises(ValueError):
        VoiceRoute(VoiceRouteKind.CONTROL)
    with pytest.raises(ValueError):
        VoiceRoute(
            VoiceRouteKind.DICTATION_THEN_CONTROL,
            dictation_text="text",
        )
