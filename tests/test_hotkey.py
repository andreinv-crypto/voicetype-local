from types import SimpleNamespace

from voicetype_local.hotkey import GlobalHotkeyListener, RIGHT_CTRL, RightCtrlTapDetector


def test_standalone_right_ctrl_toggles() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)
    assert events == ["toggle"]
    detector.release(RIGHT_CTRL)

    assert events == ["toggle"]


def test_other_key_does_not_block_right_ctrl() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)
    detector.press("c")
    detector.release("c")
    detector.release(RIGHT_CTRL)

    assert events == ["toggle"]


def test_mouse_activity_does_not_block_right_ctrl() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)
    detector.press("mouse:Button.left")
    detector.release(RIGHT_CTRL)

    assert events == ["toggle"]


def test_preheld_input_does_not_block_right_ctrl() -> None:
    for held_key in ("Key.shift", "Key.alt", "Key.ctrl_l", "mouse:Button.left"):
        events: list[str] = []
        detector = RightCtrlTapDetector(lambda: events.append("toggle"))

        detector.press(held_key)
        detector.press(RIGHT_CTRL)
        detector.release(RIGHT_CTRL)
        detector.release(held_key)

        assert events == ["toggle"]

        detector.press(RIGHT_CTRL)
        detector.release(RIGHT_CTRL)
        assert events == ["toggle", "toggle"]


def test_key_repeat_does_not_create_extra_toggle() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)
    detector.press(RIGHT_CTRL)
    detector.release(RIGHT_CTRL)

    assert events == ["toggle"]


def test_unrelated_inputs_are_completely_ignored() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    for key in ("c", "escape", "mouse:Button.left", "mouse:scroll"):
        detector.press(key)
        detector.release(key)

    assert events == []


def test_release_without_press_does_not_toggle() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.release(RIGHT_CTRL)

    assert events == []


def test_long_hold_gives_immediate_feedback_without_waiting_for_release() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)

    assert events == ["toggle"]


def test_long_hold_never_rearms_without_key_up() -> None:
    events: list[str] = []
    detector = RightCtrlTapDetector(lambda: events.append("toggle"))

    detector.press(RIGHT_CTRL)
    for _ in range(100):
        detector.press(RIGHT_CTRL)

    assert events == ["toggle"]


def test_windows_hook_reserves_only_right_ctrl() -> None:
    events: list[str] = []
    listener = GlobalHotkeyListener(lambda: events.append("toggle"))

    class _Hook:
        suppressed = 0

        def suppress_event(self) -> None:
            self.suppressed += 1

    hook = _Hook()
    listener._listener = hook  # type: ignore[assignment]

    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x41)) is True
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0xA3)) is False
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0xA3)) is False
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0xA3)) is False

    assert events == ["toggle"]
    assert hook.suppressed == 3
