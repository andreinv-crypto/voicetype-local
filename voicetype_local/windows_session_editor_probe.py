from __future__ import annotations

"""Isolated real-UIA probe used by source and packaged self-tests."""

import os


def run_isolated_session_editor_probe() -> tuple[int, str]:
    """Exercise real TextRange selection without touching user windows."""

    if os.name != "nt":
        return 60, "platform:not_windows"

    import ctypes
    from contextlib import nullcontext
    from ctypes import wintypes

    import uiautomation as automation

    from .focus import FocusMatch, FocusTarget
    from .session_editing import (
        SessionEditAction,
        SessionEditPlan,
        SessionEditRequest,
        plan_session_edit,
    )
    from .session_editor import SessionTextEditor
    from .voice_commands import CommandLanguage

    ws_overlapped = 0x00000000
    ws_child = 0x40000000
    ws_visible = 0x10000000
    es_multiline = 0x0004
    em_setsel = 0x00B1
    em_replacesel = 0x00C2

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Keep the RichEdit provider loaded until both test windows are destroyed.
    _msftedit = ctypes.WinDLL("msftedit", use_last_error=True)
    user32.CreateWindowExW.argtypes = (
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
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    user32.SetWindowTextW.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = (wintypes.HWND,)
    user32.UpdateWindow.restype = wintypes.BOOL

    class SameFocus:
        @staticmethod
        def compare_current(_expected: FocusTarget) -> FocusMatch:
            return FocusMatch.SAME

    class AutomationProxy:
        TextPatternRangeEndpoint = automation.TextPatternRangeEndpoint
        TextUnit = automation.TextUnit

        def __init__(self, control: object) -> None:
            self.control = control

        @staticmethod
        def UIAutomationInitializerInThread():
            return nullcontext()

        def GetFocusedControl(self):
            return self.control

    class NativeSelectionInserter:
        def __init__(self, edit_hwnd: int) -> None:
            self.edit_hwnd = edit_hwnd

        def replace_selection(self, text: str) -> None:
            value = ctypes.c_wchar_p(text)
            user32.SendMessageW(
                self.edit_hwnd,
                em_replacesel,
                1,
                ctypes.cast(value, ctypes.c_void_p).value or 0,
            )

    def window_text(hwnd: int) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    parent = user32.CreateWindowExW(
        0,
        "STATIC",
        "VoiceType isolated UIA probe",
        ws_overlapped,
        -32000,
        -32000,
        640,
        240,
        0,
        0,
        0,
        None,
    )
    if not parent:
        return 61, "create:parent"
    edit = 0
    try:
        initial = "Existing sentence. Voice input sentence."
        edit = user32.CreateWindowExW(
            0,
            "RICHEDIT50W",
            initial,
            ws_child | ws_visible | es_multiline,
            0,
            0,
            600,
            180,
            parent,
            0,
            0,
            None,
        )
        if not edit:
            return 62, "create:edit"
        user32.ShowWindow(parent, 4)  # SW_SHOWNOACTIVATE, outside the desktop.
        user32.UpdateWindow(parent)

        with automation.UIAutomationInitializerInThread():
            control = automation.ControlFromHandle(edit)
            if control is None:
                return 63, "uia:control"
            runtime_id = tuple(int(item) for item in control.GetRuntimeId())
            target = FocusTarget(
                foreground_hwnd=parent,
                root_hwnd=parent,
                focused_hwnd=edit,
                thread_id=1,
                process_id=os.getpid(),
                focused_class_name="RichEdit50W",
                uia_runtime_id=runtime_id,
                stable=True,
            )
            editor = SessionTextEditor(
                NativeSelectionInserter(edit),
                SameFocus(),  # type: ignore[arg-type]
                AutomationProxy(control),
            )
            cases = (
                (
                    initial,
                    " Voice input sentence.",
                    SessionEditRequest(
                        SessionEditAction.DELETE_LAST_SENTENCE,
                        CommandLanguage.EN,
                    ),
                    "Existing sentence.",
                ),
                (
                    "Existing: alpha beta gamma",
                    " alpha beta gamma",
                    SessionEditRequest(
                        SessionEditAction.REPLACE_UNIQUE,
                        CommandLanguage.EN,
                        old_text="beta",
                        new_text="BETA",
                    ),
                    "Existing: alpha BETA gamma",
                ),
            )
            for initial_text, tracked, request, expected in cases:
                if not user32.SetWindowTextW(edit, initial_text):
                    return 64, "write:test_control"
                user32.SendMessageW(
                    edit,
                    em_setsel,
                    len(initial_text),
                    len(initial_text),
                )
                plan = plan_session_edit(tracked, request)
                result = editor.apply(plan, target)
                if not result.succeeded:
                    return 65, f"edit:{request.action.value}:{result.reason_code}"
                if window_text(edit) != expected:
                    return 66, f"result:{request.action.value}"

            # A complete deletion is useful only if the exact same empty field
            # can immediately restore it.  Exercise the real RichEdit UIA
            # provider, not the in-memory fakes used by unit tests.
            full_text = "Voice input"
            if not user32.SetWindowTextW(edit, full_text):
                return 68, "write:full_delete"
            user32.SendMessageW(edit, em_setsel, len(full_text), len(full_text))
            delete_plan = plan_session_edit(
                full_text,
                SessionEditRequest(
                    SessionEditAction.DELETE_LAST_DICTATION,
                    CommandLanguage.EN,
                ),
            )
            delete_result = editor.apply(delete_plan, target)
            if not delete_result.succeeded:
                return 69, f"edit:full_delete:{delete_result.reason_code}"
            if not delete_result.empty_document_verified:
                return 70, "proof:empty_document"
            if window_text(edit) != "":
                return 71, "result:full_delete"

            restore_plan = SessionEditPlan(
                action=SessionEditAction.RESTORE_LAST_EDIT,
                original_text="",
                result_text=full_text,
                selected_text="",
                replacement_text=full_text,
                start=0,
                end=0,
            )
            restore_result = editor.apply(restore_plan, target)
            if not restore_result.succeeded:
                return 72, f"edit:restore_empty:{restore_result.reason_code}"
            if window_text(edit) != full_text:
                return 73, "result:restore_empty"
        return 0, "ok"
    except Exception as exc:
        return 67, f"exception:{type(exc).__name__}"
    finally:
        if edit:
            user32.DestroyWindow(edit)
        user32.DestroyWindow(parent)


__all__ = ["run_isolated_session_editor_probe"]
