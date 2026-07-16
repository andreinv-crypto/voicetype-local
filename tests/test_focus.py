from __future__ import annotations

from collections.abc import Iterable

from voicetype_local.focus import (
    FocusInspector,
    FocusMatch,
    FocusTarget,
    Win32FocusState,
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
