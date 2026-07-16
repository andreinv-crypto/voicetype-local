from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from collections.abc import Callable
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw


STATUS_COLORS = {
    "loading": "#2563EB",
    "ready": "#15803D",
    "starting": "#2563EB",
    "recording": "#DC2626",
    "processing": "#7C3AED",
    "pending": "#C2410C",
    "success": "#15803D",
    "cancelled": "#475569",
    "error": "#B91C1C",
}

_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_USER32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_USER32.GetAncestor.restype = wintypes.HWND
_USER32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
_USER32.GetWindowLongW.restype = wintypes.LONG
_USER32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
_USER32.SetWindowLongW.restype = wintypes.LONG
_USER32.SetWindowPos.argtypes = (
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
)
_USER32.SetWindowPos.restype = wintypes.BOOL
_USER32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_USER32.ShowWindow.restype = wintypes.BOOL


def make_tray_image(color: str) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=color, outline="#FFFFFF", width=3)
    draw.rounded_rectangle((25, 13, 39, 38), radius=7, fill="#FFFFFF")
    draw.arc((18, 22, 46, 48), start=0, end=180, fill="#FFFFFF", width=4)
    draw.line((32, 46, 32, 53), fill="#FFFFFF", width=4)
    draw.line((24, 53, 40, 53), fill="#FFFFFF", width=4)
    return image


class StatusOverlay:
    """Small non-activating status bubble that does not steal typing focus."""

    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg="#111827")
        self.window.attributes("-topmost", True)
        self.label = tk.Label(
            self.window,
            text="",
            fg="#FFFFFF",
            bg="#111827",
            font=("Segoe UI", 13, "bold"),
            padx=20,
            pady=13,
            wraplength=620,
            justify="center",
        )
        self.label.pack(fill="both", expand=True)
        self.window.update_idletasks()
        inner_hwnd = int(self.window.winfo_id())
        self.hwnd = int(_USER32.GetAncestor(inner_hwnd, 2) or inner_hwnd)
        style = _USER32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
        _USER32.SetWindowLongW(
            self.hwnd,
            self.GWL_EXSTYLE,
            style | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE,
        )
        self._hide_job: str | None = None

    def show(self, text: str, state: str, timeout_ms: int | None = None) -> None:
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        color = STATUS_COLORS.get(state, "#334155")
        self.window.configure(bg=color)
        self.label.configure(text=text, bg=color)
        self.window.update_idletasks()
        width = min(self.root.winfo_screenwidth() - 40, max(260, self.label.winfo_reqwidth()))
        height = max(52, self.label.winfo_reqheight())
        x = (self.root.winfo_screenwidth() - width) // 2
        y = 28
        _USER32.SetWindowPos(
            self.hwnd,
            wintypes.HWND(self.HWND_TOPMOST),
            x,
            y,
            width,
            height,
            self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
        )
        _USER32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)
        if timeout_ms is not None:
            self._hide_job = self.root.after(timeout_ms, self.hide)

    def hide(self) -> None:
        _USER32.ShowWindow(self.hwnd, self.SW_HIDE)
        self._hide_job = None


class TrayController:
    def __init__(
        self,
        on_toggle: Callable[[], None],
        on_cancel: Callable[[], None],
        on_insert_last: Callable[[], None],
        on_copy_last: Callable[[], None],
        on_language: Callable[[str], None],
        on_exit: Callable[[], None],
        get_language: Callable[[], str],
        has_last_text: Callable[[], bool],
        on_settings: Callable[[], None] | None = None,
        on_memory: Callable[[], None] | None = None,
    ) -> None:
        self._get_language = get_language
        self._has_last_text = has_last_text
        self.icon = pystray.Icon(
            "VoiceTypeLocal",
            make_tray_image(STATUS_COLORS["loading"]),
            "VoiceType Local — загружается",
            menu=pystray.Menu(
                pystray.MenuItem("Начать / остановить диктовку", lambda *_: on_toggle()),
                pystray.MenuItem("Отменить текущую запись", lambda *_: on_cancel()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Язык",
                    pystray.Menu(
                        self._language_item("Авто", "auto", on_language),
                        self._language_item("Русский", "ru", on_language),
                        self._language_item("Español", "es", on_language),
                        self._language_item("English", "en", on_language),
                    ),
                ),
                pystray.MenuItem(
                    "Вставить последний текст",
                    lambda *_: on_insert_last(),
                    enabled=lambda _: self._has_last_text(),
                ),
                pystray.MenuItem(
                    "Скопировать последний текст",
                    lambda *_: on_copy_last(),
                    enabled=lambda _: self._has_last_text(),
                ),
                pystray.MenuItem(
                    "Память терминов…",
                    lambda *_: on_memory() if on_memory is not None else None,
                ),
                pystray.MenuItem(
                    "Настройки и приватность…",
                    lambda *_: on_settings() if on_settings is not None else None,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda *_: on_exit()),
            ),
        )
        self._thread: threading.Thread | None = None

    def _language_item(
        self, label: str, value: str, callback: Callable[[str], None]
    ) -> pystray.MenuItem:
        return pystray.MenuItem(
            label,
            lambda *_: callback(value),
            checked=lambda _: self._get_language() == value,
            radio=True,
        )

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="tray", daemon=True)
        self._thread.start()

    def set_state(self, state: str, title: str) -> None:
        self.icon.icon = make_tray_image(STATUS_COLORS.get(state, "#334155"))
        self.icon.title = title
        self.icon.update_menu()

    def stop(self) -> None:
        self.icon.stop()
        if (
            self._thread is not None
            and self._thread.ident != threading.current_thread().ident
        ):
            self._thread.join(timeout=2.0)
        self._thread = None
