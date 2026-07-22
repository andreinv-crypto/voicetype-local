from __future__ import annotations

"""Read-only identification of the Windows field that owns keyboard focus.

The module never reads control text, takes screenshots, moves the pointer, or
changes focus.  Win32 window identity is always available on Windows.  A UI
Automation runtime id is used only when an optional provider is available and
responds quickly; all failures fall back to conservative Win32 comparison.
"""

import ctypes
import enum
import os
import queue
import threading
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast


__all__ = [
    "FocusBackend",
    "FocusComparison",
    "FocusInspector",
    "FocusMatch",
    "FocusTarget",
    "FocusUnknownReason",
    "RuntimeIdResult",
    "RuntimeIdProvider",
    "RuntimeIdStatus",
    "Win32FocusBackend",
    "Win32FocusState",
    "compare_focus_targets",
    "compare_focus_targets_detailed",
    "focus_app_id",
    "make_optional_uia_runtime_id_provider",
]


class FocusMatch(enum.StrEnum):
    """Result of comparing two read-only focus snapshots."""

    SAME = "same"
    CHANGED = "changed"
    UNKNOWN = "unknown"


class RuntimeIdStatus(enum.StrEnum):
    """Outcome of a read-only UI Automation RuntimeId probe."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    MISSING = "missing"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"
    ERROR = "error"
    SATURATED = "saturated"
    UNAVAILABLE = "unavailable"
    NOT_REQUESTED = "not_requested"
    DISCARDED_UNSTABLE = "discarded_unstable"


class FocusUnknownReason(enum.StrEnum):
    """Safe, non-content diagnostic for why focus equality is unknown."""

    EXPECTED_TARGET_MISSING = "expected_target_missing"
    CURRENT_TARGET_MISSING = "current_target_missing"
    EXPECTED_CAPTURE_UNSTABLE = "expected_capture_unstable"
    CURRENT_CAPTURE_UNSTABLE = "current_capture_unstable"
    INCOMPLETE_WIN32_IDENTITY = "incomplete_win32_identity"
    EXPECTED_RUNTIME_ID_MISSING = "expected_runtime_id_missing"
    CURRENT_RUNTIME_ID_MISSING = "current_runtime_id_missing"
    CURRENT_RUNTIME_ID_INVALID = "current_runtime_id_invalid"
    CURRENT_RUNTIME_ID_TIMED_OUT = "current_runtime_id_timed_out"
    CURRENT_RUNTIME_ID_ERROR = "current_runtime_id_error"
    CURRENT_RUNTIME_ID_SATURATED = "current_runtime_id_saturated"
    CURRENT_RUNTIME_ID_UNAVAILABLE = "current_runtime_id_unavailable"
    BOTH_RUNTIME_IDS_MISSING = "both_runtime_ids_missing"
    VIRTUAL_FIELD_WITHOUT_RUNTIME_ID = "virtual_field_without_runtime_id"


@dataclass(frozen=True, slots=True)
class RuntimeIdResult:
    """RuntimeId value plus a privacy-safe probe outcome."""

    value: tuple[int, ...] | None
    status: RuntimeIdStatus


@dataclass(frozen=True, slots=True)
class FocusComparison:
    """Detailed comparison while preserving the small ``FocusMatch`` API."""

    match: FocusMatch
    unknown_reason: FocusUnknownReason | None = None
    expected_runtime_status: RuntimeIdStatus = RuntimeIdStatus.UNKNOWN
    current_runtime_status: RuntimeIdStatus = RuntimeIdStatus.UNKNOWN
    attempts: int = 1
    encountered_unknown_reasons: tuple[FocusUnknownReason, ...] = ()


@dataclass(frozen=True, slots=True)
class Win32FocusState:
    """Raw Win32 focus identity returned by a :class:`FocusBackend`."""

    foreground_hwnd: int = 0
    root_hwnd: int = 0
    focused_hwnd: int = 0
    thread_id: int = 0
    process_id: int = 0
    focused_class_name: str = ""
    valid: bool = False

    @property
    def capture_key(self) -> tuple[int, int, int, int, int, str]:
        """Fields that must remain unchanged during a stable capture."""

        return (
            self.foreground_hwnd,
            self.root_hwnd,
            self.focused_hwnd,
            self.thread_id,
            self.process_id,
            self.focused_class_name.casefold(),
        )


@dataclass(frozen=True, slots=True)
class FocusTarget:
    """Stable identity of the field that was focused at one point in time.

    ``uia_runtime_id`` is opaque and is used only for equality comparison.
    ``stable`` is false when focus changed while the snapshot was being taken.
    Consumers should auto-insert only when comparison returns
    :attr:`FocusMatch.SAME`; both ``changed`` and ``unknown`` are safe reasons
    to keep the transcript pending.
    """

    foreground_hwnd: int = 0
    root_hwnd: int = 0
    focused_hwnd: int = 0
    thread_id: int = 0
    process_id: int = 0
    focused_class_name: str = ""
    uia_runtime_id: tuple[int, ...] | None = None
    stable: bool = False
    # Diagnostic metadata is deliberately excluded from identity/equality.
    # Hand-created FocusTarget instances from older callers therefore remain
    # compatible, while captured targets can explain a transient UNKNOWN.
    uia_runtime_id_status: RuntimeIdStatus = field(
        default=RuntimeIdStatus.UNKNOWN,
        compare=False,
    )

    @property
    def has_complete_win32_identity(self) -> bool:
        return all(
            (
                self.foreground_hwnd,
                self.root_hwnd,
                self.focused_hwnd,
                self.thread_id,
                self.process_id,
            )
        )

    def compare(self, current: FocusTarget | None) -> FocusMatch:
        return compare_focus_targets(self, current)

    @property
    def effective_runtime_id_status(self) -> RuntimeIdStatus:
        if self.uia_runtime_id is not None:
            return RuntimeIdStatus.AVAILABLE
        if self.uia_runtime_id_status is RuntimeIdStatus.AVAILABLE:
            return RuntimeIdStatus.INVALID
        return self.uia_runtime_id_status


class FocusBackend(Protocol):
    """Injectable backend used by :class:`FocusInspector`."""

    def snapshot(self) -> Win32FocusState: ...


class RuntimeIdProvider(Protocol):
    """Optional read-only UI Automation provider."""

    def focused_runtime_id(self) -> Iterable[int] | None: ...


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", _RECT),
    ]


class Win32FocusBackend:
    """Read focus handles with ``GetGUIThreadInfo`` without activating them."""

    _GA_ROOT = 2

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 focus inspection is available only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        self._user32.GetForegroundWindow.argtypes = ()
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        )
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self._user32.GetAncestor.restype = wintypes.HWND
        self._user32.GetClassNameW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.IsWindow.argtypes = (wintypes.HWND,)
        self._user32.IsWindow.restype = wintypes.BOOL

    def _root_window(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        return int(self._user32.GetAncestor(wintypes.HWND(hwnd), self._GA_ROOT) or hwnd)

    def _class_name(self, hwnd: int) -> str:
        if not hwnd:
            return ""
        buffer = ctypes.create_unicode_buffer(256)
        length = int(
            self._user32.GetClassNameW(wintypes.HWND(hwnd), buffer, len(buffer))
        )
        return buffer.value[:length] if length > 0 else ""

    def snapshot(self) -> Win32FocusState:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if not foreground or not self._user32.IsWindow(wintypes.HWND(foreground)):
            return Win32FocusState()

        process_id = wintypes.DWORD(0)
        thread_id = int(
            self._user32.GetWindowThreadProcessId(
                wintypes.HWND(foreground), ctypes.byref(process_id)
            )
            or 0
        )
        root = self._root_window(foreground)
        if not thread_id or not process_id.value or not root:
            return Win32FocusState(
                foreground_hwnd=foreground,
                root_hwnd=root,
                thread_id=thread_id,
                process_id=int(process_id.value),
            )

        gui = _GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(gui)):
            return Win32FocusState(
                foreground_hwnd=foreground,
                root_hwnd=root,
                thread_id=thread_id,
                process_id=int(process_id.value),
                valid=True,
            )

        focused = int(gui.hwndFocus or 0)
        if focused and not self._user32.IsWindow(wintypes.HWND(focused)):
            focused = 0

        # A focus handle from a different root means activation changed while
        # GetGUIThreadInfo was running. Mark it unstable instead of guessing.
        focus_root = self._root_window(focused) if focused else root
        valid = not focused or focus_root == root
        return Win32FocusState(
            foreground_hwnd=foreground,
            root_hwnd=root,
            focused_hwnd=focused,
            thread_id=thread_id,
            process_id=int(process_id.value),
            focused_class_name=self._class_name(focused),
            valid=valid,
        )


def _normalize_runtime_id(value: Iterable[int] | None) -> tuple[int, ...] | None:
    if value is None:
        return None
    try:
        normalized = tuple(int(item) for item in value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized or None


def _runtime_id_result(value: Iterable[int] | None) -> RuntimeIdResult:
    if value is None:
        return RuntimeIdResult(None, RuntimeIdStatus.MISSING)
    normalized = _normalize_runtime_id(value)
    if normalized is None:
        return RuntimeIdResult(None, RuntimeIdStatus.INVALID)
    return RuntimeIdResult(normalized, RuntimeIdStatus.AVAILABLE)


class _ComtypesRuntimeIdProvider:
    """Small optional adapter; imported only when comtypes already exists."""

    def __init__(self, comtypes_module: object, client_module: object) -> None:
        self._comtypes = comtypes_module
        self._client = client_module
        get_module = getattr(client_module, "GetModule")
        get_module("UIAutomationCore.dll")
        from comtypes.gen.UIAutomationClient import (  # type: ignore[import-not-found]
            CUIAutomation,
            IUIAutomation,
        )

        self._automation_class = CUIAutomation
        self._automation_interface = IUIAutomation

    def focused_runtime_id(self) -> Iterable[int] | None:
        co_initialize = getattr(self._comtypes, "CoInitialize")
        co_uninitialize = getattr(self._comtypes, "CoUninitialize")
        create_object = getattr(self._client, "CreateObject")
        co_initialize()
        try:
            automation = create_object(
                self._automation_class, interface=self._automation_interface
            )
            element = automation.GetFocusedElement()
            if element is None:
                return None
            # Materialize the SAFEARRAY while COM is initialized on this
            # worker thread; never pass a live automation object across it.
            return _normalize_runtime_id(element.GetRuntimeId())
        finally:
            co_uninitialize()


class _TimedRuntimeIdProvider:
    """Prevent a slow third-party UIA provider from blocking the app thread."""

    _FAILED = object()

    def __init__(
        self,
        provider: RuntimeIdProvider,
        timeout_seconds: float,
        *,
        max_concurrent_workers: int = 2,
    ) -> None:
        self._provider = provider
        self._timeout_seconds = max(0.0, float(timeout_seconds))
        self._guard = threading.Lock()
        # One abandoned provider call must not permanently poison future
        # comparisons. A second worker may probe independently, but the set is
        # bounded so a broken provider cannot create unlimited daemon threads.
        self._max_concurrent_workers = max(2, int(max_concurrent_workers))
        self._workers: set[threading.Thread] = set()

    def focused_runtime_id_result(self) -> RuntimeIdResult:
        result: queue.Queue[object] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                value: object = self._provider.focused_runtime_id()
            except Exception:
                value = self._FAILED
            try:
                result.put_nowait(value)
            finally:
                with self._guard:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=run, name="uia-focus-id", daemon=True)
        with self._guard:
            self._workers = {active for active in self._workers if active.is_alive()}
            if len(self._workers) >= self._max_concurrent_workers:
                return RuntimeIdResult(None, RuntimeIdStatus.SATURATED)
            self._workers.add(worker)
            worker.start()
        worker.join(self._timeout_seconds)
        if worker.is_alive():
            return RuntimeIdResult(None, RuntimeIdStatus.TIMED_OUT)
        try:
            value = result.get_nowait()
        except queue.Empty:
            return RuntimeIdResult(None, RuntimeIdStatus.ERROR)
        if value is self._FAILED:
            return RuntimeIdResult(None, RuntimeIdStatus.ERROR)
        return _runtime_id_result(cast(Iterable[int] | None, value))

    def focused_runtime_id(self) -> Iterable[int] | None:
        """Compatibility adapter for the original provider protocol."""

        return self.focused_runtime_id_result().value


def make_optional_uia_runtime_id_provider(
    timeout_seconds: float = 0.15,
) -> RuntimeIdProvider | None:
    """Return a bounded optional UIA provider, or ``None`` without comtypes.

    ``comtypes`` is deliberately not a project dependency.  Packaged builds
    without it use the Win32-only conservative fallback.
    """

    try:
        import comtypes  # type: ignore[import-not-found]
        import comtypes.client  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None
    try:
        provider = _ComtypesRuntimeIdProvider(comtypes, comtypes.client)
    except Exception:
        return None
    return _TimedRuntimeIdProvider(provider, timeout_seconds)


_AUTO_UIA = object()

_TRANSIENT_RUNTIME_ID_STATUSES = frozenset(
    {
        RuntimeIdStatus.MISSING,
        RuntimeIdStatus.INVALID,
        RuntimeIdStatus.TIMED_OUT,
        RuntimeIdStatus.ERROR,
        RuntimeIdStatus.SATURATED,
    }
)

_CURRENT_RUNTIME_UNKNOWN_REASONS = frozenset(
    {
        FocusUnknownReason.CURRENT_RUNTIME_ID_MISSING,
        FocusUnknownReason.CURRENT_RUNTIME_ID_INVALID,
        FocusUnknownReason.CURRENT_RUNTIME_ID_TIMED_OUT,
        FocusUnknownReason.CURRENT_RUNTIME_ID_ERROR,
        FocusUnknownReason.CURRENT_RUNTIME_ID_SATURATED,
    }
)


class FocusInspector:
    """Capture stable focus targets through an injectable Win32 backend."""

    def __init__(
        self,
        backend: FocusBackend | None = None,
        runtime_id_provider: RuntimeIdProvider | None | object = _AUTO_UIA,
        *,
        max_attempts: int = 2,
        runtime_id_attempts: int = 2,
        comparison_attempts: int = 2,
    ) -> None:
        self.backend = backend if backend is not None else Win32FocusBackend()
        if runtime_id_provider is _AUTO_UIA:
            self.runtime_id_provider = make_optional_uia_runtime_id_provider()
        else:
            self.runtime_id_provider = cast(
                RuntimeIdProvider | None, runtime_id_provider
            )
        self.max_attempts = max(1, int(max_attempts))
        self.runtime_id_attempts = max(1, int(runtime_id_attempts))
        self.comparison_attempts = max(1, int(comparison_attempts))
        self.last_comparison: FocusComparison | None = None

    def _snapshot(self) -> Win32FocusState:
        try:
            return self.backend.snapshot()
        except Exception:
            return Win32FocusState()

    def _runtime_id(self) -> RuntimeIdResult:
        if self.runtime_id_provider is None:
            return RuntimeIdResult(None, RuntimeIdStatus.UNAVAILABLE)
        try:
            detailed_reader = getattr(
                self.runtime_id_provider,
                "focused_runtime_id_result",
                None,
            )
            if callable(detailed_reader):
                result = detailed_reader()
                if isinstance(result, RuntimeIdResult):
                    if result.value is None:
                        return result
                    normalized = _normalize_runtime_id(result.value)
                    if normalized is None:
                        return RuntimeIdResult(None, RuntimeIdStatus.INVALID)
                    return RuntimeIdResult(normalized, RuntimeIdStatus.AVAILABLE)
            return _runtime_id_result(
                self.runtime_id_provider.focused_runtime_id()
            )
        except Exception:
            return RuntimeIdResult(None, RuntimeIdStatus.ERROR)

    @staticmethod
    def _target(
        state: Win32FocusState,
        runtime_id: RuntimeIdResult,
        *,
        stable: bool,
    ) -> FocusTarget:
        return FocusTarget(
            foreground_hwnd=state.foreground_hwnd,
            root_hwnd=state.root_hwnd,
            focused_hwnd=state.focused_hwnd,
            thread_id=state.thread_id,
            process_id=state.process_id,
            focused_class_name=state.focused_class_name,
            uia_runtime_id=runtime_id.value,
            uia_runtime_id_status=runtime_id.status,
            stable=stable,
        )

    def _capture(self, *, retry_runtime_id: bool) -> FocusTarget:
        """Capture focus without reading content or changing input state."""

        last = Win32FocusState()
        last_runtime = RuntimeIdResult(None, RuntimeIdStatus.NOT_REQUESTED)
        focus_retries_left = self.max_attempts - 1
        runtime_retries_left = (
            self.runtime_id_attempts - 1 if retry_runtime_id else 0
        )
        while True:
            before = self._snapshot()
            runtime_id = (
                self._runtime_id()
                if before.focused_hwnd
                else RuntimeIdResult(None, RuntimeIdStatus.NOT_REQUESTED)
            )
            after = self._snapshot()
            last = after
            last_runtime = runtime_id
            if (
                before.valid
                and after.valid
                and before.capture_key == after.capture_key
            ):
                target = self._target(after, runtime_id, stable=True)
                if (
                    runtime_retries_left > 0
                    and runtime_id.status in _TRANSIENT_RUNTIME_ID_STATUSES
                    and not _has_precise_native_hwnd(target)
                ):
                    runtime_retries_left -= 1
                    continue
                return target
            if focus_retries_left > 0:
                focus_retries_left -= 1
                continue
            break
        # A UIA id captured between two different Win32 states is not safe to
        # associate with either state, so deliberately discard it.
        discarded = RuntimeIdResult(None, RuntimeIdStatus.DISCARDED_UNSTABLE)
        if last_runtime.value is None:
            discarded = last_runtime
        return self._target(last, discarded, stable=False)

    def capture(self) -> FocusTarget:
        return self._capture(retry_runtime_id=True)

    def compare_current_detailed(
        self, expected: FocusTarget | None
    ) -> FocusComparison:
        """Compare current focus with a bounded recapture on transient UIA gaps."""

        encountered: list[FocusUnknownReason] = []
        final: FocusComparison | None = None
        attempts = 0
        for attempts in range(1, self.comparison_attempts + 1):
            current = self._capture(retry_runtime_id=False)
            final = compare_focus_targets_detailed(expected, current)
            if (
                final.unknown_reason is not None
                and final.unknown_reason not in encountered
            ):
                encountered.append(final.unknown_reason)
            if final.match is not FocusMatch.UNKNOWN or not _comparison_is_retryable(
                expected,
                current,
                final,
            ):
                break

        assert final is not None
        final = FocusComparison(
            match=final.match,
            unknown_reason=final.unknown_reason,
            expected_runtime_status=final.expected_runtime_status,
            current_runtime_status=final.current_runtime_status,
            attempts=attempts,
            encountered_unknown_reasons=tuple(encountered),
        )
        self.last_comparison = final
        return final

    def compare_current(self, expected: FocusTarget | None) -> FocusMatch:
        return self.compare_current_detailed(expected).match


_NATIVE_FIELD_CLASSES = (
    "edit",
    "richedit",
    "scintilla",
    "thundertextbox",
    "tedit",
    "tmemo",
    "windowsforms10.edit",
)


def _has_precise_native_hwnd(target: FocusTarget) -> bool:
    name = target.focused_class_name.strip().casefold()
    return bool(name) and any(name.startswith(prefix) for prefix in _NATIVE_FIELD_CLASSES)


def compare_focus_targets(
    expected: FocusTarget | None, current: FocusTarget | None
) -> FocusMatch:
    """Return the compatibility three-state result for a focus comparison."""

    return compare_focus_targets_detailed(expected, current).match


def _current_runtime_unknown_reason(
    status: RuntimeIdStatus,
) -> FocusUnknownReason:
    return {
        RuntimeIdStatus.MISSING: FocusUnknownReason.CURRENT_RUNTIME_ID_MISSING,
        RuntimeIdStatus.INVALID: FocusUnknownReason.CURRENT_RUNTIME_ID_INVALID,
        RuntimeIdStatus.TIMED_OUT: FocusUnknownReason.CURRENT_RUNTIME_ID_TIMED_OUT,
        RuntimeIdStatus.ERROR: FocusUnknownReason.CURRENT_RUNTIME_ID_ERROR,
        RuntimeIdStatus.SATURATED: FocusUnknownReason.CURRENT_RUNTIME_ID_SATURATED,
    }.get(status, FocusUnknownReason.CURRENT_RUNTIME_ID_UNAVAILABLE)


def _comparison(
    match: FocusMatch,
    expected: FocusTarget | None,
    current: FocusTarget | None,
    reason: FocusUnknownReason | None = None,
) -> FocusComparison:
    expected_status = (
        expected.effective_runtime_id_status
        if expected is not None
        else RuntimeIdStatus.UNKNOWN
    )
    current_status = (
        current.effective_runtime_id_status
        if current is not None
        else RuntimeIdStatus.UNKNOWN
    )
    encountered = (reason,) if reason is not None else ()
    return FocusComparison(
        match=match,
        unknown_reason=reason,
        expected_runtime_status=expected_status,
        current_runtime_status=current_status,
        encountered_unknown_reasons=encountered,
    )


def compare_focus_targets_detailed(
    expected: FocusTarget | None, current: FocusTarget | None
) -> FocusComparison:
    """Compare two targets without ever assuming virtual fields are equal.

    Native edit controls have one HWND per field and can be identified without
    UI Automation. Browsers, Electron, WebView and other virtualized interfaces
    commonly reuse a renderer HWND for many fields; identical HWNDs therefore
    return ``unknown`` unless both snapshots have comparable UIA runtime ids.
    The detailed result exposes a privacy-safe reason for ``unknown`` without
    reading or retaining any control content.
    """

    if expected is None:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.EXPECTED_TARGET_MISSING,
        )
    if current is None:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.CURRENT_TARGET_MISSING,
        )
    if not expected.stable:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.EXPECTED_CAPTURE_UNSTABLE,
        )
    if not current.stable:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.CURRENT_CAPTURE_UNSTABLE,
        )

    identity_fields = (
        "foreground_hwnd",
        "root_hwnd",
        "focused_hwnd",
        "thread_id",
        "process_id",
    )
    for field in identity_fields:
        old = int(getattr(expected, field))
        new = int(getattr(current, field))
        if old and new and old != new:
            return _comparison(FocusMatch.CHANGED, expected, current)

    if not (
        expected.has_complete_win32_identity
        and current.has_complete_win32_identity
    ):
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.INCOMPLETE_WIN32_IDENTITY,
        )

    old_class = expected.focused_class_name.strip().casefold()
    new_class = current.focused_class_name.strip().casefold()
    if old_class and new_class and old_class != new_class:
        return _comparison(FocusMatch.CHANGED, expected, current)

    old_runtime = expected.uia_runtime_id
    new_runtime = current.uia_runtime_id
    if old_runtime is not None and new_runtime is not None:
        match = FocusMatch.SAME if old_runtime == new_runtime else FocusMatch.CHANGED
        return _comparison(match, expected, current)
    if old_runtime is not None and new_runtime is None:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            _current_runtime_unknown_reason(
                current.effective_runtime_id_status
            ),
        )
    if old_runtime is None and new_runtime is not None:
        return _comparison(
            FocusMatch.UNKNOWN,
            expected,
            current,
            FocusUnknownReason.EXPECTED_RUNTIME_ID_MISSING,
        )

    if (
        old_class == new_class
        and _has_precise_native_hwnd(expected)
        and _has_precise_native_hwnd(current)
    ):
        return _comparison(FocusMatch.SAME, expected, current)

    if old_class == new_class:
        reason = FocusUnknownReason.BOTH_RUNTIME_IDS_MISSING
    else:
        reason = FocusUnknownReason.VIRTUAL_FIELD_WITHOUT_RUNTIME_ID
    return _comparison(FocusMatch.UNKNOWN, expected, current, reason)


def _comparison_is_retryable(
    expected: FocusTarget | None,
    current: FocusTarget | None,
    comparison: FocusComparison,
) -> bool:
    """Retry only a missing *current* RuntimeId after Win32 identity matched.

    A concrete Win32 or RuntimeId change is never retried, so a genuine target
    switch cannot be converted into SAME by a later focus change.
    """

    if expected is None or current is None or expected.uia_runtime_id is None:
        return False
    if comparison.match is not FocusMatch.UNKNOWN:
        return False
    if comparison.unknown_reason not in _CURRENT_RUNTIME_UNKNOWN_REASONS:
        return False
    if current.uia_runtime_id is not None:
        return False
    return current.effective_runtime_id_status in _TRANSIENT_RUNTIME_ID_STATUSES


def focus_app_id(target: FocusTarget | None) -> str | None:
    """Return a stable executable basename without exposing or storing its path."""

    if target is None or not target.process_id or os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    query_name.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(0x1000, False, target.process_id)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not query_name(handle, 0, buffer, ctypes.byref(size)):
            return None
        name = Path(buffer.value[: size.value]).name.strip().casefold()
        return name[:256] or None
    finally:
        close_handle(handle)
