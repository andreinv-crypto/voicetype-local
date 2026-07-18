from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass


ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF

_MUTEX_NAME = "Local\\VoiceTypeLocal-4A5B2A94"
_SETTINGS_EVENT_NAME = "Local\\VoiceTypeLocal-Settings-4A5B2A94"


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    )
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    )
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
    kernel32.SetEvent.restype = ctypes.c_bool
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_bool
    return kernel32


@dataclass(slots=True)
class InstanceActivation:
    """Per-session single-instance ownership and settings activation.

    The auto-reset event carries no text, paths, or user data. A secondary
    process can request only the harmless action "show settings".
    """

    is_primary: bool
    _mutex_handle: int | None = None
    _settings_event_handle: int | None = None

    @classmethod
    def acquire(cls) -> InstanceActivation:
        if os.name != "nt":
            return cls(is_primary=True)

        kernel32 = _kernel32()
        # Create/open the event before the mutex. This prevents a very short
        # startup race in which a secondary launch sees the mutex before the
        # primary process is ready to receive its activation request.
        event_handle = kernel32.CreateEventW(
            None,
            False,  # auto-reset: one notification wakes one listener
            False,
            _SETTINGS_EVENT_NAME,
        )
        event_value = int(event_handle) if event_handle else None

        ctypes.set_last_error(0)
        mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        mutex_value = int(mutex_handle) if mutex_handle else None
        already_running = (
            bool(mutex_value)
            and ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        )
        if already_running:
            kernel32.CloseHandle(ctypes.c_void_p(mutex_value))
            return cls(
                is_primary=False,
                _settings_event_handle=event_value,
            )

        # Match the previous fail-open behavior if Windows cannot create the
        # mutex. VoiceType remains usable, but activation IPC may be absent.
        return cls(
            is_primary=True,
            _mutex_handle=mutex_value,
            _settings_event_handle=event_value,
        )

    def notify_settings(self) -> bool:
        """Ask the primary instance to show Settings, without desktop input."""

        handle = self._settings_event_handle
        if os.name != "nt" or handle is None:
            return False
        return bool(_kernel32().SetEvent(ctypes.c_void_p(handle)))

    def wait_for_settings(self, timeout_ms: int) -> bool:
        """Wait briefly for an activation request in a background thread."""

        timeout_ms = max(0, int(timeout_ms))
        handle = self._settings_event_handle
        if os.name != "nt" or handle is None:
            time.sleep(timeout_ms / 1000.0)
            return False
        result = int(
            _kernel32().WaitForSingleObject(
                ctypes.c_void_p(handle), ctypes.c_uint32(timeout_ms)
            )
        )
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        raise OSError(f"Unexpected activation wait result: {result}")

    def close(self) -> None:
        if os.name != "nt":
            return
        kernel32 = _kernel32()
        event_handle = self._settings_event_handle
        mutex_handle = self._mutex_handle
        self._settings_event_handle = None
        self._mutex_handle = None
        if event_handle is not None:
            kernel32.CloseHandle(ctypes.c_void_p(event_handle))
        if mutex_handle is not None:
            kernel32.CloseHandle(ctypes.c_void_p(mutex_handle))
