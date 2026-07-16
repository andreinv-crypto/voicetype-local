from __future__ import annotations

import ctypes
import time
from ctypes import wintypes


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def _unicode_units(character: str) -> list[int]:
    encoded = character.encode("utf-16-le", errors="surrogatepass")
    return [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]


class UnicodeTextInserter:
    """Type Unicode through SendInput without changing the user's clipboard."""

    def __init__(self, chunk_size: int = 192) -> None:
        self.chunk_size = chunk_size
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._send_input = self._user32.SendInput
        self._send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._send_input.restype = wintypes.UINT

    def _send(self, events: list[INPUT]) -> None:
        if not events:
            return
        array_type = INPUT * len(events)
        array = array_type(*events)
        sent = self._send_input(len(events), array, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise ctypes.WinError(ctypes.get_last_error())

    def insert(self, text: str) -> None:
        # Physical Enter can submit chats/forms and Tab can move focus. VoiceType
        # therefore inserts a safe single block in its first version.
        normalized = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        events: list[INPUT] = []
        for character in normalized:
            for unit in _unicode_units(character):
                events.extend(
                    [
                        _key_event(0, unit, KEYEVENTF_UNICODE),
                        _key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                    ]
                )
            if len(events) >= self.chunk_size * 2:
                self._send(events)
                events.clear()
                time.sleep(0.002)
        self._send(events)
