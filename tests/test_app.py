from __future__ import annotations

import io
import queue
import threading
import time
from dataclasses import asdict
from types import SimpleNamespace

import voicetype_local.app as app_module
from voicetype_local.app import AppState, VoiceTypeApp
from voicetype_local.config import Settings
from voicetype_local.diagnostics import NullTechnicalLogger
from voicetype_local.focus import FocusMatch, FocusTarget
from voicetype_local.memory import MemoryContext
from voicetype_local.correction import CorrectionResult
from voicetype_local.inserter import TextInsertionError


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
    def __init__(self) -> None:
        self.settings = Settings(sounds=False)

    def get(self) -> Settings:
        return Settings(**asdict(self.settings))

    def update(self, **changes) -> Settings:
        for key, value in changes.items():
            setattr(self.settings, key, value)
        self.settings.validate()
        return self.get()


class _FocusInspector:
    def __init__(self) -> None:
        self.target = FocusTarget(
            foreground_hwnd=10,
            root_hwnd=10,
            focused_hwnd=11,
            thread_id=12,
            process_id=13,
            focused_class_name="Edit",
            stable=True,
        )
        self.match = FocusMatch.SAME

    def capture(self) -> FocusTarget:
        return self.target

    def compare_current(self, _expected: FocusTarget | None) -> FocusMatch:
        return self.match


class _PromptBuilder:
    def build_pre_asr(self, _store, _context):
        return SimpleNamespace(text="")


class _MemoryStore:
    def install_pack(self, _document):
        return None


def _bare_app() -> VoiceTypeApp:
    app = VoiceTypeApp.__new__(VoiceTypeApp)
    app.root = _Root()
    app.overlay = _Overlay()
    app.tray = _Tray()
    app.settings_store = _SettingsStore()
    app.logger = NullTechnicalLogger()
    app.state = AppState.IDLE
    app.last_text = ""
    app._insertion_uncertain = False
    app.focus_inspector = _FocusInspector()
    app.memory_store = _MemoryStore()
    app.prompt_builder = _PromptBuilder()
    app.cloud_transcriber = None
    app._target_focus = None
    app._memory_context = MemoryContext()
    app._pre_prompt = None
    app._generation = 0
    app._starting_started_at = 0.0
    app._recording_started_at = 0.0
    app._processing_started_at = 0.0
    app._processing_stage = ""
    app._actions = queue.Queue()
    app._record_timer = None
    app._closing = False
    app._audio_backend_blocked = False
    app._blocked_audio_recorder = None
    app._recorder_factory = lambda: app.recorder
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

    recorder = _Recorder()
    app.recorder = recorder
    app._recorder_factory = lambda: recorder

    before = time.perf_counter()
    app.start_recording()
    elapsed = time.perf_counter() - before

    assert elapsed < 0.2
    assert app.state == AppState.STARTING
    assert entered.wait(1)

    release.set()
    app._actions.get(timeout=1)()
    assert app.state == AppState.RECORDING
    assert app._target_focus == app.focus_inspector.target
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


def test_changed_window_saves_text_for_next_right_ctrl() -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 7
    app._target_focus = app.focus_inspector.target
    app.focus_inspector.match = FocusMatch.CHANGED

    app._final_text_ready("Сохранённый текст", "ru", 0.95, 7)

    assert app.state == AppState.PENDING_INSERT
    assert inserted == []
    assert app.last_text == "Сохранённый текст"

    app.toggle()
    assert inserted == ["Сохранённый текст"]
    assert app.state == AppState.IDLE


def test_same_window_inserts_and_returns_to_ready() -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 9
    app._target_focus = app.focus_inspector.target
    app.focus_inspector.match = FocusMatch.SAME

    app._final_text_ready("Готово", "ru", 0.9, 9)

    assert inserted == ["Готово"]
    assert app.state == AppState.IDLE


def test_unknown_field_never_auto_inserts() -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 10
    app._target_focus = app.focus_inspector.target
    app.focus_inspector.match = FocusMatch.UNKNOWN

    app._final_text_ready("Безопасно сохранено", "ru", 0.9, 10)

    assert inserted == []
    assert app.state == AppState.PENDING_INSERT
    assert app.last_text == "Безопасно сохранено"


def test_microphone_start_timeout_recovers_without_waiting_for_driver() -> None:
    app = _bare_app()
    cancelled = threading.Event()

    class _Recorder:
        def cancel(self) -> None:
            cancelled.set()

    timed_out = _Recorder()
    replacement = _Recorder()
    app.recorder = timed_out
    app._recorder_factory = lambda: replacement
    app.state = AppState.STARTING
    app._generation = 3
    app._target_focus = app.focus_inspector.target

    app._starting_timeout(3, timed_out)  # type: ignore[arg-type]

    assert app.state == AppState.IDLE
    assert app._generation == 4
    assert app._target_focus is None
    assert app.recorder is replacement
    assert cancelled.wait(1)


def test_unabortable_native_open_uses_circuit_breaker_until_it_returns() -> None:
    app = _bare_app()
    cancelled = threading.Event()

    class _Recorder:
        native_open_unabortable = True

        def cancel(self) -> None:
            cancelled.set()

    timed_out = _Recorder()
    replacement = _Recorder()
    app.recorder = timed_out
    app._recorder_factory = lambda: replacement
    app.state = AppState.STARTING
    app._generation = 4

    app._starting_timeout(4, timed_out)  # type: ignore[arg-type]

    assert app._audio_backend_blocked is True
    generation = app._generation
    app.start_recording()
    assert app._generation == generation
    assert app.state == AppState.IDLE
    assert cancelled.wait(1)

    timed_out.native_open_unabortable = False
    app._audio_open_finished(timed_out)  # type: ignore[arg-type]
    assert app._audio_backend_blocked is False


def test_microphone_stop_timeout_invalidates_stale_worker() -> None:
    app = _bare_app()
    cancelled = threading.Event()

    class _Recorder:
        def cancel(self) -> None:
            cancelled.set()

    timed_out = _Recorder()
    replacement = _Recorder()
    app.recorder = timed_out
    app._recorder_factory = lambda: replacement
    app.state = AppState.PROCESSING
    app._processing_stage = "audio_stop"
    app._generation = 5
    app._target_focus = app.focus_inspector.target

    app._audio_stop_timeout(5, timed_out)  # type: ignore[arg-type]

    assert app.state == AppState.IDLE
    assert app._generation == 6
    assert app.recorder is replacement
    assert cancelled.wait(1)


def test_optional_correction_never_blocks_ui_and_posts_final_text() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 12
    app._target_focus = app.focus_inspector.target
    app.inserter = SimpleNamespace(insert=lambda _text: None)
    release = threading.Event()

    class _Pipeline:
        def correct(self, text, _context):
            release.wait(1)
            return CorrectionResult(text + ".", True, "test")

    app.correction_pipeline = _Pipeline()

    before = time.perf_counter()
    app._transcription_ready("Готово", "ru", 0.9, 12)
    elapsed = time.perf_counter() - before

    assert elapsed < 0.2
    assert app.state == AppState.PROCESSING
    release.set()
    app._actions.get(timeout=1)()
    assert app.last_text == "Готово."
    assert app.state == AppState.IDLE


def test_cloud_transcription_failure_falls_back_to_local_with_memory_prompt() -> None:
    app = _bare_app()
    app._generation = 14
    app._pre_prompt = SimpleNamespace(text="GitHub; WordPress")
    local_calls: list[tuple[str, str]] = []

    cloud_prompts: list[str] = []

    class _Cloud:
        def transcribe(self, _audio, _language, prompt):
            cloud_prompts.append(prompt)
            raise TimeoutError("offline")

    class _Local:
        def transcribe(self, _audio, _language, *, initial_prompt, hotwords):
            local_calls.append((initial_prompt, hotwords))
            return "локальный текст", "ru", 0.8

    app.cloud_transcriber = _Cloud()
    app.transcriber = _Local()
    audio = io.BytesIO(b"audio")

    app._transcribe_worker(audio, "auto", 14)

    assert local_calls == [
        ("Точная диктовка. Dictado claro. Clear dictation.", "GitHub; WordPress")
    ]
    assert cloud_prompts == ["Точная диктовка. Dictado claro. Clear dictation."]
    assert audio.closed
    assert not app._actions.empty()


def test_cancelled_cloud_failure_does_not_use_replacement_local_worker() -> None:
    app = _bare_app()
    app._generation = 21
    local_calls: list[bool] = []

    class _Cloud:
        def transcribe(self, *_args):
            app._generation += 1
            raise TimeoutError("cancelled")

    class _Local:
        def transcribe(self, *_args, **_kwargs):
            local_calls.append(True)
            return "stale", "ru", 0.5

    app.cloud_transcriber = _Cloud()
    app.transcriber = _Local()

    app._transcribe_worker(io.BytesIO(b"audio"), "ru", 21)

    assert local_calls == []
    assert app._actions.empty()


def test_action_queue_stops_immediately_after_closing() -> None:
    app = _bare_app()
    ran_after_close: list[bool] = []
    app._actions.put(lambda: setattr(app, "_closing", True))
    app._actions.put(lambda: ran_after_close.append(True))

    app._drain_actions()

    assert ran_after_close == []
    assert app._actions.empty()


def test_cancel_pending_returns_to_ready_without_losing_last_text() -> None:
    app = _bare_app()
    app.state = AppState.PENDING_INSERT
    app.last_text = "важный текст"

    app.cancel()

    assert app.state == AppState.IDLE
    assert app.last_text == "важный текст"


def test_partial_insertion_is_never_retried_blindly() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 23
    app._target_focus = app.focus_inspector.target
    attempts: list[str] = []

    def partial(text: str) -> None:
        attempts.append(text)
        raise TextInsertionError("partial", partial=True)

    app.inserter = SimpleNamespace(insert=partial)

    app._final_text_ready("полный текст", "ru", 0.9, 23)
    app.toggle()

    assert app.state == AppState.PENDING_INSERT
    assert attempts == ["полный текст"]

    app.cancel()
    app.repeat_last()

    assert app.state == AppState.IDLE
    assert attempts == ["полный текст"]


def test_stale_correction_never_calls_provider() -> None:
    app = _bare_app()
    app._generation = 31
    calls: list[str] = []

    class _Pipeline:
        def correct(self, text, _context):
            calls.append(text)
            return CorrectionResult(text, False, "test")

    app._correction_worker(
        "stale",
        SimpleNamespace(),
        "ru",
        0.8,
        30,
        _Pipeline(),
    )

    assert calls == []


def test_deleting_api_key_rebuilds_cloud_providers_immediately(monkeypatch) -> None:
    app = _bare_app()
    deleted: list[str] = []
    app.secret_store = SimpleNamespace(delete=deleted.append)
    disabled_correction = object()
    monkeypatch.setattr(
        app_module,
        "build_correction_pipeline",
        lambda _settings, secret_store: disabled_correction,
    )
    monkeypatch.setattr(
        app_module,
        "build_cloud_transcriber",
        lambda _settings, secret_store: None,
    )

    app._delete_openai_key()

    assert deleted == ["openai_api_key"]
    assert app.correction_pipeline is disabled_correction
    assert app.cloud_transcriber is None


def test_overlay_and_tray_failures_do_not_break_state_machine() -> None:
    app = _bare_app()
    app.overlay = SimpleNamespace(show=lambda *_args: (_ for _ in ()).throw(RuntimeError()))
    app.tray = SimpleNamespace(set_state=lambda *_args: (_ for _ in ()).throw(RuntimeError()))

    app._set_state(AppState.IDLE, "готов")
    app._show_status("готов", "success")

    assert app.state == AppState.IDLE
