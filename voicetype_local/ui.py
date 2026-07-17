from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from collections.abc import Callable
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw
from tkinter import ttk

from .windows_control import NumberedElementSnapshot


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
_USER32.GetSystemMetrics.argtypes = (ctypes.c_int,)
_USER32.GetSystemMetrics.restype = ctypes.c_int


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

    _SIZE_STYLES = {
        "compact": (11, 14, 8, 480, 220, 44),
        "large": (13, 20, 13, 620, 260, 52),
        "extra_large": (17, 28, 18, 760, 320, 68),
    }
    _CONTRASTS = frozenset({"standard", "high"})
    _POSITIONS = frozenset({"top", "center", "bottom"})

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
        self._size = "large"
        self._contrast = "high"
        self._position = "top"

    def update_preferences(
        self,
        *,
        size: str = "large",
        contrast: str = "high",
        position: str = "top",
    ) -> None:
        """Apply validated visual preferences without activating the window."""

        self._size = (
            size if isinstance(size, str) and size in self._SIZE_STYLES else "large"
        )
        self._contrast = (
            contrast
            if isinstance(contrast, str) and contrast in self._CONTRASTS
            else "high"
        )
        self._position = (
            position
            if isinstance(position, str) and position in self._POSITIONS
            else "top"
        )
        font_size, padx, pady, wraplength, _min_width, _min_height = (
            self._SIZE_STYLES[self._size]
        )
        self.label.configure(
            font=(
                "Segoe UI",
                font_size,
                "bold" if self._contrast == "high" else "normal",
            ),
            fg="#FFFFFF" if self._contrast == "high" else "#F8FAFC",
            padx=padx,
            pady=pady,
            wraplength=wraplength,
        )
        self.window.update_idletasks()

    def show(self, text: str, state: str, timeout_ms: int | None = None) -> None:
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        color = STATUS_COLORS.get(state, "#334155")
        self.window.configure(bg=color)
        self.label.configure(text=text, bg=color)
        self.window.update_idletasks()
        _font, _padx, _pady, _wrap, min_width, min_height = self._SIZE_STYLES[
            self._size
        ]
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(screen_width - 40, max(min_width, self.label.winfo_reqwidth()))
        height = max(min_height, self.label.winfo_reqheight())
        x = (screen_width - width) // 2
        if self._position == "center":
            y = max(20, (screen_height - height) // 2)
        elif self._position == "bottom":
            y = max(20, screen_height - height - 48)
        else:
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


class NumberOverlay:
    """Click-through number layer for the current UI Automation snapshot."""

    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self._transparent = "#010203"
        self.window.configure(bg=self._transparent)
        try:
            self.window.attributes("-transparentcolor", self._transparent)
        except tk.TclError:
            # Packaged Windows builds support transparentcolor.  If a test or
            # unusual Tk runtime does not, fail closed by keeping the layer
            # hidden until show() verifies usable geometry.
            pass
        self.canvas = tk.Canvas(
            self.window,
            bg=self._transparent,
            highlightthickness=0,
            borderwidth=0,
            takefocus=False,
        )
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()
        inner_hwnd = int(self.window.winfo_id())
        self.hwnd = int(_USER32.GetAncestor(inner_hwnd, 2) or inner_hwnd)
        style = _USER32.GetWindowLongW(self.hwnd, self.GWL_EXSTYLE)
        _USER32.SetWindowLongW(
            self.hwnd,
            self.GWL_EXSTYLE,
            style
            | self.WS_EX_TOOLWINDOW
            | self.WS_EX_NOACTIVATE
            | self.WS_EX_TRANSPARENT,
        )
        self.snapshot_id: str | None = None

    def show(self, snapshot: NumberedElementSnapshot) -> bool:
        self.hide()
        x = int(_USER32.GetSystemMetrics(self.SM_XVIRTUALSCREEN))
        y = int(_USER32.GetSystemMetrics(self.SM_YVIRTUALSCREEN))
        width = int(_USER32.GetSystemMetrics(self.SM_CXVIRTUALSCREEN))
        height = int(_USER32.GetSystemMetrics(self.SM_CYVIRTUALSCREEN))
        if width <= 0 or height <= 0:
            return False
        usable = [
            item
            for item in snapshot.elements
            if item.bounds is not None and item.bounds.usable and not item.offscreen
        ]
        if not usable:
            return False

        self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.canvas.configure(width=width, height=height)
        self.canvas.delete("all")
        for item in usable:
            assert item.bounds is not None
            center_x = item.bounds.left - x + min(max(16, item.bounds.width // 2), 34)
            center_y = item.bounds.top - y + min(max(15, item.bounds.height // 2), 30)
            label = str(item.number)
            radius = 14 if item.number < 10 else 17
            self.canvas.create_oval(
                center_x - radius,
                center_y - radius,
                center_x + radius,
                center_y + radius,
                fill="#FBBF24",
                outline="#111827",
                width=2,
            )
            self.canvas.create_text(
                center_x,
                center_y,
                text=label,
                fill="#111827",
                font=("Segoe UI", 11, "bold"),
            )
        self.snapshot_id = snapshot.snapshot_id
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
        return True

    def hide(self) -> None:
        self.snapshot_id = None
        self.canvas.delete("all")
        _USER32.ShowWindow(self.hwnd, self.SW_HIDE)


_COMMAND_HELP = {
    "Русский": (
        "Режим «Команды»: говорите команду сразу.\n"
        "Смешанный режим: начинайте со слова «команда».\n\n"
        "помощь · покажи номера · нажми номер 7\n"
        "открой Блокнот · переключись на Chrome\n"
        "сверни окно · разверни окно\n"
        "прокрути вниз · нажми на Настройки\n"
        "нажми клавиши контрол си\n"
        "повтори последнее · скопируй последнее\n"
        "отмена · подтверждаю\n\n"
        "Удаление, отправка, Enter и другие чувствительные действия требуют "
        "отдельного подтверждения. Terminal, UAC и команды администратора запрещены."
    ),
    "Español": (
        "Modo Comandos: diga la orden directamente.\n"
        "Modo Mixto: empiece con «comando».\n\n"
        "ayuda · muestra los números · haz clic en el número 7\n"
        "abre Bloc de notas · cambia a Chrome\n"
        "minimiza la ventana · maximiza la ventana\n"
        "desplaza hacia abajo · pulsa Configuración\n"
        "pulsa Control C · repite lo último · copia lo último\n"
        "cancelar · confirmo\n\n"
        "Las acciones sensibles requieren confirmación. Terminal, UAC y "
        "acciones de administrador están bloqueados."
    ),
    "English": (
        "Commands mode: say the command directly.\n"
        "Mixed mode: begin with “command”.\n\n"
        "help · show numbers · click number 7\n"
        "open Notepad · switch to Chrome\n"
        "minimize window · maximize window\n"
        "scroll down · click Settings\n"
        "press Control C · repeat last · copy last\n"
        "cancel · confirm\n\n"
        "Sensitive actions require confirmation. Terminal, UAC and administrator "
        "actions are blocked."
    ),
}


class CommandHelpWindow:
    """Keyboard-accessible, content-complete command reference."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.window: tk.Toplevel | None = None

    def show(self, language: str = "ru") -> None:
        if self.window is not None:
            try:
                if self.window.winfo_exists():
                    self.window.deiconify()
                    self.window.lift()
                    self.window.focus_force()
                    return
            except tk.TclError:
                pass
        window = tk.Toplevel(self.root)
        self.window = window
        window.title("VoiceType Control — Что можно сказать")
        window.geometry("760x560")
        window.minsize(620, 460)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _event: self.close())
        shell = ttk.Frame(window, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(
            shell,
            text="Что можно сказать",
            font=("Segoe UI", 20, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        notebook = ttk.Notebook(shell, takefocus=True)
        notebook.pack(fill="both", expand=True)
        tabs: dict[str, ttk.Frame] = {}
        for title, content in _COMMAND_HELP.items():
            frame = ttk.Frame(notebook, padding=18)
            tabs[title] = frame
            notebook.add(frame, text=title)
            ttk.Label(
                frame,
                text=content,
                font=("Segoe UI", 13),
                justify="left",
                wraplength=660,
            ).pack(anchor="nw", fill="x")
        preferred = {"ru": "Русский", "es": "Español", "en": "English"}.get(
            language, "Русский"
        )
        notebook.select(tabs[preferred])
        close_button = ttk.Button(
            shell,
            text="Закрыть (Esc)",
            command=self.close,
            takefocus=True,
        )
        close_button.pack(anchor="e", pady=(12, 0))
        window.after_idle(notebook.focus_set)

    def close(self) -> None:
        window = self.window
        self.window = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass


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
        on_mode: Callable[[str], None] | None = None,
        get_mode: Callable[[], str] | None = None,
        on_help: Callable[[], None] | None = None,
        on_copy_raw: Callable[[], None] | None = None,
        has_raw_text: Callable[[], bool] | None = None,
        on_clear_session: Callable[[], None] | None = None,
    ) -> None:
        self._get_language = get_language
        self._get_mode = get_mode or (lambda: "dictation")
        self._has_last_text = has_last_text
        self._has_raw_text = has_raw_text or (lambda: False)
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
                    "Режим речи",
                    pystray.Menu(
                        self._mode_item("Диктовка", "dictation", on_mode),
                        self._mode_item("Команды", "commands", on_mode),
                        self._mode_item("Смешанный", "mixed", on_mode),
                    ),
                ),
                pystray.MenuItem(
                    "Вставить последний текст",
                    lambda *_: on_insert_last(),
                    enabled=lambda _: self._has_last_text(),
                ),
                pystray.MenuItem(
                    "Скопировать исходное распознавание",
                    lambda *_: on_copy_raw() if on_copy_raw is not None else None,
                    enabled=lambda _: self._has_raw_text(),
                ),
                pystray.MenuItem(
                    "Очистить текст текущей сессии",
                    lambda *_: on_clear_session()
                    if on_clear_session is not None
                    else None,
                    enabled=lambda _: self._has_last_text() or self._has_raw_text(),
                ),
                pystray.MenuItem(
                    "Что можно сказать…",
                    lambda *_: on_help() if on_help is not None else None,
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

    def _mode_item(
        self,
        label: str,
        value: str,
        callback: Callable[[str], None] | None,
    ) -> pystray.MenuItem:
        return pystray.MenuItem(
            label,
            lambda *_: callback(value) if callback is not None else None,
            checked=lambda _: self._get_mode() == value,
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
