from __future__ import annotations

import threading
from collections.abc import Callable

from pynput import keyboard


RIGHT_CTRL = "right_ctrl"

KEY_VIRTUAL_CODES = {
    RIGHT_CTRL: 0xA3,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "pause": 0x13,
}


class DedicatedKeyTapDetector:
    """Treat one configured key as a switch and ignore every other input."""

    def __init__(
        self,
        on_tap: Callable[[], None],
        key_name: str = RIGHT_CTRL,
    ) -> None:
        self._on_tap = on_tap
        self._key_name = key_name if key_name in KEY_VIRTUAL_CODES else RIGHT_CTRL
        self._held = False
        self._lock = threading.Lock()

    def press(self, key_name: str) -> None:
        if key_name != self._key_name:
            return
        should_fire = False
        with self._lock:
            if not self._held:
                self._held = True
                should_fire = True
        if should_fire:
            # Key-down gives immediate feedback even when releasing a key is
            # physically difficult. The hook suppresses Ctrl for the target.
            self._on_tap()

    def release(self, key_name: str) -> None:
        if key_name != self._key_name:
            return
        with self._lock:
            self._held = False

    def reset(self) -> None:
        with self._lock:
            self._held = False


class RightCtrlTapDetector(DedicatedKeyTapDetector):
    """Backward-compatible detector for the default accessibility key."""

    def __init__(self, on_tap: Callable[[], None]) -> None:
        super().__init__(on_tap, RIGHT_CTRL)

class GlobalHotkeyListener:
    """Global listener for the one accessibility control: Right Ctrl."""

    _KEY_DOWN_MESSAGES = {0x0100, 0x0104}
    _KEY_UP_MESSAGES = {0x0101, 0x0105}

    def __init__(
        self, on_toggle: Callable[[], None], key_name: str = RIGHT_CTRL
    ) -> None:
        self.key_name = key_name if key_name in KEY_VIRTUAL_CODES else RIGHT_CTRL
        self._virtual_key = KEY_VIRTUAL_CODES[self.key_name]
        self._detector = DedicatedKeyTapDetector(on_toggle, self.key_name)
        self._listener: keyboard.Listener | None = None

    def _win32_event_filter(self, msg: int, data: object) -> bool:
        vk_code = int(getattr(data, "vkCode", 0))
        if vk_code != self._virtual_key:
            return True
        if msg in self._KEY_DOWN_MESSAGES:
            self._detector.press(self.key_name)
        elif msg in self._KEY_UP_MESSAGES:
            self._detector.release(self.key_name)
        # The configured key is dedicated to VoiceType, so do not pass it through to
        # the focused application where an accidental chord could run a command.
        if self._listener is not None:
            self._listener.suppress_event()
        return False

    def start(self) -> None:
        if self.is_alive():
            return
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        self._detector.reset()
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
