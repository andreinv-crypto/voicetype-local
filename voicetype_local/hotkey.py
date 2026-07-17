from __future__ import annotations

import threading
from collections.abc import Callable

from pynput import keyboard

from .config import SUPPORTED_HOTKEYS, is_supported_activation_pair


RIGHT_CTRL = "right_ctrl"
TOGGLE_MODE = "toggle"
HOLD_MODE = "hold"

KEY_VIRTUAL_CODES = {
    RIGHT_CTRL: 0xA3,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "pause": 0x13,
}

MODIFIER_VIRTUAL_CODES = {
    "ctrl": frozenset({0x11, 0xA2, 0xA3}),
    "alt": frozenset({0x12, 0xA4, 0xA5}),
    "shift": frozenset({0x10, 0xA0, 0xA1}),
}

VK_ESCAPE = 0x1B

_KEY_LABELS = {
    RIGHT_CTRL: "Правый Ctrl",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "pause": "Pause",
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}


def _safe_binding(value: str) -> tuple[str | None, str]:
    key_name = (
        value
        if isinstance(value, str) and value in SUPPORTED_HOTKEYS
        else RIGHT_CTRL
    )
    if "+" not in key_name:
        return None, key_name
    modifier, primary = key_name.split("+", 1)
    return modifier, primary


def activation_key_label(value: str) -> str:
    """Return a bounded human-readable label for a validated activation key."""

    modifier, primary = _safe_binding(value)
    primary_label = _KEY_LABELS.get(primary, "Правый Ctrl")
    if modifier is None:
        return primary_label
    return f"{_KEY_LABELS[modifier]} + {primary_label}"


def activation_label(value: str, mode: str = TOGGLE_MODE) -> str:
    valid_pair = is_supported_activation_pair(value, mode)
    safe_value = value if valid_pair else RIGHT_CTRL
    safe_mode = mode if valid_pair else TOGGLE_MODE
    action = "удерживать" if safe_mode == HOLD_MODE else "нажать"
    return f"{activation_key_label(safe_value)} · {action}"


def _modifier_group(event_name: str) -> str | None:
    group = event_name.split(":", 1)[0]
    return group if group in MODIFIER_VIRTUAL_CODES else None


class ActivationKeyDetector:
    """Pure state machine for one safe activation binding.

    It has no desktop hooks and is therefore directly testable.  ``press`` and
    ``release`` return whether that particular primary/dedicated event belongs
    to VoiceType and should be suppressed by the caller.
    """

    def __init__(
        self,
        on_toggle: Callable[[], None],
        key_name: str = RIGHT_CTRL,
        *,
        activation_mode: str = TOGGLE_MODE,
        on_hold_start: Callable[[], None] | None = None,
        on_hold_stop: Callable[[], None] | None = None,
    ) -> None:
        valid_pair = is_supported_activation_pair(key_name, activation_mode)
        self.key_name = key_name if valid_pair else RIGHT_CTRL
        self.activation_mode = activation_mode if valid_pair else TOGGLE_MODE
        self.modifier, self.primary = _safe_binding(self.key_name)
        self._on_toggle = on_toggle
        self._on_hold_start = on_hold_start or on_toggle
        self._on_hold_stop = on_hold_stop or on_toggle
        self._held_inputs: set[str] = set()
        self._primary_held = False
        self._primary_captured = False
        self._lock = threading.Lock()

    def _modifier_is_held(self) -> bool:
        if self.modifier is None:
            return True
        return any(
            _modifier_group(event_name) == self.modifier
            for event_name in self._held_inputs
        )

    def press(self, event_name: str) -> bool:
        callback: Callable[[], None] | None = None
        with self._lock:
            modifier = _modifier_group(event_name)
            if modifier is not None and event_name != self.primary:
                self._held_inputs.add(event_name)
                return False
            if event_name != self.primary:
                return False
            if self._primary_held:
                # Auto-repeat belongs to VoiceType only if the original
                # primary down completed the configured chord.
                return self._primary_captured or self.modifier is None
            self._primary_held = True
            self._primary_captured = self._modifier_is_held()
            if self._primary_captured:
                callback = (
                    self._on_hold_start
                    if self.activation_mode == HOLD_MODE
                    else self._on_toggle
                )
            suppress = self._primary_captured or self.modifier is None
        if callback is not None:
            callback()
        return suppress

    def release(self, event_name: str) -> bool:
        callback: Callable[[], None] | None = None
        with self._lock:
            modifier = _modifier_group(event_name)
            if modifier is not None and event_name != self.primary:
                self._held_inputs.discard(event_name)
                return False
            if event_name != self.primary:
                return False
            captured = self._primary_captured
            dedicated = self.modifier is None
            self._primary_held = False
            self._primary_captured = False
            if captured and self.activation_mode == HOLD_MODE:
                callback = self._on_hold_stop
        if callback is not None:
            callback()
        return captured or dedicated

    def reset(self, *, notify_release: bool = False) -> None:
        callback: Callable[[], None] | None = None
        with self._lock:
            if (
                notify_release
                and self._primary_captured
                and self.activation_mode == HOLD_MODE
            ):
                callback = self._on_hold_stop
            self._held_inputs.clear()
            self._primary_held = False
            self._primary_captured = False
        if callback is not None:
            callback()


class DedicatedKeyTapDetector(ActivationKeyDetector):
    """Backward-compatible toggle detector for a dedicated single key."""

    def __init__(
        self,
        on_tap: Callable[[], None],
        key_name: str = RIGHT_CTRL,
    ) -> None:
        dedicated = key_name if "+" not in key_name else RIGHT_CTRL
        super().__init__(on_tap, dedicated, activation_mode=TOGGLE_MODE)


class RightCtrlTapDetector(DedicatedKeyTapDetector):
    """Backward-compatible detector for the default accessibility key."""

    def __init__(self, on_tap: Callable[[], None]) -> None:
        super().__init__(on_tap, RIGHT_CTRL)


class GlobalHotkeyListener:
    """Low-level hook for one validated VoiceType activation binding."""

    _KEY_DOWN_MESSAGES = {0x0100, 0x0104}
    _KEY_UP_MESSAGES = {0x0101, 0x0105}

    def __init__(
        self,
        on_toggle: Callable[[], None],
        key_name: str = RIGHT_CTRL,
        *,
        activation_mode: str = TOGGLE_MODE,
        on_hold_start: Callable[[], None] | None = None,
        on_hold_stop: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        valid_pair = is_supported_activation_pair(key_name, activation_mode)
        self.key_name = key_name if valid_pair else RIGHT_CTRL
        self.activation_mode = activation_mode if valid_pair else TOGGLE_MODE
        self._modifier, self._primary = _safe_binding(self.key_name)
        self._virtual_key = KEY_VIRTUAL_CODES[self._primary]
        self._detector = ActivationKeyDetector(
            on_toggle,
            self.key_name,
            activation_mode=self.activation_mode,
            on_hold_start=on_hold_start,
            on_hold_stop=on_hold_stop,
        )
        self._on_cancel = on_cancel
        self._should_cancel = should_cancel
        self._escape_held = False
        self._escape_captured = False
        self._listener: keyboard.Listener | None = None

    @staticmethod
    def _suppress(listener: keyboard.Listener | None) -> bool:
        if listener is not None:
            listener.suppress_event()
        return False

    def _event_name(self, vk_code: int) -> str:
        if vk_code == self._virtual_key:
            return self._primary
        for modifier, codes in MODIFIER_VIRTUAL_CODES.items():
            if vk_code in codes:
                return f"{modifier}:{vk_code}"
        return f"vk:{vk_code}"

    def _win32_event_filter(self, msg: int, data: object) -> bool:
        vk_code = int(getattr(data, "vkCode", 0))
        if vk_code == VK_ESCAPE and self._on_cancel is not None:
            if msg in self._KEY_DOWN_MESSAGES:
                capture = bool(self._should_cancel and self._should_cancel())
                if capture and not self._escape_held:
                    self._escape_held = True
                    self._escape_captured = True
                    self._on_cancel()
            elif msg in self._KEY_UP_MESSAGES:
                captured = self._escape_captured
                self._escape_held = False
                self._escape_captured = False
                if captured:
                    return self._suppress(self._listener)
            if self._escape_captured:
                return self._suppress(self._listener)
            return True

        event_name = self._event_name(vk_code)
        if msg in self._KEY_DOWN_MESSAGES:
            captured = self._detector.press(event_name)
        elif msg in self._KEY_UP_MESSAGES:
            captured = self._detector.release(event_name)
        else:
            return True
        return self._suppress(self._listener) if captured else True

    def start(self) -> None:
        if self.is_alive():
            return
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._detector.reset()
        self._escape_held = False
        self._escape_captured = False
        self._listener = keyboard.Listener(
            suppress=False,
            win32_event_filter=self._win32_event_filter,
        )
        self._listener.start()

    def is_alive(self) -> bool:
        return self._listener is not None and self._listener.is_alive()

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            finally:
                self._listener = None
        self._detector.reset(notify_release=True)
