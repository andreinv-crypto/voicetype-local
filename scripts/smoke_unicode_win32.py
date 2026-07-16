from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.inserter import UnicodeTextInserter


WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
ES_AUTOHSCROLL = 0x0080
SW_SHOW = 5
GA_ROOT = 2
PM_REMOVE = 1


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


def main() -> int:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    previous = user32.GetForegroundWindow()
    instance = kernel32.GetModuleHandleW(None)
    parent = user32.CreateWindowExW(
        0,
        "STATIC",
        "VoiceType Unicode smoke test",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        20,
        20,
        520,
        100,
        None,
        None,
        instance,
        None,
    )
    if not parent:
        return 21
    edit = user32.CreateWindowExW(
        0,
        "EDIT",
        "",
        WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_AUTOHSCROLL,
        10,
        10,
        480,
        30,
        parent,
        None,
        instance,
        None,
    )
    if not edit:
        user32.DestroyWindow(parent)
        return 22

    try:
        user32.ShowWindow(parent, SW_SHOW)
        user32.SetForegroundWindow(parent)
        user32.SetFocus(edit)
        if user32.GetForegroundWindow() != user32.GetAncestor(parent, GA_ROOT):
            return 23
        expected = "Привет, España: ñ á é í ó ú Ñ. 😀"
        UnicodeTextInserter().insert(expected)
        deadline = time.monotonic() + 0.5
        message = MSG()
        while time.monotonic() < deadline:
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.005)
        length = user32.GetWindowTextLengthW(edit)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(edit, buffer, length + 1)
        print(f"EXPECTED={expected!r}")
        print(f"ACTUAL={buffer.value!r}")
        return 0 if buffer.value == expected else 24
    finally:
        user32.DestroyWindow(parent)
        if previous:
            user32.SetForegroundWindow(previous)


if __name__ == "__main__":
    raise SystemExit(main())

