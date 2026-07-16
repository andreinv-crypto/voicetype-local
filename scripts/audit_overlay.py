from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab
from voicetype_local.ui import StatusOverlay


STATES = {
    "ready": ("✓ Готово — нажмите правый Ctrl", "success"),
    "recording": ("● Слушаю…  Правый Ctrl — закончить", "recording"),
    "processing": ("Распознаю локально… 7 с", "processing"),
    "pending": (
        "Текст сохранён. Поставьте курсор куда нужно и нажмите правый Ctrl.",
        "pending",
    ),
}


def main() -> None:
    state = sys.argv[1] if len(sys.argv) > 1 else "ready"
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    text, color_state = STATES[state]
    root = tk.Tk()
    root.withdraw()
    overlay = StatusOverlay(root)
    overlay.show(text, color_state)

    def capture() -> None:
        if output is not None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
            user32.GetWindowRect.restype = wintypes.BOOL
            rect = wintypes.RECT()
            if not user32.GetWindowRect(overlay.hwnd, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            desktop = ImageGrab.grab()
            scale_x = desktop.width / root.winfo_screenwidth()
            scale_y = desktop.height / root.winfo_screenheight()
            output.parent.mkdir(parents=True, exist_ok=True)
            desktop.crop(
                (
                    round(rect.left * scale_x),
                    round(rect.top * scale_y),
                    round(rect.right * scale_x),
                    round(rect.bottom * scale_y),
                )
            ).save(output)
        root.destroy()

    root.after(400, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
