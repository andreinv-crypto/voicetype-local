from __future__ import annotations

"""Open the real settings surface in an isolated visual-QA harness."""

import argparse
import tkinter as tk

from voicetype_local.config import Settings
from voicetype_local.settings_ui import open_settings_window


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", default="")
    parser.add_argument("--long-feedback", action="store_true")
    args = parser.parse_args()

    root = tk.Tk()
    # A small visible host keeps the real transient settings window targetable
    # by Windows capture tools during visual QA.  Production still uses the
    # normal hidden tray root from VoiceTypeApp.
    root.title("VoiceType Local Preview Host")
    root.geometry("320x160+0+0")
    feedback = {
        "heard": "Напиши другу, нажать Enter",
        "text": "Напиши другу",
        "command": "Нажать Enter",
        "outcome": "Команда выполнена успешно",
        "tone": "success",
    }
    if args.long_feedback:
        long_message = " ".join(
            "Это длинная тестовая диктовка для проверки устойчивости карточки."
            for _ in range(12)
        )
        feedback.update(
            {
                "heard": long_message,
                "text": long_message,
                "command": "Команда в конце длинного сообщения: нажать Enter",
            }
        )

    def finish() -> None:
        root.after_idle(root.destroy)

    settings_window = open_settings_window(
        root,
        Settings(interaction_mode="mixed", language="auto"),
        on_save=lambda _changes: finish(),
        on_cancel=finish,
        microphone_options=("USB Speech Microphone", "Webcam Microphone"),
        last_text="Напиши другу",
        last_raw_text="Напиши другу, нажать Enter",
        runtime_feedback=lambda: feedback,
        status_text="Готово к работе",
    )
    if args.geometry:
        settings_window.window.geometry(args.geometry)
    root.mainloop()


if __name__ == "__main__":
    main()
