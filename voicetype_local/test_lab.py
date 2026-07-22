from __future__ import annotations

"""RAM-only guided end-to-end check for VoiceType on Windows.

The visible editor is a native ``RICHEDIT50W`` child.  This is intentional:
the production session editor relies on a precise HWND and the Windows UI
Automation TextPattern, which a regular Tk text widget does not guarantee.
Nothing in this module writes audio, dictated text, or test results to disk.
"""

import ctypes
import os
import re
import tkinter as tk
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Literal

from .focus import FocusTarget
from .session_editing import SessionEditAction


_BASELINES = {
    "ru": "Первое предложение. Второе предложение.",
    "en": "First sentence. Second sentence.",
    "es": "Primera frase. Segunda frase.",
}

_COMMANDS = {
    "ru": (
        "Команда, удали последнее предложение.",
        "Команда, верни последнее исправление.",
        "Команда, замени последнее предложение на Проверка завершена.",
    ),
    "en": (
        "Command, delete last sentence.",
        "Command, undo last voice edit.",
        "Command, replace last sentence with Check complete.",
    ),
    "es": (
        "Comando, borra la última frase.",
        "Comando, deshaz la última corrección.",
        "Comando, reemplaza la última frase por Prueba completada.",
    ),
}

_REPLACEMENT_TAILS = {
    "ru": "Проверка завершена.",
    "en": "Check complete.",
    "es": "Prueba completada.",
}


@dataclass(frozen=True)
class QuickTestStep:
    step_id: str
    title: str
    phrase: str
    event_kind: str
    operation: str
    action: SessionEditAction | None = None


@dataclass(frozen=True)
class QuickTestEvent:
    """Structured metadata emitted by the app; deliberately contains no text."""

    sequence: int
    generation: int
    kind: str
    status: Literal["passed", "rejected", "failed"]
    operation: str
    reason_code: str = ""
    backend: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class QuickTestSessionSnapshot:
    """Current VoiceType-owned insertion, exposed only to the in-process lab."""

    generation: int
    target_hwnd: int
    tracked_text: str | None


@dataclass(frozen=True)
class QuickTestResult:
    step_id: str
    passed: bool
    detail: str
    backend: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class QuickTestUpdate:
    accepted: bool = False
    retry: bool = False
    blocked: bool = False
    completed: bool = False
    reset_field: bool = False
    message: str = ""


def _language_code(value: str) -> str:
    normalized = str(value or "auto").casefold()
    return normalized if normalized in {"ru", "en", "es"} else "ru"


def quick_test_steps(language: str) -> tuple[QuickTestStep, ...]:
    code = _language_code(language)
    delete_phrase, restore_phrase, replace_phrase = _COMMANDS[code]
    return (
        QuickTestStep(
            "dictation",
            "Микрофон, клавиша и вставка",
            _BASELINES[code],
            "dictation",
            "insert",
        ),
        QuickTestStep(
            "delete_last_sentence",
            "Удаление последнего предложения",
            delete_phrase,
            "session_edit",
            SessionEditAction.DELETE_LAST_SENTENCE.value,
            SessionEditAction.DELETE_LAST_SENTENCE,
        ),
        QuickTestStep(
            "restore_last_edit",
            "Возврат голосового исправления",
            restore_phrase,
            "session_edit",
            SessionEditAction.RESTORE_LAST_EDIT.value,
            SessionEditAction.RESTORE_LAST_EDIT,
        ),
        QuickTestStep(
            "replace_last_sentence",
            "Замена последнего предложения",
            replace_phrase,
            "session_edit",
            SessionEditAction.REPLACE_LAST_SENTENCE.value,
            SessionEditAction.REPLACE_LAST_SENTENCE,
        ),
    )


def _normalize_document(value: str) -> str:
    """Normalize only Windows newline aliases exposed by UIA providers."""

    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _normalize_phrase(value: str) -> str:
    return _normalize_document(value)


_SENTENCE_END_RE = re.compile(r"[.!?…]+(?:[\"'»”)]*)")


def _text_without_last_sentence(value: str) -> str | None:
    """Small independent oracle for the fixed neutral baseline.

    It intentionally does not import or call the production edit planner.
    """

    text = _normalize_document(value)
    endings = tuple(_SENTENCE_END_RE.finditer(text))
    if not endings:
        return None
    if endings[-1].end() < len(text):
        # The recognizer omitted only the final punctuation mark; the last
        # existing mark still separates the two dictated sentences.
        boundary = endings[-1]
    elif len(endings) >= 2:
        boundary = endings[-2]
    else:
        return None
    return text[: boundary.end()]


_RETRYABLE_REJECTION_REASONS = frozenset(
    {"focus_changed", "unexpected_route", "session_editor_busy"}
)


def _activation_instruction(label: str, mode: str) -> str:
    safe_label = str(label or "клавишу диктовки")[:80]
    if str(mode or "toggle").casefold() == "hold":
        return f"Удерживайте {safe_label}, произнесите фразу и отпустите."
    return f"Нажмите {safe_label}, произнесите фразу и нажмите ещё раз."


def _native_font_pixel_height(scaling: object) -> int:
    """Return a bounded 16-point native font height for the current Tk DPI."""

    try:
        pixels_per_point = float(scaling)
    except (TypeError, ValueError):
        pixels_per_point = 96.0 / 72.0
    if not 0.5 <= pixels_per_point <= 4.0:
        pixels_per_point = 96.0 / 72.0
    return max(20, min(48, round(16.0 * pixels_per_point)))


class QuickTestSession:
    """Pure state machine used by the UI and unit tests."""

    def __init__(self, *, language: str, start_generation: int = 0) -> None:
        self.language = _language_code(language)
        self.steps = quick_test_steps(self.language)
        self.step_index = 0
        self.start_generation = max(0, int(start_generation))
        self.last_sequence = 0
        self.baseline_text = ""
        self.expected_after_delete = ""
        self.expected_after_replace = ""
        self.final_text_match: bool | None = None
        self.results: list[QuickTestResult] = []
        self.blocked = False

    @property
    def asr_exact(self) -> bool | None:
        """Compatibility alias; the lab observes final inserted text, not raw ASR."""

        return self.final_text_match

    @property
    def completed(self) -> bool:
        return self.step_index >= len(self.steps)

    @property
    def current_step(self) -> QuickTestStep | None:
        if self.completed:
            return None
        return self.steps[self.step_index]

    def route_expectation(self) -> tuple[str, SessionEditAction | None]:
        if self.blocked or self.completed:
            return "none", None
        step = self.current_step
        if step is None:
            return "none", None
        return step.event_kind, step.action

    def reset(self, *, start_generation: int, last_sequence: int = 0) -> None:
        self.step_index = 0
        self.start_generation = max(0, int(start_generation))
        self.last_sequence = max(0, int(last_sequence))
        self.baseline_text = ""
        self.expected_after_delete = ""
        self.expected_after_replace = ""
        self.final_text_match = None
        self.results.clear()
        self.blocked = False

    def clear(self) -> None:
        """Scrub all session-derived text and results before releasing the UI."""

        self.step_index = len(self.steps)
        self.start_generation = 0
        self.last_sequence = 0
        self.baseline_text = ""
        self.expected_after_delete = ""
        self.expected_after_replace = ""
        self.final_text_match = None
        self.results.clear()
        self.blocked = True

    def _expected_text(self) -> str | None:
        if self.step_index == 0:
            return None
        if self.step_index == 1:
            return self.expected_after_delete
        if self.step_index == 2:
            return self.baseline_text
        if self.step_index == 3:
            return self.expected_after_replace
        return None

    def handle_event(
        self,
        event: QuickTestEvent,
        snapshot: QuickTestSessionSnapshot,
        *,
        field_hwnd: int,
        field_text: str,
    ) -> QuickTestUpdate:
        if self.blocked or self.completed:
            return QuickTestUpdate(message="Проверка уже остановлена или завершена.")
        if event.sequence <= self.last_sequence:
            return QuickTestUpdate(message="Устаревший результат пропущен.")
        self.last_sequence = event.sequence
        if event.generation <= self.start_generation:
            return QuickTestUpdate(message="Устаревший цикл диктовки пропущен.")
        if snapshot.generation != event.generation:
            return QuickTestUpdate(message="Поздний результат пропущен.")

        step = self.current_step
        if step is None:
            return QuickTestUpdate(message="Проверка завершена.")
        if event.status == "failed":
            self.blocked = True
            self.results.append(
                QuickTestResult(
                    step.step_id,
                    False,
                    "Результат операции не подтверждён",
                    event.backend,
                    event.duration_ms,
                )
            )
            return QuickTestUpdate(
                blocked=True,
                message="Результат операции не подтверждён. Начните проверку заново.",
            )
        if event.status != "passed":
            if event.reason_code not in _RETRYABLE_REJECTION_REASONS:
                self.blocked = True
                self.results.append(
                    QuickTestResult(
                        step.step_id,
                        False,
                        "Шаг отклонён и требует перезапуска проверки",
                        event.backend,
                        event.duration_ms,
                    )
                )
                return QuickTestUpdate(
                    blocked=True,
                    message="Шаг нельзя безопасно повторить. Начните проверку заново.",
                )
            message = {
                "focus_changed": "Верните курсор в тестовое поле и повторите фразу.",
                "unexpected_route": "Фраза не совпала с текущим шагом. Повторите её ещё раз.",
                "session_editor_busy": "Предыдущее изменение ещё завершается. Повторите фразу.",
            }.get(event.reason_code, "Шаг не выполнен безопасно. Повторите текущую фразу.")
            return QuickTestUpdate(retry=True, message=message)
        if event.kind != step.event_kind or event.operation != step.operation:
            return QuickTestUpdate(
                retry=True,
                message="Получено другое действие. Повторите фразу текущего шага.",
            )
        if snapshot.target_hwnd != int(field_hwnd) or field_hwnd <= 0:
            return QuickTestUpdate(
                retry=True,
                message="Курсор был не в тестовом поле. Верните его и повторите.",
            )
        if snapshot.tracked_text is None:
            self.blocked = True
            self.results.append(
                QuickTestResult(
                    step.step_id,
                    False,
                    "VoiceType не подтвердил собственную вставку",
                    event.backend,
                    event.duration_ms,
                )
            )
            return QuickTestUpdate(
                blocked=True,
                message="VoiceType не подтвердил свою вставку. Начните проверку заново.",
            )

        actual = _normalize_document(field_text)
        tracked = _normalize_document(snapshot.tracked_text)
        if actual != tracked:
            self.blocked = True
            self.results.append(
                QuickTestResult(
                    step.step_id,
                    False,
                    "Содержимое поля не совпало с подтверждённым результатом",
                    event.backend,
                    event.duration_ms,
                )
            )
            return QuickTestUpdate(
                blocked=True,
                message="Поле изменилось не так, как ожидалось. Проверка остановлена.",
            )

        if self.step_index == 0:
            expected_delete = _text_without_last_sentence(actual)
            if expected_delete is None:
                return QuickTestUpdate(
                    retry=True,
                    reset_field=True,
                    message="Вставка сработала, но не распознаны два предложения. Повторите первую фразу.",
                )
            self.baseline_text = actual
            self.expected_after_delete = expected_delete
            separator = "" if expected_delete.endswith((" ", "\t", "\n")) else " "
            self.expected_after_replace = (
                expected_delete + separator + _REPLACEMENT_TAILS[self.language]
            )
            self.final_text_match = _normalize_phrase(actual) == _normalize_phrase(
                _BASELINES[self.language]
            )
            detail = (
                "Вставка подтверждена; итоговый текст совпал с контрольным"
                if self.final_text_match
                else "Вставка подтверждена; итоговый текст имеет отличие"
            )
        else:
            expected = _normalize_document(self._expected_text() or "")
            if actual != expected:
                self.blocked = True
                self.results.append(
                    QuickTestResult(
                        step.step_id,
                        False,
                        "Голосовое изменение дало неожиданный результат",
                        event.backend,
                        event.duration_ms,
                    )
                )
                return QuickTestUpdate(
                    blocked=True,
                    message="Команда изменила текст не так, как ожидалось. Проверка остановлена.",
                )
            detail = "Изменение выполнено и проверено"

        self.results.append(
            QuickTestResult(
                step.step_id,
                True,
                detail,
                event.backend,
                event.duration_ms,
            )
        )
        self.step_index += 1
        return QuickTestUpdate(
            accepted=True,
            completed=self.completed,
            message=(
                "Быстрая проверка завершена."
                if self.completed
                else "Шаг пройден. Переходим дальше."
            ),
        )

    def summary(self) -> str:
        by_id = {result.step_id: result for result in self.results}
        lines = ["VoiceType Local — быстрая проверка"]
        for index, step in enumerate(self.steps, start=1):
            result = by_id.get(step.step_id)
            if result is None:
                status = "НЕ ПРОЙДЕНО"
            else:
                status = "ПРОЙДЕНО" if result.passed else "ОШИБКА"
            lines.append(f"{index}. {step.title}: {status}")
        if self.final_text_match is not None:
            lines.append(
                "Совпадение итогового текста: "
                + (
                    "точное совпадение"
                    if self.final_text_match
                    else "есть отличие"
                )
            )
        lines.append(
            "Итог: "
            + ("ОСНОВА РАБОТАЕТ" if self.completed and not self.blocked else "НУЖНА ПРОВЕРКА")
        )
        return "\n".join(lines)


class NativeRichEditField:
    """A bounded native RichEdit child hosted inside a Tk frame."""

    _MAX_CHARS = 4096

    def __init__(self, host: tk.Misc) -> None:
        if os.name != "nt":
            raise OSError("Quick Test Lab is available only on Windows")
        self.host = host
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self._font_handle = 0
        self._navigation_request = ""
        self._subclass_callback: object | None = None
        self._subclass_id = 0x56544C42
        self.root_hwnd = 0
        # Keep the RichEdit provider loaded for the lifetime of the control.
        self._msftedit = ctypes.WinDLL("msftedit", use_last_error=True)
        self._configure_prototypes()
        host.update_idletasks()
        parent_hwnd = int(host.winfo_id())
        if parent_hwnd <= 0:
            raise OSError("Native editor host is unavailable")

        ws_child = 0x40000000
        ws_visible = 0x10000000
        ws_vscroll = 0x00200000
        ws_tabstop = 0x00010000
        es_multiline = 0x0004
        es_autovscroll = 0x0040
        es_nohidesel = 0x0100
        es_wantreturn = 0x1000
        ws_ex_clientedge = 0x00000200
        self.hwnd = int(
            self._user32.CreateWindowExW(
                ws_ex_clientedge,
                "RICHEDIT50W",
                "",
                ws_child
                | ws_visible
                | ws_vscroll
                | ws_tabstop
                | es_multiline
                | es_autovscroll
                | es_nohidesel
                | es_wantreturn,
                0,
                0,
                max(200, int(host.winfo_width())),
                max(120, int(host.winfo_height())),
                parent_hwnd,
                0,
                0,
                None,
            )
            or 0
        )
        if self.hwnd <= 0:
            raise OSError("Could not create the native RichEdit test field")
        self.root_hwnd = int(self._user32.GetAncestor(self.hwnd, 2) or parent_hwnd)

        wm_setfont = 0x0030
        default_gui_font = 17
        em_setlimittext = 0x00C5
        try:
            scaling = host.tk.call("tk", "scaling")
        except (AttributeError, tk.TclError):
            scaling = 96.0 / 72.0
        font_height = _native_font_pixel_height(scaling)
        self._font_handle = int(
            self._gdi32.CreateFontW(
                -font_height,
                0,
                0,
                0,
                400,
                0,
                0,
                0,
                1,
                0,
                0,
                5,
                0,
                "Segoe UI",
            )
            or 0
        )
        font = self._font_handle or int(
            self._gdi32.GetStockObject(default_gui_font) or 0
        )
        if font:
            self._user32.SendMessageW(self.hwnd, wm_setfont, font, 1)
        self._user32.SendMessageW(self.hwnd, em_setlimittext, self._MAX_CHARS, 0)
        self._install_navigation_subclass()
        host.bind("<Configure>", self._on_host_configure, add="+")
        self.resize()

    def _configure_prototypes(self) -> None:
        self._user32.CreateWindowExW.argtypes = (
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        )
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.DestroyWindow.argtypes = (wintypes.HWND,)
        self._user32.DestroyWindow.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = (wintypes.HWND,)
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.MoveWindow.argtypes = (
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        )
        self._user32.MoveWindow.restype = wintypes.BOOL
        self._user32.SetFocus.argtypes = (wintypes.HWND,)
        self._user32.SetFocus.restype = wintypes.HWND
        self._user32.GetKeyState.argtypes = (ctypes.c_int,)
        self._user32.GetKeyState.restype = ctypes.c_short
        self._user32.SendMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._user32.SendMessageW.restype = ctypes.c_ssize_t
        self._user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self._user32.SetWindowTextW.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._gdi32.GetStockObject.argtypes = (ctypes.c_int,)
        self._gdi32.GetStockObject.restype = wintypes.HGDIOBJ
        self._gdi32.CreateFontW.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPCWSTR,
        )
        self._gdi32.CreateFontW.restype = wintypes.HGDIOBJ
        self._gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        self._gdi32.DeleteObject.restype = wintypes.BOOL
        self._comctl32.SetWindowSubclass.argtypes = (
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )
        self._comctl32.SetWindowSubclass.restype = wintypes.BOOL
        self._comctl32.RemoveWindowSubclass.argtypes = (
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        self._comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
        self._comctl32.DefSubclassProc.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        self._comctl32.DefSubclassProc.restype = ctypes.c_ssize_t

    def _install_navigation_subclass(self) -> None:
        """Capture navigation keys without calling Tk from a Win32 callback."""

        subclass_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )
        wm_keydown = 0x0100
        wm_char = 0x0102
        vk_tab = 0x09
        vk_escape = 0x1B
        vk_shift = 0x10

        def navigation_proc(
            hwnd: int,
            message: int,
            wparam: int,
            lparam: int,
            _subclass_id: int,
            _reference_data: int,
        ) -> int:
            try:
                shift_down = (
                    int(message) == wm_keydown
                    and int(wparam) == vk_tab
                    and int(self._user32.GetKeyState(vk_shift)) < 0
                )
                request, consumed = self._decode_navigation_message(
                    int(message),
                    int(wparam),
                    shift_down=shift_down,
                    wm_keydown=wm_keydown,
                    wm_char=wm_char,
                    vk_tab=vk_tab,
                    vk_escape=vk_escape,
                )
                if request:
                    self._navigation_request = request
                if consumed:
                    return 0
            except BaseException:
                # Never let a Python exception escape through a Win32 callback.
                pass
            return int(
                self._comctl32.DefSubclassProc(hwnd, message, wparam, lparam)
            )

        callback = subclass_proc_type(navigation_proc)
        installed = bool(
            self._comctl32.SetWindowSubclass(
                self.hwnd,
                ctypes.cast(callback, ctypes.c_void_p),
                self._subclass_id,
                0,
            )
        )
        if not installed:
            self.destroy()
            raise OSError("Could not install native field keyboard navigation")
        # ctypes callbacks must stay alive until RemoveWindowSubclass.
        self._subclass_callback = callback

    @staticmethod
    def _decode_navigation_message(
        message: int,
        key: int,
        *,
        shift_down: bool = False,
        wm_keydown: int = 0x0100,
        wm_char: int = 0x0102,
        vk_tab: int = 0x09,
        vk_escape: int = 0x1B,
    ) -> tuple[str, bool]:
        """Return a request and whether RichEdit must swallow the message."""

        if message == wm_keydown and key == vk_tab:
            return ("shift_tab" if shift_down else "tab"), True
        if message == wm_keydown and key == vk_escape:
            return "escape", True
        # TranslateMessage can enqueue WM_CHAR before the subclass sees
        # WM_KEYDOWN.  Swallow it too so TAB never enters the user's test text.
        if message == wm_char and key in {vk_tab, vk_escape}:
            return "", True
        return "", False

    def take_navigation_request(self) -> str:
        request, self._navigation_request = self._navigation_request, ""
        return request

    def _on_host_configure(self, _event: tk.Event[tk.Misc]) -> None:
        self.resize()

    def resize(self) -> None:
        if self.hwnd <= 0:
            return
        width = max(1, int(self.host.winfo_width()))
        height = max(1, int(self.host.winfo_height()))
        self._user32.MoveWindow(self.hwnd, 0, 0, width, height, True)

    def focus(self) -> None:
        if self.hwnd > 0:
            self._user32.SetFocus(self.hwnd)

    def transfer_focus(self, widget: tk.Misc) -> None:
        """Move the real Win32 focus from RichEdit to a Tk action widget."""

        if self.hwnd <= 0:
            return
        target_hwnd = int(widget.winfo_id())
        if target_hwnd > 0:
            self._user32.SetFocus(target_hwnd)

    def text(self) -> str:
        if self.hwnd <= 0:
            return ""
        length = min(self._MAX_CHARS, max(0, int(self._user32.GetWindowTextLengthW(self.hwnd))))
        buffer = ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(self.hwnd, buffer, len(buffer))
        return buffer.value

    def clear(self) -> None:
        if self.hwnd <= 0:
            return
        self._user32.SetWindowTextW(self.hwnd, "")
        self.focus()

    def set_text(self, value: str, *, focus: bool = False) -> None:
        """Set bounded neutral probe text; production dictation never calls this."""

        if self.hwnd <= 0:
            return
        text = str(value or "")[: self._MAX_CHARS]
        self._user32.SetWindowTextW(self.hwnd, text)
        em_setsel = 0x00B1
        self._user32.SendMessageW(self.hwnd, em_setsel, len(text), len(text))
        if focus:
            self.focus()

    def destroy(self) -> None:
        hwnd = self.hwnd
        callback = self._subclass_callback
        if hwnd > 0:
            self._user32.SetWindowTextW(hwnd, "")
            removed = callback is None
            if callback is not None:
                removed = bool(
                    self._comctl32.RemoveWindowSubclass(
                        hwnd,
                        ctypes.cast(callback, ctypes.c_void_p),
                        self._subclass_id,
                    )
                )
            destroyed = bool(self._user32.DestroyWindow(hwnd)) or not bool(
                self._user32.IsWindow(hwnd)
            )
            if destroyed:
                self.hwnd = 0
                self._subclass_callback = None
            elif removed:
                # The HWND still exists but no longer references Python.
                self._subclass_callback = None
                return
            else:
                # Keep both the HWND and callback alive for a later retry.
                return
        else:
            self.hwnd = 0
            self._subclass_callback = None
        self._navigation_request = ""
        font, self._font_handle = self._font_handle, 0
        if font > 0:
            self._gdi32.DeleteObject(font)
        self.root_hwnd = 0


class QuickTestWindow:
    """Accessible one-screen controller around the native test field."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        language: str,
        activation_label: str,
        activation_mode: str = "toggle",
        start_generation: int,
        snapshot_provider: Callable[[], QuickTestSessionSnapshot],
        on_reset_session: Callable[[], object],
        on_abort_cycle: Callable[[bool], object] | None = None,
        on_close: Callable[[], object] | None = None,
    ) -> None:
        self._snapshot_provider = snapshot_provider
        self._on_reset_session = on_reset_session
        self._on_abort_cycle = on_abort_cycle
        self._on_close = on_close
        self._activation_label = str(activation_label or "клавишу диктовки")[:80]
        self._activation_mode = (
            "hold" if str(activation_mode or "toggle").casefold() == "hold" else "toggle"
        )
        self._closed = False
        self._session_started = False
        self.session = QuickTestSession(
            language=language,
            start_generation=start_generation,
        )

        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("VoiceType Local — Быстрая проверка")
        self.window.configure(bg="#071827")
        self.window.minsize(820, 620)
        screen_width = max(820, self.window.winfo_screenwidth())
        screen_height = max(620, self.window.winfo_screenheight())
        width = max(820, min(980, screen_width - 70))
        height = max(620, min(760, screen_height - 90))
        left = max(0, (screen_width - width) // 2)
        top = max(0, (screen_height - height) // 2)
        self.window.geometry(f"{width}x{height}+{left}+{top}")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self._close_event())
        self.window.rowconfigure(3, weight=1)
        self.window.columnconfigure(0, weight=1)

        self._progress_var = tk.StringVar(self.window)
        self._phrase_var = tk.StringVar(self.window)
        self._status_var = tk.StringVar(self.window, "Тестовое поле готово.")
        tk.Label(
            self.window,
            text="Быстрая проверка",
            font=("Segoe UI", 22, "bold"),
            bg="#071827",
            fg="#F8FAFC",
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(20, 2))
        tk.Label(
            self.window,
            textvariable=self._progress_var,
            font=("Segoe UI", 12, "bold"),
            bg="#071827",
            fg="#38A7FF",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(2, 6))

        instruction = tk.Frame(
            self.window,
            bg="#0B2032",
            highlightbackground="#294356",
            highlightthickness=1,
            padx=18,
            pady=14,
        )
        instruction.grid(row=2, column=0, sticky="ew", padx=24, pady=(4, 12))
        instruction.columnconfigure(0, weight=1)
        tk.Label(
            instruction,
            text="Скажите:",
            font=("Segoe UI", 11),
            bg="#0B2032",
            fg="#A9BAC9",
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            instruction,
            textvariable=self._phrase_var,
            font=("Segoe UI", 17, "bold"),
            bg="#0B2032",
            fg="#FFFFFF",
            justify="left",
            anchor="w",
            wraplength=850,
        ).grid(row=1, column=0, sticky="ew", pady=(5, 6))
        tk.Label(
            instruction,
            text=_activation_instruction(
                self._activation_label,
                self._activation_mode,
            ),
            font=("Segoe UI", 10),
            bg="#0B2032",
            fg="#A9BAC9",
        ).grid(row=2, column=0, sticky="w")

        editor_panel = tk.Frame(self.window, bg="#071827")
        editor_panel.grid(row=3, column=0, sticky="nsew", padx=24)
        editor_panel.rowconfigure(1, weight=1)
        editor_panel.columnconfigure(0, weight=1)
        tk.Label(
            editor_panel,
            text="Тестовое поле — курсор должен оставаться здесь",
            font=("Segoe UI", 11, "bold"),
            bg="#071827",
            fg="#E5EDF5",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        host = tk.Frame(
            editor_panel,
            bg="#FFFFFF",
            height=220,
            highlightbackground="#2D8CFF",
            highlightthickness=2,
        )
        host.grid(row=1, column=0, sticky="nsew")
        host.grid_propagate(False)

        results = tk.Frame(self.window, bg="#071827")
        results.grid(row=4, column=0, sticky="ew", padx=24, pady=(12, 5))
        results.columnconfigure(1, weight=1)
        self._result_vars: list[tk.StringVar] = []
        self._result_labels: list[tk.Label] = []
        for row, step in enumerate(self.session.steps):
            value = tk.StringVar(self.window, "Ожидает")
            self._result_vars.append(value)
            tk.Label(
                results,
                text=f"{row + 1}. {step.title}",
                font=("Segoe UI", 10),
                bg="#071827",
                fg="#E5EDF5",
            ).grid(row=row, column=0, sticky="w", pady=2)
            result_label = tk.Label(
                results,
                textvariable=value,
                font=("Segoe UI", 10, "bold"),
                bg="#071827",
                fg="#A9BAC9",
            )
            result_label.grid(row=row, column=1, sticky="e", pady=2)
            self._result_labels.append(result_label)

        footer = tk.Frame(self.window, bg="#071827")
        footer.grid(row=5, column=0, sticky="ew", padx=24, pady=(8, 20))
        footer.columnconfigure(0, weight=1)
        self._status_label = tk.Label(
            footer,
            textvariable=self._status_var,
            font=("Segoe UI", 10, "bold"),
            bg="#071827",
            fg="#38C75B",
            anchor="w",
            justify="left",
            wraplength=880,
        )
        self._status_label.grid(
            row=0,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(0, 9),
        )
        self._restart_button = tk.Button(
            footer,
            text="Начать заново",
            command=self.restart,
            takefocus=True,
            font=("Segoe UI", 10, "bold"),
            bg="#123149",
            activebackground="#17405F",
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=7,
        )
        self._restart_button.grid(row=1, column=1, padx=4)
        self._copy_button = tk.Button(
            footer,
            text="Скопировать сводку",
            command=self.copy_summary,
            takefocus=True,
            state="disabled",
            font=("Segoe UI", 10, "bold"),
            bg="#0969DA",
            activebackground="#0B7CE5",
            disabledforeground="#708396",
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=7,
        )
        self._copy_button.grid(row=1, column=2, padx=4)
        self._close_button = tk.Button(
            footer,
            text="Закрыть",
            command=self.close,
            takefocus=True,
            font=("Segoe UI", 10, "bold"),
            bg="#123149",
            activebackground="#17405F",
            fg="#FFFFFF",
            activeforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=7,
        )
        self._close_button.grid(row=1, column=3, padx=(4, 0))
        for action_button in (
            self._restart_button,
            self._copy_button,
            self._close_button,
        ):
            action_button.bind(
                "<Return>",
                lambda _event, target=action_button: self._invoke_button(target),
            )
        native_field: NativeRichEditField | None = None
        try:
            # Create the Win32 child only after every ordinary Tk widget.  Any
            # failure after this point must explicitly detach its subclass.
            native_field = NativeRichEditField(host)
            self.native_field = native_field
            self._refresh_view()
            self.window.update_idletasks()
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            self.window.after(140, self.native_field.focus)
            self.window.after(15, self._poll_native_navigation)
        except BaseException:
            self._cleanup_failed_construction(native_field)
            raise

    @property
    def active(self) -> bool:
        return not self._closed and self.native_field.hwnd > 0

    def route_expectation(self) -> tuple[str, SessionEditAction | None]:
        return self.session.route_expectation()

    def owns_target(self, target: FocusTarget | None) -> bool:
        if not self.active or target is None or not target.stable:
            return False
        if int(target.focused_hwnd) != int(self.native_field.hwnd):
            return False
        if target.process_id and int(target.process_id) != os.getpid():
            return False
        field_root = int(getattr(self.native_field, "root_hwnd", 0) or 0)
        if target.root_hwnd and field_root and int(target.root_hwnd) != field_root:
            return False
        return True

    @staticmethod
    def _invoke_button(button: tk.Button) -> str:
        button.invoke()
        return "break"

    def _focus_action_button(self, button: tk.Button) -> None:
        # Tk tracks focus internally, while the embedded RichEdit owns the
        # actual Win32 focus. Update both so the next Enter reaches the button.
        button.focus_set()
        self.native_field.transfer_focus(button)

    def _cleanup_failed_construction(
        self,
        native_field: NativeRichEditField | None,
    ) -> None:
        self._closed = True
        if native_field is not None:
            native_field.destroy()
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _poll_native_navigation(self) -> None:
        """Apply requests captured by the native child on Tk's event loop."""

        if not self.active:
            return
        try:
            request = self.native_field.take_navigation_request()
            if request == "tab":
                self._focus_action_button(self._restart_button)
            elif request == "shift_tab":
                self._focus_action_button(self._close_button)
            elif request == "escape":
                self.close()
                return
        except Exception:
            pass
        if self.active:
            self.window.after(15, self._poll_native_navigation)

    def _set_status(self, message: str, tone: str = "neutral") -> None:
        self._status_var.set(message)
        self._status_label.configure(
            fg={
                "success": "#38C75B",
                "warning": "#F59E0B",
                "error": "#F87171",
                "ready": "#38A7FF",
            }.get(tone, "#E5EDF5")
        )

    def _abort_active_cycle(self, *, restart: bool) -> bool:
        callback = self._on_abort_cycle
        if callback is None:
            return True
        # Make a callback that ultimately delegates to the app's generic
        # cancel path unable to recursively close this same window.
        was_closed = self._closed
        self._closed = True
        try:
            callback(bool(restart))
        except Exception:
            self._closed = was_closed
            self._set_status(
                "Не удалось безопасно отменить текущий цикл. Повторите действие.",
                "error",
            )
            return False
        finally:
            self._closed = was_closed
        return True

    def handle_event(self, event: QuickTestEvent) -> None:
        if not self.active:
            return
        if (
            event.kind in {"dictation", "session_edit"}
            and event.sequence > self.session.last_sequence
            and event.generation > self.session.start_generation
        ):
            # Latch cleanup for any actual lab operation, including a rejected
            # or uncertain one. A mere route rejection must preserve whatever
            # last text existed before the user opened the lab.
            self._session_started = True
        snapshot = self._snapshot_provider()
        update = self.session.handle_event(
            event,
            snapshot,
            field_hwnd=self.native_field.hwnd,
            field_text=self.native_field.text(),
        )
        if update.reset_field:
            try:
                self._on_reset_session()
            except Exception:
                self.session.blocked = True
                self._set_status(
                    "Не удалось очистить тестовую сессию. Закройте проверку.",
                    "error",
                )
                self._refresh_view()
                self.window.after(
                    80,
                    lambda: self._focus_action_button(self._restart_button),
                )
                return
            self.native_field.clear()
            self.session.reset(
                start_generation=snapshot.generation,
                last_sequence=event.sequence,
            )
        elif update.accepted:
            self._session_started = True
        self._set_status(
            update.message or "Ожидается текущая фраза.",
            "error"
            if update.blocked
            else "warning"
            if update.retry
            else "success"
            if update.accepted
            else "neutral",
        )
        self._refresh_view()
        if self.session.completed or self.session.blocked:
            self._copy_button.configure(state="normal")
        else:
            self._copy_button.configure(state="disabled")
        if self.session.blocked:
            self.window.after(
                80,
                lambda: self._focus_action_button(self._restart_button),
            )
        elif self.session.completed:
            self.window.after(
                80,
                lambda: self._focus_action_button(self._close_button),
            )
        else:
            self.window.after(80, self.native_field.focus)

    def stop_with_internal_error(self) -> None:
        """Fail closed after an unexpected app/UI integration error."""

        if not self.active:
            return
        step = self.session.current_step
        if step is not None and not any(
            result.step_id == step.step_id for result in self.session.results
        ):
            self.session.results.append(
                QuickTestResult(
                    step.step_id,
                    False,
                    "Внутренняя проверка шага остановлена",
                )
            )
        self.session.blocked = True
        self._set_status(
            "Внутренняя проверка остановлена. Нажмите «Начать заново».",
            "error",
        )
        self._refresh_view()
        self._copy_button.configure(state="normal")
        self.window.after(
            80,
            lambda: self._focus_action_button(self._restart_button),
        )

    def _refresh_view(self) -> None:
        if self.session.completed:
            total = len(self.session.steps)
            self._progress_var.set(f"Готово — {total} шага из {total}")
            self._phrase_var.set("Проверка завершена")
        elif self.session.blocked:
            self._progress_var.set("Проверка остановлена")
            self._phrase_var.set("Нажмите «Начать заново»")
        else:
            step = self.session.current_step
            assert step is not None
            self._progress_var.set(
                f"Шаг {self.session.step_index + 1} из {len(self.session.steps)} · {step.title}"
            )
            self._phrase_var.set(step.phrase)
        by_id = {result.step_id: result for result in self.session.results}
        for index, step in enumerate(self.session.steps):
            result = by_id.get(step.step_id)
            if result is None:
                value = "Сейчас" if index == self.session.step_index else "Ожидает"
                color = (
                    "#38A7FF"
                    if index == self.session.step_index
                    and not self.session.blocked
                    and not self.session.completed
                    else "#A9BAC9"
                )
            elif result.passed:
                value = "Пройдено"
                color = "#38C75B"
                if (
                    step.step_id == "dictation"
                    and self.session.final_text_match is False
                ):
                    value = "Пройдено · итог с отличием"
            else:
                value = "Ошибка"
                color = "#F87171"
            self._result_vars[index].set(value)
            self._result_labels[index].configure(fg=color)

    def restart(self) -> None:
        if self._closed:
            return
        if not self._abort_active_cycle(restart=True):
            return
        snapshot = self._snapshot_provider()
        if self._session_started:
            try:
                self._on_reset_session()
            except Exception:
                self._set_status(
                    "Не удалось очистить тестовую сессию. Повторите действие.",
                    "error",
                )
                return
        self.native_field.clear()
        self.session.reset(
            start_generation=snapshot.generation,
            last_sequence=self.session.last_sequence,
        )
        self._session_started = False
        self._copy_button.configure(state="disabled")
        self._set_status(
            "Проверка сброшена. Начните с первой фразы.",
            "ready",
        )
        self._refresh_view()
        self.window.after(80, self.native_field.focus)

    def copy_summary(self) -> None:
        if self._closed or not (self.session.completed or self.session.blocked):
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(self.session.summary())
        self.window.update_idletasks()
        self._set_status(
            "Сводка без продиктованного текста скопирована.",
            "success",
        )
        self._focus_action_button(self._close_button)

    def _close_event(self) -> str:
        self.close()
        return "break"

    def close(self, *, clear_session: bool = True) -> None:
        if self._closed:
            return
        if clear_session and not self._abort_active_cycle(restart=False):
            return
        self._closed = True
        if clear_session and self._session_started:
            try:
                self._on_reset_session()
            except Exception:
                pass
        self.session.clear()
        try:
            self.native_field.destroy()
        finally:
            try:
                self.window.destroy()
            finally:
                if self._on_close is not None:
                    self._on_close()


def open_quick_test_window(
    parent: tk.Misc,
    *,
    language: str,
    activation_label: str,
    activation_mode: str = "toggle",
    start_generation: int,
    snapshot_provider: Callable[[], QuickTestSessionSnapshot],
    on_reset_session: Callable[[], object],
    on_abort_cycle: Callable[[bool], object] | None = None,
    on_close: Callable[[], object] | None = None,
) -> QuickTestWindow:
    return QuickTestWindow(
        parent,
        language=language,
        activation_label=activation_label,
        activation_mode=activation_mode,
        start_generation=start_generation,
        snapshot_provider=snapshot_provider,
        on_reset_session=on_reset_session,
        on_abort_cycle=on_abort_cycle,
        on_close=on_close,
    )


__all__ = [
    "NativeRichEditField",
    "QuickTestEvent",
    "QuickTestResult",
    "QuickTestSession",
    "QuickTestSessionSnapshot",
    "QuickTestStep",
    "QuickTestUpdate",
    "QuickTestWindow",
    "open_quick_test_window",
    "quick_test_steps",
]
