from __future__ import annotations

import ctypes
import enum
import os
import queue
import threading
import time
import tkinter as tk
import winsound
from collections.abc import Callable
from ctypes import wintypes
from typing import BinaryIO

from .audio import AudioRecorder
from .config import SettingsStore
from .hotkey import GlobalHotkeyListener
from .inserter import UnicodeTextInserter
from .transcriber import OfflineTranscriber
from .ui import StatusOverlay, TrayController


_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_USER32.GetForegroundWindow.argtypes = ()
_USER32.GetForegroundWindow.restype = wintypes.HWND


def _foreground_window() -> int:
    return int(_USER32.GetForegroundWindow() or 0)


class AppState(enum.StrEnum):
    LOADING = "loading"
    IDLE = "ready"
    STARTING = "starting"
    RECORDING = "recording"
    PROCESSING = "processing"
    PENDING_INSERT = "pending"
    ERROR = "error"


class VoiceTypeApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("VoiceType Local")
        self.overlay = StatusOverlay(self.root)
        self.settings_store = SettingsStore()
        settings = self.settings_store.get()
        self.transcriber = OfflineTranscriber(settings)
        self.recorder = AudioRecorder()
        self.inserter = UnicodeTextInserter()
        self.state = AppState.LOADING
        self.last_text = ""
        self._target_hwnd = 0
        self._generation = 0
        self._starting_started_at = 0.0
        self._recording_started_at = 0.0
        self._processing_started_at = 0.0
        self._actions: queue.Queue[Callable[[], None]] = queue.Queue()
        self._record_timer: threading.Timer | None = None
        self._closing = False
        self.hotkeys = GlobalHotkeyListener(self.request_toggle)
        self.tray = TrayController(
            on_toggle=self.request_toggle,
            on_cancel=self.request_cancel,
            on_insert_last=self.request_repeat_last,
            on_copy_last=self.request_copy_last,
            on_language=self.request_language,
            on_exit=self.request_exit,
            get_language=lambda: self.settings_store.get().language,
            has_last_text=lambda: bool(self.last_text),
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
            self.repeat_last()
        else:
            self._show_status(
                "Приложение не готово — проверьте сообщение об ошибке",
                "error",
                2500,
            )

    def start_recording(self) -> None:
        target_hwnd = _foreground_window()
        microphone = self.settings_store.get().microphone
        self._generation += 1
        generation = self._generation
        self._starting_started_at = time.monotonic()

        self.state = AppState.STARTING
        threading.Thread(
            target=self._start_recording_worker,
            args=(microphone, target_hwnd, generation),
            name="audio-start",
            daemon=True,
        ).start()
        self._set_state(AppState.STARTING, "включаю микрофон")
        self._show_starting_status()
        self.root.after(3000, lambda: self._starting_watchdog(generation))

    def _start_recording_worker(
        self, microphone: str, target_hwnd: int, generation: int
    ) -> None:
        try:
            self.recorder.start(microphone)
        except Exception as exc:
            self.post(lambda exc=exc: self._recording_start_failed(exc, generation))
            return
        self.post(lambda: self._recording_started(target_hwnd, generation))

    def _recording_start_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation or self.state != AppState.STARTING:
            return
        self._target_hwnd = 0
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Микрофон недоступен: {error}", "error", 5000)

    def _recording_started(self, target_hwnd: int, generation: int) -> None:
        if generation != self._generation or self.state != AppState.STARTING:
            return
        self._target_hwnd = target_hwnd
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
        duration = self.recorder.duration
        self._sound("SystemExclamation")
        language = self.settings_store.get().language
        self._generation += 1
        generation = self._generation
        self._processing_started_at = time.monotonic()

        # Change the logical state and start the worker before touching any
        # decorative UI. PortAudio stop/close can occasionally wait on a USB
        # driver, so it must never run in Tk's event thread.
        self.state = AppState.PROCESSING
        threading.Thread(
            target=self._finish_recording_worker,
            args=(duration, language, generation),
            name="audio-stop-and-transcription",
            daemon=True,
        ).start()
        self._set_state(AppState.PROCESSING, "распознаю")
        self._show_processing_status()
        self.root.after(1000, lambda: self._processing_heartbeat(generation))

    def _finish_recording_worker(
        self, duration: float, language: str, generation: int
    ) -> None:
        try:
            audio_buffer = self.recorder.stop()
        except Exception as exc:
            self.post(lambda exc=exc: self._recording_stop_failed(exc, generation))
            return
        if duration < 0.25:
            audio_buffer.close()
            self.post(lambda: self._recording_too_short(generation))
            return
        self._transcribe_worker(audio_buffer, language, generation)

    def _recording_stop_failed(self, error: Exception, generation: int) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self._target_hwnd = 0
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Запись не получена: {error}", "error", 4000)

    def _recording_too_short(self, generation: int) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        self._target_hwnd = 0
        self._set_state(AppState.IDLE, "готов")
        self._show_status("Слишком короткая запись", "cancelled", 1600)

    def _show_processing_status(self, timeout_ms: int | None = None) -> None:
        elapsed = max(0, round(time.monotonic() - self._processing_started_at))
        self._show_status(f"Распознаю локально… {elapsed} с", "processing", timeout_ms)

    def _processing_heartbeat(self, generation: int) -> None:
        if self._closing:
            return
        if generation == self._generation and self.state == AppState.PROCESSING:
            self._show_processing_status()
            self.root.after(2000, lambda: self._processing_heartbeat(generation))

    def _transcribe_worker(
        self, audio_buffer: BinaryIO, language: str, generation: int
    ) -> None:
        try:
            text, detected_language, probability = self.transcriber.transcribe(
                audio_buffer, language
            )
        except Exception as exc:
            self.post(lambda exc=exc: self._transcription_failed(exc, generation))
        else:
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
        self._target_hwnd = 0
        self._set_state(AppState.IDLE, "готов")
        self._show_status(f"Не удалось распознать: {error}", "error", 6000)

    def _transcription_ready(
        self, text: str, language: str, probability: float, generation: int
    ) -> None:
        if generation != self._generation or self.state != AppState.PROCESSING:
            return
        if not text:
            self._target_hwnd = 0
            self._set_state(AppState.IDLE, "готов")
            self._show_status("Речь не обнаружена", "cancelled", 1800)
            return
        self.last_text = text
        self._update_menu()
        target_hwnd = self._target_hwnd
        self._target_hwnd = 0
        current_hwnd = _foreground_window()
        if not target_hwnd or current_hwnd != target_hwnd:
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            self._show_status(
                "Текст сохранён. Поставьте курсор куда нужно и нажмите правый Ctrl.",
                "pending",
            )
            return
        try:
            self.inserter.insert(text)
        except Exception:
            self._set_state(AppState.PENDING_INSERT, "текст ждёт вставки")
            self._show_status(
                "Текст сохранён. Поставьте курсор в другое поле и нажмите правый Ctrl.",
                "pending",
            )
            return
        self._set_state(AppState.IDLE, "готов")
        confidence = f"{probability:.0%}" if probability >= 0 else ""
        self._show_status(
            f"✓ Вставлено · {language.upper()} {confidence}", "success", 1500
        )

    def cancel(self) -> None:
        if self.state == AppState.RECORDING:
            self._cancel_timer()
            self.recorder.cancel()
            self._target_hwnd = 0
            self._set_state(AppState.IDLE, "готов")
            self._sound("SystemHand")
            self._show_status("Запись отменена", "cancelled", 1400)
        elif self.state == AppState.PROCESSING:
            # faster-whisper cannot safely interrupt an in-process inference.
            # Leaving the real worker alive while pretending it was cancelled
            # used to wedge the next request behind the model lock.
            self._show_processing_status(1800)

    def repeat_last(self) -> None:
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        pending = self.state == AppState.PENDING_INSERT
        try:
            self.inserter.insert(self.last_text)
        except Exception:
            if pending:
                self._show_status(
                    "Не удалось вставить. Выберите другое поле и снова нажмите правый Ctrl.",
                    "pending",
                )
            else:
                self._show_status("Повторная вставка заблокирована", "error", 4000)
            return
        if pending:
            self._set_state(AppState.IDLE, "готов")
        self._show_status("✓ Последний текст вставлен", "success", 1500)

    def copy_last(self) -> None:
        if not self.last_text:
            self._show_status("Последнего текста пока нет", "cancelled", 1600)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_text)
        self.root.update_idletasks()
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
                self._show_status(f"Правый Ctrl недоступен: {exc}", "error", 4000)
        self.root.after(2000, self._watch_hotkeys)

    def exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._cancel_timer()
        if self.recorder.is_recording:
            self.recorder.cancel()
        self.hotkeys.stop()
        self.tray.stop()
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
