from __future__ import annotations

import enum
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
import winsound
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from . import __version__
from .audio import AudioRecorder
from .config import SettingsStore
from .correction import CorrectionContext
from .diagnostics import TechnicalLogger
from .focus import FocusInspector, FocusMatch, FocusTarget, focus_app_id
from .hotkey import GlobalHotkeyListener
from .inserter import TextInsertionError, UnicodeTextInserter
from .memory import MemoryContext, MemoryStore, open_memory_store_resilient
from .memory_ui import MemoryWindow, open_memory_window
from .paths import model_dir, resource_root
from .prompt_builder import PromptBuilder, PromptSlice, whisper_token_counter
from .providers import build_cloud_transcriber, build_correction_pipeline
from .secrets import DpapiSecretStore
from .settings_ui import SettingsWindow, open_settings_window
from .text_rules import SafeNormalizer
from .transcriber import OfflineTranscriber
from .ui import StatusOverlay, TrayController


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
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("VoiceType Local")
        self.overlay = StatusOverlay(self.root)
        self.settings_store = SettingsStore()
        settings = self.settings_store.get()
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
        self.cloud_transcriber = build_cloud_transcriber(
            settings, secret_store=self.secret_store
        )
        self._recorder_factory = AudioRecorder
        self.recorder = self._recorder_factory()
        self.inserter = UnicodeTextInserter()
        self.focus_inspector = FocusInspector()
        self.state = AppState.LOADING
        self.last_text = ""
        self._insertion_uncertain = False
        self._target_focus: FocusTarget | None = None
        self._memory_context = MemoryContext()
        self._pre_prompt: PromptSlice | None = None
        self._generation = 0
        self._starting_started_at = 0.0
        self._recording_started_at = 0.0
        self._processing_started_at = 0.0
        self._processing_stage = ""
        self._actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self._record_timer: threading.Timer | None = None
        self._closing = False
        self._settings_window: SettingsWindow | None = None
        self._memory_window: MemoryWindow | None = None
        self._audio_backend_blocked = False
        self._blocked_audio_recorder: AudioRecorder | None = None
        self._install_builtin_packs()
        self.hotkeys = GlobalHotkeyListener(
            self.request_toggle, settings.activation_key
        )
        self.tray = TrayController(
            on_toggle=self.request_toggle,
            on_cancel=self.request_cancel,
            on_insert_last=self.request_repeat_last,
            on_copy_last=self.request_copy_last,
            on_language=self.request_language,
            on_memory=self.request_memory,
            on_settings=self.request_settings,
            on_exit=self.request_exit,
            get_language=lambda: self.settings_store.get().language,
            has_last_text=lambda: bool(self.last_text),
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

    def request_cancel(self) -> None:
        self.post(self.cancel)

    def request_copy_last(self) -> None:
        self.post(self.copy_last)

    def request_repeat_last(self) -> None:
        self.post(self.repeat_last)

    def request_language(self, language: str) -> None:
        self.post(lambda: self.set_language(language))

    def request_exit(self) -> None:
        self.post(self.exit)

    def request_settings(self) -> None:
        self.post(self.open_settings)

    def request_memory(self) -> None:
        self.post(self.open_memory)

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
                    return
            except tk.TclError:
                pass
        self._settings_window = open_settings_window(
            self.root,
            self.settings_store.get(),
            on_save=self._settings_saved,
            save_secret=lambda value: self.secret_store.set("openai_api_key", value),
            delete_secret=self._delete_openai_key,
            on_cancel=lambda: setattr(self, "_settings_window", None),
        )

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

    def _settings_saved(self, changes: dict[str, object]) -> None:
        previous = self.settings_store.get()
        updated = self.settings_store.update(**changes)
        if updated.activation_key != previous.activation_key:
            self.hotkeys.stop()
            self.hotkeys = GlobalHotkeyListener(
                self.request_toggle, updated.activation_key
            )
            self.hotkeys.start()
        self.prompt_builder = PromptBuilder(
            token_counter=whisper_token_counter(
                model_dir(updated.model_name) / "tokenizer.json"
            ),
            max_tokens=224,
            reserve_tokens=224 - updated.memory_prompt_token_budget,
        )
        self.correction_pipeline = build_correction_pipeline(
            updated, secret_store=self.secret_store
        )
        self.cloud_transcriber = build_cloud_transcriber(
            updated, secret_store=self.secret_store
        )
        self._settings_window = None
        self._update_menu()
        self._show_status("✓ Настройки сохранены", "success", 1600)
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

    def _set_state(self, state: AppState, title: str) -> None:
        self.state = state
        try:
            self.tray.set_state(state.value, f"VoiceType Local — {title}")
        except Exception:
            pass

    def _show_status(
        self, text: str, state: str, timeout_ms: int | None = None
    ) -> None:
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
        self._set_state(AppState.IDLE, "готов")
        self._show_status("✓ Готово — нажмите правый Ctrl", "success", 1800)

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
                self._show_status("● Слушаю…  Правый Ctrl — закончить", "recording")
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

    def start_recording(self) -> None:
        if self._closing or self.state != AppState.IDLE:
            return
        if self._audio_backend_blocked:
            self._show_status(
                "Драйвер микрофона завис при открытии. Перезапустите VoiceType Local через «Выход».",
                "error",
                5000,
            )
            return
        settings = self.settings_store.get()
        target_focus = self.focus_inspector.capture()
        app_id = focus_app_id(target_focus) if settings.app_scoped_memory else None
        domain = settings.active_domains[0] if settings.active_domains else None
        memory_context = MemoryContext(
            language=None if settings.language == "auto" else settings.language,
            domain=domain,
            app_id=app_id,
        )
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
        self._show_status("● Слушаю…  Правый Ctrl — закончить", "recording")
        self._sound("SystemAsterisk")
        self._record_timer = threading.Timer(
            self.settings_store.get().max_record_seconds,
            lambda: self.post(self._auto_stop),
        )
        self._record_timer.daemon = True
        self._record_timer.start()
        self.logger.event("recording_started", state=self.state.value)

    def _starting_timeout(self, generation: int, recorder: AudioRecorder) -> None:
        if self._closing:
            return
        if generation != self._generation or self.state != AppState.STARTING:
            return
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
            self.stop_recording()

    def _cancel_timer(self) -> None:
        if self._record_timer is not None:
            self._record_timer.cancel()
            self._record_timer = None

    def stop_recording(self) -> None:
        self._cancel_timer()
        recorder = self.recorder
        duration = recorder.duration
        self._sound("SystemExclamation")
        settings = self.settings_store.get()
        language = settings.language
        self._generation += 1
        generation = self._generation
        self._processing_started_at = time.monotonic()
        self._processing_stage = "audio_stop"

        # Change the logical state and start the worker before touching any
        # decorative UI. PortAudio stop/close can occasionally wait on a USB
        # driver, so it must never run in Tk's event thread.
        self.state = AppState.PROCESSING
        threading.Thread(
            target=self._finish_recording_worker,
            args=(recorder, duration, language, generation),
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
    ) -> None:
        try:
            audio_buffer = recorder.stop()
        except Exception as exc:
            self.post(lambda exc=exc: self._recording_stop_failed(exc, generation))
            return
        if self._closing or generation != self._generation:
            audio_buffer.close()
            return
        if duration < 0.25:
            audio_buffer.close()
            self.post(lambda: self._recording_too_short(generation))
            return
        self._transcribe_worker(audio_buffer, language, generation)

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
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status("Слишком короткая запись", "cancelled", 1600)

    def _show_processing_status(self, timeout_ms: int | None = None) -> None:
        elapsed = max(0, round(time.monotonic() - self._processing_started_at))
        if self._processing_stage == "audio_stop":
            label = "Завершаю запись"
        elif self._processing_stage == "correction":
            label = "Бережно исправляю текст"
        elif self.cloud_transcriber is not None:
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
        self, audio_buffer: BinaryIO, language: str, generation: int
    ) -> None:
        local_transcriber = self.transcriber
        cloud_transcriber = self.cloud_transcriber
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
        self._target_focus = None
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Не удалось распознать: {error}", "error", 6000)
        self.logger.event(
            "transcription_failed",
            state=self.state.value,
            operation="transcription",
            error_type=type(error).__name__,
        )

    def _transcription_ready(
        self, text: str, language: str, probability: float, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if not text:
            self._target_focus = None
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Речь не обнаружена", "cancelled", 1800)
            return
        context = MemoryContext(
            language=language or self._memory_context.language,
            domain=self._memory_context.domain,
            app_id=self._memory_context.app_id,
        )
        terms: tuple[str, ...] = ()
        styles: dict[str, str] = {}
        local_text = text
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
        self._processing_stage = "correction"
        correction_context = CorrectionContext(
            language=language or "auto", terms=terms, style=styles
        )
        correction_pipeline = self.correction_pipeline
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

    def _final_text_ready(
        self, text: str, language: str, probability: float, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self.last_text = text
        self._insertion_uncertain = False
        self._update_menu()
        target_focus = self._target_focus
        self._target_focus = None
        focus_match = self.focus_inspector.compare_current(target_focus)
        if focus_match is not FocusMatch.SAME:
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            self._show_status(
                "Текст сохранён до выхода. Поставьте курсор куда нужно и нажмите правый Ctrl.",
                "pending",
            )
            return
        try:
            self.inserter.insert(text)
        except TextInsertionError as exc:
            self._insertion_uncertain = exc.partial
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            message = (
                "Вставка могла быть частичной. Полный текст доступен в меню до выхода."
                if exc.partial
                else "Текст не вставлен. Поставьте курсор в другое поле и нажмите правый Ctrl."
            )
            self._show_status(message, "error", 5000 if exc.partial else None)
            return
        except Exception:
            self._insertion_uncertain = True
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            self._show_status(
                "Неизвестно, вставился ли текст полностью. Полный текст доступен в меню до выхода.",
                "error",
                5000,
            )
            return
        self._set_state(AppState.IDLE, "готов")
        confidence = f"{probability:.0%}" if probability >= 0 else ""
        self._show_status(
            f"✓ Вставлено · {language.upper()} {confidence}", "success", 1500
        )
        self.logger.event("text_inserted", state=self.state.value)

    def cancel(self) -> None:
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
            self._generation += 1
            self._target_focus = None
            if self._processing_stage == "transcription":
                old_transcriber = self.transcriber
                self._set_state(AppState.LOADING, "перезапускаю модель")
                self._show_status("Отменяю и перезапускаю распознавание…", "loading")
                threading.Thread(
                    target=self._restart_transcriber_worker,
                    args=(old_transcriber,),
                    name="whisper-restart",
                    daemon=True,
                ).start()
            else:
                self._set_state(AppState.IDLE, "готов")
                self._show_status("Обработка отменена", "cancelled", 1400)
            self.logger.event("processing_cancelled", operation=self._processing_stage)
        elif self.state == AppState.PENDING_INSERT:
            self._target_focus = None
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "Ожидание вставки отменено. Последний текст остался в меню.",
                "cancelled",
                2200,
            )

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

    def repeat_last(self) -> None:
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        pending = self.state == AppState.PENDING_INSERT
        if self._insertion_uncertain:
            self._show_status(
                "Повтор заблокирован: предыдущая вставка могла быть частичной. Используйте «Скопировать последний текст».",
                "error",
                4000,
            )
            return
        try:
            self.inserter.insert(self.last_text)
        except TextInsertionError as exc:
            self._insertion_uncertain = exc.partial
            if pending:
                message = (
                    "Вставка могла быть частичной. Используйте «Скопировать последний текст»."
                    if exc.partial
                    else "Не удалось вставить. Выберите другое поле и снова нажмите правый Ctrl."
                )
                self._show_status(message, "error" if exc.partial else "pending", 4000 if exc.partial else None)
            else:
                self._show_status("Повторная вставка заблокирована", "error", 4000)
            return
        except Exception:
            self._insertion_uncertain = True
            if pending:
                self._show_status(
                    "Неизвестно, вставился ли текст полностью. Используйте копирование из меню.",
                    "error",
                    4000,
                )
            else:
                self._show_status("Повторная вставка заблокирована", "error", 4000)
            return
        if pending:
            self._set_state(AppState.IDLE, "готов")
        self._insertion_uncertain = False
        self._show_status("✓ Последний текст вставлен", "success", 1500)

    def copy_last(self) -> None:
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_text)
        self.root.update_idletasks()
        if self.state == AppState.PENDING_INSERT:
            self._set_state(AppState.IDLE, "готов")
            self._show_status(
                "Последний текст скопирован; можно начать новую диктовку",
                "success",
                2200,
            )
        else:
            self._show_status("Последний текст скопирован", "success", 1500)

    def set_language(self, language: str) -> None:
        labels = {"auto": "авто", "ru": "русский", "es": "español", "en": "English"}
        self.settings_store.update(language=language)
        self._update_menu()
        self._show_status(f"Язык: {labels.get(language, language)}", "success", 1500)

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
        self._generation += 1
        self._cancel_timer()
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
        cleanup.join(timeout=0.4)
        worker_cleanup.join(timeout=1.0)
        try:
            self.memory_store.close()
        finally:
            self.logger.event("app_exiting", app_version=__version__)
            self.logger.close()
        self.root.quit()

    def run(self, smoke_timeout_ms: int | None = None) -> None:
        self._show_status("Загружаю локальную модель…", "loading")
        self.tray.start()
        self.hotkeys.start()
        self.root.after(35, self._drain_actions)
        self.root.after(2000, self._watch_hotkeys)
        if smoke_timeout_ms is not None:
            self.root.after(smoke_timeout_ms, self.exit)
        threading.Thread(target=self._load_model, name="model-loader", daemon=True).start()
        try:
            self.root.mainloop()
        finally:
            if not self._closing:
                self.exit()


def run_app(smoke_timeout_ms: int | None = None) -> None:
    if os.name != "nt":
        raise SystemExit("VoiceType Local currently supports Windows only.")
    VoiceTypeApp().run(smoke_timeout_ms)


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
        root.quit()

    def show_memory() -> None:
        nonlocal memory_window
        if closed:
            return
        if settings_window is not None:
            settings_window.close()
        memory_window = open_memory_window(root, store)

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
        root.after(max(700, int(smoke_timeout_ms)), close_all)
        root.mainloop()
    finally:
        close_all()
        store.close()
        temporary.cleanup()
        try:
            root.destroy()
        except tk.TclError:
            pass
