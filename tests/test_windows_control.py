from __future__ import annotations

from dataclasses import asdict, replace
from inspect import signature

import pytest

import voicetype_local.windows_control as windows_control
from voicetype_local.windows_control import (
    ApplicationRegistry,
    BackendActionResult,
    BackendStatus,
    CanonicalIntent,
    ConfirmationChallenge,
    ControlRequest,
    ElementBounds,
    ForegroundWindowGate,
    KnownApplication,
    NumberedElement,
    NumberedElementSnapshot,
    ResultStatus,
    ScrollDirection,
    Sensitivity,
    UiaControlBackend,
    WindowAction,
    WindowsControlExecutor,
)


def _registry() -> ApplicationRegistry:
    return ApplicationRegistry(
        (
            KnownApplication(
                "notepad",
                r"C:\Windows\System32\notepad.exe",
                aliases=("блокнот",),
            ),
            KnownApplication(
                "calculator",
                r"C:\Windows\System32\calc.exe",
                aliases=("калькулятор",),
            ),
        )
    )


class _Native:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.outcome = BackendActionResult.success("fake_native")
        self.gate_token = "gate-a"
        self.gate_outcome: BackendActionResult | None = None

    def foreground_gate(
        self,
    ) -> tuple[ForegroundWindowGate | None, BackendActionResult | None]:
        if self.gate_outcome is not None:
            return None, self.gate_outcome
        return ForegroundWindowGate(self.gate_token), None

    def open_application(self, application: KnownApplication) -> BackendActionResult:
        self.calls.append(("open", application))
        return self.outcome

    def switch_application(self, application: KnownApplication) -> BackendActionResult:
        self.calls.append(("switch", application))
        return self.outcome

    def window_action(
        self, action: WindowAction, application: KnownApplication | None
    ) -> BackendActionResult:
        self.calls.append(("window", action, application))
        return self.outcome

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult:
        self.calls.append(("scroll", direction, amount))
        return self.outcome

    def send_key(
        self,
        key: str,
        expected_gate_token: str | None = None,
        input_guard=None,
    ) -> BackendActionResult:
        if expected_gate_token is not None and expected_gate_token != self.gate_token:
            return BackendActionResult.blocked("fake_native", "foreground_changed")
        if input_guard is not None and not input_guard():
            return BackendActionResult.blocked(
                "fake_native", "input_guard_rejected"
            )
        self.calls.append(("key", key))
        return self.outcome

    def send_hotkey(
        self,
        keys: tuple[str, ...],
        expected_gate_token: str | None = None,
        input_guard=None,
    ) -> BackendActionResult:
        if expected_gate_token is not None and expected_gate_token != self.gate_token:
            return BackendActionResult.blocked("fake_native", "foreground_changed")
        if input_guard is not None and not input_guard():
            return BackendActionResult.blocked(
                "fake_native", "input_guard_rejected"
            )
        self.calls.append(("hotkey", keys))
        return self.outcome


class _Uia:
    def __init__(self, snapshot: NumberedElementSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[object, ...]] = []
        self.scroll_outcome = BackendActionResult.unsupported(
            "fake_uia", "scroll_pattern_unavailable"
        )
        self.invoke_outcome = BackendActionResult.success("fake_uia")
        self.snapshot_current = True

    def capture_elements(self, gate_token: str) -> NumberedElementSnapshot | None:
        self.calls.append(("capture", gate_token))
        if self.snapshot is None:
            return None
        if self.snapshot.gate_token != gate_token:
            self.snapshot = replace(self.snapshot, gate_token=gate_token)
        return self.snapshot

    def clear_snapshot(self) -> None:
        self.calls.append(("clear",))
        self.snapshot = None

    def invoke(self, snapshot_id: str, number: int) -> BackendActionResult:
        self.calls.append(("invoke", snapshot_id, number))
        return self.invoke_outcome

    def is_snapshot_current(self, snapshot_id: str) -> bool:
        self.calls.append(("current", snapshot_id))
        return self.snapshot_current

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult:
        self.calls.append(("scroll", direction, amount))
        return self.scroll_outcome


class _Policy:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.challenges: list[ConfirmationChallenge] = []

    def confirm(self, challenge: ConfirmationChallenge) -> bool:
        self.challenges.append(challenge)
        return self.allow


def _snapshot() -> NumberedElementSnapshot:
    return NumberedElementSnapshot(
        "snapshot-1",
        "window-1",
        (
            NumberedElement(
                1,
                "Settings",
                "ButtonControl",
                ElementBounds(10, 20, 100, 30),
            ),
            NumberedElement(
                2,
                "Delete account",
                "ButtonControl",
                ElementBounds(10, 60, 100, 30),
                destructive=True,
            ),
            NumberedElement(3, "Settings", "ButtonControl"),
        ),
        "gate-a",
    )


def _executor(
    *,
    native: _Native | None = None,
    uia: _Uia | None = None,
    policy: _Policy | None = None,
) -> WindowsControlExecutor:
    effective_native = native if native is not None else (_Native() if uia is not None else None)
    return WindowsControlExecutor(
        registry=_registry(),
        native_backend=effective_native,
        uia_backend=uia,
        confirmation_policy=policy,
    )


def test_help_and_cancel_are_true_noops() -> None:
    native = _Native()
    executor = _executor(native=native)

    help_result = executor.execute(ControlRequest(CanonicalIntent.HELP))
    cancel_result = executor.execute(ControlRequest(CanonicalIntent.CANCEL))

    assert help_result.status is ResultStatus.NOOP
    assert cancel_result.status is ResultStatus.NOOP
    assert native.calls == []


def test_open_app_resolves_only_fixed_allowlist_entry() -> None:
    native = _Native()
    executor = _executor(native=native)

    result = executor.execute(
        ControlRequest(CanonicalIntent.OPEN_APP, app_id="блокнот")
    )

    assert result.status is ResultStatus.EXECUTED
    assert result.target_id == "notepad"
    operation, app = native.calls[0]
    assert operation == "open"
    assert isinstance(app, KnownApplication)
    assert app.executable == r"C:\Windows\System32\notepad.exe"
    assert app.arguments == ()

    rejected = executor.execute(
        ControlRequest(CanonicalIntent.OPEN_APP, app_id="cmd /c whoami")
    )
    assert rejected.status is ResultStatus.REJECTED
    assert rejected.reason_code == "application_not_allowlisted"
    assert len(native.calls) == 1


@pytest.mark.parametrize(
    "application",
    (
        KnownApplication("cmd", r"C:\Windows\System32\cmd.exe"),
        KnownApplication("powershell", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
        KnownApplication("relative", "notepad.exe"),
    ),
)
def test_registry_rejects_terminal_admin_and_relative_executables(
    application: KnownApplication,
) -> None:
    with pytest.raises(ValueError):
        ApplicationRegistry((application,))


def test_switch_and_window_actions_use_known_or_current_window() -> None:
    native = _Native()
    executor = _executor(native=native)

    switched = executor.execute(
        ControlRequest(CanonicalIntent.SWITCH_APP, app_id="calculator")
    )
    minimized = executor.execute(ControlRequest(CanonicalIntent.MINIMIZE_WINDOW))
    maximized = executor.execute(
        ControlRequest(CanonicalIntent.MAXIMIZE_WINDOW, app_id="notepad")
    )

    assert switched.status is ResultStatus.EXECUTED
    assert minimized.target_id == "current"
    assert maximized.target_id == "notepad"
    assert native.calls[0][0] == "switch"
    assert native.calls[1] == ("window", WindowAction.MINIMIZE, None)
    assert native.calls[2][0:2] == ("window", WindowAction.MAXIMIZE)


def test_backend_block_for_admin_or_terminal_maps_to_rejection() -> None:
    native = _Native()
    native.outcome = BackendActionResult.blocked(
        "fake_native", "terminal_or_admin_prohibited"
    )
    executor = _executor(native=native)

    result = executor.execute(ControlRequest(CanonicalIntent.MINIMIZE_WINDOW))

    assert result.status is ResultStatus.REJECTED
    assert result.reason_code == "terminal_or_admin_prohibited"


def test_scroll_uses_uia_first_and_falls_back_only_when_bounded() -> None:
    native = _Native()
    uia = _Uia()
    uia.scroll_outcome = BackendActionResult.success("fake_uia")
    executor = _executor(native=native, uia=uia)

    semantic = executor.execute(
        ControlRequest(CanonicalIntent.SCROLL, direction="down", amount=2)
    )

    assert semantic.status is ResultStatus.EXECUTED
    assert semantic.backend == "fake_uia"
    assert native.calls == []

    uia.scroll_outcome = BackendActionResult.unsupported("fake_uia")
    fallback = executor.execute(
        ControlRequest(CanonicalIntent.SCROLL, direction=ScrollDirection.UP, amount=3)
    )
    assert fallback.status is ResultStatus.EXECUTED
    assert native.calls == [("scroll", ScrollDirection.UP, 3)]

    rejected = executor.execute(
        ControlRequest(CanonicalIntent.SCROLL, direction="down", amount=11)
    )
    assert rejected.status is ResultStatus.REJECTED
    assert rejected.reason_code == "scroll_out_of_bounds"
    assert len(native.calls) == 1


def test_safe_key_executes_but_enter_requires_trusted_policy() -> None:
    native = _Native()
    denied_policy = _Policy(False)
    executor = _executor(native=native, policy=denied_policy)

    safe = executor.execute(ControlRequest(CanonicalIntent.SEND_KEY, key="Page Down"))
    denied = executor.execute(ControlRequest(CanonicalIntent.SEND_KEY, key="Enter"))

    assert safe.status is ResultStatus.EXECUTED
    assert native.calls == [("key", "PAGEDOWN")]
    assert denied.status is ResultStatus.CONFIRMATION_REQUIRED
    assert denied.confirmation_required is True
    assert denied_policy.challenges[0].sensitivity is Sensitivity.SENSITIVE

    allowed_policy = _Policy(True)
    allowed_executor = _executor(native=native, policy=allowed_policy)
    allowed = allowed_executor.execute(
        ControlRequest(CanonicalIntent.SEND_KEY, key="Return")
    )
    assert allowed.status is ResultStatus.EXECUTED
    assert native.calls[-1] == ("key", "ENTER")


@pytest.mark.parametrize(
    ("spoken_key", "canonical"),
    (("escape", "ESCAPE"), ("f1", "F1"), ("page_up", "PAGEUP")),
)
def test_safe_single_keys_match_voice_parser(
    spoken_key: str, canonical: str
) -> None:
    native = _Native()
    result = _executor(native=native).execute(
        ControlRequest(CanonicalIntent.SEND_KEY, key=spoken_key)
    )

    assert result.status is ResultStatus.EXECUTED
    assert native.calls == [("key", canonical)]


def test_space_requires_confirmation_because_it_can_activate_focused_button() -> None:
    native = _Native()
    policy = _Policy(False)

    result = _executor(native=native, policy=policy).execute(
        ControlRequest(CanonicalIntent.SEND_KEY, key="space")
    )

    assert result.status is ResultStatus.CONFIRMATION_REQUIRED
    assert policy.challenges[0].sensitivity is Sensitivity.SENSITIVE
    assert native.calls == []


def test_hotkeys_are_canonicalized_and_unsafe_combinations_are_rejected() -> None:
    native = _Native()
    executor = _executor(native=native)

    copied = executor.execute(
        ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=("c", "control"))
    )
    terminal = executor.execute(
        ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=("win", "r"))
    )
    close = executor.execute(
        ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=("alt", "f4"))
    )

    assert copied.status is ResultStatus.EXECUTED
    assert native.calls == [("hotkey", ("CTRL", "C"))]
    assert terminal.reason_code == "unsafe_hotkey"
    assert close.status is ResultStatus.CONFIRMATION_REQUIRED
    assert len(native.calls) == 1


def test_safe_hotkeys_match_the_existing_voice_parser_allowlist() -> None:
    native = _Native()
    executor = _executor(native=native)
    combinations = (
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
    )

    results = [
        executor.execute(ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=keys))
        for keys in combinations
    ]

    assert all(result.status is ResultStatus.EXECUTED for result in results)


def test_destructive_hotkey_needs_confirmation() -> None:
    native = _Native()
    policy = _Policy(False)
    executor = _executor(native=native, policy=policy)

    result = executor.execute(
        ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=("ctrl", "x"))
    )

    assert result.status is ResultStatus.CONFIRMATION_REQUIRED
    assert native.calls == []
    assert policy.challenges[0].sensitivity is Sensitivity.DESTRUCTIVE


def test_confirmed_flag_is_one_shot_and_only_for_matching_pending_intent() -> None:
    native = _Native()
    executor = _executor(native=native)

    pending = executor.execute_action("send_key", {"key": "enter"})
    wrong = executor.execute_action(
        "send_key", {"key": "delete"}, confirmed=True
    )

    assert pending.status is ResultStatus.CONFIRMATION_REQUIRED
    assert wrong.status is ResultStatus.REJECTED
    assert wrong.reason_code == "no_matching_pending_confirmation"
    assert native.calls == []

    # The mismatched attempt did not execute and the different intent expired
    # the old pending action, so Enter must be requested once more.
    pending_again = executor.execute_action("send_key", {"key": "enter"})
    confirmed = executor.execute_action(
        "send_key", {"key": "enter"}, confirmed=True
    )
    replay = executor.execute_action("send_key", {"key": "enter"}, confirmed=True)

    assert pending_again.status is ResultStatus.CONFIRMATION_REQUIRED
    assert confirmed.status is ResultStatus.EXECUTED
    assert native.calls == [("key", "ENTER")]
    assert replay.status is ResultStatus.REJECTED
    assert replay.reason_code == "no_matching_pending_confirmation"


def test_executor_confirmation_expires_without_app_timer() -> None:
    native = _Native()
    now = [100.0]
    executor = WindowsControlExecutor(
        registry=_registry(),
        native_backend=native,
        uia_backend=None,
        confirmation_ttl_seconds=5.0,
        clock=lambda: now[0],
    )
    request = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")

    pending = executor.execute(request)
    now[0] = 105.01
    confirmed = executor.execute(request, confirmed=True)
    replay = executor.execute(request, confirmed=True)

    assert pending.status is ResultStatus.CONFIRMATION_REQUIRED
    assert confirmed.status is ResultStatus.REJECTED
    assert confirmed.reason_code == "confirmation_expired"
    assert replay.reason_code == "no_matching_pending_confirmation"
    assert native.calls == []


def test_action_adapter_rejects_unknown_fields_and_never_accepts_commands() -> None:
    native = _Native()
    executor = _executor(native=native)

    unknown = executor.execute_action("launch_shell", {"command": "whoami"})
    injected = executor.execute_action(
        "open_app",
        {"app_id": "notepad", "command": "cmd /c whoami"},
    )

    assert unknown.status is ResultStatus.REJECTED
    assert injected.status is ResultStatus.REJECTED
    assert native.calls == []


def test_named_element_requires_exact_unique_match_and_confirmation() -> None:
    uia = _Uia(_snapshot())
    policy = _Policy(False)
    executor = _executor(uia=uia, policy=policy)

    ambiguous = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NAMED_ELEMENT,
            element_name="Settings",
        )
    )
    denied = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NAMED_ELEMENT,
            element_name="Delete account",
        )
    )

    assert ambiguous.status is ResultStatus.REJECTED
    assert ambiguous.reason_code == "element_name_ambiguous"
    assert denied.status is ResultStatus.CONFIRMATION_REQUIRED
    assert denied.target_id == "element:snapshot-1:2"
    assert policy.challenges[-1].sensitivity is Sensitivity.DESTRUCTIVE
    assert not any(call[0] == "invoke" for call in uia.calls)


def test_numbered_element_requires_matching_snapshot_and_approved_policy() -> None:
    snapshot = _snapshot()
    uia = _Uia(snapshot)
    policy = _Policy(True)
    executor = _executor(uia=uia, policy=policy)
    captured = executor.capture_numbered_elements()
    assert captured is snapshot

    stale = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=1,
            snapshot_id="old",
        )
    )
    executed = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=1,
            snapshot_id=snapshot.snapshot_id,
        )
    )

    assert stale.status is ResultStatus.REJECTED
    assert stale.reason_code == "stale_or_missing_snapshot"
    assert executed.status is ResultStatus.EXECUTED
    assert ("invoke", snapshot.snapshot_id, 1) in uia.calls


def test_safe_element_executes_without_confirmation_challenge() -> None:
    snapshot = _snapshot()
    uia = _Uia(snapshot)
    policy = _Policy(False)
    executor = _executor(uia=uia, policy=policy)
    executor.capture_numbered_elements()

    result = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=1,
            snapshot_id=snapshot.snapshot_id,
        )
    )

    assert result.status is ResultStatus.EXECUTED
    assert policy.challenges == []
    assert ("invoke", snapshot.snapshot_id, 1) in uia.calls


def _single_element_snapshot(
    snapshot_id: str,
    window_id: str,
    name: str,
    *,
    destructive: bool,
    gate_token: str = "gate-a",
) -> NumberedElementSnapshot:
    return NumberedElementSnapshot(
        snapshot_id,
        window_id,
        (
            NumberedElement(
                1,
                name,
                "ButtonControl",
                ElementBounds(10, 20, 120, 32),
                destructive=destructive,
                runtime_id=(7, 1),
            ),
        ),
        gate_token,
    )


def test_named_confirmation_cannot_move_to_same_named_button_in_new_window() -> None:
    native = _Native()
    window_a = _single_element_snapshot(
        "snapshot-a", "window-a", "Send", destructive=True
    )
    window_b = _single_element_snapshot(
        "snapshot-b", "window-b", "Send", destructive=True, gate_token="gate-b"
    )
    uia = _Uia(window_a)
    executor = _executor(native=native, uia=uia, policy=_Policy(False))
    request = ControlRequest(
        CanonicalIntent.INVOKE_NAMED_ELEMENT, element_name="Send"
    )

    pending = executor.execute(request)
    native.gate_token = "gate-b"
    uia.snapshot = window_b
    confirmed = executor.execute(request, confirmed=True)
    replay = executor.execute(request, confirmed=True)

    assert pending.status is ResultStatus.CONFIRMATION_REQUIRED
    assert pending.target_id == "element:snapshot-a:1"
    assert confirmed.status is ResultStatus.REJECTED
    assert confirmed.reason_code == "foreground_changed"
    assert replay.reason_code == "no_matching_pending_confirmation"
    assert not any(call[0] == "invoke" for call in uia.calls)


def test_overwritten_named_snapshot_rejects_without_creating_another_prompt() -> None:
    native = _Native()
    uia = _Uia(
        _single_element_snapshot(
            "snapshot-a", "window-a", "Send", destructive=True
        )
    )
    policy = _Policy(False)
    executor = _executor(native=native, uia=uia, policy=policy)
    request = ControlRequest(
        CanonicalIntent.INVOKE_NAMED_ELEMENT, element_name="Send"
    )

    assert executor.execute(request).status is ResultStatus.CONFIRMATION_REQUIRED
    uia.snapshot = _single_element_snapshot(
        "snapshot-b", "window-a", "Send", destructive=True
    )
    assert executor.capture_numbered_elements() is not None
    confirmed = executor.execute(request, confirmed=True)

    assert confirmed.status is ResultStatus.REJECTED
    assert confirmed.reason_code == "no_matching_pending_confirmation"
    assert len(policy.challenges) == 1
    assert not any(call[0] == "invoke" for call in uia.calls)


def test_sensitive_key_confirmation_is_bound_to_foreground_identity() -> None:
    native = _Native()
    executor = _executor(native=native)
    request = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")

    assert executor.execute(request).status is ResultStatus.CONFIRMATION_REQUIRED
    native.gate_token = "gate-b"
    confirmed = executor.execute(request, confirmed=True)

    assert confirmed.status is ResultStatus.REJECTED
    assert confirmed.reason_code == "no_matching_pending_confirmation"
    assert native.calls == []


def test_sensitive_key_rechecks_bound_foreground_inside_input_backend() -> None:
    class _SwitchingNative(_Native):
        def send_key(
            self, key: str, expected_gate_token: str | None = None
        ) -> BackendActionResult:
            self.gate_token = "gate-b"
            return super().send_key(key, expected_gate_token)

    native = _SwitchingNative()
    executor = _executor(native=native)
    request = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")

    assert executor.execute(request).status is ResultStatus.CONFIRMATION_REQUIRED
    confirmed = executor.execute(request, confirmed=True)

    assert confirmed.status is ResultStatus.REJECTED
    assert confirmed.reason_code == "foreground_changed"
    assert native.calls == []


def test_parser_sensitivity_cannot_be_lost_on_safe_named_element() -> None:
    snapshot = _single_element_snapshot(
        "snapshot-a", "window-a", "Continue", destructive=False
    )
    policy = _Policy(False)
    result = _executor(uia=_Uia(snapshot), policy=policy).execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NAMED_ELEMENT,
            element_name="Continue",
            sensitivity=Sensitivity.SENSITIVE,
        )
    )

    assert result.status is ResultStatus.CONFIRMATION_REQUIRED
    assert policy.challenges[0].sensitivity is Sensitivity.SENSITIVE


@pytest.mark.parametrize(
    "name",
    (
        "Confirm",
        "Authorize",
        "Share",
        "Publish",
        "Sign in",
        "Sign out",
        "Подтвердить",
        "Опубликовать",
        "Войти",
        "Confirmar",
        "Compartir",
        "Publicar",
        "Iniciar sesion",
        "Iniciar sesión",
        "Cerrar sesión",
        "Acceder",
        "Enviar",
    ),
)
def test_sensitive_uia_names_require_confirmation(name: str) -> None:
    assert windows_control._looks_destructive(name) is True
    snapshot = _single_element_snapshot(
        "snapshot-a", "window-a", name, destructive=True
    )
    policy = _Policy(False)
    executor = _executor(uia=_Uia(snapshot), policy=policy)
    captured = executor.capture_numbered_elements()
    assert captured is not None

    result = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=1,
            snapshot_id=captured.snapshot_id,
        )
    )

    assert result.status is ResultStatus.CONFIRMATION_REQUIRED
    assert policy.challenges[0].sensitivity is Sensitivity.DESTRUCTIVE


@pytest.mark.parametrize("role", ("HyperlinkControl", "MenuItemControl"))
def test_unknown_external_uia_roles_require_confirmation(role: str) -> None:
    snapshot = NumberedElementSnapshot(
        "snapshot-a",
        "window-a",
        (
            NumberedElement(
                1,
                "Neutral label",
                role,
                ElementBounds(10, 20, 120, 32),
                runtime_id=(7, 1),
            ),
        ),
        "gate-a",
    )
    policy = _Policy(False)
    executor = _executor(uia=_Uia(snapshot), policy=policy)
    captured = executor.capture_numbered_elements()
    assert captured is not None

    result = executor.execute(
        ControlRequest(
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            element_number=1,
            snapshot_id=captured.snapshot_id,
        )
    )

    assert result.status is ResultStatus.CONFIRMATION_REQUIRED
    assert policy.challenges[0].sensitivity is Sensitivity.SENSITIVE


@pytest.mark.parametrize(
    "name",
    (
        "Run as administrator",
        "Open terminal",
        "Checkout",
        "От имени администратора",
        "Открыть терминал",
        "Como administrador",
        "Finalizar compra",
    ),
)
def test_indirect_admin_terminal_and_checkout_names_are_sensitive(name: str) -> None:
    assert windows_control._looks_destructive(name) is True


def test_uia_requires_available_safe_win32_foreground_gate() -> None:
    uia = _Uia(_snapshot())
    executor = WindowsControlExecutor(
        registry=_registry(), native_backend=None, uia_backend=uia
    )

    assert executor.capture_numbered_elements() is None
    scroll = executor.execute(
        ControlRequest(CanonicalIntent.SCROLL, direction="down")
    )

    assert scroll.status is ResultStatus.REJECTED
    assert scroll.reason_code == "foreground_gate_unavailable"
    assert not any(call[0] in {"capture", "scroll"} for call in uia.calls)


def test_clear_snapshot_releases_executor_and_backend_element_names() -> None:
    uia = _Uia(_snapshot())
    executor = _executor(uia=uia)
    snapshot = executor.capture_numbered_elements()
    assert snapshot is not None

    executor.clear_snapshot()

    assert executor.is_snapshot_current(snapshot.snapshot_id) is False
    assert uia.snapshot is None
    assert uia.calls[-1] == ("clear",)


def test_cancel_epoch_prevents_late_confirmation_from_becoming_pending() -> None:
    native = _Native()

    class _CancellingPolicy:
        executor: WindowsControlExecutor

        def confirm(self, _challenge: ConfirmationChallenge) -> bool:
            self.executor.cancel_pending()
            return False

    policy = _CancellingPolicy()
    executor = _executor(native=native, policy=policy)  # type: ignore[arg-type]
    policy.executor = executor

    result = executor.execute(ControlRequest(CanonicalIntent.SEND_KEY, key="enter"))
    confirmed = executor.execute(
        ControlRequest(CanonicalIntent.SEND_KEY, key="enter"), confirmed=True
    )

    assert result.status is ResultStatus.REJECTED
    assert result.reason_code == "request_cancelled"
    assert confirmed.reason_code == "no_matching_pending_confirmation"
    assert native.calls == []


def test_elevated_controller_fails_closed_before_touching_target_window() -> None:
    backend = windows_control.Win32ControlBackend.__new__(
        windows_control.Win32ControlBackend
    )
    backend._controller_elevated = True

    blocked = backend._safe_window(1)

    assert blocked is not None
    assert blocked.reason_code == "controller_elevated_or_unknown"


def test_native_input_guard_runs_immediately_before_sendinput() -> None:
    events: list[str] = []

    class _User32:
        @staticmethod
        def SendInput(count, _inputs, _size):
            events.append("sendinput")
            return count

    backend = windows_control.Win32ControlBackend.__new__(
        windows_control.Win32ControlBackend
    )
    backend._user32 = _User32()

    def guard() -> bool:
        events.append("guard")
        return True

    result = backend._send((windows_control._INPUT(),), guard)

    assert result.status is BackendStatus.SUCCESS
    assert events == ["guard", "sendinput"]


@pytest.mark.parametrize("raises", (False, True))
def test_native_input_guard_blocks_without_sendinput(raises: bool) -> None:
    sendinput_calls: list[int] = []

    class _User32:
        @staticmethod
        def SendInput(count, _inputs, _size):
            sendinput_calls.append(count)
            return count

    backend = windows_control.Win32ControlBackend.__new__(
        windows_control.Win32ControlBackend
    )
    backend._user32 = _User32()

    def guard() -> bool:
        if raises:
            raise RuntimeError("focus probe failed")
        return False

    result = backend._send((windows_control._INPUT(),), guard)

    assert result.status is BackendStatus.BLOCKED
    assert result.reason_code == (
        "input_guard_failed" if raises else "input_guard_rejected"
    )
    assert sendinput_calls == []


@pytest.mark.parametrize(
    "control_request",
    (
        ControlRequest(CanonicalIntent.SEND_KEY, key="down"),
        ControlRequest(CanonicalIntent.SEND_HOTKEY, keys=("ctrl", "c")),
    ),
)
def test_executor_maps_native_input_guard_rejection_without_action(
    control_request: ControlRequest,
) -> None:
    native = _Native()
    executor = _executor(native=native)

    result = executor.execute(control_request, input_guard=lambda: False)

    assert result.status is ResultStatus.REJECTED
    assert result.reason_code == "input_guard_rejected"
    assert native.calls == []


def test_result_is_metadata_only() -> None:
    native = _Native()
    executor = _executor(native=native)
    result = executor.execute(
        ControlRequest(CanonicalIntent.OPEN_APP, app_id="блокнот")
    )

    serialized = repr(asdict(result))
    assert "notepad.exe" not in serialized.casefold()
    assert "C:\\Windows" not in serialized
    assert "блокнот" not in serialized
    assert set(asdict(result)) == {
        "status",
        "intent",
        "backend",
        "target_id",
        "reason_code",
        "confirmation_required",
    }


def test_non_windows_factories_are_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_control.os, "name", "posix")

    assert windows_control.make_optional_native_backend() is None
    assert windows_control.make_optional_uia_backend() is None


def test_backend_exception_does_not_leak_exception_text() -> None:
    class _Broken(_Native):
        def send_key(
            self, key: str, expected_gate_token: str | None = None
        ) -> BackendActionResult:
            del expected_gate_token
            raise RuntimeError(f"secret desktop content for {key}")

    executor = _executor(native=_Broken())
    result = executor.execute(ControlRequest(CanonicalIntent.SEND_KEY, key="down"))

    assert result.status is ResultStatus.FAILED
    assert "secret" not in repr(asdict(result)).casefold()
    assert result.reason_code == "backend_failed"


def test_snapshot_structure_contains_only_actionable_metadata() -> None:
    snapshot = _snapshot()

    assert snapshot.by_number(2) is not None
    assert snapshot.by_number(2).destructive is True  # type: ignore[union-attr]
    assert snapshot.by_number(99) is None
    assert snapshot.elements[0].bounds == ElementBounds(10, 20, 100, 30)
    assert not hasattr(snapshot.elements[0], "value")
    assert not hasattr(snapshot.elements[0], "text")


def test_uia_bounds_accepts_property_only_rectangle_wrapper() -> None:
    class _PropertyRectangle:
        Left = 10
        Top = 20
        Right = 110
        Bottom = 50

    class _Control:
        BoundingRectangle = _PropertyRectangle()

    assert UiaControlBackend._bounds(_Control()) == ElementBounds(10, 20, 100, 30)


def test_public_confirmation_flags_are_opt_in() -> None:
    assert signature(WindowsControlExecutor.execute).parameters["confirmed"].default is False
    assert signature(WindowsControlExecutor.execute).parameters["input_guard"].default is None
    assert (
        signature(WindowsControlExecutor.execute_action)
        .parameters["confirmed"]
        .default
        is False
    )


class _DirectRect:
    def __init__(self, left: int = 10, right: int = 110) -> None:
        self.left = left
        self.top = 20
        self.right = right
        self.bottom = 50


class _DirectInvokePattern:
    def __init__(self, control: "_DirectControl") -> None:
        self.control = control

    def Invoke(self) -> None:
        if self.control.fail_if_invoked:
            raise AssertionError("captured live control was reused")
        self.control.invocations += 1


class _DirectControl:
    ControlTypeName = "ButtonControl"
    IsEnabled = True
    IsOffscreen = False

    def __init__(
        self,
        name: str = "Settings",
        *,
        runtime_id: tuple[int, ...] = (7, 11),
        rect: _DirectRect | None = None,
        fail_if_invoked: bool = False,
    ) -> None:
        self.Name = name
        self.BoundingRectangle = rect or _DirectRect()
        self.runtime_id = runtime_id
        self.fail_if_invoked = fail_if_invoked
        self.invocations = 0

    def GetRuntimeId(self) -> tuple[int, ...]:
        return self.runtime_id

    def GetInvokePattern(self) -> _DirectInvokePattern:
        return _DirectInvokePattern(self)


class _DirectRoot:
    ControlTypeName = "WindowControl"
    NativeWindowHandle = 99

    def __init__(
        self,
        control: _DirectControl,
        runtime_id: tuple[int, ...] = (1, 99),
    ) -> None:
        self.control = control
        self.runtime_id = runtime_id

    def GetRuntimeId(self) -> tuple[int, ...]:
        return self.runtime_id


class _DirectInitializer:
    def __init__(self, automation: "_DirectAutomation") -> None:
        self.automation = automation

    def __enter__(self) -> "_DirectInitializer":
        self.automation.entered += 1
        self.automation.active += 1
        return self

    def __exit__(self, *_args: object) -> None:
        self.automation.active -= 1
        self.automation.exited += 1


class _DirectAutomation:
    def __init__(self, controls: tuple[_DirectControl, ...]) -> None:
        self.roots = tuple(_DirectRoot(control) for control in controls)
        self.foreground_calls = 0
        self.entered = 0
        self.exited = 0
        self.active = 0

    def UIAutomationInitializerInThread(self) -> _DirectInitializer:
        return _DirectInitializer(self)

    def GetForegroundControl(self) -> _DirectRoot:
        assert self.active == 1
        index = min(self.foreground_calls, len(self.roots) - 1)
        self.foreground_calls += 1
        return self.roots[index]

    def WalkControl(self, root: _DirectRoot, **_kwargs: object) -> tuple[_DirectControl]:
        assert self.active == 1
        return (root.control,)


def test_direct_uia_initializes_each_call_and_reenumerates_before_invoke() -> None:
    captured_control = _DirectControl(fail_if_invoked=True)
    fresh_control = _DirectControl()
    automation = _DirectAutomation((captured_control, fresh_control))
    backend = UiaControlBackend(automation)

    snapshot = backend.capture_elements("gate-a")
    assert snapshot is not None
    result = backend.invoke(snapshot.snapshot_id, 1)
    scroll = backend.scroll(ScrollDirection.DOWN, 1)

    assert result.status is BackendStatus.SUCCESS
    assert scroll.status is BackendStatus.UNSUPPORTED
    assert automation.entered == 3
    assert automation.exited == 3
    assert automation.active == 0
    assert captured_control.invocations == 0
    assert fresh_control.invocations == 1
    assert snapshot.elements[0].runtime_id == (7, 11)
    assert not hasattr(backend, "_controls")


def test_direct_uia_blocks_element_changed_since_snapshot() -> None:
    captured_control = _DirectControl()
    moved_control = _DirectControl(rect=_DirectRect(right=120))
    automation = _DirectAutomation((captured_control, moved_control))
    backend = UiaControlBackend(automation)

    snapshot = backend.capture_elements("gate-a")
    assert snapshot is not None
    result = backend.invoke(snapshot.snapshot_id, 1)

    assert result.status is BackendStatus.BLOCKED
    assert result.reason_code == "element_changed"
    assert captured_control.invocations == 0
    assert moved_control.invocations == 0
    assert automation.entered == 2
    assert automation.exited == 2


def test_long_uia_name_is_bounded_and_requires_confirmation() -> None:
    automation = _DirectAutomation((_DirectControl(name="A" * 121),))
    backend = UiaControlBackend(automation)

    snapshot = backend.capture_elements("gate-a")

    assert snapshot is not None
    assert len(snapshot.elements[0].name) == 120
    assert snapshot.elements[0].destructive is True


def test_uia_bounds_accepts_lowercase_attribute_rectangle() -> None:
    class _Control:
        BoundingRectangle = _DirectRect(left=5, right=45)

    assert UiaControlBackend._bounds(_Control()) == ElementBounds(5, 20, 40, 30)


def test_direct_uia_snapshot_current_uses_initializer_and_foreground_only() -> None:
    automation = _DirectAutomation((_DirectControl(), _DirectControl()))
    backend = UiaControlBackend(automation)

    snapshot = backend.capture_elements("gate-a")
    assert snapshot is not None
    assert backend.is_snapshot_current(snapshot.snapshot_id) is True

    assert automation.entered == 2
    assert automation.exited == 2
    assert automation.active == 0


def test_direct_uia_snapshot_current_rejects_changed_foreground_window() -> None:
    automation = _DirectAutomation((_DirectControl(), _DirectControl()))
    automation.roots = (
        _DirectRoot(_DirectControl(), (1, 99)),
        _DirectRoot(_DirectControl(), (1, 100)),
    )
    backend = UiaControlBackend(automation)

    snapshot = backend.capture_elements("gate-a")
    assert snapshot is not None
    assert backend.is_snapshot_current(snapshot.snapshot_id) is False
    assert backend.is_snapshot_current("stale-id") is False

    # A stale id is rejected from the immutable in-memory snapshot before a
    # COM/UIA context is opened.
    assert automation.entered == 2
    assert automation.exited == 2


def test_executor_snapshot_current_is_read_only_and_fails_closed() -> None:
    snapshot = _snapshot()
    uia = _Uia(snapshot)
    executor = _executor(uia=uia)
    assert executor.capture_numbered_elements() is snapshot

    assert executor.is_snapshot_current(snapshot.snapshot_id) is True
    uia.snapshot_current = False
    assert executor.is_snapshot_current(snapshot.snapshot_id) is False
    call_count = len(uia.calls)
    assert executor.is_snapshot_current("different-snapshot") is False
    assert len(uia.calls) == call_count
