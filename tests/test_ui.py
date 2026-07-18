from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import voicetype_local.ui as ui_module
from voicetype_local.ui import StatusOverlay, TrayController


class _Label:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def winfo_reqwidth(self) -> int:
        return 300

    def winfo_reqheight(self) -> int:
        return 60


class _Window:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)

    def update_idletasks(self) -> None:
        return


class _Root:
    def winfo_screenwidth(self) -> int:
        return 1200

    def winfo_screenheight(self) -> int:
        return 900

    def after(self, _timeout: int, _callback: object) -> str:
        return "job"

    def after_cancel(self, _job: str) -> None:
        return


def _overlay_without_desktop() -> StatusOverlay:
    overlay = StatusOverlay.__new__(StatusOverlay)
    overlay.root = _Root()
    overlay.window = _Window()
    overlay.label = _Label()
    overlay.hwnd = 1
    overlay._hide_job = None
    overlay._size = "large"
    overlay._contrast = "high"
    overlay._position = "top"
    return overlay


def test_overlay_preferences_apply_without_opening_a_window() -> None:
    overlay = _overlay_without_desktop()

    overlay.update_preferences(
        size="extra_large", contrast="standard", position="bottom"
    )

    assert overlay._size == "extra_large"
    assert overlay._contrast == "standard"
    assert overlay._position == "bottom"
    assert overlay.label.options["font"] == ("Segoe UI", 17, "normal")
    assert overlay.label.options["wraplength"] == 760


def test_overlay_invalid_preferences_fail_closed_and_position_is_applied(
    monkeypatch,
) -> None:
    overlay = _overlay_without_desktop()
    positions: list[tuple[object, ...]] = []
    fake_user32 = SimpleNamespace(
        SetWindowPos=lambda *args: positions.append(args),
        ShowWindow=lambda *_args: True,
    )
    monkeypatch.setattr(ui_module, "_USER32", fake_user32)

    overlay.update_preferences(size="huge", contrast="low", position="left")
    overlay.show("Готово", "success")

    assert (overlay._size, overlay._contrast, overlay._position) == (
        "large",
        "high",
        "top",
    )
    assert positions[-1][3:5] == (28, 300)


def _tray(*, on_settings: Callable[[], None] | None) -> TrayController:
    return TrayController(
        on_toggle=lambda: None,
        on_cancel=lambda: None,
        on_insert_last=lambda: None,
        on_copy_last=lambda: None,
        on_language=lambda _language: None,
        on_exit=lambda: None,
        get_language=lambda: "auto",
        has_last_text=lambda: False,
        on_settings=on_settings,
    )


def test_tray_activation_opens_settings_and_keeps_visible_menu_item() -> None:
    opened: list[str] = []
    tray = _tray(on_settings=lambda: opened.append("settings"))

    settings_item = next(
        item
        for item in tray.icon.menu.items
        if item.text == "Настройки и приватность…"
    )

    assert settings_item.visible is True
    assert settings_item.default is True

    # pystray dispatches primary icon activation through the menu's default
    # item; invoke that public behaviour directly without starting a GUI loop.
    tray.icon.menu(tray.icon)

    assert opened == ["settings"]


def test_tray_activation_is_safe_when_settings_callback_is_absent() -> None:
    tray = _tray(on_settings=None)

    assert tray.icon.menu(tray.icon) is None
