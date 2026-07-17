from __future__ import annotations

"""Pure routing boundary between speech parsing and Windows execution.

The router has no desktop side effects.  It converts an already recognised
command into the narrow :mod:`windows_control` request schema, while ordinary
dictation remains text.  Spoken command text is deliberately not retained in a
control route.
"""

import enum
from dataclasses import dataclass

from .voice_commands import (
    CommandIntent,
    CommandLanguage,
    CommandRisk,
    ParseDisposition,
    VoiceCommand,
    VoiceCommandParser,
    VoiceMode,
)
from .windows_control import CanonicalIntent, ControlRequest, Sensitivity


class VoiceRouteKind(enum.StrEnum):
    EMPTY = "empty"
    DICTATION = "dictation"
    REJECTED = "rejected"
    CONTROL = "control"
    HELP = "help"
    SHOW_NUMBERS = "show_numbers"
    REPEAT_LAST = "repeat_last"
    COPY_LAST = "copy_last"
    CANCEL = "cancel"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class VoiceRoute:
    kind: VoiceRouteKind
    dictation_text: str | None = None
    request: ControlRequest | None = None
    reason_code: str | None = None
    risk: CommandRisk | None = None

    def __post_init__(self) -> None:
        if self.kind is VoiceRouteKind.DICTATION:
            if self.dictation_text is None or self.request is not None:
                raise ValueError("dictation routes contain text only")
        elif self.kind is VoiceRouteKind.CONTROL:
            if self.request is None or self.dictation_text is not None:
                raise ValueError("control routes contain a canonical request only")
        elif self.dictation_text is not None or self.request is not None:
            raise ValueError("local/rejected routes cannot retain speech or requests")


_LOCAL_INTENTS: dict[CommandIntent, VoiceRouteKind] = {
    CommandIntent.HELP: VoiceRouteKind.HELP,
    CommandIntent.SHOW_NUMBERS: VoiceRouteKind.SHOW_NUMBERS,
    CommandIntent.REPEAT_LAST: VoiceRouteKind.REPEAT_LAST,
    CommandIntent.COPY_LAST: VoiceRouteKind.COPY_LAST,
    CommandIntent.CANCEL: VoiceRouteKind.CANCEL,
    CommandIntent.CONFIRM: VoiceRouteKind.CONFIRM,
}


def _control_request(
    command: VoiceCommand, *, snapshot_id: str | None
) -> ControlRequest | None:
    sensitivity = {
        CommandRisk.SAFE: None,
        CommandRisk.SENSITIVE: Sensitivity.SENSITIVE,
        CommandRisk.DESTRUCTIVE: Sensitivity.DESTRUCTIVE,
    }.get(command.risk)
    if command.risk is not CommandRisk.SAFE and sensitivity is None:
        raise ValueError("unsupported command risk cannot reach the executor schema")
    if command.intent is CommandIntent.OPEN_APP:
        return ControlRequest(
            CanonicalIntent.OPEN_APP,
            app_id=command.app,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.SWITCH_APP:
        return ControlRequest(
            CanonicalIntent.SWITCH_APP,
            app_id=command.app,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.MINIMIZE_APP:
        return ControlRequest(
            CanonicalIntent.MINIMIZE_WINDOW,
            app_id=command.app,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.MAXIMIZE_APP:
        return ControlRequest(
            CanonicalIntent.MAXIMIZE_WINDOW,
            app_id=command.app,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.SCROLL:
        return ControlRequest(
            CanonicalIntent.SCROLL,
            direction=command.direction.value if command.direction else None,
            amount=command.scroll_steps,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.CLICK_NAMED:
        return ControlRequest(
            CanonicalIntent.INVOKE_NAMED_ELEMENT,
            element_name=command.target,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.CLICK_NUMBER:
        if not snapshot_id:
            return None
        return ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=command.number,
            snapshot_id=snapshot_id,
            sensitivity=sensitivity,
        )
    if command.intent is CommandIntent.PRESS_KEYS:
        if len(command.keys) == 1:
            return ControlRequest(
                CanonicalIntent.SEND_KEY,
                key=command.keys[0],
                sensitivity=sensitivity,
            )
        return ControlRequest(
            CanonicalIntent.SEND_HOTKEY,
            keys=command.keys,
            sensitivity=sensitivity,
        )
    return None


class VoiceControlRouter:
    """Stateless, testable routing for Dictation / Commands / Mixed modes."""

    def __init__(self, parser: VoiceCommandParser | None = None) -> None:
        self.parser = parser or VoiceCommandParser()

    def route(
        self,
        text: str,
        *,
        mode: VoiceMode | str,
        language: CommandLanguage | str = CommandLanguage.AUTO,
        snapshot_id: str | None = None,
    ) -> VoiceRoute:
        try:
            command_language = CommandLanguage(language)
        except ValueError:
            command_language = CommandLanguage.AUTO
        parsed = self.parser.parse(text, mode=mode, language=command_language)
        if parsed.disposition is ParseDisposition.EMPTY:
            return VoiceRoute(VoiceRouteKind.EMPTY, reason_code=parsed.reason_code)
        if parsed.disposition is ParseDisposition.DICTATION:
            return VoiceRoute(VoiceRouteKind.DICTATION, dictation_text=text)
        if parsed.disposition is ParseDisposition.REJECTED or parsed.command is None:
            return VoiceRoute(
                VoiceRouteKind.REJECTED,
                reason_code=parsed.reason_code or "unknown_command",
                risk=parsed.risk,
            )

        command = parsed.command
        local_kind = _LOCAL_INTENTS.get(command.intent)
        if local_kind is not None:
            return VoiceRoute(local_kind, risk=command.risk)
        request = _control_request(command, snapshot_id=snapshot_id)
        if request is None:
            reason = (
                "number_overlay_not_active"
                if command.intent is CommandIntent.CLICK_NUMBER
                else "unsupported_intent"
            )
            return VoiceRoute(
                VoiceRouteKind.REJECTED,
                reason_code=reason,
                risk=command.risk,
            )
        return VoiceRoute(
            VoiceRouteKind.CONTROL,
            request=request,
            risk=command.risk,
        )


__all__ = [
    "VoiceControlRouter",
    "VoiceRoute",
    "VoiceRouteKind",
]
