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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast


__all__ = [
    "FocusBackend",
    "FocusInspector",
    "FocusMatch",
    "FocusTarget",
    "RuntimeIdProvider",
    "Win32FocusBackend",
    "Win32FocusState",
    "compare_focus_targets",
    "focus_app_id",
    "make_optional_uia_runtime_id_provider",
]


class FocusMatch(enum.StrEnum):
    """Result of comparing two read-only focus snapshots."""

    SAME = "same"
    CHANGED = "changed"
    UNKNOWN = "unknown"


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

    def __init__(self, provider: RuntimeIdProvider, timeout_seconds: float) -> None:
        self._provider = provider
        self._timeout_seconds = max(0.0, float(timeout_seconds))
        self._guard = threading.Lock()
        self._in_flight = False

    def focused_runtime_id(self) -> Iterable[int] | None:
        with self._guard:
            if self._in_flight:
                return None
            self._in_flight = True

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
                    self._in_flight = False

        worker = threading.Thread(target=run, name="uia-focus-id", daemon=True)
        worker.start()
        worker.join(self._timeout_seconds)
        if worker.is_alive():
            return None
        try:
            value = result.get_nowait()
        except queue.Empty:
            return None
        if value is self._FAILED:
            return None
        return cast(Iterable[int] | None, value)


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


class FocusInspector:
    """Capture stable focus targets through an injectable Win32 backend."""

    def __init__(
        self,
        backend: FocusBackend | None = None,
        runtime_id_provider: RuntimeIdProvider | None | object = _AUTO_UIA,
        *,
        max_attempts: int = 2,
    ) -> None:
        self.backend = backend if backend is not None else Win32FocusBackend()
        if runtime_id_provider is _AUTO_UIA:
            self.runtime_id_provider = make_optional_uia_runtime_id_provider()
        else:
            self.runtime_id_provider = cast(
                RuntimeIdProvider | None, runtime_id_provider
            )
        self.max_attempts = max(1, int(max_attempts))

    def _snapshot(self) -> Win32FocusState:
        try:
            return self.backend.snapshot()
        except Exception:
            return Win32FocusState()

    def _runtime_id(self) -> tuple[int, ...] | None:
        if self.runtime_id_provider is None:
            return None
        try:
            return _normalize_runtime_id(
                self.runtime_id_provider.focused_runtime_id()
            )
        except Exception:
            return None

    @staticmethod
    def _target(
        state: Win32FocusState,
        runtime_id: tuple[int, ...] | None,
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
            uia_runtime_id=runtime_id,
            stable=stable,
        )

    def capture(self) -> FocusTarget:
        """Capture focus without reading content or changing input state."""

        last = Win32FocusState()
        for _ in range(self.max_attempts):
            before = self._snapshot()
            runtime_id = self._runtime_id() if before.focused_hwnd else None
            after = self._snapshot()
            last = after
            if (
                before.valid
                and after.valid
                and before.capture_key == after.capture_key
            ):
                return self._target(after, runtime_id, stable=True)
        # A UIA id captured between two different Win32 states is not safe to
        # associate with either state, so deliberately discard it.
        return self._target(last, None, stable=False)

    def compare_current(self, expected: FocusTarget | None) -> FocusMatch:
        return compare_focus_targets(expected, self.capture())


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
    """Compare two targets without ever assuming virtual fields are equal.

    Native edit controls have one HWND per field and can be identified without
    UI Automation. Browsers, Electron, WebView and other virtualized interfaces
    commonly reuse a renderer HWND for many fields; identical HWNDs therefore
    return ``unknown`` unless both snapshots have comparable UIA runtime ids.
    """

    if expected is None or current is None:
        return FocusMatch.UNKNOWN
    if not expected.stable or not current.stable:
        return FocusMatch.UNKNOWN

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
            return FocusMatch.CHANGED

    if not (
        expected.has_complete_win32_identity
        and current.has_complete_win32_identity
    ):
        return FocusMatch.UNKNOWN

    old_class = expected.focused_class_name.strip().casefold()
    new_class = current.focused_class_name.strip().casefold()
    if old_class and new_class and old_class != new_class:
        return FocusMatch.CHANGED

    old_runtime = expected.uia_runtime_id
    new_runtime = current.uia_runtime_id
    if old_runtime is not None and new_runtime is not None:
        return FocusMatch.SAME if old_runtime == new_runtime else FocusMatch.CHANGED
    if (old_runtime is None) != (new_runtime is None):
        return FocusMatch.UNKNOWN

    if (
        old_class == new_class
        and _has_precise_native_hwnd(expected)
        and _has_precise_native_hwnd(current)
    ):
        return FocusMatch.SAME
    return FocusMatch.UNKNOWN


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
