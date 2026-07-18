from __future__ import annotations

import io
import queue
import threading
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

import voicetype_local.app as app_module
from voicetype_local.app import AppState, VoiceTypeApp
from voicetype_local.config import Settings
from voicetype_local.diagnostics import NullTechnicalLogger
from voicetype_local.focus import FocusMatch, FocusTarget
from voicetype_local.memory import MemoryContext
from voicetype_local.correction import CorrectionResult
from voicetype_local.inserter import TextInsertionError
from voicetype_local.session_editing import (
    SessionEditAction,
    SessionEditRequest,
    plan_session_edit,
)
from voicetype_local.session_editor import SessionEditorResult, SessionEditorStatus
from voicetype_local.voice_control import VoiceControlRouter
from voicetype_local.windows_control import (
    CanonicalIntent,
    ControlRequest,
    ControlResult,
    ElementBounds,
    NumberedElement,
    NumberedElementSnapshot,
    ResultStatus,
)


class _Root:
    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []
        self.clipboard = ""

    def after(self, milliseconds: int, callback: object) -> None:
        self.after_calls.append((milliseconds, callback))

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, text: str) -> None:
        self.clipboard += text

    def update_idletasks(self) -> None:
        return


class _Overlay:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, int | None]] = []
        self.preferences: list[dict[str, str]] = []

    def show(self, text: str, state: str, timeout: int | None = None) -> None:
        self.messages.append((text, state, timeout))

    def update_preferences(self, **preferences: str) -> None:
        self.preferences.append(preferences)


class _Tray:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.icon = SimpleNamespace(update_menu=lambda: None)

    def set_state(self, state: str, title: str) -> None:
        del title
        self.states.append(state)


class _NumberOverlay:
    def __init__(self) -> None:
        self.snapshot_id: str | None = None

    def show(self, snapshot) -> bool:
        self.snapshot_id = snapshot.snapshot_id
        return True

    def hide(self) -> None:
        self.snapshot_id = None


class _CommandHelp:
    def __init__(self) -> None:
        self.languages: list[str] = []

    def show(self, language: str) -> None:
        self.languages.append(language)

    def close(self) -> None:
        return


class _ControlExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[ControlRequest, bool]] = []
        self.cancel_count = 0
        self.clear_count = 0

    def execute(
        self, request: ControlRequest, *, confirmed: bool = False
    ) -> ControlResult:
        self.calls.append((request, confirmed))
        return ControlResult(ResultStatus.EXECUTED, request.intent, backend="fake")

    def capture_numbered_elements(self):
        return None

    def is_snapshot_current(self, _snapshot_id: str) -> bool:
        return True

    def cancel_pending(self) -> None:
        self.cancel_count += 1

    def clear_snapshot(self) -> None:
        self.clear_count += 1


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
    app.number_overlay = _NumberOverlay()
    app.command_help = _CommandHelp()
    app.tray = _Tray()
    app.settings_store = _SettingsStore()
    app.logger = NullTechnicalLogger()
    app.state = AppState.IDLE
    app.last_raw_text = ""
    app.last_local_text = ""
    app.last_text = ""
    app._runtime_feedback = {
        "heard": "",
        "text": "",
        "command": "",
        "outcome": "Готово к работе",
        "tone": "ready",
    }
    app._insertion_uncertain = False
    app._last_insertion_target = None
    app._last_insertion_at = 0.0
    app._last_insertion_tail = ""
    app._tracked_session_insertion = None
    app._last_session_edit = None
    app.focus_inspector = _FocusInspector()
    app.session_editor = SimpleNamespace(
        apply=lambda *_args, **_kwargs: SessionEditorResult(
            SessionEditorStatus.UNAVAILABLE, "not_configured"
        )
    )
    app.voice_router = VoiceControlRouter()
    app.control_executor = _ControlExecutor()
    app.memory_store = _MemoryStore()
    app.prompt_builder = _PromptBuilder()
    app.cloud_transcriber = None
    app._target_focus = None
    app._pending_control_request = None
    app._pending_dictation_action = None
    app._pending_control_deadline = 0.0
    app._pending_control_token = 0
    app._number_snapshot = None
    app._number_snapshot_poll_token = 0
    app._number_snapshot_poll_in_flight = None
    app._number_snapshot_poll_queue = queue.Queue(maxsize=1)
    app._number_snapshot_poll_stop = threading.Event()
    app._number_snapshot_poll_worker = None
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
    app._activation_waiter = None
    app._activation_listener_stop = threading.Event()
    app._activation_listener_thread = None
    app._open_settings_on_start = False
    app._silent_start = False
    app._settings_window = None
    app._microphone_options_cache = ()
    app._microphone_scan_lock = threading.Lock()
    app._microphone_scan_in_progress = False
    app._audio_backend_blocked = False
    app._blocked_audio_recorder = None
    app._recorder_factory = lambda: app.recorder
    app._sound = lambda _alias: None  # type: ignore[method-assign]
    return app


def test_instance_activation_listener_posts_settings_on_ui_queue() -> None:
    app = _bare_app()
    responses = iter((True, False))
    waits: list[int] = []

    def wait_for_activation(timeout_ms: int) -> bool:
        waits.append(timeout_ms)
        requested = next(responses)
        if not requested:
            app._activation_listener_stop.set()
        return requested

    app._activation_waiter = wait_for_activation
    app._activation_listener_loop()

    queued = app._actions.get_nowait()
    assert queued == app.open_settings
    assert waits == [250, 250]


def test_silent_start_suppresses_model_ready_overlay_only() -> None:
    app = _bare_app()
    app.state = AppState.LOADING
    app._model_load_started_at = 0.0
    app._silent_start = True

    app._model_ready()

    assert app.state == AppState.IDLE
    assert app.overlay.messages == []
    app._show_status("● Слушаю…", "recording")
    assert app.overlay.messages == [("● Слушаю…", "recording", None)]


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


def test_microphone_device_scan_is_backgrounded_and_single_flight(monkeypatch) -> None:
    app = _bare_app()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_devices() -> list[str]:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2)
        return ["Test microphone"]

    monkeypatch.setattr(app_module.AudioRecorder, "input_devices", slow_devices)

    before = time.perf_counter()
    app._start_microphone_scan()
    app._start_microphone_scan()
    elapsed = time.perf_counter() - before

    assert elapsed < 0.2
    assert entered.wait(1)
    assert calls == 1
    release.set()
    app._actions.get(timeout=1)()
    assert app._microphone_options_cache == ("Test microphone",)

    entered.clear()
    app._start_microphone_scan()
    assert entered.wait(1)
    app._actions.get(timeout=1)()
    assert calls == 2


def test_hold_release_stops_recording_and_cancels_unfinished_start() -> None:
    app = _bare_app()
    calls: list[str] = []
    app.cancel = lambda: calls.append("cancel")  # type: ignore[method-assign]
    app.stop_recording = lambda: calls.append("stop")  # type: ignore[method-assign]

    app.state = AppState.STARTING
    app._hold_release()
    app.state = AppState.RECORDING
    app._hold_release()
    app.state = AppState.IDLE
    app._hold_release()

    assert calls == ["cancel", "stop"]


def test_settings_recreate_listener_when_activation_mode_changes(
    monkeypatch,
) -> None:
    app = _bare_app()
    stopped: list[str] = []
    app.hotkeys = SimpleNamespace(stop=lambda: stopped.append("old"))
    app.secret_store = object()
    created: list[object] = []

    class _Listener:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs
            self.started = False
            created.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr(app_module, "GlobalHotkeyListener", _Listener)
    monkeypatch.setattr(app_module, "model_dir", lambda _name: app_module.Path("."))
    monkeypatch.setattr(app_module, "whisper_token_counter", lambda _path: None)
    monkeypatch.setattr(app_module, "PromptBuilder", lambda **_kwargs: object())
    monkeypatch.setattr(
        app_module, "build_correction_pipeline", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        app_module, "build_cloud_transcriber", lambda *_args, **_kwargs: object()
    )

    app._settings_saved(
        {
            "activation_mode": "hold",
            "overlay_size": "extra_large",
            "overlay_contrast": "standard",
            "overlay_position": "bottom",
        }
    )

    assert stopped == ["old"]
    assert len(created) == 1
    listener = created[0]
    assert listener.args[1] == "right_ctrl"  # type: ignore[attr-defined]
    assert listener.kwargs["activation_mode"] == "hold"  # type: ignore[attr-defined]
    assert listener.kwargs["on_hold_stop"].__self__ is app  # type: ignore[attr-defined]
    assert listener.started is True  # type: ignore[attr-defined]
    assert app.overlay.preferences[-1] == {
        "size": "extra_large",
        "contrast": "standard",
        "position": "bottom",
    }


def test_settings_provider_preflight_failure_keeps_old_runtime_and_config(
    monkeypatch,
) -> None:
    app = _bare_app()
    app.secret_store = object()
    old_prompt = app.prompt_builder
    old_correction = object()
    old_cloud = object()
    app.correction_pipeline = old_correction
    app.cloud_transcriber = old_cloud
    previous = app.settings_store.get()

    monkeypatch.setattr(app_module, "model_dir", lambda _name: app_module.Path("."))
    monkeypatch.setattr(app_module, "whisper_token_counter", lambda _path: None)
    monkeypatch.setattr(app_module, "PromptBuilder", lambda **_kwargs: object())
    monkeypatch.setattr(
        app_module,
        "build_correction_pipeline",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider preflight failed")
        ),
    )

    with pytest.raises(RuntimeError, match="provider preflight failed"):
        app._settings_saved({"language": "es"})

    assert app.settings_store.get() == previous
    assert app.prompt_builder is old_prompt
    assert app.correction_pipeline is old_correction
    assert app.cloud_transcriber is old_cloud


def test_settings_hotkey_start_failure_rolls_back_config_and_runtime(
    monkeypatch,
) -> None:
    app = _bare_app()
    app.secret_store = object()
    events: list[str] = []

    class _OldListener:
        def stop(self) -> None:
            events.append("old-stop")

        def start(self) -> None:
            events.append("old-restart")

    class _FailingListener:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            events.append("new-start")
            raise OSError("hook unavailable")

        def stop(self) -> None:
            events.append("new-stop")

    old_listener = _OldListener()
    app.hotkeys = old_listener
    previous = app.settings_store.get()
    monkeypatch.setattr(app_module, "GlobalHotkeyListener", _FailingListener)
    monkeypatch.setattr(app_module, "model_dir", lambda _name: app_module.Path("."))
    monkeypatch.setattr(app_module, "whisper_token_counter", lambda _path: None)
    monkeypatch.setattr(app_module, "PromptBuilder", lambda **_kwargs: object())
    monkeypatch.setattr(
        app_module, "build_correction_pipeline", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        app_module, "build_cloud_transcriber", lambda *_args, **_kwargs: object()
    )

    with pytest.raises(OSError, match="hook unavailable"):
        app._settings_saved({"activation_mode": "hold"})

    assert events == ["old-stop", "new-start", "new-stop", "old-restart"]
    assert app.settings_store.get() == previous
    assert app.hotkeys is old_listener


def test_changed_window_saves_text_for_next_right_ctrl() -> None:
    app = _bare_app()
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.state = AppState.PROCESSING
    app._generation = 7
    app._target_focus = app.focus_inspector.target
    app._last_insertion_target = app.focus_inspector.target
    app._last_insertion_at = time.monotonic()
    app._last_insertion_tail = "x"
    app.focus_inspector.match = FocusMatch.CHANGED

    app._final_text_ready("Сохранённый текст", "ru", 0.95, 7)

    assert app.state == AppState.PENDING_INSERT
    assert inserted == []
    assert app.last_text == "Сохранённый текст"
    assert app._last_insertion_target is None

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


def test_automatic_spacing_applies_only_to_recent_same_target() -> None:
    app = _bare_app()
    app.settings_store.settings.automatic_spacing = True
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)

    app.state = AppState.PROCESSING
    app._generation = 51
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("Hello", "en", 0.9, 51)

    app.state = AppState.PROCESSING
    app._generation = 52
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("world", "en", 0.9, 52)

    assert inserted == ["Hello", " world"]
    assert app.last_text == "world"
    assert app._last_insertion_tail == "d"


def test_automatic_spacing_expires_after_thirty_seconds() -> None:
    app = _bare_app()
    app.settings_store.settings.automatic_spacing = True
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)

    app.state = AppState.PROCESSING
    app._generation = 53
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("First", "en", 0.9, 53)
    app._last_insertion_at = time.monotonic() - 31.0

    app.state = AppState.PROCESSING
    app._generation = 54
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("Second", "en", 0.9, 54)

    assert inserted == ["First", "Second"]


def test_automatic_spacing_does_not_cross_focus_targets() -> None:
    app = _bare_app()
    app.settings_store.settings.automatic_spacing = True
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)

    app.state = AppState.PROCESSING
    app._generation = 55
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("First", "en", 0.9, 55)

    changed_target = FocusTarget(
        foreground_hwnd=20,
        root_hwnd=20,
        focused_hwnd=21,
        thread_id=22,
        process_id=23,
        focused_class_name="Edit",
        stable=True,
    )
    app.state = AppState.PROCESSING
    app._generation = 56
    app._target_focus = changed_target
    app._final_text_ready("Second", "en", 0.9, 56)

    assert inserted == ["First", "Second"]
    assert app._last_insertion_target is changed_target


def test_failed_insertion_clears_automatic_spacing_tracker() -> None:
    app = _bare_app()
    app.settings_store.settings.automatic_spacing = True
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)

    app.state = AppState.PROCESSING
    app._generation = 57
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("First", "en", 0.9, 57)

    def fail(text: str) -> None:
        inserted.append(text)
        raise TextInsertionError("failed", partial=False)

    app.inserter = SimpleNamespace(insert=fail)
    app.state = AppState.PROCESSING
    app._generation = 58
    app._target_focus = app.focus_inspector.target
    app._final_text_ready("Second", "en", 0.9, 58)

    assert inserted == ["First", " Second"]
    assert app._last_insertion_target is None
    assert app._last_insertion_tail == ""


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


def test_current_dictation_stages_stay_available_in_session_ram() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 12
    app._target_focus = app.focus_inspector.target
    app.inserter = SimpleNamespace(insert=lambda _text: None)
    app.prompt_builder = SimpleNamespace(
        build_post_asr=lambda *_args, **_kwargs: SimpleNamespace(terms=(), styles=())
    )
    app.memory_store = SimpleNamespace(
        replacement_candidates=lambda *_args, **_kwargs: (),
        mark_terms_used=lambda *_args, **_kwargs: None,
    )
    app.normalizer = SimpleNamespace(
        normalize=lambda text, *_args, **_kwargs: SimpleNamespace(
            text=f"{text} словарь", applied=()
        )
    )
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(
            text=f"{text} итог",
            changed=True,
            provider="local",
            fallback_reason=None,
        )
    )

    app._transcription_ready("сырой", "ru", 0.9, 12)

    assert app.last_raw_text == "сырой"
    assert app.last_local_text == "сырой словарь"
    app._actions.get(timeout=1)()
    assert app.last_text == "сырой словарь итог"


def test_dictation_transform_keeps_raw_text_and_logs_metadata_only() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 41
    app._target_focus = app.focus_inspector.target
    inserted: list[str] = []
    events: list[tuple[str, dict[str, object]]] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )
    app.logger = SimpleNamespace(
        event=lambda event, **fields: events.append((event, fields)),
        close=lambda: None,
    )
    source = "Привет запятая мир новая строка Дальше"

    app._transcription_ready(source, "ru", 0.9, 41)

    assert app.last_raw_text == source
    assert app.last_local_text == "Привет, мир\nДальше"
    app._actions.get(timeout=1)()
    assert inserted == ["Привет, мир\nДальше"]
    transform_events = [
        (event, fields)
        for event, fields in events
        if event == "dictation_transform_completed"
    ]
    assert transform_events == [
        (
            "dictation_transform_completed",
            {"operation": "commands", "code": "comma+new_line", "count": 2},
        )
    ]
    assert source not in repr(transform_events)


def test_filler_setting_does_not_force_dictation_commands_on() -> None:
    app = _bare_app()
    app.settings_store.settings.dictation_commands_enabled = False
    app.settings_store.settings.remove_fillers = True
    app.state = AppState.PROCESSING
    app._generation = 42
    app._target_focus = app.focus_inspector.target
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )

    app._transcription_ready("um hello comma world", "en", 0.9, 42)
    app._actions.get(timeout=1)()

    assert app.last_raw_text == "um hello comma world"
    assert app.last_text == "hello comma world"
    assert inserted == ["hello comma world"]


def test_new_dictation_clears_stale_control_confirmation_and_numbers() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.state = AppState.PROCESSING
    app._generation = 43
    app._target_focus = app.focus_inspector.target
    app._pending_control_request = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")
    app._pending_control_deadline = time.monotonic() + 30.0
    app.number_overlay.snapshot_id = "stale-snapshot"
    app._number_snapshot = SimpleNamespace(snapshot_id="stale-snapshot")
    app.inserter = SimpleNamespace(insert=lambda _text: None)
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )

    app._transcription_ready("ordinary dictation", "en", 0.9, 43)
    app._actions.get(timeout=1)()

    assert app._pending_control_request is None
    assert app._pending_control_deadline == 0.0
    assert app.number_overlay.snapshot_id is None
    assert app._number_snapshot is None
    assert app.control_executor.cancel_count >= 1
    assert app.control_executor.clear_count >= 1


def test_copy_raw_uses_only_current_session_text() -> None:
    app = _bare_app()
    app.last_raw_text = "исходное распознавание"

    app.copy_raw()

    assert app.root.clipboard == "исходное распознавание"


def test_clear_session_text_removes_all_ram_stages() -> None:
    app = _bare_app()
    app.state = AppState.PENDING_INSERT
    app.last_raw_text = "сырой"
    app.last_local_text = "словарный"
    app.last_text = "итог"
    app._insertion_uncertain = True

    app.clear_session_text()

    assert app.last_raw_text == ""
    assert app.last_local_text == ""
    assert app.last_text == ""
    assert app._insertion_uncertain is False
    assert app.state == AppState.IDLE


def test_clear_session_text_invalidates_in_flight_session_edit_worker() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._processing_stage = "session_edit"
    app._generation = 30
    app.last_raw_text = "исходный текст"
    app.last_local_text = "Первое. Второе."
    app.last_text = "Первое. Второе."
    tracked = app_module._TrackedSessionInsertion(
        app.last_text,
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._tracked_session_insertion = tracked
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_SENTENCE,
        language="ru",  # type: ignore[arg-type]
    )
    plan = plan_session_edit(tracked.text, request)
    entered = threading.Event()
    release = threading.Event()

    def apply(_plan, _target, *, is_current):
        assert is_current()
        entered.set()
        assert release.wait(2)
        return SessionEditorResult(
            SessionEditorStatus.EXECUTED,
            "ok",
            backend="delayed-fake",
        )

    app.session_editor = SimpleNamespace(apply=apply)
    worker = threading.Thread(
        target=app._session_edit_worker,
        args=(request, plan, tracked, 30),
    )
    worker.start()
    assert entered.wait(1)

    app.clear_session_text()
    feedback_after_clear = dict(app._runtime_feedback)
    release.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert app._generation == 31
    assert app.state == AppState.IDLE
    assert app.last_raw_text == ""
    assert app.last_local_text == ""
    assert app.last_text == ""
    assert app._tracked_session_insertion is None
    assert app._last_session_edit is None
    assert app._insertion_uncertain is True
    assert app._actions.empty()
    assert app._runtime_feedback == feedback_after_clear
    assert app._runtime_feedback["heard"] == ""
    assert app._runtime_feedback["text"] == ""
    assert app._runtime_feedback["command"] == ""
    assert "не подтверждён" in app._runtime_feedback["outcome"]


def test_commands_mode_executes_typed_request_without_retaining_spoken_text() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "commands"
    app.state = AppState.PROCESSING
    app._generation = 31
    app._target_focus = app.focus_inspector.target

    app._transcription_ready("открой блокнот", "ru", 0.9, 31)
    app._actions.get(timeout=1)()

    requests = [item for item, _confirmed in app.control_executor.calls]
    assert any(item.intent is CanonicalIntent.OPEN_APP for item in requests)
    opened = next(item for item in requests if item.intent is CanonicalIntent.OPEN_APP)
    assert opened.app_id == "блокнот"
    assert app.last_raw_text == ""
    assert app.last_local_text == ""
    assert app.last_text == ""
    assert app.state == AppState.IDLE


def test_mixed_mode_without_prefix_stays_on_dictation_path() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.state = AppState.PROCESSING
    app._generation = 32
    app._target_focus = app.focus_inspector.target
    app.inserter = SimpleNamespace(insert=lambda _text: None)
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )

    app._transcription_ready("открой блокнот", "ru", 0.9, 32)
    app._actions.get(timeout=1)()

    assert app.last_raw_text == "открой блокнот"
    assert app.last_text == "открой блокнот"
    assert not any(
        request.intent is CanonicalIntent.OPEN_APP
        for request, _confirmed in app.control_executor.calls
    )


def test_successful_dictation_becomes_the_only_session_edit_target() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 31
    app._target_focus = app.focus_inspector.target
    app.inserter = SimpleNamespace(insert=lambda _text: None)

    app._final_text_ready("Первое. Второе.", "ru", 0.9, 31)

    assert app._tracked_session_insertion is not None
    assert app._tracked_session_insertion.text == "Первое. Второе."
    assert app._tracked_session_insertion.target == app.focus_inspector.target


def test_voice_session_edit_updates_only_the_verified_last_insertion() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.settings_store.settings.language = "ru"
    app.state = AppState.PROCESSING
    app._generation = 71
    app._target_focus = app.focus_inspector.target
    app.last_text = "Первое. Второе."
    app.last_local_text = app.last_text
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        app.last_text,
        app.focus_inspector.target,
        time.monotonic(),
    )
    plans = []

    def apply(plan, _target, **_kwargs):
        plans.append(plan)
        return SessionEditorResult(
            SessionEditorStatus.EXECUTED,
            "ok",
            backend="fake",
        )

    app.session_editor = SimpleNamespace(apply=apply)

    app._transcription_ready(
        "команда удали последнее предложение", "ru", 0.9, 71
    )
    app._actions.get(timeout=1)()

    assert len(plans) == 1
    assert plans[0].selected_text == " Второе."
    assert app.last_text == "Первое."
    assert app._tracked_session_insertion is not None
    assert app._tracked_session_insertion.text == "Первое."
    assert app._last_session_edit is not None
    assert app.state == AppState.IDLE


def test_session_edit_keeps_service_spacing_out_of_last_text_and_syncs_tail() -> None:
    app = _bare_app()
    app.settings_store.settings.automatic_spacing = True
    app.state = AppState.PROCESSING
    app._processing_stage = "session_edit"
    app._generation = 73
    tracked = app_module._TrackedSessionInsertion(
        " Первое. Второе.",
        app.focus_inspector.target,
        time.monotonic(),
        " ",
    )
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_SENTENCE,
        language="ru",  # type: ignore[arg-type]
    )
    plan = plan_session_edit(tracked.text, request)

    app._session_edit_ready(
        request,
        plan,
        tracked,
        SessionEditorResult(SessionEditorStatus.EXECUTED, "ok", backend="fake"),
        73,
        10,
    )

    assert app.last_text == "Первое."
    assert app.last_local_text == "Первое."
    assert app._tracked_session_insertion is not None
    assert app._tracked_session_insertion.text == " Первое."
    assert app._last_insertion_tail == "."

    app.state = AppState.PROCESSING
    delete_all = SessionEditRequest(
        SessionEditAction.DELETE_LAST_DICTATION,
        language="ru",  # type: ignore[arg-type]
    )
    current = app._tracked_session_insertion
    delete_plan = plan_session_edit(current.text, delete_all)
    app._session_edit_ready(
        delete_all,
        delete_plan,
        current,
        SessionEditorResult(SessionEditorStatus.EXECUTED, "ok", backend="fake"),
        73,
        10,
    )

    assert app.last_text == ""
    assert app._last_insertion_tail == ""
    assert app._last_insertion_target is None


def test_restore_after_full_delete_uses_bound_empty_field_transaction() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._processing_stage = "session_edit"
    app._generation = 74
    app.focus_inspector.target = replace(
        app.focus_inspector.target,
        uia_runtime_id=(101, 102, 103),
    )
    original = "Полностью удалённая диктовка."
    tracked = app_module._TrackedSessionInsertion(
        original,
        app.focus_inspector.target,
        time.monotonic(),
    )
    delete_request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_DICTATION,
        language="ru",  # type: ignore[arg-type]
    )
    delete_plan = plan_session_edit(original, delete_request)
    app._session_edit_ready(
        delete_request,
        delete_plan,
        tracked,
        SessionEditorResult(
            SessionEditorStatus.EXECUTED,
            "ok",
            backend="fake",
            empty_document_verified=True,
        ),
        74,
        10,
    )
    assert app._tracked_session_insertion is not None
    assert app._tracked_session_insertion.text == ""
    assert app._last_session_edit is not None

    captured: list[SessionEditPlan] = []

    def apply(plan, _target, **_kwargs):
        captured.append(plan)
        return SessionEditorResult(SessionEditorStatus.EXECUTED, "ok", backend="fake")

    app.session_editor = SimpleNamespace(apply=apply)
    app.state = AppState.PROCESSING
    restore_request = SessionEditRequest(
        SessionEditAction.RESTORE_LAST_EDIT,
        language="ru",  # type: ignore[arg-type]
    )
    app._start_session_edit(
        restore_request,
        app.focus_inspector.target,
        74,
    )
    app._actions.get(timeout=1)()

    assert len(captured) == 1
    assert captured[0].original_text == ""
    assert captured[0].selected_text == ""
    assert captured[0].result_text == original
    assert app.last_text == original
    assert app._tracked_session_insertion is not None
    assert app._tracked_session_insertion.text == original
    assert app._last_session_edit is None

    app.state = AppState.PROCESSING
    app._start_session_edit(restore_request, app.focus_inspector.target, 74)

    assert app.state == AppState.IDLE
    assert len(captured) == 1


def test_restore_after_full_delete_requires_verified_empty_field_capability() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 75
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        "",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._last_session_edit = app_module._SessionEditTransaction(
        "Не восстанавливать вслепую",
        "",
        app.focus_inspector.target,
        time.monotonic(),
        allow_empty_restore=False,
    )
    app.session_editor = SimpleNamespace(
        apply=lambda *_args, **_kwargs: pytest.fail("backend must not run")
    )
    request = SessionEditRequest(
        SessionEditAction.RESTORE_LAST_EDIT,
        language="ru",  # type: ignore[arg-type]
    )

    app._start_session_edit(request, app.focus_inspector.target, 75)

    assert app.state == AppState.IDLE
    assert app._tracked_session_insertion.text == ""
    assert app._last_session_edit is not None


def test_restore_after_full_delete_requires_exact_uia_field_identity() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 76
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        "",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._last_session_edit = app_module._SessionEditTransaction(
        "Не восстанавливать без точного поля",
        "",
        app.focus_inspector.target,
        time.monotonic(),
        allow_empty_restore=True,
    )
    app.session_editor = SimpleNamespace(
        apply=lambda *_args, **_kwargs: pytest.fail("backend must not run")
    )
    request = SessionEditRequest(
        SessionEditAction.RESTORE_LAST_EDIT,
        language="ru",  # type: ignore[arg-type]
    )

    app._start_session_edit(request, app.focus_inspector.target, 76)

    assert app.state == AppState.IDLE
    assert app._tracked_session_insertion.text == ""
    assert app._last_session_edit is not None


def test_accepted_empty_restore_attempt_consumes_capability_on_rejection() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 77
    app.focus_inspector.target = replace(
        app.focus_inspector.target,
        uia_runtime_id=(201, 202, 203),
    )
    target = app.focus_inspector.target
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        "",
        target,
        time.monotonic(),
    )
    app._last_session_edit = app_module._SessionEditTransaction(
        "Старый текст",
        "",
        target,
        time.monotonic(),
        allow_empty_restore=True,
    )
    app.session_editor = SimpleNamespace(
        apply=lambda *_args, **_kwargs: SessionEditorResult(
            SessionEditorStatus.REJECTED,
            "caret_or_text_changed",
            backend="fake",
        )
    )
    request = SessionEditRequest(
        SessionEditAction.RESTORE_LAST_EDIT,
        language="ru",  # type: ignore[arg-type]
    )

    app._start_session_edit(request, target, 77)
    app._actions.get(timeout=1)()

    assert app.state == AppState.IDLE
    assert app._tracked_session_insertion.text == ""
    assert app._last_session_edit is None


def test_session_editor_exception_invalidates_all_edit_tracking() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 78
    tracked = app_module._TrackedSessionInsertion(
        "Текст для изменения.",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._tracked_session_insertion = tracked
    app._last_session_edit = app_module._SessionEditTransaction(
        "Предыдущий текст.",
        tracked.text,
        tracked.target,
        time.monotonic(),
    )

    def fail_after_unknown_phase(*_args, **_kwargs):
        raise RuntimeError("unknown backend phase")

    app.session_editor = SimpleNamespace(apply=fail_after_unknown_phase)
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_WORD,
        language="ru",  # type: ignore[arg-type]
    )

    app._start_session_edit(request, app.focus_inspector.target, 78)
    app._actions.get(timeout=1)()

    assert app.state == AppState.IDLE
    assert app._tracked_session_insertion is None
    assert app._last_session_edit is None
    assert app._insertion_uncertain is True


def test_cancel_during_session_edit_invalidates_tracking_and_spacing() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._processing_stage = "session_edit"
    app._generation = 74
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        " text",
        app.focus_inspector.target,
        time.monotonic(),
        " ",
    )
    app._last_session_edit = app_module._SessionEditTransaction(
        " old",
        " text",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._last_insertion_target = app.focus_inspector.target
    app._last_insertion_at = time.monotonic()
    app._last_insertion_tail = "t"

    app.cancel()

    assert app.state == AppState.IDLE
    assert app._generation == 75
    assert app._tracked_session_insertion is None
    assert app._last_session_edit is None
    assert app._insertion_uncertain is True
    assert app._last_insertion_target is None
    assert app._last_insertion_tail == ""


def test_session_edit_watchdog_invalidates_late_worker() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._processing_stage = "session_edit"
    app._generation = 75
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        "text",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app._last_insertion_target = app.focus_inspector.target
    app._last_insertion_tail = "t"
    request = SessionEditRequest(
        SessionEditAction.DELETE_LAST_WORD,
        language="en",  # type: ignore[arg-type]
    )

    app._session_edit_timeout(request, 75)

    assert app.state == AppState.IDLE
    assert app._generation == 76
    assert app._tracked_session_insertion is None
    assert app._insertion_uncertain is True
    assert app._last_insertion_tail == ""


def test_session_edit_refuses_focus_change_before_backend_side_effect() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.settings_store.settings.language = "ru"
    app.state = AppState.PROCESSING
    app._generation = 72
    changed = FocusTarget(
        foreground_hwnd=99,
        root_hwnd=99,
        focused_hwnd=100,
        thread_id=101,
        process_id=102,
        focused_class_name="Edit",
        stable=True,
    )
    app._target_focus = changed
    app._tracked_session_insertion = app_module._TrackedSessionInsertion(
        "Не трогать.",
        app.focus_inspector.target,
        time.monotonic(),
    )
    app.session_editor = SimpleNamespace(
        apply=lambda *_args, **_kwargs: pytest.fail("backend must not run")
    )

    app._transcription_ready(
        "команда удали последнее предложение", "ru", 0.9, 72
    )

    assert app.state == AppState.IDLE
    assert app._tracked_session_insertion.text == "Не трогать."
    assert "не тронут" in app.overlay.messages[-1][0].casefold()


def test_everyday_mode_inserts_text_then_requires_confirmation_for_end_action() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.settings_store.settings.language = "auto"

    class _ConfirmingExecutor(_ControlExecutor):
        def execute(
            self, request: ControlRequest, *, confirmed: bool = False
        ) -> ControlResult:
            self.calls.append((request, confirmed))
            if not confirmed:
                return ControlResult(
                    ResultStatus.CONFIRMATION_REQUIRED,
                    request.intent,
                    reason_code="key_requires_confirmation",
                    confirmation_required=True,
                )
            return ControlResult(ResultStatus.EXECUTED, request.intent, backend="fake")

    executor = _ConfirmingExecutor()
    app.control_executor = executor
    inserted: list[str] = []
    app.inserter = SimpleNamespace(insert=inserted.append)
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )
    app.normalizer = SimpleNamespace(
        normalize=lambda text, _rules, protected_terms=(): SimpleNamespace(
            text=text, applied=()
        )
    )
    app.memory_store = SimpleNamespace(
        replacement_candidates=lambda _context, draft: (),
        mark_terms_used=lambda _ids: None,
    )
    app.prompt_builder = SimpleNamespace(
        build_post_asr=lambda _store, _text, _context: SimpleNamespace(
            terms=(), styles=()
        )
    )
    app.state = AppState.PROCESSING
    app._generation = 40
    app._target_focus = app.focus_inspector.target

    # Detected language is deliberately wrong; Auto must still try all three
    # strict grammars for the short final command.
    app._transcription_ready("Напиши другу, нажать Enter", "en", 0.9, 40)
    app._actions.get(timeout=1)()
    app._actions.get(timeout=1)()

    assert inserted == ["Напиши другу"]
    assert app.last_raw_text == "Напиши другу, нажать Enter"
    assert app.last_text == "Напиши другу"
    assert [confirmed for _request, confirmed in executor.calls] == [False]
    assert app._pending_control_request is not None
    assert app._runtime_feedback["command"] == "Нажать Enter"

    app.state = AppState.PROCESSING
    app._generation = 41
    app._transcription_ready("подтверждаю", "en", 0.9, 41)
    app._actions.get(timeout=1)()

    assert [confirmed for _request, confirmed in executor.calls] == [False, True]
    assert app._pending_control_request is None
    assert app.state == AppState.IDLE


def test_end_action_is_blocked_when_focus_changes_before_insertion() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "mixed"
    app.settings_store.settings.language = "ru"
    app.focus_inspector.match = FocusMatch.CHANGED
    app.inserter = SimpleNamespace(insert=lambda _text: pytest.fail("must not insert"))
    app.correction_pipeline = SimpleNamespace(
        correct=lambda text, _context: CorrectionResult(text, False, "local")
    )
    app.normalizer = SimpleNamespace(
        normalize=lambda text, _rules, protected_terms=(): SimpleNamespace(
            text=text, applied=()
        )
    )
    app.memory_store = SimpleNamespace(
        replacement_candidates=lambda _context, draft: (),
        mark_terms_used=lambda _ids: None,
    )
    app.prompt_builder = SimpleNamespace(
        build_post_asr=lambda _store, _text, _context: SimpleNamespace(
            terms=(), styles=()
        )
    )
    app.state = AppState.PROCESSING
    app._generation = 42
    app._target_focus = app.focus_inspector.target

    app._transcription_ready("Текст, отправить сообщение", "ru", 0.9, 42)
    app._actions.get(timeout=1)()

    assert app._pending_dictation_action is None
    assert app.control_executor.calls == []
    assert app.state == AppState.PENDING_INSERT
    assert "не выполнена" in app._runtime_feedback["outcome"].casefold()


def test_sensitive_command_confirmation_is_bound_and_one_shot() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "commands"

    class _ConfirmingExecutor(_ControlExecutor):
        def execute(
            self, request: ControlRequest, *, confirmed: bool = False
        ) -> ControlResult:
            self.calls.append((request, confirmed))
            if request.intent is CanonicalIntent.SEND_KEY and not confirmed:
                return ControlResult(
                    ResultStatus.CONFIRMATION_REQUIRED,
                    request.intent,
                    reason_code="key_requires_confirmation",
                    confirmation_required=True,
                )
            return ControlResult(ResultStatus.EXECUTED, request.intent, backend="fake")

    executor = _ConfirmingExecutor()
    app.control_executor = executor
    app.state = AppState.PROCESSING
    app._generation = 33

    app._transcription_ready("press Enter", "en", 0.9, 33)
    app._actions.get(timeout=1)()

    assert app.state == AppState.IDLE
    assert app._pending_control_request is not None
    assert app._pending_control_request.intent is CanonicalIntent.SEND_KEY

    app.state = AppState.PROCESSING
    app._generation = 34
    app._transcription_ready("confirm", "en", 0.9, 34)
    app._actions.get(timeout=1)()

    key_calls = [
        confirmed
        for request, confirmed in executor.calls
        if request.intent is CanonicalIntent.SEND_KEY
    ]
    assert key_calls == [False, True]
    assert app._pending_control_request is None
    assert app.state == AppState.IDLE


def test_non_number_control_releases_older_number_snapshot_before_worker() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "commands"
    app.state = AppState.PROCESSING
    app._generation = 50
    app._number_snapshot = SimpleNamespace(snapshot_id="snapshot-old")
    app.number_overlay.snapshot_id = "snapshot-old"

    app._transcription_ready("press Enter", "en", 0.9, 50)

    assert app._number_snapshot is None
    assert app.number_overlay.snapshot_id is None
    assert app.control_executor.clear_count >= 1

    app._actions.get(timeout=1)()


@pytest.mark.parametrize(
    ("text", "language"),
    (
        ("what can I say", "en"),
        ("повтори последнее", "ru"),
        ("copy last text", "en"),
    ),
)
def test_new_local_utterance_invalidates_old_confirmation(
    text: str, language: str
) -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "commands"
    app.state = AppState.PROCESSING
    app._generation = 51
    app._pending_control_request = ControlRequest(
        CanonicalIntent.SEND_KEY, key="enter"
    )
    app._pending_control_deadline = time.monotonic() + 30.0
    app._number_snapshot = SimpleNamespace(snapshot_id="snapshot-old")
    app.number_overlay.snapshot_id = "snapshot-old"
    app.repeat_last = lambda: None  # type: ignore[method-assign]
    app.copy_last = lambda: None  # type: ignore[method-assign]

    app._transcription_ready(text, language, 0.9, 51)

    assert app._pending_control_request is None
    assert app.number_overlay.snapshot_id is None
    assert app.control_executor.cancel_count >= 1
    assert app.control_executor.clear_count >= 1


def test_confirmation_ttl_callback_clears_request_overlay_and_snapshot() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._generation = 52
    request = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")
    app._number_snapshot = SimpleNamespace(snapshot_id="snapshot-sensitive")
    app.number_overlay.snapshot_id = "snapshot-sensitive"

    app._control_result_ready(
        ControlResult(
            ResultStatus.CONFIRMATION_REQUIRED,
            request.intent,
            confirmation_required=True,
        ),
        request,
        52,
        3,
    )
    delay, expire = app.root.after_calls[-1]

    assert delay == int(app_module.CONTROL_CONFIRMATION_TTL_SECONDS * 1000)
    assert app._pending_control_request == request
    expire()
    assert app._pending_control_request is None
    assert app.number_overlay.snapshot_id is None
    assert app._number_snapshot is None
    assert app.control_executor.cancel_count >= 1
    assert app.control_executor.clear_count >= 1


def test_old_confirmation_ttl_token_cannot_cancel_a_new_pending_request() -> None:
    app = _bare_app()
    first = ControlRequest(CanonicalIntent.SEND_KEY, key="enter")
    second = ControlRequest(CanonicalIntent.SEND_KEY, key="space")

    app.state = AppState.PROCESSING
    app._generation = 53
    app._control_result_ready(
        ControlResult(
            ResultStatus.CONFIRMATION_REQUIRED,
            first.intent,
            confirmation_required=True,
        ),
        first,
        53,
        1,
    )
    _delay, old_expire = app.root.after_calls[-1]
    app.state = AppState.PROCESSING
    app._generation = 54
    app._control_result_ready(
        ControlResult(
            ResultStatus.CONFIRMATION_REQUIRED,
            second.intent,
            confirmation_required=True,
        ),
        second,
        54,
        1,
    )

    old_expire()

    assert app._pending_control_request == second


def test_escape_during_command_invalidates_late_pending_result() -> None:
    app = _bare_app()
    app.state = AppState.PROCESSING
    app._processing_stage = "command"
    app._generation = 55
    app._number_snapshot = SimpleNamespace(snapshot_id="snapshot-command")
    app.number_overlay.snapshot_id = "snapshot-command"

    app.cancel()
    app._control_result_ready(
        ControlResult(
            ResultStatus.CONFIRMATION_REQUIRED,
            CanonicalIntent.SEND_KEY,
            confirmation_required=True,
        ),
        ControlRequest(CanonicalIntent.SEND_KEY, key="enter"),
        55,
        1,
    )

    assert app._generation == 56
    assert app._pending_control_request is None
    assert app.number_overlay.snapshot_id is None
    assert app.control_executor.cancel_count >= 1
    assert app.control_executor.clear_count >= 1


def test_number_overlay_snapshot_is_required_and_hidden_after_selection() -> None:
    app = _bare_app()
    app.settings_store.settings.interaction_mode = "commands"
    snapshot = NumberedElementSnapshot(
        "snapshot-voice",
        "window-voice",
        (
            NumberedElement(
                1,
                "Settings",
                "ButtonControl",
                ElementBounds(10, 10, 80, 30),
            ),
        ),
    )

    class _NumberExecutor(_ControlExecutor):
        def capture_numbered_elements(self):
            return snapshot

    executor = _NumberExecutor()
    app.control_executor = executor
    app.state = AppState.PROCESSING
    app._generation = 35

    app._transcription_ready("show numbers", "en", 0.9, 35)
    app._actions.get(timeout=1)()

    assert app.number_overlay.snapshot_id == snapshot.snapshot_id
    assert app._number_snapshot is snapshot
    assert app.state == AppState.IDLE

    app.state = AppState.PROCESSING
    app._generation = 36
    app._transcription_ready("click number 1", "en", 0.9, 36)
    app._actions.get(timeout=1)()

    clicked = next(
        request
        for request, _confirmed in executor.calls
        if request.intent is CanonicalIntent.INVOKE_NUMBERED_ELEMENT
    )
    assert clicked.snapshot_id == snapshot.snapshot_id
    assert clicked.element_number == 1
    assert app.number_overlay.snapshot_id is None
    assert app._number_snapshot is None


def test_number_overlay_poll_hides_after_foreground_changes_and_reuses_worker() -> None:
    app = _bare_app()
    snapshot = NumberedElementSnapshot(
        "snapshot-watch",
        "window-watch",
        (
            NumberedElement(
                1,
                "Settings",
                "ButtonControl",
                ElementBounds(10, 10, 80, 30),
            ),
        ),
    )

    class _WatchingExecutor(_ControlExecutor):
        def __init__(self) -> None:
            super().__init__()
            self.current_results = iter((True, False))
            self.snapshot_checks: list[str] = []

        def is_snapshot_current(self, snapshot_id: str) -> bool:
            self.snapshot_checks.append(snapshot_id)
            return next(self.current_results)

    executor = _WatchingExecutor()
    app.control_executor = executor
    app._number_snapshot = snapshot
    assert app.number_overlay.show(snapshot)
    app._start_number_snapshot_poll(snapshot.snapshot_id)

    delay, first_poll = app.root.after_calls[-1]
    assert delay == app_module.NUMBER_SNAPSHOT_POLL_MS
    first_poll()
    app._actions.get(timeout=1)()
    worker = app._number_snapshot_poll_worker
    assert worker is not None
    assert app.number_overlay.snapshot_id == snapshot.snapshot_id

    delay, second_poll = app.root.after_calls[-1]
    assert delay == app_module.NUMBER_SNAPSHOT_POLL_MS
    second_poll()
    app._actions.get(timeout=1)()

    assert app._number_snapshot_poll_worker is worker
    assert executor.snapshot_checks == [snapshot.snapshot_id, snapshot.snapshot_id]
    assert app.number_overlay.snapshot_id is None
    assert app._number_snapshot is None
    app._number_snapshot_poll_stop.set()
    try:
        app._number_snapshot_poll_queue.put_nowait(None)
    except queue.Full:
        pass


def test_hiding_overlay_invalidates_a_scheduled_snapshot_poll() -> None:
    app = _bare_app()
    snapshot = NumberedElementSnapshot("snapshot-old", "window-old", ())
    app._number_snapshot = snapshot
    app.number_overlay.snapshot_id = snapshot.snapshot_id
    app._start_number_snapshot_poll(snapshot.snapshot_id)
    _delay, scheduled_poll = app.root.after_calls[-1]

    app._hide_number_overlay()
    scheduled_poll()

    assert app._number_snapshot_poll_worker is None
    assert app._number_snapshot_poll_in_flight is None


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
