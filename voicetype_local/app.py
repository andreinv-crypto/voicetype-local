from __future__ import annotations

import enum
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import winsound
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .audio import AudioRecorder
from .config import SettingsStore
from .correction import CorrectionContext, CorrectionPipeline, LocalBasicCorrector
from .diagnostics import TechnicalLogger
from .dictation_transform import combine_insertions, transform_dictation
from .focus import FocusInspector, FocusMatch, FocusTarget, focus_app_id
from .hotkey import GlobalHotkeyListener, activation_key_label, activation_label
from .inserter import TextInsertionError, UnicodeTextInserter
from .memory import MemoryContext, MemoryStore, open_memory_store_resilient
from .memory_ui import MemoryWindow, open_memory_window
from .paths import model_dir, resource_root
from .prompt_builder import PromptBuilder, PromptSlice, whisper_token_counter
from .providers import build_cloud_transcriber, build_correction_pipeline
from .secrets import DpapiSecretStore
from .session_editing import (
    SessionEditAction,
    SessionEditPlan,
    SessionEditPlanError,
    SessionEditRequest,
    plan_session_edit,
)
from .session_editor import SessionEditorResult, SessionEditorStatus, SessionTextEditor
from .settings_ui import SettingsWindow, open_settings_window
from .targeted_inserter import RICHEDIT_D2DPT_CLASS, TargetedRichEditInserter
from .test_lab import (
    QuickTestEvent,
    QuickTestSessionSnapshot,
    QuickTestWindow,
    open_quick_test_window,
)
from .text_rules import SafeNormalizer
from .transcriber import OfflineTranscriber
from .ui import CommandHelpWindow, NumberOverlay, StatusOverlay, TrayController
from .voice_control import VoiceControlRouter, VoiceRoute, VoiceRouteKind
from .windows_control import (
    CanonicalIntent,
    ControlRequest,
    ControlResult,
    NumberedElementSnapshot,
    ResultStatus,
    WindowsControlExecutor,
)


CONTROL_CONFIRMATION_TTL_SECONDS = 45.0
AUTOMATIC_SPACING_TTL_SECONDS = 30.0
NUMBER_SNAPSHOT_POLL_MS = 500
SESSION_EDIT_TTL_SECONDS = 600.0
SESSION_EDIT_TIMEOUT_SECONDS = 5.0
TARGETED_INSERT_TIMEOUT_SECONDS = 5.0
SENDINPUT_VERIFY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _TrackedSessionInsertion:
    text: str
    target: FocusTarget
    created_at: float
    automatic_prefix: str = ""


@dataclass(frozen=True, slots=True)
class _SessionEditTransaction:
    before_text: str
    after_text: str
    target: FocusTarget
    created_at: float
    allow_empty_restore: bool = False


@dataclass(frozen=True, slots=True)
class _PendingInsert:
    """One recognized payload waiting for an explicit, newly verified target."""

    payload: str
    source_target: FocusTarget | None
    created_at: float
    reason_code: str
    uncertain: bool = False


class AppState(enum.StrEnum):
    LOADING = "loading"
    IDLE = "ready"
    STARTING = "starting"
    RECORDING = "recording"
    PROCESSING = "processing"
    PENDING_INSERT = "pending"
    ERROR = "error"


def _style_initial_prompt(language: str) -> str:
    prompts = {
        "ru": "Это пример точной диктовки с грамотной пунктуацией.",
        "es": "Este es un ejemplo de dictado claro con puntuación correcta.",
        "en": "This is an example of clear dictation with correct punctuation.",
        "auto": "Точная диктовка. Dictado claro. Clear dictation.",
    }
    return prompts.get(language, prompts["auto"])


class VoiceTypeApp:
    def __init__(
        self,
        *,
        activation_waiter: Callable[[int], bool] | None = None,
        open_settings_on_start: bool = False,
        silent_start: bool = False,
    ) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("VoiceType Local")
        self.overlay = StatusOverlay(self.root)
        self.number_overlay = NumberOverlay(self.root)
        self.command_help = CommandHelpWindow(self.root)
        self.settings_store = SettingsStore()
        settings = self.settings_store.get()
        self.overlay.update_preferences(
            size=settings.overlay_size,
            contrast=settings.overlay_contrast,
            position=settings.overlay_position,
        )
        self.logger = TechnicalLogger()
        self.secret_store = DpapiSecretStore()
        self.memory_store, recovered_memory = open_memory_store_resilient(
            app_version=__version__
        )
        self.prompt_builder = PromptBuilder(
            token_counter=whisper_token_counter(
                model_dir(settings.model_name) / "tokenizer.json"
            ),
            max_tokens=224,
            reserve_tokens=224 - settings.memory_prompt_token_budget,
        )
        self.normalizer = SafeNormalizer()
        self.transcriber = OfflineTranscriber(
            settings,
            startup_timeout=settings.worker_start_timeout_seconds,
            transcription_timeout=settings.transcription_timeout_seconds,
        )
        self.correction_pipeline = build_correction_pipeline(
            settings, secret_store=self.secret_store
        )
        # Test Lab must remain local even when the everyday profile opts into
        # a cloud text corrector.  The instance is stateless and stores no text.
        self._quick_test_local_correction = CorrectionPipeline(LocalBasicCorrector())
        self.cloud_transcriber = build_cloud_transcriber(
            settings, secret_store=self.secret_store
        )
        self._recorder_factory = AudioRecorder
        self.recorder = self._recorder_factory()
        self.inserter = UnicodeTextInserter()
        self.focus_inspector = FocusInspector()
        self.targeted_inserter = TargetedRichEditInserter()
        self.session_editor = SessionTextEditor(
            self.inserter,
            self.focus_inspector,
            targeted_inserter=self.targeted_inserter,
        )
        self.voice_router = VoiceControlRouter()
        self.control_executor = WindowsControlExecutor()
        self.state = AppState.LOADING
        # The current dictation stages are intentionally session-only. They
        # make before/after inspection possible without creating a history.
        self.last_raw_text = ""
        self.last_local_text = ""
        self.last_text = ""
        self._runtime_feedback: dict[str, str] = {
            "heard": "",
            "text": "",
            "command": "",
            "outcome": "Готово к работе",
            "tone": "ready",
        }
        self._insertion_uncertain = False
        self._pending_insert: _PendingInsert | None = None
        self._pending_repeat_waiting = False
        self._last_insertion_target: FocusTarget | None = None
        self._last_insertion_at = 0.0
        self._last_insertion_tail = ""
        self._tracked_session_insertion: _TrackedSessionInsertion | None = None
        self._last_session_edit: _SessionEditTransaction | None = None
        self._pending_control_request: ControlRequest | None = None
        self._pending_control_target: FocusTarget | None = None
        self._pending_dictation_action: tuple[
            int, ControlRequest, str
        ] | None = None
        self._pending_control_deadline = 0.0
        self._pending_control_token = 0
        self._number_snapshot: NumberedElementSnapshot | None = None
        self._number_snapshot_poll_token = 0
        self._number_snapshot_poll_in_flight: int | None = None
        self._number_snapshot_poll_queue: queue.Queue[
            tuple[int, str] | None
        ] = queue.Queue(maxsize=1)
        self._number_snapshot_poll_stop = threading.Event()
        self._number_snapshot_poll_worker: threading.Thread | None = None
        self._target_focus: FocusTarget | None = None
        self._memory_context = MemoryContext()
        self._pre_prompt: PromptSlice | None = None
        self._generation = 0
        self._starting_started_at = 0.0
        self._recording_started_at = 0.0
        self._processing_started_at = 0.0
        self._model_load_started_at = 0.0
        self._processing_stage = ""
        self._actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self._record_timer: threading.Timer | None = None
        self._closing = False
        self._activation_waiter = activation_waiter
        self._activation_listener_stop = threading.Event()
        self._activation_listener_thread: threading.Thread | None = None
        self._open_settings_on_start = bool(open_settings_on_start)
        self._silent_start = bool(silent_start)
        self._settings_window: SettingsWindow | None = None
        self._memory_window: MemoryWindow | None = None
        self._quick_test_window: QuickTestWindow | None = None
        self._quick_test_event_sequence = 0
        # This separate guard deliberately does not depend on Tk/window calls.
        # If the Test Lab window becomes unreadable, voice routing fails closed
        # until its explicit cleanup completes.
        self._quick_test_guard_active = False
        self._quick_test_guard_broken = False
        self._quick_test_cycle_active = False
        self._quick_test_attempted = False
        self._quick_test_ram_dirty = False
        self._microphone_options_cache: tuple[str, ...] = ()
        self._microphone_scan_lock = threading.Lock()
        self._microphone_scan_in_progress = False
        self._audio_backend_blocked = False
        self._blocked_audio_recorder: AudioRecorder | None = None
        self._install_builtin_packs()
        self.hotkeys = GlobalHotkeyListener(
            self.request_toggle,
            settings.activation_key,
            activation_mode=settings.activation_mode,
            on_hold_stop=self.request_hold_release,
            on_cancel=self.request_cancel,
            should_cancel=self._escape_cancellable,
        )
        self.tray = TrayController(
            on_toggle=self.request_toggle,
            on_cancel=self.request_cancel,
            on_insert_last=self.request_repeat_last,
            on_copy_last=self.request_copy_last,
            on_copy_raw=self.request_copy_raw,
            on_clear_session=self.request_clear_session_text,
            on_language=self.request_language,
            on_mode=self.request_interaction_mode,
            on_help=self.request_command_help,
            on_memory=self.request_memory,
            on_settings=self.request_settings,
            on_exit=self.request_exit,
            get_language=lambda: self.settings_store.get().language,
            get_mode=lambda: self.settings_store.get().interaction_mode,
            has_last_text=lambda: bool(self.last_text),
            has_raw_text=lambda: bool(self.last_raw_text),
        )
        self.logger.event("app_initialized", app_version=__version__, state=self.state.value)
        if recovered_memory is not None:
            self.logger.event(
                "memory_recovered",
                operation="new_clean_database",
                code="corrupt_preserved",
            )

    def post(self, action: Callable[[], None]) -> None:
        if not self._closing:
            self._actions.put(action)

    def _drain_actions(self) -> None:
        for _ in range(64):
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                break
            try:
                action()
            except Exception as exc:
                if not self._closing:
                    self._show_status(f"Ошибка приложения: {exc}", "error", 5000)
            if self._closing:
                while True:
                    try:
                        self._actions.get_nowait()
                    except queue.Empty:
                        break
                break
        if not self._closing:
            self.root.after(15 if not self._actions.empty() else 35, self._drain_actions)

    def request_toggle(self) -> None:
        self.post(self.toggle)

    def request_hold_release(self) -> None:
        self.post(self._hold_release)

    def request_cancel(self) -> None:
        self.post(self.cancel)

    def request_copy_last(self) -> None:
        self.post(self.copy_last)

    def request_copy_raw(self) -> None:
        self.post(self.copy_raw)

    def request_clear_session_text(self) -> None:
        self.post(self.clear_session_text)

    def request_repeat_last(self) -> None:
        self.post(self.repeat_last)

    def request_language(self, language: str) -> None:
        self.post(lambda: self.set_language(language))

    def request_interaction_mode(self, mode: str) -> None:
        self.post(lambda: self.set_interaction_mode(mode))

    def request_command_help(self) -> None:
        self.post(self.open_command_help)

    def request_exit(self) -> None:
        self.post(self.exit)

    def request_settings(self) -> None:
        self.post(self.open_settings)

    def _start_activation_listener(self) -> None:
        if self._activation_waiter is None:
            return
        listener = self._activation_listener_thread
        if listener is not None and listener.is_alive():
            return
        listener = threading.Thread(
            target=self._activation_listener_loop,
            name="instance-activation",
            daemon=True,
        )
        self._activation_listener_thread = listener
        listener.start()

    def _activation_listener_loop(self) -> None:
        waiter = self._activation_waiter
        if waiter is None:
            return
        while not self._activation_listener_stop.is_set():
            try:
                requested = waiter(250)
            except Exception as exc:
                self.logger.event(
                    "instance_activation_failed",
                    operation="wait_for_settings",
                    error_type=type(exc).__name__,
                )
                return
            if requested and not self._activation_listener_stop.is_set():
                self.request_settings()

    def request_memory(self) -> None:
        self.post(self.open_memory)

    def open_command_help(self) -> None:
        language = self.settings_store.get().language
        self.command_help.show(language if language in {"ru", "es", "en"} else "ru")

    def _install_builtin_packs(self) -> None:
        packs_dir = resource_root() / "assets" / "packs"
        if not packs_dir.is_dir():
            return
        for path in sorted(packs_dir.glob("*.json")):
            try:
                self.memory_store.install_pack(path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.logger.event(
                    "memory_pack_failed",
                    operation="install_builtin_pack",
                    error_type=type(exc).__name__,
                )

    def open_settings(self) -> None:
        if self._closing:
            return
        existing = self._settings_window
        if existing is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.deiconify()
                    existing.window.lift()
                    existing.window.focus_force()
                    self.logger.event(
                        "settings_window_opened",
                        operation="show_settings",
                        code="existing",
                    )
                    return
            except tk.TclError:
                pass
        self._settings_window = open_settings_window(
            self.root,
            self.settings_store.get(),
            on_save=self._settings_saved,
            save_secret=lambda value: self.secret_store.set_with_rollback(
                "openai_api_key", value
            ),
            has_saved_secret=self.secret_store.has("openai_api_key"),
            delete_secret=self._delete_openai_key,
            on_cancel=lambda: setattr(self, "_settings_window", None),
            on_repeat_last=self.repeat_last,
            on_copy_last=self.copy_last,
            on_copy_raw=self.copy_raw,
            on_clear_session=self.clear_session_text,
            on_memory=self.open_memory,
            on_help=self.open_command_help,
            on_quick_test=self.open_quick_test,
            has_last_text=bool(self.last_text),
            has_raw_text=bool(self.last_raw_text),
            microphone_options=self._microphone_options_cache,
            last_text=self.last_text,
            last_raw_text=self.last_raw_text,
            runtime_feedback=self._runtime_feedback_snapshot,
            status_text={
                AppState.IDLE: "Готово к работе",
                AppState.RECORDING: "Идёт запись",
                AppState.PROCESSING: "Обрабатываю речь",
                AppState.PENDING_INSERT: "Текст ждёт вставки",
                AppState.ERROR: "Нужна проверка ошибки",
            }.get(self.state, "VoiceType запущен"),
        )
        self.logger.event(
            "settings_window_opened",
            operation="show_settings",
            code="created",
        )
        self._start_microphone_scan()

    def _start_microphone_scan(self) -> None:
        with self._microphone_scan_lock:
            if self._microphone_scan_in_progress:
                return
            self._microphone_scan_in_progress = True
        threading.Thread(
            target=self._microphone_scan_worker,
            name="microphone-device-scan",
            daemon=True,
        ).start()

    def _microphone_scan_worker(self) -> None:
        try:
            options = tuple(AudioRecorder.input_devices())
        except Exception:
            options = ()
        if self._closing:
            return
        self.post(lambda: self._microphone_scan_ready(options))

    def _microphone_scan_ready(self, options: tuple[str, ...]) -> None:
        with self._microphone_scan_lock:
            self._microphone_scan_in_progress = False
        self._microphone_options_cache = options
        window = self._settings_window
        if window is None:
            return
        try:
            if window.window.winfo_exists():
                window.set_microphone_options(options)
        except tk.TclError:
            return

    def _delete_openai_key(self) -> None:
        if self.state == AppState.PROCESSING:
            self.cancel()
        self.secret_store.delete("openai_api_key")
        settings = self.settings_store.get()
        self.correction_pipeline = build_correction_pipeline(
            settings, secret_store=self.secret_store
        )
        self.cloud_transcriber = build_cloud_transcriber(
            settings, secret_store=self.secret_store
        )
        self.logger.event(
            "settings_updated", state=self.state.value, code="api_key_deleted"
        )

    def open_memory(self) -> None:
        if self._closing:
            return
        existing = self._memory_window
        if existing is not None:
            try:
                if existing.window.winfo_exists():
                    existing.window.deiconify()
                    existing.window.lift()
                    existing.window.focus_force()
                    return
            except tk.TclError:
                pass
        self._memory_window = open_memory_window(
            self.root,
            self.memory_store,
            on_close=lambda: setattr(self, "_memory_window", None),
        )

    def _quick_test_snapshot(self) -> QuickTestSessionSnapshot:
        tracked = self._tracked_session_insertion
        return QuickTestSessionSnapshot(
            generation=self._generation,
            target_hwnd=(tracked.target.focused_hwnd if tracked is not None else 0),
            tracked_text=(tracked.text if tracked is not None else None),
        )

    def _active_quick_test(self) -> QuickTestWindow | None:
        window = getattr(self, "_quick_test_window", None)
        if window is None:
            return None
        try:
            return window if window.active else None
        except Exception:
            return None

    def _quick_test_closed(self) -> None:
        if (
            bool(getattr(self, "_quick_test_ram_dirty", False))
            and not self._closing
        ):
            self._reset_quick_test_session()
        self._quick_test_guard_active = False
        self._quick_test_guard_broken = False
        self._quick_test_cycle_active = False
        self._quick_test_attempted = False
        self._quick_test_ram_dirty = False
        self._quick_test_window = None

    def _reset_quick_test_session(self) -> None:
        """Scrub Test Lab text once while preserving untouched pre-lab RAM."""

        if not bool(getattr(self, "_quick_test_ram_dirty", False)):
            return
        self.clear_session_text()
        self._quick_test_ram_dirty = False

    def _abort_quick_test_cycle(self, _restarting: bool = False) -> None:
        """Invalidate in-flight Test Lab work before its HWND is reset/destroyed."""

        attempted = bool(getattr(self, "_quick_test_attempted", False))
        cycle_active = bool(getattr(self, "_quick_test_cycle_active", False))
        if _restarting:
            self._quick_test_guard_broken = False
        if not attempted and not cycle_active:
            return
        self._quick_test_cycle_active = False
        if self.state in {
            AppState.STARTING,
            AppState.RECORDING,
            AppState.PROCESSING,
            AppState.PENDING_INSERT,
        }:
            self.cancel()
        if bool(getattr(self, "_quick_test_ram_dirty", False)):
            self._reset_quick_test_session()
        # QuickTestWindow calls its reset callback after this invalidation when
        # an actual lab operation reached the app.  Keeping cancellation and
        # RAM scrubbing separate preserves pre-lab session text if the user
        # merely opened the window and closed it before dictating anything.
        self._quick_test_attempted = False

    def open_quick_test(self) -> None:
        """Open one isolated native field for a short end-to-end voice check."""

        if self._closing:
            return
        existing = self._active_quick_test()
        if existing is not None:
            try:
                self._quick_test_guard_active = True
                existing.window.deiconify()
                existing.window.lift()
                existing.window.focus_force()
                existing.window.after(80, existing.native_field.focus)
                return
            except tk.TclError:
                self._quick_test_window = None
        if self.state is not AppState.IDLE:
            self._show_status(
                "Сначала завершите или отмените текущую запись, затем откройте быструю проверку.",
                "warning",
                3500,
            )
            return
        self._clear_pending_dictation_action()
        self._clear_pending_control()
        self._hide_number_overlay()
        settings = self.settings_store.get()
        try:
            window = open_quick_test_window(
                self.root,
                language=settings.language,
                activation_label=self._activation_key_label(),
                activation_mode=settings.activation_mode,
                start_generation=self._generation,
                snapshot_provider=self._quick_test_snapshot,
                on_reset_session=self._reset_quick_test_session,
                on_abort_cycle=self._abort_quick_test_cycle,
                on_close=self._quick_test_closed,
            )
            self._quick_test_window = window
            self._quick_test_guard_active = True
            self._quick_test_guard_broken = False
            self._quick_test_cycle_active = False
            self._quick_test_attempted = False
            self._quick_test_ram_dirty = False
        except Exception:
            self._quick_test_guard_active = False
            self._quick_test_guard_broken = False
            self._quick_test_cycle_active = False
            self._quick_test_attempted = False
            self._quick_test_ram_dirty = False
            self._quick_test_window = None
            self._show_status(
                "Не удалось открыть безопасное тестовое поле.",
                "error",
                4000,
            )

    def _quick_test_route_mode(
        self, target_focus: FocusTarget | None
    ) -> str | None:
        window = self._active_quick_test()
        if window is None:
            return None
        try:
            if not window.owns_target(target_focus):
                return None
            expected_kind, _expected_action = window.route_expectation()
        except Exception:
            return None
        if expected_kind == "dictation":
            return "dictation"
        # Exact command prefixes are used in the lab regardless of the user's
        # saved everyday mode. This override is transient and never persisted.
        return "mixed"

    def _quick_test_route_allowed(
        self,
        route: VoiceRoute,
        target_focus: FocusTarget | None,
    ) -> tuple[bool, str]:
        guarded = bool(getattr(self, "_quick_test_guard_active", False))
        window = self._active_quick_test()
        if not guarded:
            return True, ""
        if bool(getattr(self, "_quick_test_guard_broken", False)):
            return False, "quick_test_guard_error"
        if window is None:
            return False, "quick_test_guard_error"
        try:
            owns_target = window.owns_target(target_focus)
        except Exception:
            return False, "quick_test_guard_error"
        if not owns_target:
            return False, "focus_changed"
        try:
            expected_kind, expected_action = window.route_expectation()
        except Exception:
            return False, "quick_test_guard_error"
        if expected_kind == "dictation":
            if route.kind is VoiceRouteKind.DICTATION:
                return True, ""
            return False, "unexpected_route"
        if expected_kind == "session_edit":
            if (
                route.kind is VoiceRouteKind.SESSION_EDIT
                and route.edit_request is not None
                and route.edit_request.action is expected_action
            ):
                return True, ""
            return False, "unexpected_route"
        return False, "quick_test_complete"

    def _emit_quick_test_event(
        self,
        *,
        generation: int,
        kind: str,
        status: str,
        operation: str,
        reason_code: str = "",
        backend: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        window = self._active_quick_test()
        if window is None:
            if bool(getattr(self, "_quick_test_guard_active", False)):
                self._quick_test_guard_broken = True
                self._quick_test_cycle_active = False
                self._show_status(
                    "Быстрая проверка остановлена безопасно. Закройте её и откройте снова.",
                    "error",
                    4000,
                )
            return
        self._quick_test_cycle_active = False
        self._quick_test_event_sequence = (
            int(getattr(self, "_quick_test_event_sequence", 0)) + 1
        )
        event = QuickTestEvent(
            sequence=self._quick_test_event_sequence,
            generation=int(generation),
            kind=str(kind)[:40],
            status=(
                status
                if status in {"passed", "rejected", "failed"}
                else "failed"
            ),
            operation=str(operation)[:80],
            reason_code=str(reason_code or "")[:80],
            backend=str(backend or "")[:80],
            duration_ms=max(0, int(duration_ms)),
        )
        try:
            window.handle_event(event)
        except Exception:
            self._quick_test_guard_broken = True
            try:
                window.stop_with_internal_error()
            except Exception:
                pass
            self._show_status(
                "Быстрая проверка остановлена из-за внутренней ошибки; обычная диктовка не повреждена.",
                "error",
                4000,
            )

    def _settings_saved(self, changes: dict[str, object]) -> None:
        previous = self.settings_store.get()
        candidate = previous.__class__(**asdict(previous))
        for key, value in changes.items():
            if hasattr(candidate, key):
                setattr(candidate, key, value)
        candidate.validate()

        # Preflight every component that can reject the new configuration.
        # Nothing persistent or active changes before these constructors pass.
        candidate_prompt_builder = PromptBuilder(
            token_counter=whisper_token_counter(
                model_dir(candidate.model_name) / "tokenizer.json"
            ),
            max_tokens=224,
            reserve_tokens=224 - candidate.memory_prompt_token_budget,
        )
        candidate_correction_pipeline = build_correction_pipeline(
            candidate, secret_store=self.secret_store
        )
        candidate_cloud_transcriber = build_cloud_transcriber(
            candidate, secret_store=self.secret_store
        )
        hotkey_changed = (
            candidate.activation_key != previous.activation_key
            or candidate.activation_mode != previous.activation_mode
        )
        candidate_hotkeys = (
            GlobalHotkeyListener(
                self.request_toggle,
                candidate.activation_key,
                activation_mode=candidate.activation_mode,
                on_hold_stop=self.request_hold_release,
                on_cancel=self.request_cancel,
                should_cancel=self._escape_cancellable,
            )
            if hotkey_changed
            else None
        )

        updated = self.settings_store.update(**changes)
        if candidate_hotkeys is not None:
            previous_hotkeys = self.hotkeys
            try:
                previous_hotkeys.stop()
                candidate_hotkeys.start()
            except Exception:
                stop_candidate = getattr(candidate_hotkeys, "stop", None)
                if callable(stop_candidate):
                    try:
                        stop_candidate()
                    except Exception:
                        pass
                restart_previous = getattr(previous_hotkeys, "start", None)
                if callable(restart_previous):
                    try:
                        restart_previous()
                    except Exception:
                        pass
                # SettingsStore is copy-on-write, so a successful rollback
                # restores both its in-memory value and settings.json.
                self.settings_store.update(**asdict(previous))
                raise
            self.hotkeys = candidate_hotkeys

        # From this point the transaction is committed. These assignments do
        # not perform I/O and cannot leave a half-built provider active.
        self.prompt_builder = candidate_prompt_builder
        self.correction_pipeline = candidate_correction_pipeline
        self.cloud_transcriber = candidate_cloud_transcriber

        if updated.interaction_mode != previous.interaction_mode:
            self._clear_pending_dictation_action()
            self._clear_pending_control()
            self._hide_number_overlay()
        if updated.automatic_spacing != previous.automatic_spacing:
            self._clear_automatic_spacing_context()
        try:
            self.overlay.update_preferences(
                size=updated.overlay_size,
                contrast=updated.overlay_contrast,
                position=updated.overlay_position,
            )
        except Exception:
            pass
        self._settings_window = None
        try:
            self._update_menu()
            self._show_status("✓ Настройки сохранены", "success", 1600)
        except Exception:
            pass
        self.logger.event("settings_updated", state=self.state.value)

    def _sound(self, alias: str) -> None:
        if not self.settings_store.get().sounds:
            return
        try:
            winsound.PlaySound(
                alias,
                winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except (RuntimeError, OSError):
            pass

    def _escape_cancellable(self) -> bool:
        return self.state in {
            AppState.STARTING,
            AppState.RECORDING,
            AppState.PROCESSING,
            AppState.PENDING_INSERT,
        } or bool(self._pending_control_request) or bool(
            self.number_overlay.snapshot_id
        ) or self._active_quick_test() is not None

    def _set_state(self, state: AppState, title: str) -> None:
        self.state = state
        try:
            self.tray.set_state(state.value, f"VoiceType Local — {title}")
        except Exception:
            pass

    def _runtime_feedback_snapshot(self) -> Mapping[str, str]:
        """Return a session-only UI snapshot; it is never logged or saved."""

        return dict(self._runtime_feedback)

    def _set_runtime_feedback(self, **changes: object) -> None:
        allowed = {"heard", "text", "command", "outcome", "tone"}
        for key, value in changes.items():
            if key not in allowed:
                continue
            limit = 600 if key in {"heard", "text"} else 180
            self._runtime_feedback[key] = str(value or "")[:limit]

    def _show_status(
        self, text: str, state: str, timeout_ms: int | None = None
    ) -> None:
        self._set_runtime_feedback(outcome=text, tone=state)
        try:
            self.overlay.show(text, state, timeout_ms)
        except Exception:
            pass

    def _update_menu(self) -> None:
        try:
            self.tray.icon.update_menu()
        except Exception:
            pass

    def _load_model(self) -> None:
        try:
            self.transcriber.load()
        except Exception as exc:  # shown to the user; no sensitive logging
            self.post(lambda exc=exc: self._model_failed(exc))
        else:
            self.post(self._model_ready)

    def _model_ready(self) -> None:
        if self._model_load_started_at > 0:
            self.logger.event(
                "model_loaded",
                operation="local_whisper",
                duration_ms=round(
                    (time.monotonic() - self._model_load_started_at) * 1000
                ),
            )
            self._model_load_started_at = 0.0
        self._set_state(AppState.IDLE, "готов")
        settings = self.settings_store.get()
        if not self._silent_start:
            self._show_status(
                f"✓ Готово — {activation_label(settings.activation_key, settings.activation_mode)}",
                "success",
                1800,
            )

    def _model_failed(self, error: Exception) -> None:
        self._set_state(AppState.ERROR, "ошибка модели")
        self._show_status(f"Ошибка: {error}", "error", 8000)

    def toggle(self) -> None:
        if self.state == AppState.IDLE:
            self.start_recording()
        elif self.state == AppState.STARTING:
            self._show_starting_status(1800)
        elif self.state == AppState.RECORDING:
            if time.monotonic() - self._recording_started_at < 0.35:
                self._show_status(
                    f"● Слушаю…  {self._recording_hint()}", "recording"
                )
            else:
                self.stop_recording()
        elif self.state == AppState.LOADING:
            self._show_status("Подождите: загружается локальная модель…", "loading", 1800)
        elif self.state == AppState.PROCESSING:
            self._show_processing_status(1800)
        elif self.state == AppState.PENDING_INSERT:
            if self._insertion_uncertain:
                self._show_status(
                    "Вставка могла быть частичной. Скопируйте полный текст из меню или отмените ожидание.",
                    "error",
                    3500,
                )
            else:
                self.repeat_last()
        else:
            self._show_status(
                "Приложение не готово — проверьте сообщение об ошибке",
                "error",
                2500,
            )

    def _hold_release(self) -> None:
        """Finish only a recording that belongs to a hold-style activation."""

        if self.state == AppState.STARTING:
            self.cancel()
        elif self.state == AppState.RECORDING:
            self.stop_recording()

    def _activation_key_label(self) -> str:
        return activation_key_label(self.settings_store.get().activation_key)

    def _recording_hint(self) -> str:
        settings = self.settings_store.get()
        key_label = activation_key_label(settings.activation_key)
        if settings.activation_mode == "hold":
            return f"отпустите {key_label} — закончить"
        return f"{key_label} — закончить"

    def start_recording(self) -> None:
        if self._closing or self.state != AppState.IDLE:
            return
        self._clear_pending_dictation_action()
        if self._audio_backend_blocked:
            self._show_status(
                "Драйвер микрофона завис при открытии. Перезапустите VoiceType Local через «Выход».",
                "error",
                5000,
            )
            return
        settings = self.settings_store.get()
        target_focus = self.focus_inspector.capture()
        guarded_quick_test = bool(
            getattr(self, "_quick_test_guard_active", False)
        )
        if guarded_quick_test and bool(
            getattr(self, "_quick_test_guard_broken", False)
        ):
            self._quick_test_cycle_active = False
            self._show_status(
                "Быстрая проверка остановлена. Нажмите «Начать заново».",
                "error",
                3200,
            )
            return
        quick_test_window = self._active_quick_test()
        quick_test_cycle = False
        if guarded_quick_test and quick_test_window is not None:
            try:
                quick_test_cycle = bool(
                    quick_test_window.owns_target(target_focus)
                )
            except Exception:
                quick_test_cycle = False
        if guarded_quick_test and not quick_test_cycle:
            self._quick_test_cycle_active = False
            self._show_status(
                "Быстрая проверка ещё открыта. Вернитесь в белое поле или закройте её для обычной диктовки.",
                "warning",
                4200,
            )
            return
        app_id = (
            None
            if quick_test_cycle
            else focus_app_id(target_focus) if settings.app_scoped_memory else None
        )
        domain = (
            None
            if quick_test_cycle
            else settings.active_domains[0] if settings.active_domains else None
        )
        memory_context = MemoryContext(
            language=None if settings.language == "auto" else settings.language,
            domain=domain,
            app_id=app_id,
        )
        if quick_test_cycle:
            pre_prompt = None
        else:
            try:
                pre_prompt = self.prompt_builder.build_pre_asr(
                    self.memory_store, memory_context
                )
            except Exception as exc:
                self.logger.event(
                    "memory_prompt_failed",
                    operation="pre_asr",
                    error_type=type(exc).__name__,
                )
                pre_prompt = None
        recorder = self._recorder_factory()
        self.recorder = recorder
        self._generation += 1
        generation = self._generation
        self._starting_started_at = time.monotonic()
        self._target_focus = target_focus
        self._memory_context = memory_context
        self._pre_prompt = pre_prompt
        self._quick_test_cycle_active = quick_test_cycle
        if quick_test_cycle:
            self._quick_test_attempted = True

        self.state = AppState.STARTING
        threading.Thread(
            target=self._start_recording_worker,
            args=(recorder, settings.microphone, generation),
            name="audio-start",
            daemon=True,
        ).start()
        self._set_state(AppState.STARTING, "включаю микрофон")
        self._show_starting_status()
        self.root.after(1000, lambda: self._starting_watchdog(generation))
        self.root.after(
            int(settings.microphone_start_timeout_seconds * 1000),
            lambda: self._starting_timeout(generation, recorder),
        )
        self.logger.event("recording_start_requested", state=self.state.value)

    def _start_recording_worker(
        self, recorder: AudioRecorder, microphone: str, generation: int
    ) -> None:
        try:
            recorder.start(microphone)
        except Exception as exc:
            if generation != self._generation:
                self.post(lambda: self._audio_open_finished(recorder))
                return
            self.post(lambda exc=exc: self._recording_start_failed(exc, generation))
            return
        if self._closing or generation != self._generation:
            recorder.cancel()
            self.post(lambda: self._audio_open_finished(recorder))
            return
        self.post(lambda: self._recording_started(recorder, generation))

    def _recording_start_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation or self.state != AppState.STARTING:
            return
        self._quick_test_cycle_active = False
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Микрофон недоступен: {error}", "error", 5000)
        self.logger.event(
            "recording_start_failed",
            state=self.state.value,
            error_type=type(error).__name__,
        )

    def _recording_started(self, recorder: AudioRecorder, generation: int) -> None:
        if generation != self._generation or self.state != AppState.STARTING:
            threading.Thread(target=recorder.cancel, daemon=True).start()
            return
        self.recorder = recorder
        self._recording_started_at = time.monotonic()
        self._set_state(AppState.RECORDING, "слушаю")
        self._show_status(f"● Слушаю…  {self._recording_hint()}", "recording")
        self._sound("SystemAsterisk")
        self._record_timer = threading.Timer(
            self.settings_store.get().max_record_seconds,
            lambda: self.post(self._auto_stop),
        )
        self._record_timer.daemon = True
        self._record_timer.start()
        self.logger.event("recording_started", state=self.state.value)
        self.logger.event(
            "recording_start_completed",
            operation="audio_start",
            duration_ms=round(
                (self._recording_started_at - self._starting_started_at) * 1000
            ),
        )

    def _starting_timeout(self, generation: int, recorder: AudioRecorder) -> None:
        if self._closing:
            return
        if generation != self._generation or self.state != AppState.STARTING:
            return
        self._quick_test_cycle_active = False
        self._generation += 1
        native_open_stuck = bool(
            getattr(recorder, "native_open_unabortable", False)
        )
        if native_open_stuck:
            self._audio_backend_blocked = True
            self._blocked_audio_recorder = recorder
        self._target_focus = None
        self.recorder = self._recorder_factory()
        self._set_state(AppState.IDLE, "готов")
        message = (
            "Драйвер микрофона завис до создания потока. Перезапустите VoiceType Local через «Выход»."
            if native_open_stuck
            else "Микрофон не ответил вовремя. Можно попробовать ещё раз."
        )
        self._show_status(message, "error", 6000 if native_open_stuck else 5000)
        threading.Thread(target=recorder.cancel, daemon=True).start()
        self.logger.event(
            "recording_start_timeout", state=self.state.value, operation="audio_start"
        )

    def _audio_open_finished(self, recorder: AudioRecorder) -> None:
        if self._blocked_audio_recorder is not recorder:
            return
        if bool(getattr(recorder, "native_open_unabortable", False)):
            return
        self._blocked_audio_recorder = None
        self._audio_backend_blocked = False
        if not self._closing:
            self._show_status(
                "Микрофонный драйвер снова отвечает. Можно попробовать ещё раз.",
                "success",
                2500,
            )

    def _show_starting_status(self, timeout_ms: int | None = None) -> None:
        elapsed = max(0, round(time.monotonic() - self._starting_started_at))
        text = "Включаю микрофон…" if elapsed < 3 else f"Микрофон отвечает… {elapsed} с"
        self._show_status(text, "starting", timeout_ms)

    def _starting_watchdog(self, generation: int) -> None:
        if self._closing:
            return
        if generation == self._generation and self.state == AppState.STARTING:
            self._show_starting_status()
            self.root.after(3000, lambda: self._starting_watchdog(generation))

    def _auto_stop(self) -> None:
        if self.state == AppState.RECORDING:
            self.stop_recording(refresh_target=False)

    def _cancel_timer(self) -> None:
        if self._record_timer is not None:
            self._record_timer.cancel()
            self._record_timer = None

    def stop_recording(self, *, refresh_target: bool = True) -> None:
        self._cancel_timer()
        if refresh_target:
            self._refresh_target_focus_at_stop()
        recorder = self.recorder
        duration = recorder.duration
        self._sound("SystemExclamation")
        settings = self.settings_store.get()
        language = settings.language
        self._generation += 1
        generation = self._generation
        force_local = bool(getattr(self, "_quick_test_cycle_active", False))
        self._processing_started_at = time.monotonic()
        self._processing_stage = "audio_stop"

        # Change the logical state and start the worker before touching any
        # decorative UI. PortAudio stop/close can occasionally wait on a USB
        # driver, so it must never run in Tk's event thread.
        self.state = AppState.PROCESSING
        threading.Thread(
            target=self._finish_recording_worker,
            args=(recorder, duration, language, generation, force_local),
            name="audio-stop-and-transcription",
            daemon=True,
        ).start()
        self._set_state(AppState.PROCESSING, "распознаю")
        self._show_processing_status()
        self.root.after(1000, lambda: self._processing_heartbeat(generation))
        self.root.after(
            int(settings.microphone_stop_timeout_seconds * 1000),
            lambda: self._audio_stop_timeout(generation, recorder),
        )
        self.logger.event("recording_stop_requested", state=self.state.value)

    def _finish_recording_worker(
        self,
        recorder: AudioRecorder,
        duration: float,
        language: str,
        generation: int,
        force_local: bool = False,
    ) -> None:
        stop_started = time.monotonic()
        try:
            audio_buffer = recorder.stop()
        except Exception as exc:
            self.post(lambda exc=exc: self._recording_stop_failed(exc, generation))
            return
        self.logger.event(
            "audio_stop_completed",
            operation="audio_stop",
            duration_ms=round((time.monotonic() - stop_started) * 1000),
        )
        if self._closing or generation != self._generation:
            audio_buffer.close()
            return
        if duration < 0.25:
            audio_buffer.close()
            self.post(lambda: self._recording_too_short(generation))
            return
        self._transcribe_worker(
            audio_buffer,
            language,
            generation,
            force_local=force_local,
        )

    def _audio_stop_timeout(
        self, generation: int, recorder: AudioRecorder
    ) -> None:
        if self._closing:
            return
        if (
            generation != self._generation
            or self.state != AppState.PROCESSING
            or self._processing_stage != "audio_stop"
        ):
            return
        self._quick_test_cycle_active = False
        self._generation += 1
        self._target_focus = None
        self.recorder = self._recorder_factory()
        self._set_state(AppState.IDLE, "готов")
        self._show_status(
            "Микрофон не завершил запись вовремя. Приложение восстановлено.",
            "error",
            5000,
        )
        threading.Thread(target=recorder.cancel, daemon=True).start()
        self.logger.event(
            "recording_stop_timeout", state=self.state.value, operation="audio_stop"
        )

    def _recording_stop_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self._quick_test_cycle_active = False
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Запись не получена: {error}", "error", 4000)
        self.logger.event(
            "recording_stop_failed",
            state=self.state.value,
            error_type=type(error).__name__,
        )

    def _recording_too_short(self, generation: int) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self._quick_test_cycle_active = False
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status("Слишком короткая запись", "cancelled", 1600)

    def _show_processing_status(self, timeout_ms: int | None = None) -> None:
        elapsed = max(0, round(time.monotonic() - self._processing_started_at))
        if self._processing_stage == "audio_stop":
            label = "Завершаю запись"
        elif self._processing_stage == "correction":
            label = "Бережно исправляю текст"
        elif self._processing_stage == "command":
            label = "Выполняю голосовую команду"
        elif self._processing_stage == "number_scan":
            label = "Нахожу доступные элементы"
        elif self._processing_stage == "session_edit":
            label = "Проверяю и исправляю последний текст"
        elif self._processing_stage in {"targeted_insert", "sendinput_verify"}:
            label = "Проверяю вставку текста"
        elif (
            not bool(getattr(self, "_quick_test_cycle_active", False))
            and self.cloud_transcriber is not None
        ):
            label = "Распознаю"
        else:
            label = "Распознаю локально"
        self._show_status(f"{label}… {elapsed} с", "processing", timeout_ms)

    def _processing_heartbeat(self, generation: int) -> None:
        if self._closing:
            return
        if generation == self._generation and self.state == AppState.PROCESSING:
            self._show_processing_status()
            self.root.after(2000, lambda: self._processing_heartbeat(generation))

    def _transcribe_worker(
        self,
        audio_buffer: BinaryIO,
        language: str,
        generation: int,
        *,
        force_local: bool = False,
    ) -> None:
        local_transcriber = self.transcriber
        cloud_transcriber = None if force_local else self.cloud_transcriber
        if self._closing or generation != self._generation:
            audio_buffer.close()
            return
        self._processing_stage = "transcription"
        started = time.monotonic()
        hotwords = self._pre_prompt.text if self._pre_prompt is not None else ""
        initial_prompt = _style_initial_prompt(language)
        # The audio consent covers the recording, not the user's private
        # vocabulary. Memory hotwords stay local even in cloud transcription.
        cloud_prompt = initial_prompt
        try:
            result: tuple[str, str, float] | None = None
            if cloud_transcriber is not None:
                try:
                    result = cloud_transcriber.transcribe(
                        audio_buffer, language, cloud_prompt
                    )
                except Exception as exc:
                    self.logger.event(
                        "cloud_transcription_fallback",
                        operation="cloud_transcription",
                        error_type=type(exc).__name__,
                    )
            if self._closing or generation != self._generation:
                return
            if result is None:
                result = local_transcriber.transcribe(
                    audio_buffer,
                    language,
                    initial_prompt=initial_prompt,
                    hotwords=hotwords,
                )
            if self._closing or generation != self._generation:
                return
            text, detected_language, probability = result
        except Exception as exc:
            self.post(lambda exc=exc: self._transcription_failed(exc, generation))
        else:
            self.logger.event(
                "transcription_completed",
                operation="transcription",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            self.post(
                lambda: self._transcription_ready(
                    text, detected_language, probability, generation
                )
            )
        finally:
            audio_buffer.close()

    def _transcription_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self._quick_test_cycle_active = False
        self._clear_pending_dictation_action()
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Не удалось распознать: {error}", "error", 6000)
        self.logger.event(
            "transcription_failed",
            state=self.state.value,
            operation="transcription",
            error_type=type(error).__name__,
        )

    def _current_snapshot_id(self) -> str | None:
        snapshot = self._number_snapshot
        if snapshot is None:
            return None
        if self.number_overlay.snapshot_id != snapshot.snapshot_id:
            return None
        return snapshot.snapshot_id

    def _hide_number_overlay(self) -> None:
        self._number_snapshot_poll_token += 1
        self._number_snapshot = None
        try:
            self.number_overlay.hide()
        except Exception:
            pass
        try:
            self.control_executor.clear_snapshot()
        except Exception:
            pass

    def _start_number_snapshot_poll(self, snapshot_id: str) -> None:
        self._number_snapshot_poll_token += 1
        token = self._number_snapshot_poll_token
        self.root.after(
            NUMBER_SNAPSHOT_POLL_MS,
            lambda: self._queue_number_snapshot_poll(token, snapshot_id),
        )

    def _queue_number_snapshot_poll(self, token: int, snapshot_id: str) -> None:
        if (
            self._closing
            or token != self._number_snapshot_poll_token
            or self._current_snapshot_id() != snapshot_id
        ):
            return
        if self._number_snapshot_poll_in_flight is not None:
            self.root.after(
                NUMBER_SNAPSHOT_POLL_MS,
                lambda: self._queue_number_snapshot_poll(token, snapshot_id),
            )
            return
        self._ensure_number_snapshot_poll_worker()
        self._number_snapshot_poll_in_flight = token
        try:
            self._number_snapshot_poll_queue.put_nowait((token, snapshot_id))
        except queue.Full:
            self._number_snapshot_poll_in_flight = None
            self.root.after(
                NUMBER_SNAPSHOT_POLL_MS,
                lambda: self._queue_number_snapshot_poll(token, snapshot_id),
            )

    def _ensure_number_snapshot_poll_worker(self) -> None:
        worker = self._number_snapshot_poll_worker
        if worker is not None and worker.is_alive():
            return
        if self._number_snapshot_poll_stop.is_set():
            return
        worker = threading.Thread(
            target=self._number_snapshot_poll_worker_loop,
            name="number-snapshot-watch",
            daemon=True,
        )
        self._number_snapshot_poll_worker = worker
        worker.start()

    def _number_snapshot_poll_worker_loop(self) -> None:
        while not self._number_snapshot_poll_stop.is_set():
            try:
                request = self._number_snapshot_poll_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if request is None or self._number_snapshot_poll_stop.is_set():
                return
            token, snapshot_id = request
            try:
                current = self.control_executor.is_snapshot_current(snapshot_id)
            except Exception:
                current = False
            if self._closing or self._number_snapshot_poll_stop.is_set():
                continue
            self.post(
                lambda token=token, snapshot_id=snapshot_id, current=current: self._number_snapshot_poll_ready(
                    token, snapshot_id, current
                )
            )

    def _number_snapshot_poll_ready(
        self, token: int, snapshot_id: str, current: bool
    ) -> None:
        if self._number_snapshot_poll_in_flight == token:
            self._number_snapshot_poll_in_flight = None
        if (
            self._closing
            or token != self._number_snapshot_poll_token
            or self._current_snapshot_id() != snapshot_id
        ):
            return
        if not current:
            self._hide_number_overlay()
            return
        self.root.after(
            NUMBER_SNAPSHOT_POLL_MS,
            lambda: self._queue_number_snapshot_poll(token, snapshot_id),
        )

    def _clear_pending_control(self, *, notify_executor: bool = True) -> None:
        self._pending_control_token += 1
        self._pending_control_request = None
        self._pending_control_target = None
        self._pending_control_deadline = 0.0
        if notify_executor:
            try:
                self.control_executor.cancel_pending()
            except Exception:
                pass

    def _clear_pending_dictation_action(self) -> None:
        self._pending_dictation_action = None

    @staticmethod
    def _control_request_label(request: ControlRequest) -> str:
        if request.intent is CanonicalIntent.SEND_KEY and request.key == "enter":
            return "Нажать Enter"
        labels = {
            CanonicalIntent.OPEN_APP: "Открыть приложение",
            CanonicalIntent.SWITCH_APP: "Переключить приложение",
            CanonicalIntent.MINIMIZE_WINDOW: "Свернуть окно",
            CanonicalIntent.MAXIMIZE_WINDOW: "Развернуть окно",
            CanonicalIntent.RESTORE_WINDOW: "Восстановить окно",
            CanonicalIntent.SCROLL: "Прокрутить",
            CanonicalIntent.INVOKE_NAMED_ELEMENT: "Нажать элемент",
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT: "Нажать элемент по номеру",
            CanonicalIntent.SEND_KEY: "Нажать клавишу",
            CanonicalIntent.SEND_HOTKEY: "Нажать сочетание клавиш",
        }
        return labels.get(request.intent, "Голосовая команда")

    def _expire_pending_control(
        self, token: int, request: ControlRequest
    ) -> None:
        if (
            token != self._pending_control_token
            or self._pending_control_request != request
        ):
            return
        self._clear_pending_control()
        self._hide_number_overlay()
        if self.state == AppState.IDLE:
            self._show_status(
                "Время подтверждения истекло. Повторите команду.",
                "cancelled",
                2500,
            )

    def _handle_voice_route(
        self,
        route: VoiceRoute,
        generation: int,
        language: str,
        target_focus: FocusTarget | None = None,
    ) -> None:
        self._target_focus = None
        if route.kind is VoiceRouteKind.EMPTY:
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Речь не обнаружена", "cancelled", 1800)
            return
        if route.kind is VoiceRouteKind.REJECTED:
            self._clear_pending_control()
            self._set_state(AppState.IDLE, "готов")
            reason = route.reason_code or "unknown_command"
            message = {
                "restricted_operation": "Эта команда заблокирована ради безопасности",
                "restricted_syntax": "Пути, URL и системные команды здесь запрещены",
                "number_overlay_not_active": "Сначала скажите «покажи номера»",
                "missing_command": "После слова «команда» скажите действие",
                "invalid_edit_command": "Команда редактирования неполная",
                "invalid_edit_text": "Текст для замены пустой или слишком длинный",
            }.get(reason, "Команда не распознана. Скажите «помощь»")
            self._show_status(message, "error", 3500)
            self.logger.event(
                "voice_command_blocked", operation="parse", code=reason
            )
            return
        if route.kind is VoiceRouteKind.HELP:
            self._set_state(AppState.IDLE, "готов")
            self.command_help.show(language if language in {"ru", "es", "en"} else "ru")
            self._show_status("Открыта справка голосовых команд", "success", 1800)
            return
        if route.kind is VoiceRouteKind.SHOW_NUMBERS:
            self._clear_pending_control()
            self._hide_number_overlay()
            self._processing_stage = "number_scan"
            threading.Thread(
                target=self._capture_numbered_worker,
                args=(generation,),
                name="control-number-scan",
                daemon=True,
            ).start()
            return
        if route.kind is VoiceRouteKind.REPEAT_LAST:
            self._set_state(AppState.IDLE, "готов")
            self.repeat_last()
            return
        if route.kind is VoiceRouteKind.COPY_LAST:
            self._set_state(AppState.IDLE, "готов")
            self.copy_last()
            return
        if (
            route.kind is VoiceRouteKind.SESSION_EDIT
            and route.edit_request is not None
        ):
            self._start_session_edit(
                route.edit_request,
                target_focus,
                generation,
            )
            return
        if route.kind is VoiceRouteKind.CANCEL:
            self._clear_pending_control()
            self._hide_number_overlay()
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Команда отменена", "cancelled", 1600)
            return
        if route.kind is VoiceRouteKind.CONFIRM:
            self._confirm_pending_control(generation)
            return
        if route.kind is VoiceRouteKind.CONTROL and route.request is not None:
            self._set_runtime_feedback(
                heard="Голосовая команда распознана",
                text="",
                command=self._control_request_label(route.request),
            )
            self._clear_pending_control()
            # A numbered invocation must keep the displayed snapshot until the
            # executor verifies it. Every other command releases any older
            # overlay first, so its poller cannot invalidate a new named-control
            # snapshot while the user is being asked for confirmation.
            if route.request.intent is not CanonicalIntent.INVOKE_NUMBERED_ELEMENT:
                self._hide_number_overlay()
            self._start_control_request(
                route.request,
                generation,
                expected_target=target_focus,
            )
            return
        self._set_state(AppState.IDLE, "готов")
        self._show_status("Команда не поддерживается", "error", 2500)

    @staticmethod
    def _session_edit_label(request: SessionEditRequest) -> str:
        return {
            SessionEditAction.DELETE_LAST_WORD: "Удалить последнее слово",
            SessionEditAction.DELETE_LAST_SENTENCE: "Удалить последнее предложение",
            SessionEditAction.DELETE_LAST_DICTATION: "Удалить последнюю диктовку",
            SessionEditAction.REPLACE_LAST_WORD: "Заменить последнее слово",
            SessionEditAction.REPLACE_LAST_SENTENCE: "Заменить последнее предложение",
            SessionEditAction.REPLACE_LAST_DICTATION: "Заменить последнюю диктовку",
            SessionEditAction.REPLACE_UNIQUE: "Точная замена в последней диктовке",
            SessionEditAction.RESTORE_LAST_EDIT: "Вернуть последнее исправление",
        }.get(request.action, "Изменить последний текст")

    def _session_edit_rejected(
        self,
        request: SessionEditRequest,
        reason_code: str,
        *,
        duration_ms: int = 0,
        uncertain: bool = False,
        backend: str | None = None,
    ) -> None:
        messages = {
            "no_session_insertion": "Сначала продиктуйте текст в это поле",
            "session_edit_expired": "Последняя диктовка уже слишком старая для безопасного изменения",
            "focus_changed": "Поле изменилось — текст не тронут",
            "focus_changed_after_selection": "Поле изменилось — редактирование остановлено",
            "caret_or_text_changed": "Курсор или текст изменились — ничего не удалено",
            "selection_changed": "В поле уже есть другое выделение — текст не тронут",
            "text_pattern_unavailable": "Это поле пока не поддерживает точное голосовое редактирование",
            "deletion_verification_unavailable": "В этом поле нельзя надёжно подтвердить полное удаление",
            "caret_restore_unavailable": "В этом поле нельзя безопасно восстановить позицию курсора",
            "selection_restore_failed": "Не удалось безопасно снять выделение; результат не подтверждён",
            "edit_caret_restore_failed": "Текст изменён, но позицию курсора подтвердить не удалось",
            "session_editor_busy": "Предыдущее редактирование ещё не завершилось",
            "session_edit_timeout": "Редактирование остановлено: текстовое поле отвечает слишком долго",
            "focused_control_unavailable": "Не удалось безопасно определить текстовое поле",
            "password_field_prohibited": "Поля паролей голосом не редактируются",
            "replacement_not_found": "Фраза для замены не найдена в последней диктовке",
            "replacement_ambiguous": "Фраза встречается несколько раз — уточните замену",
            "last_word_not_found": "Последнее слово не найдено",
            "last_sentence_not_found": "Последнее предложение не найдено",
            "edit_would_not_change_text": "Замена ничего не меняет",
            "restore_requires_transaction": "Нет исправления, которое можно вернуть",
            "restore_unavailable_after_empty_edit": "Полное удаление пока нельзя безопасно вернуть голосом",
            "request_cancelled": "Редактирование отменено",
            "edit_may_be_partial": "Изменение могло выполниться частично; дальнейшее редактирование заблокировано",
            "edit_verification_failed": "Не удалось подтвердить результат; дальнейшее редактирование заблокировано",
        }
        self._set_state(AppState.IDLE, "готов")
        self._show_status(
            messages.get(reason_code, "Не удалось безопасно изменить последний текст"),
            "error" if uncertain else "cancelled",
            5000 if uncertain else 3500,
        )
        self._set_runtime_feedback(
            command=self._session_edit_label(request),
            outcome="Текст не изменён безопасно" if not uncertain else "Результат изменения не подтверждён",
            tone="error" if uncertain else "cancelled",
        )
        self.logger.event(
            "voice_command_blocked",
            operation=request.action.value,
            code=reason_code,
            backend=backend,
            duration_ms=duration_ms,
        )
        self._emit_quick_test_event(
            generation=self._generation,
            kind="session_edit",
            status="failed" if uncertain else "rejected",
            operation=request.action.value,
            reason_code=reason_code,
            backend=backend,
            duration_ms=duration_ms,
        )

    def _start_session_edit(
        self,
        request: SessionEditRequest,
        command_focus: FocusTarget | None,
        generation: int,
    ) -> None:
        self._clear_pending_control()
        self._hide_number_overlay()
        tracked = self._tracked_session_insertion
        if tracked is None:
            self._session_edit_rejected(request, "no_session_insertion")
            return
        if time.monotonic() - tracked.created_at > SESSION_EDIT_TTL_SECONDS:
            self._tracked_session_insertion = None
            self._last_session_edit = None
            self._session_edit_rejected(request, "session_edit_expired")
            return
        if (
            command_focus is None
            or tracked.target.compare(command_focus) is not FocusMatch.SAME
        ):
            self._session_edit_rejected(request, "focus_changed")
            return

        consume_empty_restore = False
        try:
            if request.action is SessionEditAction.RESTORE_LAST_EDIT:
                transaction = self._last_session_edit
                if (
                    transaction is None
                    or transaction.after_text != tracked.text
                    or transaction.target.compare(tracked.target)
                    is not FocusMatch.SAME
                ):
                    raise SessionEditPlanError("restore_requires_transaction")
                if not transaction.after_text and (
                    not transaction.allow_empty_restore
                    or transaction.target.uia_runtime_id is None
                ):
                    raise SessionEditPlanError(
                        "restore_unavailable_after_empty_edit"
                    )
                consume_empty_restore = not transaction.after_text
                plan = SessionEditPlan(
                    action=request.action,
                    original_text=transaction.after_text,
                    result_text=transaction.before_text,
                    selected_text=transaction.after_text,
                    replacement_text=transaction.before_text,
                    start=0,
                    end=len(transaction.after_text),
                )
            else:
                plan = plan_session_edit(tracked.text, request)
        except SessionEditPlanError as exc:
            self._session_edit_rejected(request, exc.reason_code)
            return

        # The empty-field capability has no content fingerprint to re-check
        # after a failed attempt.  Consume it before starting the worker so a
        # later manual type-and-clear cycle cannot revive an old restoration.
        if consume_empty_restore:
            self._last_session_edit = None
        self._processing_stage = "session_edit"
        self._set_runtime_feedback(
            heard="Команда редактирования распознана",
            text=tracked.text,
            command=self._session_edit_label(request),
            outcome="Проверяю поле, курсор и последний ввод",
            tone="processing",
        )
        threading.Thread(
            target=self._session_edit_worker,
            args=(request, plan, tracked, generation),
            name="session-text-edit",
            daemon=True,
        ).start()
        self.root.after(
            int(SESSION_EDIT_TIMEOUT_SECONDS * 1000),
            lambda: self._session_edit_timeout(request, generation),
        )
        self._show_processing_status()

    def _session_edit_timeout(
        self,
        request: SessionEditRequest,
        generation: int,
    ) -> None:
        if (
            generation != self._generation
            or self.state != AppState.PROCESSING
            or self._processing_stage != "session_edit"
        ):
            return
        # Invalidate the worker before returning to Idle. If a slow provider
        # eventually responds, every gate in SessionTextEditor will refuse a
        # late edit. Its non-blocking operation lock also prevents thread
        # accumulation while the provider remains stuck.
        self._generation += 1
        self._tracked_session_insertion = None
        self._last_session_edit = None
        self._insertion_uncertain = True
        self._clear_automatic_spacing_context()
        self._session_edit_rejected(
            request,
            "session_edit_timeout",
            duration_ms=round(SESSION_EDIT_TIMEOUT_SECONDS * 1000),
            uncertain=True,
        )

    def _session_edit_worker(
        self,
        request: SessionEditRequest,
        plan: SessionEditPlan,
        tracked: _TrackedSessionInsertion,
        generation: int,
    ) -> None:
        started = time.monotonic()
        try:
            result = self.session_editor.apply(
                plan,
                tracked.target,
                is_current=lambda: (
                    not self._closing
                    and generation == self._generation
                    and self.state is AppState.PROCESSING
                ),
            )
        except Exception:
            result = SessionEditorResult(
                SessionEditorStatus.UNCERTAIN,
                "edit_result_unknown",
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        if self._closing or generation != self._generation:
            return
        self.post(
            lambda: self._session_edit_ready(
                request,
                plan,
                tracked,
                result,
                generation,
                duration_ms,
            )
        )

    def _session_edit_ready(
        self,
        request: SessionEditRequest,
        plan: SessionEditPlan,
        tracked: _TrackedSessionInsertion,
        result: SessionEditorResult,
        generation: int,
        duration_ms: int,
    ) -> None:
        if generation != self._generation or self.state is not AppState.PROCESSING:
            return
        if not result.succeeded:
            uncertain = result.status in {
                SessionEditorStatus.UNCERTAIN,
                SessionEditorStatus.FAILED,
            }
            if uncertain:
                self._tracked_session_insertion = None
                self._last_session_edit = None
                self._insertion_uncertain = True
                self._clear_automatic_spacing_context()
            self._session_edit_rejected(
                request,
                result.reason_code,
                duration_ms=duration_ms,
                uncertain=uncertain,
                backend=result.backend,
            )
            return

        previous = tracked.text
        self._tracked_session_insertion = _TrackedSessionInsertion(
            plan.result_text,
            tracked.target,
            time.monotonic(),
            tracked.automatic_prefix,
        )
        if request.action is SessionEditAction.RESTORE_LAST_EDIT:
            self._last_session_edit = None
        else:
            self._last_session_edit = _SessionEditTransaction(
                previous,
                plan.result_text,
                tracked.target,
                time.monotonic(),
                allow_empty_restore=bool(
                    not plan.result_text
                    and result.empty_document_verified
                    and tracked.target.uia_runtime_id is not None
                ),
            )
        logical_text = plan.result_text
        if tracked.automatic_prefix and logical_text.startswith(
            tracked.automatic_prefix
        ):
            logical_text = logical_text[len(tracked.automatic_prefix) :]
        self.last_text = logical_text
        self.last_local_text = logical_text
        self._remember_successful_insertion(
            plan.result_text,
            tracked.target,
            enabled=self.settings_store.get().automatic_spacing,
        )
        self._insertion_uncertain = False
        self._update_menu()
        self._set_state(AppState.IDLE, "готов")
        label = self._session_edit_label(request)
        self._show_status(f"✓ {label}", "success", 1800)
        self._set_runtime_feedback(
            text=plan.result_text,
            command=label,
            outcome="Изменение выполнено и проверено",
            tone="success",
        )
        self.logger.event(
            "voice_command_completed",
            operation=request.action.value,
            code="ok",
            backend=result.backend,
            duration_ms=duration_ms,
        )
        self._emit_quick_test_event(
            generation=generation,
            kind="session_edit",
            status="passed",
            operation=request.action.value,
            reason_code="ok",
            backend=result.backend,
            duration_ms=duration_ms,
        )

    def _capture_numbered_worker(self, generation: int) -> None:
        try:
            snapshot = self.control_executor.capture_numbered_elements()
        except Exception:
            snapshot = None
        if self._closing or generation != self._generation:
            return
        self.post(lambda: self._numbered_elements_ready(snapshot, generation))

    def _numbered_elements_ready(
        self, snapshot: NumberedElementSnapshot | None, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if snapshot is None or not snapshot.elements:
            self._hide_number_overlay()
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "Не удалось найти доступные элементы в этом окне",
                "error",
                3500,
            )
            self.logger.event(
                "voice_command_blocked", operation="show_numbers", code="snapshot_unavailable"
            )
            return
        try:
            shown = self.number_overlay.show(snapshot)
        except Exception:
            shown = False
        if not shown:
            self._hide_number_overlay()
            self._set_state(AppState.IDLE, "готов")
            self._show_status("У элементов нет видимых координат", "error", 3000)
            return
        self._number_snapshot = snapshot
        self._start_number_snapshot_poll(snapshot.snapshot_id)
        self._set_state(AppState.IDLE, "готов")
        self._show_status(
            f"Показано элементов: {len(snapshot.elements)}. Скажите «нажми номер…»",
            "success",
            4000,
        )
        self.logger.event(
            "voice_command_completed", operation="show_numbers", code="ok", backend="uia"
        )
        self.root.after(
            120_000,
            lambda snapshot_id=snapshot.snapshot_id: self._expire_number_snapshot(
                snapshot_id
            ),
        )

    def _expire_number_snapshot(self, snapshot_id: str) -> None:
        if self._current_snapshot_id() == snapshot_id:
            self._hide_number_overlay()

    def _start_control_request(
        self,
        request: ControlRequest,
        generation: int,
        *,
        confirmed: bool = False,
        expected_target: FocusTarget | None = None,
    ) -> None:
        self._processing_stage = "command"
        threading.Thread(
            target=self._execute_control_worker,
            args=(request, generation, confirmed, expected_target),
            name="windows-control",
            daemon=True,
        ).start()
        self._show_processing_status()

    def _execute_control_worker(
        self,
        request: ControlRequest,
        generation: int,
        confirmed: bool,
        expected_target: FocusTarget | None,
    ) -> None:
        started = time.monotonic()
        if (
            expected_target is not None
            and self.focus_inspector.compare_current(expected_target)
            is not FocusMatch.SAME
        ):
            result = ControlResult(
                ResultStatus.REJECTED,
                request.intent,
                reason_code="focus_changed",
            )
        else:
            try:
                input_guard = None
                if expected_target is not None and request.intent in {
                    CanonicalIntent.SEND_KEY,
                    CanonicalIntent.SEND_HOTKEY,
                }:
                    input_guard = (
                        lambda expected_target=expected_target: (
                            not self._closing
                            and generation == self._generation
                            and self.state is AppState.PROCESSING
                            and self._processing_stage == "command"
                            and self.focus_inspector.compare_current(
                                expected_target
                            )
                            is FocusMatch.SAME
                        )
                    )
                result = self.control_executor.execute(
                    request,
                    confirmed=confirmed,
                    input_guard=input_guard,
                )
            except Exception:
                result = ControlResult(
                    ResultStatus.FAILED,
                    request.intent,
                    reason_code="executor_failed",
                )
        duration_ms = round((time.monotonic() - started) * 1000)
        if self._closing or generation != self._generation:
            return
        self.post(
            lambda: self._control_result_ready(
                result,
                request,
                generation,
                duration_ms,
                expected_target=expected_target,
            )
        )

    def _confirm_pending_control(self, generation: int) -> None:
        request = self._pending_control_request
        expected_target = self._pending_control_target
        if request is None or time.monotonic() > self._pending_control_deadline:
            self._clear_pending_control()
            self._hide_number_overlay()
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Нет команды, ожидающей подтверждения", "cancelled", 2500)
            return
        self._start_control_request(
            request,
            generation,
            confirmed=True,
            expected_target=expected_target,
        )

    def _control_result_ready(
        self,
        result: ControlResult,
        request: ControlRequest,
        generation: int,
        duration_ms: int,
        *,
        expected_target: FocusTarget | None = None,
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if result.status is ResultStatus.CONFIRMATION_REQUIRED:
            self._pending_control_token += 1
            token = self._pending_control_token
            self._pending_control_request = request
            self._pending_control_target = expected_target
            self._pending_control_deadline = (
                time.monotonic() + CONTROL_CONFIRMATION_TTL_SECONDS
            )
            self._set_state(AppState.IDLE, "жду подтверждение")
            self._show_status(
                "Нужно подтверждение. Скажите «подтверждаю» или «отмена»",
                "pending",
            )
            self.logger.event(
                "voice_command_blocked",
                operation=request.intent.value,
                code="confirmation_required",
                duration_ms=duration_ms,
            )
            self.root.after(
                int(CONTROL_CONFIRMATION_TTL_SECONDS * 1000),
                lambda token=token, request=request: self._expire_pending_control(
                    token, request
                ),
            )
            return

        self._clear_pending_control(notify_executor=False)
        self._hide_number_overlay()
        self._set_state(AppState.IDLE, "готов")
        if result.status in {ResultStatus.EXECUTED, ResultStatus.NOOP}:
            labels = {
                CanonicalIntent.OPEN_APP: "Приложение открыто",
                CanonicalIntent.SWITCH_APP: "Приложение выбрано",
                CanonicalIntent.MINIMIZE_WINDOW: "Окно свёрнуто",
                CanonicalIntent.MAXIMIZE_WINDOW: "Окно развёрнуто",
                CanonicalIntent.RESTORE_WINDOW: "Окно восстановлено",
                CanonicalIntent.SCROLL: "Страница прокручена",
                CanonicalIntent.INVOKE_NAMED_ELEMENT: "Элемент нажат",
                CanonicalIntent.INVOKE_NUMBERED_ELEMENT: "Элемент нажат",
                CanonicalIntent.SEND_KEY: "Клавиша нажата",
                CanonicalIntent.SEND_HOTKEY: "Сочетание нажато",
            }
            self._show_status(
                "✓ " + labels.get(request.intent, "Команда выполнена"),
                "success",
                1600,
            )
            event = "voice_command_completed"
        else:
            message = {
                "application_not_allowlisted": "Это приложение пока не добавлено в безопасный список",
                "executable_not_found": "Приложение не найдено на компьютере",
                "uia_backend_unavailable": "Нажатие элементов недоступно без UI Automation",
                "uia_snapshot_unavailable": "Элементы этого окна недоступны",
                "element_not_found": "Элемент не найден",
                "element_name_ambiguous": "Найдено несколько элементов; используйте номера",
                "stale_or_missing_snapshot": "Номера устарели; скажите «покажи номера» ещё раз",
                "terminal_or_admin_prohibited": "Terminal и окна администратора заблокированы",
                "elevated_or_unknown_target": "Нельзя безопасно управлять этим окном",
                "unsafe_key": "Эта клавиша не входит в безопасный список",
                "unsafe_hotkey": "Это сочетание заблокировано",
                "no_matching_pending_confirmation": "Подтверждение устарело или относится к другой команде",
                "confirmation_expired": "Время подтверждения истекло. Повторите команду",
            }.get(result.reason_code, "Не удалось выполнить команду безопасно")
            self._show_status(message, "error", 4000)
            event = "voice_command_blocked"
        self.logger.event(
            event,
            operation=request.intent.value,
            code=result.reason_code or result.status.value,
            backend=result.backend,
            duration_ms=duration_ms,
        )

    def _transcription_ready(
        self, text: str, language: str, probability: float, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if not text:
            self._clear_pending_dictation_action()
            self._quick_test_cycle_active = False
            self._target_focus = None
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Речь не обнаружена", "cancelled", 1800)
            return
        settings = self.settings_store.get()
        quick_test_cycle = bool(
            getattr(self, "_quick_test_cycle_active", False)
        )
        heard_text = text
        command_focus = self._target_focus
        self._clear_pending_dictation_action()
        parser_language = (
            "auto" if settings.language == "auto" else settings.language
        )
        quick_test_mode = self._quick_test_route_mode(command_focus)
        route = self.voice_router.route(
            text,
            mode=quick_test_mode or settings.interaction_mode,
            language=parser_language,
            snapshot_id=self._current_snapshot_id(),
        )
        # While a sensitive action is waiting, exact plain confirm/cancel is
        # accepted without a prefix.  Any other phrase keeps normal everyday
        # semantics, cancels the stale confirmation, and remains dictation.
        if (
            self._pending_control_request is not None
            and settings.interaction_mode == "mixed"
        ):
            pending_route = self.voice_router.route(
                text,
                mode="commands",
                language=parser_language,
                snapshot_id=self._current_snapshot_id(),
            )
            if pending_route.kind in {VoiceRouteKind.CONFIRM, VoiceRouteKind.CANCEL}:
                route = pending_route
        quick_test_allowed, quick_test_reason = self._quick_test_route_allowed(
            route,
            command_focus,
        )
        if not quick_test_allowed:
            self._clear_pending_dictation_action()
            self._clear_pending_control()
            self._hide_number_overlay()
            self._target_focus = None
            self._set_state(AppState.IDLE, "готов")
            self._set_runtime_feedback(
                heard="Тестовая фраза не подошла",
                text="",
                command="",
                outcome="Быстрая проверка: повторите текущую фразу",
                tone="warning",
            )
            self._show_status(
                "Быстрая проверка: повторите фразу текущего шага",
                "warning",
                2800,
            )
            self._emit_quick_test_event(
                generation=generation,
                kind="route",
                status="rejected",
                operation=route.kind.value,
                reason_code=quick_test_reason,
            )
            return
        if (
            self._pending_control_request is not None
            and route.kind not in {VoiceRouteKind.CONFIRM, VoiceRouteKind.CANCEL}
        ):
            self._clear_pending_control()
            self._hide_number_overlay()
        if route.kind not in {
            VoiceRouteKind.DICTATION,
            VoiceRouteKind.DICTATION_THEN_CONTROL,
        }:
            self._handle_voice_route(
                route,
                generation,
                language,
                target_focus=command_focus,
            )
            return
        self._hide_number_overlay()
        text = route.dictation_text or text
        if quick_test_cycle:
            self._quick_test_ram_dirty = True
        self.last_raw_text = heard_text
        if (
            route.kind is VoiceRouteKind.DICTATION_THEN_CONTROL
            and route.request is not None
            and route.action_code is not None
        ):
            action_label = {
                "press_enter": "Нажать Enter",
                "send_message": "Отправить сообщение",
            }.get(route.action_code, self._control_request_label(route.request))
            self._pending_dictation_action = (
                generation,
                route.request,
                action_label,
            )
            self._set_runtime_feedback(
                heard=heard_text,
                text=text,
                command=action_label,
                outcome="Текст распознан; команда ждёт успешной вставки",
                tone="pending",
            )
        else:
            self._set_runtime_feedback(
                heard=heard_text,
                text="",
                command="",
                outcome="Текст распознан; выполняется обработка",
                tone="processing",
            )
        dictation_result = transform_dictation(
            text,
            language=language if language in {"ru", "es", "en"} else "auto",
            templates={},
            commands_enabled=settings.dictation_commands_enabled,
            remove_fillers=settings.remove_fillers,
        )
        text = dictation_result.text
        if dictation_result.applied_commands:
            command_types = "+".join(
                sorted({item.command.value for item in dictation_result.applied_commands})
            )
            self.logger.event(
                "dictation_transform_completed",
                operation="commands",
                code=command_types,
                count=len(dictation_result.applied_commands),
            )
        if dictation_result.fillers_removed:
            self.logger.event(
                "dictation_transform_completed",
                operation="fillers",
                code="closed_list",
                count=dictation_result.fillers_removed,
            )
        context = MemoryContext(
            language=language or self._memory_context.language,
            domain=None if quick_test_cycle else self._memory_context.domain,
            app_id=None if quick_test_cycle else self._memory_context.app_id,
        )
        terms: tuple[str, ...] = ()
        styles: dict[str, str] = {}
        local_text = text
        if not quick_test_cycle:
            try:
                post_prompt = self.prompt_builder.build_post_asr(
                    self.memory_store, text, context
                )
                terms = tuple(term.canonical_text for term in post_prompt.terms)
                styles = {item.key: item.value for item in post_prompt.styles}
                rules = self.memory_store.replacement_candidates(
                    context, draft=text
                )
                normalized = self.normalizer.normalize(
                    text, rules, protected_terms=terms
                )
                local_text = normalized.text
                used_ids = {
                    item.term_id
                    for item in normalized.applied
                    if item.term_id is not None
                }
                if used_ids:
                    self.memory_store.mark_terms_used(used_ids)
            except Exception as exc:
                self.logger.event(
                    "memory_postprocess_failed",
                    operation="post_asr",
                    error_type=type(exc).__name__,
                )
        self.last_local_text = local_text
        self._processing_stage = "correction"
        correction_context = CorrectionContext(
            language=language or "auto", terms=terms, style=styles
        )
        correction_pipeline = (
            self._quick_test_local_correction
            if quick_test_cycle
            else self.correction_pipeline
        )
        threading.Thread(
            target=self._correction_worker,
            args=(
                local_text,
                correction_context,
                language,
                probability,
                generation,
                correction_pipeline,
            ),
            name="text-correction",
            daemon=True,
        ).start()

    def _correction_worker(
        self,
        local_text: str,
        context: CorrectionContext,
        language: str,
        probability: float,
        generation: int,
        correction_pipeline: object,
    ) -> None:
        if self._closing or generation != self._generation:
            return
        started = time.monotonic()
        result = correction_pipeline.correct(local_text, context)  # type: ignore[attr-defined]
        self.logger.event(
            "correction_completed",
            operation=result.provider,
            duration_ms=round((time.monotonic() - started) * 1000),
            code=result.fallback_reason or "ok",
        )
        if self._closing or generation != self._generation:
            return
        self.post(
            lambda: self._final_text_ready(
                result.text, language, probability, generation
            )
        )

    def _clear_automatic_spacing_context(self) -> None:
        self._last_insertion_target = None
        self._last_insertion_at = 0.0
        self._last_insertion_tail = ""

    def _set_pending_insert(
        self,
        payload: str,
        target: FocusTarget | None,
        reason_code: str,
        *,
        uncertain: bool = False,
    ) -> None:
        """Retain one bounded RAM-only recovery payload until insert/cancel/exit."""

        self._pending_insert = _PendingInsert(
            payload=payload,
            source_target=target,
            created_at=time.monotonic(),
            reason_code=reason_code,
            uncertain=uncertain,
        )

    def _update_pending_insert(
        self,
        reason_code: str,
        *,
        uncertain: bool,
    ) -> None:
        pending = self._pending_insert
        if pending is None:
            self._set_pending_insert(
                self.last_text,
                None,
                reason_code,
                uncertain=uncertain,
            )
            return
        self._set_pending_insert(
            pending.payload,
            pending.source_target,
            reason_code,
            uncertain=uncertain,
        )

    def _clear_pending_insert(self) -> None:
        self._pending_insert = None

    def _automatic_spacing_payload(
        self,
        text: str,
        target_focus: FocusTarget | None,
        *,
        enabled: bool,
    ) -> str:
        """Return only the new insertion, with a conservative optional prefix."""

        if not enabled:
            self._clear_automatic_spacing_context()
            return text
        previous_target = self._last_insertion_target
        previous_tail = self._last_insertion_tail
        elapsed = time.monotonic() - self._last_insertion_at
        if (
            previous_target is None
            or target_focus is None
            or not previous_tail
            or not 0.0 <= elapsed <= AUTOMATIC_SPACING_TTL_SECONDS
            or previous_target.compare(target_focus) is not FocusMatch.SAME
        ):
            self._clear_automatic_spacing_context()
            return text
        combined = combine_insertions(previous_tail, text)
        return combined[len(previous_tail) :]

    def _remember_successful_insertion(
        self,
        text: str,
        target_focus: FocusTarget | None,
        *,
        enabled: bool,
    ) -> None:
        if not enabled or not text or target_focus is None:
            self._clear_automatic_spacing_context()
            return
        self._last_insertion_target = target_focus
        self._last_insertion_at = time.monotonic()
        # combine_insertions needs only the final code point. Keeping no more
        # than that avoids turning this session-only tracker into text history.
        self._last_insertion_tail = text[-1:]

    def _insert_text_into_target(
        self,
        text: str,
        target: FocusTarget,
        *,
        is_current: Callable[[], bool] | None = None,
    ) -> str:
        """Insert once, using the verified native Notepad path when applicable."""

        if target.focused_class_name == RICHEDIT_D2DPT_CLASS:
            targeted_insert = getattr(self.session_editor, "insert_targeted", None)
            targeted_backend = getattr(self, "targeted_inserter", None)
            if not callable(targeted_insert) or targeted_backend is None:
                raise TextInsertionError(
                    "Verified Notepad insertion is unavailable",
                    partial=False,
                    reason_code="targeted_inserter_unavailable",
                )
            if is_current is None:
                result = targeted_insert(text, target, targeted_backend)
            else:
                result = targeted_insert(
                    text,
                    target,
                    targeted_backend,
                    is_current=is_current,
                )
            if result.status is SessionEditorStatus.EXECUTED:
                return result.backend
            raise TextInsertionError(
                f"Verified Notepad insertion failed: {result.reason_code}",
                partial=result.status
                in {SessionEditorStatus.FAILED, SessionEditorStatus.UNCERTAIN},
                reason_code=result.reason_code,
            )

        self.inserter.insert(
            text,
            before_batch=lambda: self.focus_inspector.compare_current(target)
            is FocusMatch.SAME,
        )
        return "sendinput_unicode"

    def _targeted_insertion_is_current(self, generation: int) -> bool:
        return bool(
            not self._closing
            and generation == self._generation
            and self.state is AppState.PROCESSING
            and self._processing_stage == "targeted_insert"
        )

    def _start_targeted_insert(
        self,
        text: str,
        target: FocusTarget,
        generation: int,
        on_ready: Callable[[str | None, TextInsertionError | None, bool], None],
    ) -> None:
        """Run the potentially blocking Notepad/UIA route outside Tk."""

        self._processing_stage = "targeted_insert"
        threading.Thread(
            target=self._targeted_insert_worker,
            args=(text, target, generation, on_ready),
            name="targeted-text-insert",
            daemon=True,
        ).start()
        self.root.after(
            int(TARGETED_INSERT_TIMEOUT_SECONDS * 1000),
            lambda: self._targeted_insert_timeout(generation),
        )

    def _targeted_insert_worker(
        self,
        text: str,
        target: FocusTarget,
        generation: int,
        on_ready: Callable[[str | None, TextInsertionError | None, bool], None],
    ) -> None:
        backend: str | None = None
        failure: TextInsertionError | None = None
        unknown_failure = False
        try:
            backend = self._insert_text_into_target(
                text,
                target,
                is_current=lambda: self._targeted_insertion_is_current(generation),
            )
        except TextInsertionError as exc:
            failure = exc
        except Exception:
            unknown_failure = True
        if not self._targeted_insertion_is_current(generation):
            return
        self.post(
            lambda: self._targeted_insert_ready_if_current(
                generation,
                on_ready,
                backend,
                failure,
                unknown_failure,
            )
        )

    def _targeted_insert_ready_if_current(
        self,
        generation: int,
        on_ready: Callable[[str | None, TextInsertionError | None, bool], None],
        backend: str | None,
        failure: TextInsertionError | None,
        unknown_failure: bool,
    ) -> None:
        if not self._targeted_insertion_is_current(generation):
            return
        on_ready(backend, failure, unknown_failure)

    def _targeted_insert_timeout(self, generation: int) -> None:
        if not self._targeted_insertion_is_current(generation):
            return
        # Invalidate the worker before it can enter its final native guard. If
        # the native call already happened, the outcome is deliberately unknown
        # and repeating the text is blocked until the user explicitly cancels.
        self._generation += 1
        self._clear_pending_dictation_action()
        self._clear_automatic_spacing_context()
        self._tracked_session_insertion = None
        self._last_session_edit = None
        self._insertion_uncertain = True
        self._update_pending_insert(
            "targeted_insert_timeout",
            uncertain=True,
        )
        self._set_state(AppState.PENDING_INSERT, "результат вставки не подтверждён")
        self._show_status(
            "Проверка вставки заняла слишком долго. Не повторяйте текст вслепую; он доступен в меню.",
            "error",
            5000,
        )
        self._set_runtime_feedback(
            outcome="Результат вставки не подтверждён: превышено время ожидания",
            tone="error",
        )
        self.logger.event(
            "text_insertion_failed",
            operation="richedit_targeted",
            code="targeted_insert_timeout",
        )

    def _sendinput_verification_is_current(self, generation: int) -> bool:
        return bool(
            not self._closing
            and generation == self._generation
            and self.state is AppState.PROCESSING
            and self._processing_stage == "sendinput_verify"
        )

    def _start_sendinput_verification(
        self,
        text: str,
        target: FocusTarget,
        generation: int,
        on_ready: Callable[[SessionEditorResult], None],
        *,
        operation: str,
    ) -> None:
        """Read back a SendInput result outside Tk before claiming success."""

        self._processing_stage = "sendinput_verify"
        threading.Thread(
            target=self._sendinput_verification_worker,
            args=(text, target, generation, on_ready),
            name="sendinput-postcheck",
            daemon=True,
        ).start()
        self.root.after(
            int(SENDINPUT_VERIFY_TIMEOUT_SECONDS * 1000),
            lambda: self._sendinput_verification_timeout(
                generation,
                operation=operation,
            ),
        )

    def _sendinput_verification_worker(
        self,
        text: str,
        target: FocusTarget,
        generation: int,
        on_ready: Callable[[SessionEditorResult], None],
    ) -> None:
        inserter = getattr(self.session_editor, "insert_verified", None)
        try:
            if not callable(inserter):
                result = SessionEditorResult(
                    SessionEditorStatus.UNAVAILABLE,
                    "verified_inserter_unavailable",
                    backend="uia_text+sendinput_verified",
                )
            else:
                result = inserter(
                    text,
                    target,
                    is_current=lambda: self._sendinput_verification_is_current(
                        generation
                    ),
                )
                if not isinstance(result, SessionEditorResult):
                    result = SessionEditorResult(
                        SessionEditorStatus.UNCERTAIN,
                        "insertion_verification_failed",
                        backend="uia_text+sendinput_verified",
                    )
        except Exception:
            result = SessionEditorResult(
                SessionEditorStatus.UNCERTAIN,
                "insertion_verification_failed",
                backend="uia_text+sendinput_verified",
            )
        if not self._sendinput_verification_is_current(generation):
            return
        self.post(
            lambda: self._sendinput_verification_ready_if_current(
                generation,
                on_ready,
                result,
            )
        )

    def _sendinput_verification_ready_if_current(
        self,
        generation: int,
        on_ready: Callable[[SessionEditorResult], None],
        result: SessionEditorResult,
    ) -> None:
        if not self._sendinput_verification_is_current(generation):
            return
        on_ready(result)

    def _sendinput_postcheck_failed(
        self,
        reason_code: str,
        *,
        generation: int,
        operation: str,
    ) -> None:
        had_action = self._pending_dictation_action is not None
        self._clear_pending_dictation_action()
        self._clear_automatic_spacing_context()
        self._tracked_session_insertion = None
        self._last_session_edit = None
        self._insertion_uncertain = True
        self._update_pending_insert(reason_code, uncertain=True)
        self._set_state(AppState.PENDING_INSERT, "результат вставки не подтверждён")
        self._show_status(
            "Windows получил ввод, но поле не подтвердило текст. Не повторяйте его вслепую; полный текст доступен в меню.",
            "error",
            5000,
        )
        self._set_runtime_feedback(
            outcome="Результат вставки не подтверждён; текст сохранён в памяти сессии",
            tone="error",
        )
        self.logger.event(
            "text_insertion_failed",
            operation=operation,
            code=reason_code,
        )
        if had_action:
            self._set_runtime_feedback(
                outcome="Команда не выполнена: результат вставки текста не подтверждён",
                tone="error",
            )
        self._emit_quick_test_event(
            generation=generation,
            kind="dictation",
            status="failed",
            operation="verify_insert",
            reason_code=reason_code,
            backend="uia_text_postcheck",
        )

    def _sendinput_verification_timeout(
        self,
        generation: int,
        *,
        operation: str,
    ) -> None:
        if not self._sendinput_verification_is_current(generation):
            return
        # A SendInput side effect may already have happened. Invalidate any
        # late worker and keep repeat disabled until the user explicitly clears
        # the uncertainty.
        self._generation += 1
        self._sendinput_postcheck_failed(
            "sendinput_postcheck_timeout",
            generation=self._generation,
            operation=operation,
        )

    def _dictation_insertion_failed(
        self,
        failure: TextInsertionError | None,
        *,
        unknown_failure: bool,
    ) -> None:
        had_action = self._pending_dictation_action is not None
        self._clear_pending_dictation_action()
        self._clear_automatic_spacing_context()
        if failure is not None and not unknown_failure:
            self._insertion_uncertain = failure.partial
            self._update_pending_insert(
                failure.reason_code or "text_insertion_failed",
                uncertain=failure.partial,
            )
            if failure.partial:
                self._tracked_session_insertion = None
                self._last_session_edit = None
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            message = (
                "Вставка могла быть частичной. Полный текст доступен в меню до выхода."
                if failure.partial
                else f"Текст не вставлен. Поставьте курсор в другое поле и нажмите {self._activation_key_label()}."
            )
            self._show_status(
                message,
                "error",
                5000 if failure.partial else None,
            )
            self.logger.event(
                "text_insertion_failed",
                operation="sendinput_unicode",
                code=failure.reason_code or "text_insertion_failed",
            )
            if had_action:
                self._set_runtime_feedback(
                    outcome="Команда не выполнена: текст не вставлен полностью",
                    tone="error",
                )
            self._emit_quick_test_event(
                generation=self._generation,
                kind="dictation",
                status="failed" if failure.partial else "rejected",
                operation="insert",
                reason_code=failure.reason_code or "text_insertion_failed",
            )
            if not failure.partial and self._active_quick_test() is not None:
                self.clear_session_text()
            return

        self._insertion_uncertain = True
        self._update_pending_insert(
            "insertion_result_unknown",
            uncertain=True,
        )
        self._tracked_session_insertion = None
        self._last_session_edit = None
        self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
        self._show_status(
            "Неизвестно, вставился ли текст полностью. Полный текст доступен в меню до выхода.",
            "error",
            5000,
        )
        self.logger.event(
            "text_insertion_failed",
            operation="sendinput_unicode",
            code="insertion_result_unknown",
        )
        if had_action:
            self._set_runtime_feedback(
                outcome="Команда не выполнена: результат вставки неизвестен",
                tone="error",
            )
        self._emit_quick_test_event(
            generation=self._generation,
            kind="dictation",
            status="failed",
            operation="insert",
            reason_code="insertion_result_unknown",
        )

    def _complete_dictation_insertion(
        self,
        *,
        text: str,
        language: str,
        probability: float,
        generation: int,
        insertion_text: str,
        target_focus: FocusTarget,
        automatic_spacing: bool,
        insertion_started: float,
        insertion_backend: str,
        insertion_verified: bool = True,
    ) -> None:
        insertion_duration_ms = round(
            (time.monotonic() - insertion_started) * 1000
        )
        self.logger.event(
            "text_insertion_completed",
            operation=insertion_backend,
            code="verified" if insertion_verified else "guarded_unverified",
            duration_ms=insertion_duration_ms,
        )
        self._clear_pending_insert()
        self._remember_successful_insertion(
            text,
            target_focus,
            enabled=automatic_spacing,
        )
        self._tracked_session_insertion = (
            _TrackedSessionInsertion(
                insertion_text,
                target_focus,
                time.monotonic(),
                insertion_text[: len(insertion_text) - len(text)]
                if text and insertion_text.endswith(text)
                else "",
            )
            if insertion_verified
            else None
        )
        self._last_session_edit = None
        self.logger.event(
            "dictation_cycle_completed",
            operation="stop_to_insert",
            duration_ms=round(
                (time.monotonic() - self._processing_started_at) * 1000
            ),
        )
        pending_action = self._pending_dictation_action
        self._clear_pending_dictation_action()
        if pending_action is not None and pending_action[0] == generation:
            _action_generation, request, action_label = pending_action
            if not insertion_verified:
                # A field without UIA TextPattern can still accept ordinary
                # keyboard input, but VoiceType cannot prove the resulting
                # text. Never press Enter/send after such an insertion.
                self._set_state(AppState.IDLE, "готов")
                self._show_status(
                    "✓ Текст отправлен · Enter не нажат: поле не подтвердило вставку",
                    "warning",
                    4000,
                )
                self._set_runtime_feedback(
                    text=text,
                    command=action_label,
                    outcome="Текст отправлен; конечная команда безопасно пропущена",
                    tone="warning",
                )
                self.logger.event(
                    "dictation_end_action_skipped",
                    operation=request.intent.value,
                    code="insertion_not_readable",
                )
                return
            self._set_runtime_feedback(
                text=text,
                command=action_label,
                outcome="Текст вставлен; команда ожидает безопасной проверки",
                tone="processing",
            )
            self.logger.event(
                "dictation_end_action_started",
                operation=request.intent.value,
                code="after_successful_insert",
            )
            self._start_control_request(
                request,
                generation,
                expected_target=target_focus,
            )
            return
        self._set_state(AppState.IDLE, "готов")
        confidence = f"{probability:.0%}" if probability >= 0 else ""
        self._show_status(
            (
                f"✓ Вставлено · {language.upper()} {confidence}"
                if insertion_verified
                else f"✓ Текст отправлен · {language.upper()} {confidence}"
            ),
            "success",
            1500,
        )
        self._set_runtime_feedback(
            text=text,
            command="",
            outcome=(
                "Текст вставлен и проверен"
                if insertion_verified
                else "Текст отправлен в активное поле"
            ),
            tone="success",
        )
        self.logger.event(
            "text_inserted",
            state=self.state.value,
            code="verified" if insertion_verified else "guarded_unverified",
        )
        self._emit_quick_test_event(
            generation=generation,
            kind="dictation",
            status="passed" if insertion_verified else "failed",
            operation="insert",
            reason_code=("ok" if insertion_verified else "text_pattern_unavailable"),
            backend=insertion_backend,
            duration_ms=insertion_duration_ms,
        )

    def _targeted_dictation_insert_ready(
        self,
        backend: str | None,
        failure: TextInsertionError | None,
        unknown_failure: bool,
        *,
        text: str,
        language: str,
        probability: float,
        generation: int,
        insertion_text: str,
        target_focus: FocusTarget,
        automatic_spacing: bool,
        insertion_started: float,
    ) -> None:
        if failure is not None or unknown_failure or backend is None:
            self._dictation_insertion_failed(
                failure,
                unknown_failure=unknown_failure or backend is None,
            )
            return
        self._complete_dictation_insertion(
            text=text,
            language=language,
            probability=probability,
            generation=generation,
            insertion_text=insertion_text,
            target_focus=target_focus,
            automatic_spacing=automatic_spacing,
            insertion_started=insertion_started,
            insertion_backend=backend,
        )

    def _sendinput_dictation_verified(
        self,
        result: SessionEditorResult,
        *,
        text: str,
        language: str,
        probability: float,
        generation: int,
        insertion_text: str,
        target_focus: FocusTarget,
        automatic_spacing: bool,
        insertion_started: float,
    ) -> None:
        if result.status is not SessionEditorStatus.EXECUTED:
            if result.status is SessionEditorStatus.UNCERTAIN:
                self._sendinput_postcheck_failed(
                    result.reason_code,
                    generation=generation,
                    operation="sendinput_unicode+uia_delta_verify",
                )
            else:
                self._dictation_insertion_failed(
                    TextInsertionError(
                        "Verified insertion was rejected before any side effect",
                        partial=False,
                        reason_code=result.reason_code,
                    ),
                    unknown_failure=False,
                )
            return
        self._complete_dictation_insertion(
            text=text,
            language=language,
            probability=probability,
            generation=generation,
            insertion_text=insertion_text,
            target_focus=target_focus,
            automatic_spacing=automatic_spacing,
            insertion_started=insertion_started,
            insertion_backend=f"sendinput_unicode+{result.backend}",
            insertion_verified=(result.reason_code == "ok"),
        )

    def _final_text_ready(
        self, text: str, language: str, probability: float, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if text == "":
            self._target_focus = None
            self._clear_pending_dictation_action()
            self._clear_pending_insert()
            self._insertion_uncertain = False
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "После обработки текста не осталось — ничего не изменено",
                "cancelled",
                2500,
            )
            self._set_runtime_feedback(
                text="",
                command="",
                outcome="Ничего не вставлено: после обработки текста не осталось",
                tone="cancelled",
            )
            return
        self.last_text = text
        self._set_runtime_feedback(text=text)
        self._insertion_uncertain = False
        self._update_menu()
        target_focus = self._target_focus
        self._target_focus = None
        focus_match = self.focus_inspector.compare_current(target_focus)
        if focus_match is not FocusMatch.SAME:
            had_action = self._pending_dictation_action is not None
            self._clear_pending_dictation_action()
            self._clear_automatic_spacing_context()
            focus_comparison = getattr(
                self.focus_inspector,
                "last_comparison",
                None,
            )
            detailed_reason = getattr(
                getattr(focus_comparison, "unknown_reason", None),
                "value",
                None,
            )
            focus_reason = (
                f"focus_{detailed_reason}"
                if focus_match is FocusMatch.UNKNOWN and detailed_reason
                else f"focus_{focus_match.value}"
            )
            comparison_attempts = max(
                1,
                int(getattr(focus_comparison, "attempts", 1)),
            )
            self._set_pending_insert(text, target_focus, focus_reason)
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            self._show_status(
                f"Текст сохранён · поставьте курсор и нажмите {self._activation_key_label()} — вставить сюда · Esc — отменить.",
                "pending",
            )
            self.logger.event(
                "text_insertion_failed",
                operation="focus_validation",
                code=focus_reason,
                retry_count=comparison_attempts - 1,
            )
            if had_action:
                self._set_runtime_feedback(
                    outcome="Команда не выполнена: активное поле изменилось",
                    tone="error",
                )
            quick_test_active = self._active_quick_test() is not None
            self._emit_quick_test_event(
                generation=generation,
                kind="dictation",
                status="rejected",
                operation="insert",
                reason_code=focus_reason,
            )
            if quick_test_active:
                # A quick check must never leave an invisible pending insert
                # that a later hotkey press could place into another window.
                self.clear_session_text()
            return
        automatic_spacing = self.settings_store.get().automatic_spacing
        insertion_text = self._automatic_spacing_payload(
            text,
            target_focus,
            enabled=automatic_spacing,
        )
        assert target_focus is not None
        self._set_pending_insert(
            insertion_text,
            target_focus,
            "insert_in_progress",
        )
        insertion_started = time.monotonic()
        if target_focus.focused_class_name == RICHEDIT_D2DPT_CLASS:
            self._start_targeted_insert(
                insertion_text,
                target_focus,
                generation,
                lambda backend, failure, unknown_failure: self._targeted_dictation_insert_ready(
                    backend,
                    failure,
                    unknown_failure,
                    text=text,
                    language=language,
                    probability=probability,
                    generation=generation,
                    insertion_text=insertion_text,
                    target_focus=target_focus,
                    automatic_spacing=automatic_spacing,
                    insertion_started=insertion_started,
                ),
            )
            return
        self._start_sendinput_verification(
            insertion_text,
            target_focus,
            generation,
            lambda result: self._sendinput_dictation_verified(
                result,
                text=text,
                language=language,
                probability=probability,
                generation=generation,
                insertion_text=insertion_text,
                target_focus=target_focus,
                automatic_spacing=automatic_spacing,
                insertion_started=insertion_started,
            ),
            operation="sendinput_unicode",
        )
        self._show_processing_status()

    @staticmethod
    def _same_virtual_input_surface(
        expected: FocusTarget | None,
        current: FocusTarget | None,
    ) -> bool:
        """Recognize one unchanged browser/Electron HWND without UIA text."""

        if expected is None or current is None:
            return False
        if not expected.stable or not current.stable:
            return False
        if not (
            expected.has_complete_win32_identity
            and current.has_complete_win32_identity
        ):
            return False
        identity = (
            "foreground_hwnd",
            "root_hwnd",
            "focused_hwnd",
            "thread_id",
            "process_id",
        )
        if any(
            int(getattr(expected, field)) != int(getattr(current, field))
            for field in identity
        ):
            return False
        old_class = expected.focused_class_name.strip().casefold()
        new_class = current.focused_class_name.strip().casefold()
        if not old_class or old_class != new_class:
            return False
        # Do not infer a virtual editor merely because the class is not one of
        # a short list of native controls. Chromium and Electron use this
        # explicit top-level class for DOM-backed fields.
        return old_class.startswith("chrome_widgetwin_")

    def _refresh_target_focus_at_stop(self) -> None:
        """Refresh a recreated Chromium target at the user's stop boundary.

        The second activation press is the last explicit user action selecting
        where ordinary dictation belongs. After this point `_final_text_ready`
        again requires the exact UIA RuntimeId, so a later click or DOM focus
        move cannot redirect text or an end action to another field.
        """

        expected = self._target_focus
        if expected is None:
            return
        try:
            current = self.focus_inspector.capture()
        except Exception:
            return
        if expected.compare(current) is FocusMatch.SAME:
            self._target_focus = current
            return
        if not self._same_virtual_input_surface(expected, current):
            return
        if expected.uia_runtime_id is None or current.uia_runtime_id is None:
            return
        self._target_focus = current
        self.logger.event(
            "dictation_target_refreshed",
            operation="stop_activation_boundary",
            code="virtual_runtime_id_changed",
        )

    def cancel(self) -> None:
        self._pending_repeat_waiting = False
        self._clear_pending_dictation_action()
        self._quick_test_cycle_active = False
        if self.state in {AppState.STARTING, AppState.RECORDING}:
            self._cancel_timer()
            self._generation += 1
            recorder = self.recorder
            self.recorder = self._recorder_factory()
            threading.Thread(target=recorder.cancel, daemon=True).start()
            self._target_focus = None
            self._set_state(AppState.IDLE, "готов")
            self._sound("SystemHand")
            self._show_status("Запись отменена", "cancelled", 1400)
        elif self.state == AppState.PROCESSING:
            processing_stage = self._processing_stage
            self._generation += 1
            self._target_focus = None
            if processing_stage == "transcription":
                old_transcriber = self.transcriber
                self._set_state(AppState.LOADING, "перезапускаю модель")
                self._show_status("Отменяю и перезапускаю распознавание…", "loading")
                threading.Thread(
                    target=self._restart_transcriber_worker,
                    args=(old_transcriber,),
                    name="whisper-restart",
                    daemon=True,
                ).start()
            elif processing_stage == "session_edit":
                self._tracked_session_insertion = None
                self._last_session_edit = None
                self._insertion_uncertain = True
                self._clear_automatic_spacing_context()
                self._clear_pending_control()
                self._hide_number_overlay()
                self._set_state(AppState.IDLE, "готов")
                self._show_status(
                    "Редактирование отменено; результат не подтверждён",
                    "error",
                    3500,
                )
            elif processing_stage in {"targeted_insert", "sendinput_verify"}:
                self._tracked_session_insertion = None
                self._last_session_edit = None
                self._insertion_uncertain = True
                self._update_pending_insert(
                    (
                        "targeted_insert_cancelled"
                        if processing_stage == "targeted_insert"
                        else "sendinput_postcheck_cancelled"
                    ),
                    uncertain=True,
                )
                self._clear_automatic_spacing_context()
                self._clear_pending_control()
                self._hide_number_overlay()
                self._set_state(
                    AppState.PENDING_INSERT,
                    "результат вставки не подтверждён",
                )
                self._show_status(
                    "Проверка вставки отменена; не повторяйте текст вслепую.",
                    "error",
                    3500,
                )
            else:
                self._clear_pending_control()
                self._hide_number_overlay()
                self._set_state(AppState.IDLE, "готов")
                self._show_status("Обработка отменена", "cancelled", 1400)
            self.logger.event("processing_cancelled", operation=processing_stage)
        elif self.state == AppState.PENDING_INSERT:
            self._target_focus = None
            self._clear_pending_insert()
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "Ожидание вставки отменено. Последний текст остался в меню.",
                "cancelled",
                2200,
            )
        elif self._pending_control_request is not None or self.number_overlay.snapshot_id:
            self._clear_pending_control()
            self._hide_number_overlay()
            self._show_status("Голосовая команда отменена", "cancelled", 1600)
        else:
            quick_test_window = self._active_quick_test()
            if quick_test_window is not None:
                quick_test_window.close()

    def _restart_transcriber_worker(self, old_transcriber: OfflineTranscriber) -> None:
        old_transcriber.close()
        if self._closing:
            return
        settings = self.settings_store.get()
        replacement = OfflineTranscriber(
            settings,
            startup_timeout=settings.worker_start_timeout_seconds,
            transcription_timeout=settings.transcription_timeout_seconds,
        )
        try:
            replacement.load()
        except Exception as exc:
            replacement.close()
            self.post(lambda exc=exc: self._model_failed(exc))
            return
        self.post(lambda: self._transcriber_restarted(replacement))

    def _transcriber_restarted(self, replacement: OfflineTranscriber) -> None:
        if self._closing:
            replacement.close()
            return
        self.transcriber = replacement
        self._model_ready()

    def _complete_repeat_insertion(
        self,
        *,
        was_pending: bool,
        target_focus: FocusTarget,
        inserted_text: str,
        operation: str,
        insertion_verified: bool = True,
    ) -> None:
        self._set_state(AppState.IDLE, "готов")
        if was_pending:
            automatic_prefix = (
                inserted_text[: len(inserted_text) - len(self.last_text)]
                if self.last_text and inserted_text.endswith(self.last_text)
                else ""
            )
            self._tracked_session_insertion = (
                _TrackedSessionInsertion(
                    inserted_text,
                    target_focus,
                    time.monotonic(),
                    automatic_prefix,
                )
                if insertion_verified
                else None
            )
            self._remember_successful_insertion(
                self.last_text,
                target_focus,
                enabled=self.settings_store.get().automatic_spacing,
            )
            self._set_runtime_feedback(
                outcome=(
                    "Сохранённый текст вставлен; голосовое редактирование доступно"
                    if insertion_verified
                    else "Сохранённый текст отправлен в активное поле"
                ),
                tone="success",
            )
        else:
            # An ordinary duplicate repeat has no ownership proof relative to
            # the previous copy, so semantic editing remains disabled for it.
            self._tracked_session_insertion = None
        self._last_session_edit = None
        self._insertion_uncertain = False
        self._clear_pending_insert()
        self.logger.event(
            "text_insertion_completed",
            operation=operation,
            code="verified" if insertion_verified else "guarded_unverified",
        )
        self._show_status(
            (
                "✓ Последний текст вставлен"
                if insertion_verified
                else "✓ Последний текст отправлен"
            ),
            "success",
            1500,
        )

    def _targeted_repeat_insert_ready(
        self,
        backend: str | None,
        failure: TextInsertionError | None,
        unknown_failure: bool,
        *,
        was_pending: bool,
        target_focus: FocusTarget,
        inserted_text: str,
    ) -> None:
        if failure is not None and not unknown_failure:
            self._insertion_uncertain = failure.partial
            if was_pending:
                self._update_pending_insert(
                    failure.reason_code or "repeat_insert_failed",
                    uncertain=failure.partial,
                )
            self.logger.event(
                "text_insertion_failed",
                operation="pending_reinsert_targeted"
                if was_pending
                else "repeat_insert_targeted",
                code=failure.reason_code or "repeat_insert_failed",
            )
            if failure.partial:
                self._tracked_session_insertion = None
                self._last_session_edit = None
                self._set_state(
                    AppState.PENDING_INSERT,
                    "результат вставки не подтверждён",
                )
                self._show_status(
                    "Вставка могла быть частичной. Используйте «Скопировать последний текст».",
                    "error",
                    4000,
                )
            else:
                self._set_state(
                    AppState.PENDING_INSERT if was_pending else AppState.IDLE,
                    "текст ждёт вставки" if was_pending else "готов",
                )
                self._show_status(
                    f"Не удалось вставить. Выберите другое поле и снова нажмите {self._activation_key_label()}."
                    if was_pending
                    else "Повторная вставка заблокирована",
                    "pending" if was_pending else "error",
                    None if was_pending else 4000,
                )
            return
        if unknown_failure or backend is None:
            self._insertion_uncertain = True
            if was_pending:
                self._update_pending_insert(
                    "repeat_insert_unknown",
                    uncertain=True,
                )
            self.logger.event(
                "text_insertion_failed",
                operation="pending_reinsert_targeted"
                if was_pending
                else "repeat_insert_targeted",
                code="repeat_insert_unknown",
            )
            self._tracked_session_insertion = None
            self._last_session_edit = None
            self._set_state(
                AppState.PENDING_INSERT,
                "результат вставки не подтверждён",
            )
            self._show_status(
                "Неизвестно, вставился ли текст полностью. Используйте копирование из меню.",
                "error",
                4000,
            )
            return

        self._complete_repeat_insertion(
            was_pending=was_pending,
            target_focus=target_focus,
            inserted_text=inserted_text,
            operation=(
                "pending_reinsert_targeted"
                if was_pending
                else "repeat_insert_targeted"
            ),
        )

    def _sendinput_repeat_verified(
        self,
        result: SessionEditorResult,
        *,
        was_pending: bool,
        target_focus: FocusTarget,
        inserted_text: str,
        generation: int,
    ) -> None:
        operation = (
            "pending_reinsert_sendinput_unicode+uia_delta_verify"
            if was_pending
            else "repeat_insert_sendinput_unicode+uia_delta_verify"
        )
        if result.status is not SessionEditorStatus.EXECUTED:
            if result.status is SessionEditorStatus.UNCERTAIN:
                self._sendinput_postcheck_failed(
                    result.reason_code,
                    generation=generation,
                    operation=operation,
                )
            else:
                self._insertion_uncertain = False
                if was_pending:
                    self._update_pending_insert(
                        result.reason_code,
                        uncertain=False,
                    )
                self._set_state(
                    AppState.PENDING_INSERT if was_pending else AppState.IDLE,
                    "текст ждёт вставки" if was_pending else "готов",
                )
                self._show_status(
                    (
                        f"Не удалось подтвердить это поле. Выберите другое поле и снова нажмите {self._activation_key_label()}."
                        if was_pending
                        else "Повторная вставка безопасно отклонена"
                    ),
                    "pending" if was_pending else "error",
                    None if was_pending else 3500,
                )
                self.logger.event(
                    "text_insertion_failed",
                    operation=operation,
                    code=result.reason_code,
                )
            return
        self._complete_repeat_insertion(
            was_pending=was_pending,
            target_focus=target_focus,
            inserted_text=inserted_text,
            operation=f"{operation}:{result.backend}",
            insertion_verified=(result.reason_code == "ok"),
        )

    def _activation_binding_is_held(self) -> bool:
        checker = getattr(getattr(self, "hotkeys", None), "activation_is_held", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            # Failing closed here avoids injecting text with an unknown
            # modifier state.  The user can still copy the saved text.
            return True

    def _repeat_pending_after_activation_release(self) -> None:
        if (
            self._closing
            or self.state is not AppState.PENDING_INSERT
            or not self.last_text
        ):
            self._pending_repeat_waiting = False
            return
        if self._activation_binding_is_held():
            self.root.after(20, self._repeat_pending_after_activation_release)
            return
        self._pending_repeat_waiting = False
        self.repeat_last(_wait_for_activation_release=False)

    def repeat_last(self, *, _wait_for_activation_release: bool = True) -> None:
        if self.state not in {AppState.IDLE, AppState.PENDING_INSERT}:
            self._show_status(
                "Сначала завершите текущую запись или обработку текста.",
                "processing",
                2200,
            )
            return
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        pending = self.state == AppState.PENDING_INSERT
        if (
            pending
            and _wait_for_activation_release
            and self._activation_binding_is_held()
        ):
            if not self._pending_repeat_waiting:
                self._pending_repeat_waiting = True
                self._show_status(
                    f"Отпустите {self._activation_key_label()} — сохранённый текст вставится после отпускания.",
                    "pending",
                )
                self.root.after(20, self._repeat_pending_after_activation_release)
            return
        self._pending_repeat_waiting = False
        if self._insertion_uncertain:
            self._show_status(
                "Повтор заблокирован: предыдущая вставка могла быть частичной. Используйте «Скопировать последний текст».",
                "error",
                4000,
            )
            return
        if self._pending_control_request is not None or self.number_overlay.snapshot_id:
            self._clear_pending_control()
            self._hide_number_overlay()
        # A manual repeat has no verified start-of-dictation focus snapshot, so
        # it must not participate in automatic spacing for the next dictation.
        self._clear_automatic_spacing_context()
        target_focus = self.focus_inspector.capture()
        if not target_focus.stable or not target_focus.has_complete_win32_identity:
            if pending:
                self._update_pending_insert(
                    "retry_focus_unstable",
                    uncertain=False,
                )
            self.logger.event(
                "text_insertion_failed",
                operation="pending_reinsert_focus"
                if pending
                else "repeat_insert_focus",
                code="retry_focus_unstable",
            )
            self._show_status(
                "Не удалось надёжно определить поле. Поставьте курсор и повторите.",
                "pending",
                3000,
            )
            return
        # A saved service prefix (normally one automatic leading space) was
        # calculated for the old caret position.  FocusTarget identifies the
        # field, not its current caret, so a manual retry must use the semantic
        # transcript and never replay that potentially stale prefix.
        insertion_text = self.last_text
        if target_focus.focused_class_name == RICHEDIT_D2DPT_CLASS:
            self._generation += 1
            generation = self._generation
            self._processing_started_at = time.monotonic()
            self._processing_stage = "targeted_insert"
            self._set_state(AppState.PROCESSING, "проверяю вставку")
            self._show_processing_status()
            self._start_targeted_insert(
                insertion_text,
                target_focus,
                generation,
                lambda backend, failure, unknown_failure: self._targeted_repeat_insert_ready(
                    backend,
                    failure,
                    unknown_failure,
                    was_pending=pending,
                    target_focus=target_focus,
                    inserted_text=insertion_text,
                ),
            )
            return
        self._generation += 1
        generation = self._generation
        self._processing_started_at = time.monotonic()
        self._processing_stage = "sendinput_verify"
        self._set_state(AppState.PROCESSING, "проверяю вставку")
        self._show_processing_status()
        self._start_sendinput_verification(
            insertion_text,
            target_focus,
            generation,
            lambda result: self._sendinput_repeat_verified(
                result,
                was_pending=pending,
                target_focus=target_focus,
                inserted_text=insertion_text,
                generation=generation,
            ),
            operation=(
                "pending_reinsert_sendinput_unicode"
                if pending
                else "repeat_insert_sendinput_unicode"
            ),
        )

    def copy_last(self) -> None:
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_text)
        self.root.update_idletasks()
        if self.state == AppState.PENDING_INSERT:
            self._pending_repeat_waiting = False
            self._clear_pending_insert()
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "Последний текст скопирован; можно начать новую диктовку",
                "success",
                2200,
            )
        else:
            self._show_status("Последний текст скопирован", "success", 1500)

    def copy_raw(self) -> None:
        if not self.last_raw_text:
            self._show_status("Исходного текста пока нет", "cancelled", 1600)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_raw_text)
        self.root.update_idletasks()
        self._show_status("Исходный текст скопирован", "success", 1500)

    def clear_session_text(self) -> None:
        self._pending_repeat_waiting = False
        side_effect_in_flight = (
            self.state is AppState.PROCESSING
            and self._processing_stage
            in {"session_edit", "targeted_insert", "sendinput_verify"}
        )
        if side_effect_in_flight:
            # Invalidate the worker before releasing its tracked text. A UIA
            # provider may already be inside a native call, so the document
            # result is deliberately treated as uncertain even though a late
            # worker can no longer repopulate session RAM or feedback.
            self._generation += 1
            self._target_focus = None
            self._processing_stage = ""
            self._set_state(AppState.IDLE, "готов")
        self._clear_pending_dictation_action()
        self.last_raw_text = ""
        self.last_local_text = ""
        self.last_text = ""
        self._set_runtime_feedback(
            heard="",
            text="",
            command="",
            outcome=(
                "Текст сессии очищен; результат незавершённого редактирования в поле не подтверждён"
                if side_effect_in_flight
                else "Текст текущей сессии очищен"
            ),
            tone="warning" if side_effect_in_flight else "success",
        )
        self._insertion_uncertain = side_effect_in_flight
        self._clear_pending_insert()
        self._tracked_session_insertion = None
        self._last_session_edit = None
        self._clear_automatic_spacing_context()
        if self.state == AppState.PENDING_INSERT:
            self._set_state(AppState.IDLE, "готов")
        self._update_menu()
        if side_effect_in_flight:
            self._show_status(
                "Текст сессии очищен; результат операции в поле не подтверждён",
                "warning",
                3500,
            )
        else:
            self._show_status("Текст текущей сессии очищен", "success", 1600)

    def set_language(self, language: str) -> None:
        labels = {"auto": "авто", "ru": "русский", "es": "español", "en": "English"}
        self.settings_store.update(language=language)
        self._update_menu()
        self._show_status(f"Язык: {labels.get(language, language)}", "success", 1500)

    def set_interaction_mode(self, mode: str) -> None:
        labels = {
            "dictation": "Диктовка",
            "commands": "Управление Windows",
            "mixed": "Обычный · текст + точные команды",
        }
        updated = self.settings_store.update(interaction_mode=mode)
        self._clear_pending_dictation_action()
        self._clear_pending_control()
        self._hide_number_overlay()
        self._update_menu()
        self._show_status(
            f"Режим: {labels.get(updated.interaction_mode, 'Диктовка')}",
            "success",
            2200,
        )

    def _watch_hotkeys(self) -> None:
        if self._closing:
            return
        if not self.hotkeys.is_alive():
            try:
                self.hotkeys.start()
            except Exception as exc:
                self._show_status(f"Клавиша диктовки недоступна: {exc}", "error", 4000)
        self.root.after(2000, self._watch_hotkeys)

    def exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._activation_listener_stop.set()
        self._generation += 1
        self._cancel_timer()
        self._clear_pending_dictation_action()
        self._clear_pending_control()
        self._hide_number_overlay()
        self._number_snapshot_poll_stop.set()
        try:
            self._number_snapshot_poll_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.command_help.close()
        except Exception:
            pass
        settings_window = self._settings_window
        if settings_window is not None:
            try:
                settings_window.close()
            except Exception:
                pass
        memory_window = self._memory_window
        if memory_window is not None:
            try:
                memory_window.close()
            except Exception:
                pass
        quick_test_window = getattr(self, "_quick_test_window", None)
        if quick_test_window is not None:
            try:
                quick_test_window.close(clear_session=False)
            except Exception:
                pass
        cleanup = threading.Thread(
            target=self.recorder.cancel,
            name="audio-exit",
            daemon=True,
        )
        cleanup.start()
        worker_cleanup = threading.Thread(
            target=self.transcriber.close,
            name="whisper-exit",
            daemon=True,
        )
        worker_cleanup.start()
        self.hotkeys.stop()
        self.tray.stop()
        activation_listener = self._activation_listener_thread
        if activation_listener is not None:
            activation_listener.join(timeout=0.35)
        cleanup.join(timeout=0.4)
        worker_cleanup.join(timeout=1.0)
        try:
            self.memory_store.close()
        finally:
            # Session text is never persisted by VoiceType and is explicitly
            # released on exit. The clipboard, if the user copied text there,
            # remains under Windows/user control.
            self.last_raw_text = ""
            self.last_local_text = ""
            self.last_text = ""
            self._clear_automatic_spacing_context()
            self.logger.event("app_exiting", app_version=__version__)
            self.logger.close()
        self.root.quit()

    def run(self, smoke_timeout_ms: int | None = None) -> None:
        if not self._silent_start:
            self._show_status("Загружаю локальную модель…", "loading")
        self.tray.start()
        self.hotkeys.start()
        self._start_activation_listener()
        self.root.after(35, self._drain_actions)
        self.root.after(2000, self._watch_hotkeys)
        if self._open_settings_on_start:
            self.root.after(0, self.request_settings)
        if smoke_timeout_ms is not None:
            self.root.after(smoke_timeout_ms, self.exit)
        self._model_load_started_at = time.monotonic()
        threading.Thread(target=self._load_model, name="model-loader", daemon=True).start()
        try:
            self.root.mainloop()
        finally:
            if not self._closing:
                self.exit()


def run_app(
    smoke_timeout_ms: int | None = None,
    *,
    activation_waiter: Callable[[int], bool] | None = None,
    open_settings_on_start: bool = False,
    silent_start: bool = False,
) -> None:
    if os.name != "nt":
        raise SystemExit("VoiceType Local currently supports Windows only.")
    VoiceTypeApp(
        activation_waiter=activation_waiter,
        open_settings_on_start=open_settings_on_start,
        silent_start=silent_start,
    ).run(smoke_timeout_ms)


def run_ui_smoke(smoke_timeout_ms: int = 1200) -> None:
    """Exercise packaged Tk surfaces without tray, hotkeys, model, or user data."""

    if os.name != "nt":
        raise SystemExit("VoiceType Local currently supports Windows only.")
    from .config import Settings

    root = tk.Tk()
    root.withdraw()
    root.title("VoiceType Local UI smoke")
    overlay = StatusOverlay(root)
    temporary = tempfile.TemporaryDirectory(prefix="voicetype-ui-smoke-")
    store = MemoryStore(Path(temporary.name) / "memory.sqlite3")
    settings_window: SettingsWindow | None = None
    memory_window: MemoryWindow | None = None
    quick_test_window: QuickTestWindow | None = None
    quick_test_snapshot = QuickTestSessionSnapshot(0, 0, None)
    closed = False

    def close_all() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        for window in (settings_window, memory_window):
            if window is not None:
                try:
                    window.close()
                except Exception:
                    pass
        if quick_test_window is not None:
            try:
                quick_test_window.close(clear_session=False)
            except Exception:
                pass
        root.quit()

    def show_memory() -> None:
        nonlocal memory_window
        if closed:
            return
        if settings_window is not None:
            settings_window.close()
        memory_window = open_memory_window(root, store)

    def show_quick_test() -> None:
        nonlocal quick_test_window
        if closed:
            return
        if memory_window is not None:
            memory_window.close()
        quick_test_window = open_quick_test_window(
            root,
            language="auto",
            activation_label="правый Ctrl",
            start_generation=0,
            snapshot_provider=lambda: quick_test_snapshot,
            on_reset_session=lambda: None,
        )
        root.after(100, lambda: drive_quick_test(0))

    def drive_quick_test(step_index: int) -> None:
        nonlocal quick_test_snapshot
        window = quick_test_window
        if closed or window is None:
            return
        baseline = "Первое предложение. Второе предложение."
        field_values = (
            baseline,
            "Первое предложение.",
            baseline,
            "Первое предложение. Проверка завершена.",
        )
        operations = (
            "insert",
            "delete_last_sentence",
            "restore_last_edit",
            "replace_last_sentence",
        )
        kinds = (
            "dictation",
            "session_edit",
            "session_edit",
            "session_edit",
        )
        generation = step_index + 1
        value = field_values[step_index]
        window.native_field.set_text(value)
        quick_test_snapshot = QuickTestSessionSnapshot(
            generation,
            window.native_field.hwnd,
            value,
        )
        window.handle_event(
            QuickTestEvent(
                sequence=generation,
                generation=generation,
                kind=kinds[step_index],
                status="passed",
                operation=operations[step_index],
                reason_code="ok",
                backend="ui_smoke",
            )
        )
        if step_index < 3:
            root.after(90, lambda: drive_quick_test(step_index + 1))

    try:
        overlay.show("VoiceType Local — UI smoke", "ready", 250)
        settings_window = open_settings_window(
            root,
            Settings(),
            on_save=lambda _changes: None,
            save_secret=lambda _value: None,
            delete_secret=lambda: None,
        )
        root.after(250, show_memory)
        root.after(500, show_quick_test)
        root.after(max(950, int(smoke_timeout_ms)), close_all)
        root.mainloop()
    finally:
        close_all()
        store.close()
        temporary.cleanup()
        try:
            root.destroy()
        except tk.TclError:
            pass
