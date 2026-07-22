from __future__ import annotations

import threading
from collections.abc import Iterable

from voicetype_local.focus import (
    FocusInspector,
    FocusMatch,
    FocusTarget,
    FocusUnknownReason,
    RuntimeIdStatus,
    Win32FocusState,
    _TimedRuntimeIdProvider,
    compare_focus_targets,
)


class _Backend:
    def __init__(self, *states: Win32FocusState) -> None:
        self.states = list(states)
        self.calls = 0

    def snapshot(self) -> Win32FocusState:
        self.calls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


class _RuntimeIds:
    def __init__(self, *values: Iterable[int] | None) -> None:
        self.values = list(values)
        self.calls = 0

    def focused_runtime_id(self) -> Iterable[int] | None:
        self.calls += 1
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class _BrokenRuntimeIds:
    def focused_runtime_id(self) -> Iterable[int] | None:
        raise RuntimeError("UIA provider unavailable")


class _FirstRuntimeIdCallStalls:
    def __init__(self, value: Iterable[int]) -> None:
        self.value = value
        self.calls = 0
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def focused_runtime_id(self) -> Iterable[int] | None:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            self.release_first.wait(timeout=2.0)
        return self.value


def _state(
    *,
    foreground: int = 100,
    root: int = 100,
    focused: int = 110,
    thread: int = 7,
    process: int = 9,
    class_name: str = "Edit",
) -> Win32FocusState:
    return Win32FocusState(
        foreground_hwnd=foreground,
        root_hwnd=root,
        focused_hwnd=focused,
        thread_id=thread,
        process_id=process,
        focused_class_name=class_name,
        valid=True,
    )


def _target(
    *,
    foreground: int = 100,
    root: int = 100,
    focused: int = 110,
    thread: int = 7,
    process: int = 9,
    class_name: str = "Edit",
    runtime_id: tuple[int, ...] | None = None,
    stable: bool = True,
) -> FocusTarget:
    return FocusTarget(
        foreground_hwnd=foreground,
        root_hwnd=root,
        focused_hwnd=focused,
        thread_id=thread,
        process_id=process,
        focused_class_name=class_name,
        uia_runtime_id=runtime_id,
        stable=stable,
    )


def test_capture_uses_injected_backend_and_optional_runtime_id() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    backend = _Backend(state, state)
    runtime_ids = _RuntimeIds([42, 7, -1])
    inspector = FocusInspector(backend, runtime_ids)

    target = inspector.capture()

    assert target == _target(
        class_name="Chrome_RenderWidgetHostHWND", runtime_id=(42, 7, -1)
    )
    assert backend.calls == 2
    assert runtime_ids.calls == 1


def test_capture_retries_when_focus_changes_during_uia_capture() -> None:
    first = _state(focused=110)
    second = _state(focused=120)
    backend = _Backend(first, second, second, second)
    runtime_ids = _RuntimeIds([1], [2])
    inspector = FocusInspector(backend, runtime_ids, max_attempts=2)

    target = inspector.capture()

    assert target.focused_hwnd == 120
    assert target.uia_runtime_id == (2,)
    assert target.stable
    assert backend.calls == 4


def test_unstable_capture_discards_unpaired_runtime_id() -> None:
    backend = _Backend(_state(focused=110), _state(focused=120))
    inspector = FocusInspector(backend, _RuntimeIds([99]), max_attempts=1)

    target = inspector.capture()

    assert target.focused_hwnd == 120
    assert target.uia_runtime_id is None
    assert not target.stable


def test_uia_failure_falls_back_without_losing_stable_win32_state() -> None:
    state = _state()
    inspector = FocusInspector(_Backend(state, state), _BrokenRuntimeIds())

    target = inspector.capture()

    assert target.stable
    assert target.uia_runtime_id is None
    assert target.focused_hwnd == 110


def test_capture_retries_missing_runtime_id_for_virtual_field() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    runtime_ids = _RuntimeIds(None, [42, 7])
    inspector = FocusInspector(
        _Backend(state),
        runtime_ids,
        runtime_id_attempts=2,
    )

    target = inspector.capture()

    assert target.stable
    assert target.uia_runtime_id == (42, 7)
    assert target.uia_runtime_id_status is RuntimeIdStatus.AVAILABLE
    assert runtime_ids.calls == 2


def test_capture_runtime_id_retries_are_bounded() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    runtime_ids = _RuntimeIds(None)
    inspector = FocusInspector(
        _Backend(state),
        runtime_ids,
        runtime_id_attempts=2,
    )

    target = inspector.capture()

    assert target.stable
    assert target.uia_runtime_id is None
    assert target.uia_runtime_id_status is RuntimeIdStatus.MISSING
    assert runtime_ids.calls == 2


def test_same_native_edit_hwnd_is_same_field_without_uia() -> None:
    assert compare_focus_targets(_target(), _target()) is FocusMatch.SAME


def test_different_focused_hwnd_is_changed_field() -> None:
    assert (
        compare_focus_targets(_target(focused=110), _target(focused=120))
        is FocusMatch.CHANGED
    )


def test_different_window_process_or_thread_is_changed() -> None:
    original = _target()

    assert original.compare(_target(foreground=200, root=200)) is FocusMatch.CHANGED
    assert original.compare(_target(process=10)) is FocusMatch.CHANGED
    assert original.compare(_target(thread=8)) is FocusMatch.CHANGED


def test_browser_renderer_without_uia_is_conservatively_unknown() -> None:
    browser = _target(class_name="Chrome_RenderWidgetHostHWND")

    assert browser.compare(browser) is FocusMatch.UNKNOWN


def test_browser_runtime_id_identifies_same_and_changed_fields() -> None:
    original = _target(
        class_name="Chrome_RenderWidgetHostHWND", runtime_id=(42, 7, 1)
    )
    same = _target(
        class_name="Chrome_RenderWidgetHostHWND", runtime_id=(42, 7, 1)
    )
    changed = _target(
        class_name="Chrome_RenderWidgetHostHWND", runtime_id=(42, 7, 2)
    )

    assert original.compare(same) is FocusMatch.SAME
    assert original.compare(changed) is FocusMatch.CHANGED


def test_runtime_id_available_on_only_one_side_is_unknown() -> None:
    with_uia = _target(class_name="MozillaWindowClass", runtime_id=(1, 2, 3))
    without_uia = _target(class_name="MozillaWindowClass")

    assert with_uia.compare(without_uia) is FocusMatch.UNKNOWN


def test_missing_or_unstable_identity_is_unknown() -> None:
    assert compare_focus_targets(None, _target()) is FocusMatch.UNKNOWN
    assert _target(stable=False).compare(_target()) is FocusMatch.UNKNOWN
    assert _target(focused=0).compare(_target(focused=0)) is FocusMatch.UNKNOWN


def test_compare_recaptures_when_end_runtime_id_is_temporarily_missing() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    runtime_ids = _RuntimeIds([42, 7], None, [42, 7])
    inspector = FocusInspector(
        _Backend(state),
        runtime_ids,
        runtime_id_attempts=1,
        comparison_attempts=2,
    )
    expected = inspector.capture()

    comparison = inspector.compare_current_detailed(expected)

    assert comparison.match is FocusMatch.SAME
    assert comparison.attempts == 2
    assert comparison.unknown_reason is None
    assert comparison.encountered_unknown_reasons == (
        FocusUnknownReason.CURRENT_RUNTIME_ID_MISSING,
    )
    assert runtime_ids.calls == 3
    assert inspector.last_comparison is comparison


def test_both_missing_runtime_ids_keep_virtual_field_unknown_without_guessing() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    runtime_ids = _RuntimeIds(None)
    inspector = FocusInspector(
        _Backend(state),
        runtime_ids,
        runtime_id_attempts=1,
        comparison_attempts=2,
    )
    expected = inspector.capture()

    comparison = inspector.compare_current_detailed(expected)

    assert comparison.match is FocusMatch.UNKNOWN
    assert comparison.unknown_reason is FocusUnknownReason.BOTH_RUNTIME_IDS_MISSING
    assert comparison.attempts == 1
    assert comparison.expected_runtime_status is RuntimeIdStatus.MISSING
    assert comparison.current_runtime_status is RuntimeIdStatus.MISSING
    assert runtime_ids.calls == 2


def test_timeout_probe_can_recover_while_first_worker_is_still_stuck() -> None:
    state = _state(class_name="Chrome_RenderWidgetHostHWND")
    slow_then_ready = _FirstRuntimeIdCallStalls([42, 7])
    timed = _TimedRuntimeIdProvider(slow_then_ready, timeout_seconds=0.05)
    inspector = FocusInspector(
        _Backend(state),
        timed,
        runtime_id_attempts=1,
        comparison_attempts=2,
    )
    expected = _target(
        class_name="Chrome_RenderWidgetHostHWND",
        runtime_id=(42, 7),
    )

    try:
        comparison = inspector.compare_current_detailed(expected)
    finally:
        slow_then_ready.release_first.set()

    assert slow_then_ready.first_started.is_set()
    assert slow_then_ready.calls == 2
    assert comparison.match is FocusMatch.SAME
    assert comparison.attempts == 2
    assert comparison.encountered_unknown_reasons == (
        FocusUnknownReason.CURRENT_RUNTIME_ID_TIMED_OUT,
    )


def test_genuine_changed_target_is_not_retried_after_detection() -> None:
    changed_state = _state(focused=120, class_name="Chrome_RenderWidgetHostHWND")
    runtime_ids = _RuntimeIds(None, [42, 7])
    inspector = FocusInspector(
        _Backend(changed_state),
        runtime_ids,
        runtime_id_attempts=1,
        comparison_attempts=2,
    )
    expected = _target(
        focused=110,
        class_name="Chrome_RenderWidgetHostHWND",
        runtime_id=(42, 7),
    )

    comparison = inspector.compare_current_detailed(expected)

    assert comparison.match is FocusMatch.CHANGED
    assert comparison.attempts == 1
    assert comparison.encountered_unknown_reasons == ()
    assert runtime_ids.calls == 1
