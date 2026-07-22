from __future__ import annotations

"""Isolated native probe for the targeted RichEdit message backend.

The probe owns both of its invisible windows.  It never enumerates the desktop,
reads foreground state, changes focus, or addresses a user-owned handle.
"""

import os


def run_isolated_targeted_inserter_probe() -> tuple[int, str]:
    """Exercise real ``SendMessageTimeoutW`` delivery and timeout handling."""

    if os.name != "nt":
        return 80, "platform:not_windows"

    import ctypes
    import threading
    from ctypes import wintypes

    from .targeted_inserter import (
        ERROR_TIMEOUT,
        TargetedInsertionStatus,
        Win32RichEditMessageBackend,
    )

    ws_child = 0x40000000
    es_multiline = 0x0004
    pm_remove = 0x0001

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Keep the provider loaded until its control has been destroyed.
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
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    )
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
    user32.DispatchMessageW.restype = ctypes.c_ssize_t

    ready = threading.Event()
    stop = threading.Event()
    block_requested = threading.Event()
    blocked = threading.Event()
    state: dict[str, int | str] = {"parent": 0, "edit": 0}

    def window_thread() -> None:
        parent = 0
        edit = 0
        try:
            parent = int(
                user32.CreateWindowExW(
                    0,
                    "STATIC",
                    "VoiceType isolated targeted probe",
                    0,  # Invisible: no WS_VISIBLE and no ShowWindow call.
                    -32000,
                    -32000,
                    320,
                    120,
                    0,
                    0,
                    0,
                    None,
                )
                or 0
            )
            if not parent:
                state["error"] = "create:parent"
                return
            edit = int(
                user32.CreateWindowExW(
                    0,
                    "RICHEDIT50W",
                    "",
                    ws_child | es_multiline,
                    0,
                    0,
                    300,
                    100,
                    parent,
                    0,
                    0,
                    None,
                )
                or 0
            )
            if not edit:
                state["error"] = "create:edit"
                return
            state["parent"] = parent
            state["edit"] = edit
            ready.set()

            message = wintypes.MSG()
            while not stop.is_set():
                if block_requested.is_set():
                    block_requested.clear()
                    blocked.set()
                    # Deliberately pause only this probe-owned window thread.
                    stop.wait(0.25)
                    continue
                while user32.PeekMessageW(
                    ctypes.byref(message), 0, 0, 0, pm_remove
                ):
                    user32.TranslateMessage(ctypes.byref(message))
                    user32.DispatchMessageW(ctypes.byref(message))
                stop.wait(0.005)
        except Exception as exc:
            state["error"] = f"thread:{type(exc).__name__}"
        finally:
            ready.set()
            if edit:
                user32.DestroyWindow(edit)
            if parent:
                user32.DestroyWindow(parent)

    worker = threading.Thread(
        target=window_thread,
        name="VoiceTypeTargetedProbe",
        daemon=True,
    )
    worker.start()
    try:
        if not ready.wait(3.0):
            return 81, "create:timeout"
        if "error" in state:
            return 82, str(state["error"])
        edit = int(state["edit"])
        if edit <= 0:
            return 83, "create:missing_handle"

        payload = "Русский — English — Español — 😀"
        backend = Win32RichEditMessageBackend()
        delivered = backend.replace_selection(
            edit,
            payload,
            timeout_ms=500,
            before_call=lambda: True,
        )
        if delivered.status is not TargetedInsertionStatus.ACCEPTED:
            return 84, f"delivery:{delivered.reason_code}"
        if (
            delivered.reason_code != "message_accepted"
            or not delivered.postcheck_required
            or delivered.safe_to_retry
        ):
            return 92, "delivery:result_policy"

        length = int(user32.GetWindowTextLengthW(edit))
        buffer = ctypes.create_unicode_buffer(length + 1)
        if user32.GetWindowTextW(edit, buffer, len(buffer)) != length:
            return 85, "roundtrip:read"
        if buffer.value != payload:
            return 86, "roundtrip:mismatch"

        # A late timed-out message, if Windows eventually dispatches it,
        # remains confined to this temporary control.
        block_requested.set()
        if not blocked.wait(1.0):
            return 87, "timeout:block_not_entered"
        timed_out = backend.replace_selection(
            edit,
            "isolated-timeout-probe",
            timeout_ms=25,
            before_call=lambda: True,
        )
        if timed_out.status is not TargetedInsertionStatus.TIMEOUT:
            return 88, f"timeout:{timed_out.reason_code}"
        if (
            timed_out.reason_code != "message_timeout"
            or timed_out.native_error != ERROR_TIMEOUT
        ):
            return 89, "timeout:native_error"
        if not timed_out.postcheck_required or timed_out.safe_to_retry:
            return 90, "timeout:result_policy"
        return 0, "ok"
    except Exception as exc:
        return 91, f"exception:{type(exc).__name__}"
    finally:
        stop.set()
        worker.join(2.0)


__all__ = ["run_isolated_targeted_inserter_probe"]
