from types import SimpleNamespace

from voicetype_local.hotkey import (
    ActivationKeyDetector,
    GlobalHotkeyListener,
    RIGHT_CTRL,
    RightCtrlTapDetector,
    activation_key_label,
    activation_label,
)


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


def test_escape_cancels_once_and_is_suppressed_only_while_active() -> None:
    cancelled: list[str] = []
    active = True
    listener = GlobalHotkeyListener(
        lambda: None,
        on_cancel=lambda: cancelled.append("cancel"),
        should_cancel=lambda: active,
    )

    class _Hook:
        suppressed = 0

        def suppress_event(self) -> None:
            self.suppressed += 1

    hook = _Hook()
    listener._listener = hook  # type: ignore[assignment]

    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x1B)) is False
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x1B)) is False
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0x1B)) is False
    assert cancelled == ["cancel"]

    active = False
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x1B)) is True
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0x1B)) is True
    assert cancelled == ["cancel"]


def test_combo_modifier_never_fires_and_primary_autorepeat_fires_once() -> None:
    events: list[str] = []
    detector = ActivationKeyDetector(
        lambda: events.append("toggle"), key_name="ctrl+f8"
    )

    assert detector.press("ctrl:162") is False
    assert events == []
    assert detector.press("f8") is True
    assert detector.press("f8") is True
    assert detector.release("f8") is True
    assert detector.release("ctrl:162") is False
    assert events == ["toggle"]


def test_combo_primary_without_modifier_is_untouched() -> None:
    events: list[str] = []
    detector = ActivationKeyDetector(
        lambda: events.append("toggle"), key_name="alt+f9"
    )

    assert detector.press("f9") is False
    assert detector.press("alt:164") is False
    assert detector.press("f9") is False  # auto-repeat must not arm late
    assert detector.release("f9") is False
    assert detector.release("alt:164") is False
    assert events == []


def test_hold_starts_on_primary_down_and_stops_on_primary_release() -> None:
    events: list[str] = []
    detector = ActivationKeyDetector(
        lambda: events.append("toggle"),
        key_name="shift+f10",
        activation_mode="hold",
        on_hold_start=lambda: events.append("start"),
        on_hold_stop=lambda: events.append("stop"),
    )

    detector.press("shift:160")
    assert detector.press("f10") is True
    detector.release("shift:160")
    assert events == ["start"]
    assert detector.release("f10") is True
    assert events == ["start", "stop"]


def test_combo_listener_suppresses_only_a_matched_primary() -> None:
    events: list[str] = []
    listener = GlobalHotkeyListener(
        lambda: events.append("toggle"), key_name="ctrl+f8"
    )

    class _Hook:
        suppressed = 0

        def suppress_event(self) -> None:
            self.suppressed += 1

    hook = _Hook()
    listener._listener = hook  # type: ignore[assignment]

    # F8 by itself remains available to the focused application.
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x77)) is True
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0x77)) is True
    # Ctrl is observed but never swallowed; only matched F8 down/up is reserved.
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0xA2)) is True
    assert listener._win32_event_filter(0x0100, SimpleNamespace(vkCode=0x77)) is False
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0x77)) is False
    assert listener._win32_event_filter(0x0101, SimpleNamespace(vkCode=0xA2)) is True

    assert events == ["toggle"]
    assert hook.suppressed == 2


def test_human_readable_activation_labels_fail_closed() -> None:
    assert activation_key_label("right_ctrl") == "Правый Ctrl"
    assert activation_key_label("ctrl+f8") == "Ctrl + F8"
    assert activation_label("alt+f9", "hold") == "Alt + F9 · удерживать"
    assert activation_label("pause", "hold") == "Правый Ctrl · нажать"
    assert activation_label("win+f8", "hold") == "Правый Ctrl · нажать"

    invalid_listener = GlobalHotkeyListener(
        lambda: None, key_name="ctrl+f8", activation_mode="legacy"
    )
    assert invalid_listener.key_name == "right_ctrl"
    assert invalid_listener.activation_mode == "toggle"

    pause_hold_listener = GlobalHotkeyListener(
        lambda: None, key_name="pause", activation_mode="hold"
    )
    assert pause_hold_listener.key_name == "right_ctrl"
    assert pause_hold_listener.activation_mode == "toggle"
