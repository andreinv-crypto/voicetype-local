from __future__ import annotations

"""Target-bound text insertion for the Windows 11 Notepad RichEdit.

This module deliberately supports one exact native window class.  It does not
discover windows, move focus, inspect text, use the clipboard, or perform a
postcondition check.  The caller must first identify the target and must verify
the resulting document through its own UI Automation session.

``EM_REPLACESEL`` is synchronous but has no meaningful message result.  An
``ACCEPTED`` result therefore means only that Windows reported that the target
processed the message; it is not proof that the requested text is present.
Both ``ACCEPTED`` and ``TIMEOUT`` require a caller-owned postcheck and are never
safe reasons to repeat the insertion blindly.
"""

import ctypes
import enum
import math
import os
import unicodedata
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol


__all__ = [
    "RICHEDIT_D2DPT_CLASS",
    "RichEditMessageBackend",
    "TargetedInsertionResult",
    "TargetedInsertionStatus",
    "TargetedRichEditInserter",
    "Win32RichEditMessageBackend",
]


RICHEDIT_D2DPT_CLASS = "RichEditD2DPT"

EM_REPLACESEL = 0x00C2
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002
SMTO_ERRORONEXIT = 0x0020
ERROR_TIMEOUT = 1460

_SEND_TIMEOUT_FLAGS = SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT
_DEFAULT_TIMEOUT_MS = 500
_MAX_TIMEOUT_MS = 5_000


class TargetedInsertionStatus(enum.StrEnum):
    """Native dispatch state, intentionally separate from UIA verification."""

    ACCEPTED = "accepted"
    TIMEOUT = "timeout"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class TargetedInsertionResult:
    """Metadata-only result of one targeted insertion attempt.

    ``postcheck_required`` tells the router that native dispatch alone did not
    prove the document contents.  ``safe_to_retry`` is false whenever the
    native call may have reached the target.
    """

    status: TargetedInsertionStatus
    reason_code: str
    postcheck_required: bool
    safe_to_retry: bool
    native_error: int = 0

    @classmethod
    def accepted(cls) -> TargetedInsertionResult:
        return cls(
            TargetedInsertionStatus.ACCEPTED,
            "message_accepted",
            postcheck_required=True,
            safe_to_retry=False,
        )

    @classmethod
    def timeout(cls, native_error: int = ERROR_TIMEOUT) -> TargetedInsertionResult:
        return cls(
            TargetedInsertionStatus.TIMEOUT,
            "message_timeout",
            postcheck_required=True,
            safe_to_retry=False,
            native_error=max(0, int(native_error)),
        )

    @classmethod
    def rejected(
        cls,
        reason_code: str,
        *,
        postcheck_required: bool = False,
        safe_to_retry: bool = True,
        native_error: int = 0,
    ) -> TargetedInsertionResult:
        return cls(
            TargetedInsertionStatus.REJECTED,
            reason_code,
            postcheck_required=postcheck_required,
            safe_to_retry=safe_to_retry,
            native_error=max(0, int(native_error)),
        )


class RichEditMessageBackend(Protocol):
    """Injectable backend; tests never need to address a live desktop window."""

    def replace_selection(
        self,
        hwnd: int,
        text: str,
        *,
        timeout_ms: int,
        before_call: Callable[[], bool],
    ) -> TargetedInsertionResult: ...


def _validated_text(text: str) -> str:
    """Apply the same conservative Unicode policy as ordinary dictation."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(
        character not in {"\n", "\t"}
        and not character.isprintable()
        and unicodedata.category(character) != "Cf"
        for character in normalized
    ):
        raise ValueError("text contains an unsupported control or surrogate")
    return normalized


class Win32RichEditMessageBackend:
    """Bounded ``SendMessageTimeoutW`` adapter for ``EM_REPLACESEL``.

    A callable may be injected for ABI-level unit tests.  Production uses the
    Unicode User32 entry point.  The UTF-16 buffer is prepared before the guard
    so target re-authorization remains immediately adjacent to the native call.
    """

    def __init__(self, send_message_timeout: Callable[..., int] | None = None) -> None:
        if send_message_timeout is not None:
            self._send_message_timeout = send_message_timeout
            return
        if os.name != "nt":
            raise OSError("targeted RichEdit insertion is available only on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        native = user32.SendMessageTimeoutW
        native.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(wintypes.WPARAM),
        )
        native.restype = wintypes.LPARAM
        self._send_message_timeout = native

    def replace_selection(
        self,
        hwnd: int,
        text: str,
        *,
        timeout_ms: int,
        before_call: Callable[[], bool],
    ) -> TargetedInsertionResult:
        buffer = ctypes.create_unicode_buffer(text)
        result = wintypes.WPARAM(0)

        try:
            allowed = bool(before_call())
        except Exception:
            return TargetedInsertionResult.rejected("guard_error")
        if not allowed:
            return TargetedInsertionResult.rejected("guard_rejected")

        # The guard is deliberately the final user-provided operation before
        # the native call.  Resetting last-error does not inspect or mutate the
        # target; it only lets us distinguish an explicit Windows timeout.
        ctypes.set_last_error(0)
        try:
            delivered = int(
                self._send_message_timeout(
                    int(hwnd),
                    EM_REPLACESEL,
                    1,  # TRUE: keep the replacement in the RichEdit undo stack.
                    ctypes.addressof(buffer),
                    _SEND_TIMEOUT_FLAGS,
                    int(timeout_ms),
                    ctypes.byref(result),
                )
                or 0
            )
        except Exception:
            # A foreign callable can fail after it has entered native code.
            # Conservatively require a postcheck and forbid an automatic retry.
            return TargetedInsertionResult.rejected(
                "backend_error",
                postcheck_required=True,
                safe_to_retry=False,
            )

        if delivered:
            return TargetedInsertionResult.accepted()

        native_error = int(ctypes.get_last_error() or 0)
        if native_error == ERROR_TIMEOUT:
            return TargetedInsertionResult.timeout(native_error)
        return TargetedInsertionResult.rejected(
            "message_rejected",
            postcheck_required=True,
            safe_to_retry=False,
            native_error=native_error,
        )


class TargetedRichEditInserter:
    """Policy gate for the one exact RichEdit class used by modern Notepad."""

    def __init__(
        self,
        backend: RichEditMessageBackend | None = None,
        *,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    ) -> None:
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int | float)
            or not math.isfinite(float(timeout_ms))
            or int(timeout_ms) != timeout_ms
            or not 1 <= int(timeout_ms) <= _MAX_TIMEOUT_MS
        ):
            raise ValueError(
                f"timeout_ms must be an integer from 1 to {_MAX_TIMEOUT_MS}"
            )
        self.timeout_ms = int(timeout_ms)
        if backend is not None:
            self._backend: RichEditMessageBackend | None = backend
        else:
            try:
                self._backend = Win32RichEditMessageBackend()
            except OSError:
                self._backend = None

    @staticmethod
    def supports_class(class_name: str) -> bool:
        """Return true only for the canonical class name, never a prefix."""

        return isinstance(class_name, str) and class_name == RICHEDIT_D2DPT_CLASS

    @staticmethod
    def normalize_text(text: str) -> str:
        """Expose the exact payload normalization for caller-owned postchecks."""

        return _validated_text(text)

    def insert(
        self,
        text: str,
        *,
        hwnd: int,
        class_name: str,
        before_call: Callable[[], bool],
    ) -> TargetedInsertionResult:
        """Replace the active selection, or insert at the caret.

        The caller owns target discovery and the UIA postcondition.  No native
        call is made for an unsupported class, invalid handle, invalid text, or
        missing backend.
        """

        if not self.supports_class(class_name):
            return TargetedInsertionResult.rejected("class_not_allowlisted")
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            return TargetedInsertionResult.rejected("invalid_window_handle")
        if not callable(before_call):
            return TargetedInsertionResult.rejected("guard_missing")
        try:
            normalized = self.normalize_text(text)
        except (TypeError, ValueError, UnicodeError):
            return TargetedInsertionResult.rejected("invalid_text")
        if self._backend is None:
            return TargetedInsertionResult.rejected("backend_unavailable")
        try:
            return self._backend.replace_selection(
                hwnd,
                normalized,
                timeout_ms=self.timeout_ms,
                before_call=before_call,
            )
        except Exception:
            # A backend is allowed to be injected, but not to leak exceptions
            # into the application state machine after an attempted dispatch.
            return TargetedInsertionResult.rejected(
                "backend_error",
                postcheck_required=True,
                safe_to_retry=False,
            )
