from __future__ import annotations

import io
import queue
import threading
import time
from types import SimpleNamespace

import voicetype_local.app as app_module
from voicetype_local.app import AppState, VoiceTypeApp


class _Root:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []

    def after(self, milliseconds: int, callback: object) -> None:
        self.after_calls.append((milliseconds, callback))


class _Overlay:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int | None]] = []

    def show(self, text: str, state: str, timeout: int | None = None) -> None:
        self.messages.append((text, state, timeout))


class _Tray:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.icon = SimpleNamespace(update_menu=lambda: None)

    def set_state(self, state: str, title: str) -> None:
        del title
        self.states.append(state)


class _SettingsStore:
    def get(self) -> SimpleNamespace:
        return SimpleNamespace(
            language="auto",
            microphone="default",
            max_record_seconds=300,
            sounds=False,
        )


def _bare_app() -> VoiceTypeApp:
    app = VoiceTypeApp.__new__(VoiceTypeApp)
    app.root = _Root()
    app.overlay = _Overlay()
    app.tray = _Tray()
    app.settings_store = _SettingsStore()
    app.state = AppState.IDLE
    app.last_text = ""
    app._target_hwnd = 0
    app._generation = 0
    app._starting_started_at = 0.0
    app._recording_started_at = 0.0
    app._processing_started_at = 0.0
    app._actions = queue.Queue()
    app._record_timer = None
    app._closing = False
    app._sound = lambda _alias: None  # type: ignore[method-assign]
    return app


def test_microphone_start_never_blocks_the_ui(monkeypatch) -> None:
    app = _bare_app()
    entered = threading.Event()
    release = threading.Event()

    class _Recorder:
        def start(self, _microphone: str) -> None:
            entered.set()
            release.wait(2)

    app.recorder = _Recorder()
    monkeypatch.setattr(app_module, "_foreground_window", lambda: 123)

    before = time.perf_counter()
    app.start_recording()
    elapsed = time.perf_counter() - before

    assert elapsed < 0.2
    assert app.state == AppState.STARTING
    assert entered.wait(1)

    release.set()
    app._actions.get(timeout=1)()
    assert app.state == AppState.RECORDING
    assert app._target_hwnd == 123
    app._cancel_timer()


def test_microphone_stop_never_blocks_the_ui() -> None:
    app = _bare_app()
    entered = threading.Event()
    release = threading.Event()

    class _Recorder:
        duration = 0.1

        def stop(self) -> io.BytesIO:
            entered.set()
            release.wait(2)
            return io.BytesIO(b"audio")

    app.recorder = _Recorder()
    app.state = AppState.RECORDING

    before = time.perf_counter()
    app.stop_recording()
    elapsed = time.perf_counter() - before

    assert elapsed < 0.2
    assert app.state == AppState.PROCESSING
    assert entered.wait(1)

    release.set()
    app._actions.get(timeout=1)()
    assert app.state == AppState.IDLE


def test_changed_window_saves_text_for_next_right_ctrl(monkeypatch) -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 7
    app._target_hwnd = 100
    monkeypatch.setattr(app_module, "_foreground_window", lambda: 200)

    app._transcription_ready("Сохранённый текст", "ru", 0.95, 7)

    assert app.state == AppState.PENDING_INSERT
    assert inserted == []
    assert app.last_text == "Сохранённый текст"

    app.toggle()
    assert inserted == ["Сохранённый текст"]
    assert app.state == AppState.IDLE


def test_same_window_inserts_and_returns_to_ready(monkeypatch) -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 9
    app._target_hwnd = 300
    monkeypatch.setattr(app_module, "_foreground_window", lambda: 300)

    app._transcription_ready("Готово", "ru", 0.9, 9)

    assert inserted == ["Готово"]
    assert app.state == AppState.IDLE
