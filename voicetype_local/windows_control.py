from __future__ import annotations

"""Conservative, dependency-injected Windows control primitives.

The module accepts *canonical* intents only.  It deliberately does not parse
free-form speech, execute arbitrary commands, inspect document text, or return
captured content.  UI Automation is preferred for element invocation and
semantic scrolling when the optional ``uiautomation`` package is available.
The Win32 fallback is limited to allow-listed applications, window state,
bounded scrolling, and a small key allow-list.

Every public result contains metadata and stable reason codes only.  Tests can
inject fake backends, so importing or exercising the policy layer never needs
to touch the real desktop.
"""

import ctypes
import enum
import hashlib
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import nullcontext
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol, cast


__all__ = [
    "ApplicationRegistry",
    "BackendActionResult",
    "BackendStatus",
    "CanonicalIntent",
    "ConfirmationChallenge",
    "ConfirmationPolicy",
    "ControlRequest",
    "ControlResult",
    "DenyConfirmationPolicy",
    "ElementBounds",
    "ForegroundWindowGate",
    "KnownApplication",
    "NumberedElement",
    "NumberedElementSnapshot",
    "ResultStatus",
    "ScrollDirection",
    "Sensitivity",
    "UiaControlBackend",
    "Win32ControlBackend",
    "WindowAction",
    "WindowsControlExecutor",
    "default_application_registry",
    "make_optional_native_backend",
    "make_optional_uia_backend",
]


class CanonicalIntent(enum.StrEnum):
    HELP = "help"
    CANCEL = "cancel"
    OPEN_APP = "open_app"
    SWITCH_APP = "switch_app"
    MINIMIZE_WINDOW = "minimize_window"
    MAXIMIZE_WINDOW = "maximize_window"
    RESTORE_WINDOW = "restore_window"
    SCROLL = "scroll"
    INVOKE_NAMED_ELEMENT = "invoke_named_element"
    INVOKE_NUMBERED_ELEMENT = "invoke_numbered_element"
    SEND_KEY = "send_key"
    SEND_HOTKEY = "send_hotkey"


class WindowAction(enum.StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    RESTORE = "restore"


class ScrollDirection(enum.StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class ResultStatus(enum.StrEnum):
    EXECUTED = "executed"
    NOOP = "noop"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class BackendStatus(enum.StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    FAILED = "failed"


class Sensitivity(enum.StrEnum):
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class BackendActionResult:
    status: BackendStatus
    backend: str
    reason_code: str = ""

    @classmethod
    def success(cls, backend: str) -> BackendActionResult:
        return cls(BackendStatus.SUCCESS, backend, "ok")

    @classmethod
    def not_found(cls, backend: str, reason: str = "not_found") -> BackendActionResult:
        return cls(BackendStatus.NOT_FOUND, backend, reason)

    @classmethod
    def unsupported(
        cls, backend: str, reason: str = "unsupported"
    ) -> BackendActionResult:
        return cls(BackendStatus.UNSUPPORTED, backend, reason)

    @classmethod
    def blocked(cls, backend: str, reason: str = "protected_target") -> BackendActionResult:
        return cls(BackendStatus.BLOCKED, backend, reason)

    @classmethod
    def failed(cls, backend: str, reason: str = "backend_failed") -> BackendActionResult:
        return cls(BackendStatus.FAILED, backend, reason)


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Metadata-only execution result.

    ``target_id`` is an allow-listed application id, ``current``, or an opaque
    snapshot id plus local element number. It never contains an executable
    path, UI text, transcript, command line, or exception message.
    """

    status: ResultStatus
    intent: CanonicalIntent
    backend: str = "none"
    target_id: str = ""
    reason_code: str = ""
    confirmation_required: bool = False


@dataclass(frozen=True, slots=True)
class ControlRequest:
    intent: CanonicalIntent
    app_id: str | None = None
    element_name: str | None = None
    element_number: int | None = None
    snapshot_id: str | None = None
    sensitivity: Sensitivity | str | None = None
    direction: ScrollDirection | str | None = None
    amount: int = 1
    key: str | None = None
    keys: tuple[str, ...] = ()

    @classmethod
    def from_action(
        cls, action: str, args: Mapping[str, object] | None = None
    ) -> ControlRequest:
        """Build a request from a future voice-command adapter.

        Only canonical action names and the fixed schema below are accepted.
        There is intentionally no executable, command line, text-to-type, URL,
        shell, or elevation argument.
        """

        try:
            intent = CanonicalIntent(str(action).strip().casefold())
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown canonical action") from exc
        values = dict(args or {})
        allowed = {
            "app_id",
            "element_name",
            "element_number",
            "snapshot_id",
            "sensitivity",
            "direction",
            "amount",
            "key",
            "keys",
        }
        if any(key not in allowed for key in values):
            raise ValueError("Unknown action argument")

        def optional_text(key: str) -> str | None:
            value = values.get(key)
            if value is None:
                return None
            if not isinstance(value, str) or len(value) > 160:
                raise ValueError("Invalid text action argument")
            return value

        element_number_value = values.get("element_number")
        if element_number_value is not None and (
            isinstance(element_number_value, bool)
            or not isinstance(element_number_value, int)
        ):
            raise ValueError("Invalid element number")
        amount_value = values.get("amount", 1)
        if isinstance(amount_value, bool) or not isinstance(amount_value, int):
            raise ValueError("Invalid amount")
        keys_value = values.get("keys", ())
        if isinstance(keys_value, str) or not isinstance(keys_value, Sequence):
            raise ValueError("Invalid hotkey")
        if len(keys_value) > 4 or any(not isinstance(item, str) for item in keys_value):
            raise ValueError("Invalid hotkey")

        sensitivity_value = optional_text("sensitivity")
        try:
            sensitivity = (
                Sensitivity(sensitivity_value)
                if sensitivity_value is not None
                else None
            )
        except ValueError as exc:
            raise ValueError("Invalid sensitivity") from exc

        return cls(
            intent=intent,
            app_id=optional_text("app_id"),
            element_name=optional_text("element_name"),
            element_number=cast(int | None, element_number_value),
            snapshot_id=optional_text("snapshot_id"),
            sensitivity=sensitivity,
            direction=optional_text("direction"),
            amount=amount_value,
            key=optional_text("key"),
            keys=tuple(cast(Sequence[str], keys_value)),
        )


@dataclass(frozen=True, slots=True)
class ConfirmationChallenge:
    intent: CanonicalIntent
    sensitivity: Sensitivity
    target_id: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class ForegroundWindowGate:
    """Opaque identity proving that a non-protected window is foreground."""

    token: str


class ConfirmationPolicy(Protocol):
    """Trusted UI/application policy, separate from the untrusted request."""

    def confirm(self, challenge: ConfirmationChallenge) -> bool: ...


class DenyConfirmationPolicy:
    """Safe default: sensitive and destructive actions never execute."""

    def confirm(self, challenge: ConfirmationChallenge) -> bool:
        del challenge
        return False


class _ApplicationKind(enum.StrEnum):
    NORMAL = "normal"
    TERMINAL = "terminal"
    ADMIN = "admin"


_FORBIDDEN_EXECUTABLES = frozenset(
    {
        "bash.exe",
        "cmd.exe",
        "conhost.exe",
        "consent.exe",
        "control.exe",
        "elevate.exe",
        "gsudo.exe",
        "mmc.exe",
        "powershell.exe",
        "pwsh.exe",
        "regedit.exe",
        "runas.exe",
        "services.exe",
        "sudo.exe",
        "taskmgr.exe",
        "wsl.exe",
        "wt.exe",
        "windowsterminal.exe",
    }
)
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _normalize_label(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def _executable_name(path: str) -> str:
    return PureWindowsPath(path).name.strip().casefold()


def _is_forbidden_executable(path_or_name: str) -> bool:
    return _executable_name(path_or_name) in _FORBIDDEN_EXECUTABLES


@dataclass(frozen=True, slots=True)
class KnownApplication:
    """A fixed, trusted launch target.

    Arguments are part of the registry entry.  A :class:`ControlRequest` has no
    command-line field and therefore cannot turn an allowed application into a
    command runner.
    """

    app_id: str
    executable: str
    aliases: tuple[str, ...] = ()
    arguments: tuple[str, ...] = ()
    kind: _ApplicationKind = _ApplicationKind.NORMAL


class ApplicationRegistry:
    def __init__(self, applications: Iterable[KnownApplication] = ()) -> None:
        by_id: dict[str, KnownApplication] = {}
        aliases: dict[str, str] = {}
        for application in applications:
            app_id = application.app_id.strip().casefold()
            if not _APP_ID_RE.fullmatch(app_id):
                raise ValueError("Application ids must be short canonical identifiers")
            path = PureWindowsPath(application.executable)
            if not path.is_absolute():
                raise ValueError("Allow-listed executables must use absolute paths")
            if application.kind is not _ApplicationKind.NORMAL:
                raise ValueError("Terminal and administrative applications are prohibited")
            if _is_forbidden_executable(str(path)):
                raise ValueError("Terminal and administrative executables are prohibited")
            if app_id in by_id:
                raise ValueError("Duplicate application id")

            normalized = KnownApplication(
                app_id=app_id,
                executable=str(path),
                aliases=tuple(application.aliases),
                arguments=tuple(application.arguments),
                kind=_ApplicationKind.NORMAL,
            )
            by_id[app_id] = normalized
            for label in (app_id, *application.aliases):
                alias = _normalize_label(label)
                if not alias or len(alias) > 80:
                    raise ValueError("Application aliases must be non-empty and bounded")
                previous = aliases.get(alias)
                if previous is not None and previous != app_id:
                    raise ValueError("Duplicate application alias")
                aliases[alias] = app_id
        self._by_id = by_id
        self._aliases = aliases

    def resolve(self, label: str | None) -> KnownApplication | None:
        if label is None:
            return None
        app_id = self._aliases.get(_normalize_label(label))
        return self._by_id.get(app_id) if app_id is not None else None

    @property
    def app_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))


def default_application_registry() -> ApplicationRegistry:
    windows = PureWindowsPath(os.environ.get("WINDIR", r"C:\Windows"))
    system32 = windows / "System32"
    program_files = PureWindowsPath(
        os.environ.get("ProgramFiles", r"C:\Program Files")
    )
    program_files_x86 = PureWindowsPath(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    local_app_data = PureWindowsPath(
        os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local")
    )

    applications: list[KnownApplication] = [
        KnownApplication(
            "notepad",
            str(system32 / "notepad.exe"),
            aliases=("блокнот", "bloc de notas", "notepad"),
        ),
        KnownApplication(
            "calculator",
            str(system32 / "calc.exe"),
            aliases=("калькулятор", "calculadora", "calculator", "calc"),
        ),
        KnownApplication(
            "paint",
            str(system32 / "mspaint.exe"),
            aliases=("paint", "рисование", "pintura"),
        ),
        KnownApplication(
            "explorer",
            str(windows / "explorer.exe"),
            aliases=(
                "проводник",
                "explorer",
                "file explorer",
                "explorador",
                "explorador de archivos",
            ),
        ),
    ]

    def add_installed(
        app_id: str,
        candidates: tuple[PureWindowsPath, ...],
        aliases: tuple[str, ...],
    ) -> None:
        for candidate in candidates:
            if Path(str(candidate)).is_file():
                applications.append(
                    KnownApplication(app_id, str(candidate), aliases=aliases)
                )
                return

    add_installed(
        "chrome",
        (
            program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
            program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
        ),
        ("chrome", "google chrome", "гугл хром"),
    )
    add_installed(
        "edge",
        (
            program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ),
        ("edge", "microsoft edge", "эдж"),
    )
    add_installed(
        "firefox",
        (
            program_files / "Mozilla Firefox" / "firefox.exe",
            program_files_x86 / "Mozilla Firefox" / "firefox.exe",
        ),
        ("firefox", "mozilla firefox", "фаерфокс"),
    )
    add_installed(
        "vscode",
        (
            local_app_data / "Programs" / "Microsoft VS Code" / "Code.exe",
            program_files / "Microsoft VS Code" / "Code.exe",
        ),
        ("visual studio code", "vs code", "vscode"),
    )
    add_installed(
        "word",
        (
            program_files / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
            program_files_x86 / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
        ),
        ("word", "microsoft word", "ворд"),
    )
    add_installed(
        "outlook",
        (
            program_files / "Microsoft Office" / "root" / "Office16" / "OUTLOOK.EXE",
            program_files_x86 / "Microsoft Office" / "root" / "Office16" / "OUTLOOK.EXE",
        ),
        ("outlook", "microsoft outlook", "аутлук"),
    )
    return ApplicationRegistry(applications)


@dataclass(frozen=True, slots=True)
class ElementBounds:
    left: int
    top: int
    width: int
    height: int

    @property
    def usable(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True, slots=True)
class NumberedElement:
    """Actionable UIA metadata suitable for a future numbered overlay."""

    number: int
    name: str
    role: str
    bounds: ElementBounds | None = None
    enabled: bool = True
    offscreen: bool = False
    invoke_supported: bool = True
    destructive: bool = False
    runtime_id: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class NumberedElementSnapshot:
    snapshot_id: str
    window_id: str
    elements: tuple[NumberedElement, ...]
    gate_token: str = ""

    def by_number(self, number: int) -> NumberedElement | None:
        return next((item for item in self.elements if item.number == number), None)


class NativeControlBackend(Protocol):
    def foreground_gate(
        self,
    ) -> tuple[ForegroundWindowGate | None, BackendActionResult | None]: ...

    def open_application(self, application: KnownApplication) -> BackendActionResult: ...

    def switch_application(self, application: KnownApplication) -> BackendActionResult: ...

    def window_action(
        self, action: WindowAction, application: KnownApplication | None
    ) -> BackendActionResult: ...

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult: ...

    def send_key(
        self, key: str, expected_gate_token: str | None = None
    ) -> BackendActionResult: ...

    def send_hotkey(
        self, keys: tuple[str, ...], expected_gate_token: str | None = None
    ) -> BackendActionResult: ...


class UIAutomationControlBackend(Protocol):
    def capture_elements(self, gate_token: str) -> NumberedElementSnapshot | None: ...

    def clear_snapshot(self) -> None: ...

    def is_snapshot_current(self, snapshot_id: str) -> bool: ...

    def invoke(self, snapshot_id: str, number: int) -> BackendActionResult: ...

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult: ...


_DESTRUCTIVE_WORDS = (
    "delete",
    "remove",
    "erase",
    "uninstall",
    "reset",
    "format",
    "purchase",
    "buy",
    "pay",
    "submit",
    "send",
    "confirm",
    "approve",
    "authorize",
    "share",
    "publish",
    "sign in",
    "sign-in",
    "sign out",
    "sign-out",
    "log in",
    "login",
    "log out",
    "logout",
    "order",
    "checkout",
    "post",
    "run as administrator",
    "administrator",
    "terminal",
    "command prompt",
    "powershell",
    "удал",
    "стер",
    "сброс",
    "оплат",
    "купить",
    "отправ",
    "подтверд",
    "одобр",
    "разреш",
    "авториз",
    "подел",
    "опубли",
    "войти",
    "вход",
    "выйти",
    "выход",
    "заказ",
    "оформить",
    "от имени администратора",
    "администратор",
    "терминал",
    "командная строка",
    "eliminar",
    "borrar",
    "desinstalar",
    "restablecer",
    "pagar",
    "comprar",
    "enviar",
    "confirm",
    "aprobar",
    "autorizar",
    "compartir",
    "publicar",
    "iniciar sesion",
    "iniciar sesión",
    "cerrar sesion",
    "cerrar sesión",
    "acceder",
    "entrar",
    "salir",
    "pedido",
    "finalizar compra",
    "como administrador",
    "administrador",
    "terminal",
    "simbolo del sistema",
    "símbolo del sistema",
)


def _looks_destructive(name: str) -> bool:
    normalized = _normalize_label(name)
    return any(fragment in normalized for fragment in _DESTRUCTIVE_WORDS)


def _bounded_uia_name(value: object) -> str:
    return " ".join(str(value or "").split())[:120]


def _opaque_window_id(value: object) -> str:
    digest = hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()
    return digest[:20]


class UiaControlBackend:
    """Adapter for the optional third-party ``uiautomation`` package.

    It exposes names and geometry of actionable controls only; control values
    and document text are never read.  A numbered action is accepted only for
    the most recent snapshot and only while the foreground UIA root is stable.
    """

    _ACTIONABLE_ROLES = frozenset(
        {
            "buttoncontrol",
            "checkboxcontrol",
            "comboboxcontrol",
            "hyperlinkcontrol",
            "listitemcontrol",
            "menuitemcontrol",
            "radiobuttoncontrol",
            "tabitemcontrol",
        }
    )

    def __init__(self, automation_module: object, *, max_elements: int = 80) -> None:
        self._automation = automation_module
        self._max_elements = max(1, min(int(max_elements), 200))
        self._snapshot: NumberedElementSnapshot | None = None
        self._state_lock = threading.RLock()
        self._snapshot_epoch = 0

    def _thread_initializer(self):
        """Create the package's per-thread COM/UIA context when available."""

        initializer = getattr(
            self._automation, "UIAutomationInitializerInThread", None
        )
        return initializer() if callable(initializer) else nullcontext()

    def _foreground_root(self) -> object | None:
        getter = getattr(self._automation, "GetForegroundControl", None)
        return getter() if callable(getter) else None

    @staticmethod
    def _runtime_id(control: object) -> tuple[int, ...] | None:
        getter = getattr(control, "GetRuntimeId", None)
        if not callable(getter):
            return None
        try:
            value = tuple(int(item) for item in getter())
        except Exception:
            return None
        return value or None

    def _root_id(self, root: object) -> str:
        identity: object = self._runtime_id(root)
        if identity is None:
            identity = (
                getattr(root, "NativeWindowHandle", 0),
                getattr(root, "ControlTypeName", ""),
            )
        return _opaque_window_id(identity)

    @staticmethod
    def _bounds(control: object) -> ElementBounds | None:
        def coordinate(rect: object, lower: str, upper: str, index: int) -> int:
            # UIA rectangle wrappers commonly expose attributes but are not
            # subscriptable.  Sequence access must therefore be a final,
            # lazily-evaluated fallback rather than a nested getattr default.
            missing = object()
            value = getattr(rect, lower, missing)
            if value is not missing:
                return int(value)
            value = getattr(rect, upper, missing)
            if value is not missing:
                return int(value)
            return int(rect[index])  # type: ignore[index]

        try:
            rect = getattr(control, "BoundingRectangle")
            left = coordinate(rect, "left", "Left", 0)
            top = coordinate(rect, "top", "Top", 1)
            right = coordinate(rect, "right", "Right", 2)
            bottom = coordinate(rect, "bottom", "Bottom", 3)
        except Exception:
            return None
        result = ElementBounds(left, top, right - left, bottom - top)
        return result if result.usable else None

    def _walk(self, root: object) -> Iterable[object]:
        walk = getattr(self._automation, "WalkControl", None)
        if not callable(walk):
            return ()
        try:
            values = walk(root, includeTop=False, maxDepth=8)
        except TypeError:
            values = walk(root)

        def controls() -> Iterable[object]:
            for item in values:
                if isinstance(item, tuple) and item:
                    yield item[0]
                else:
                    yield item

        return controls()

    def _describe_control(
        self, number: int, control: object
    ) -> NumberedElement | None:
        role = _bounded_uia_name(getattr(control, "ControlTypeName", ""))
        if role.casefold() not in self._ACTIONABLE_ROLES:
            return None
        raw_name = str(getattr(control, "Name", "") or "")
        name = _bounded_uia_name(raw_name)
        if not name:
            return None
        enabled = bool(getattr(control, "IsEnabled", True))
        offscreen = bool(getattr(control, "IsOffscreen", False))
        if offscreen:
            return None
        return NumberedElement(
            number=number,
            name=name,
            role=role[:64],
            bounds=self._bounds(control),
            enabled=enabled,
            offscreen=offscreen,
            invoke_supported=True,
            # Names are deliberately bounded in memory and on the overlay. If
            # classification would require inspecting text beyond that bound,
            # fail conservatively and require confirmation.
            destructive=len(raw_name) > 120 or _looks_destructive(name),
            runtime_id=self._runtime_id(control),
        )

    def _actionable_controls(
        self, root: object
    ) -> Iterable[tuple[NumberedElement, object]]:
        number = 0
        for control in self._walk(root):
            descriptor = self._describe_control(number + 1, control)
            if descriptor is None:
                continue
            number += 1
            yield descriptor, control
            if number >= self._max_elements:
                break

    @staticmethod
    def _same_element(
        expected: NumberedElement, current: NumberedElement
    ) -> bool:
        return (
            expected.number == current.number
            and expected.name == current.name
            and expected.role == current.role
            and expected.bounds == current.bounds
            and expected.runtime_id == current.runtime_id
        )

    def capture_elements(self, gate_token: str) -> NumberedElementSnapshot | None:
        if not gate_token:
            return None
        with self._state_lock:
            self._snapshot_epoch += 1
            capture_epoch = self._snapshot_epoch
            self._snapshot = None
        try:
            with self._thread_initializer():
                root = self._foreground_root()
                if root is None:
                    return None
                window_id = self._root_id(root)
                elements = tuple(
                    descriptor
                    for descriptor, _control in self._actionable_controls(root)
                )
                snapshot = NumberedElementSnapshot(
                    uuid.uuid4().hex, window_id, elements, gate_token
                )
                # The snapshot contains immutable descriptors only. Live COM
                # controls stay local to this initialized thread.
                with self._state_lock:
                    if self._snapshot_epoch != capture_epoch:
                        return None
                    self._snapshot = snapshot
                return snapshot
        except Exception:
            with self._state_lock:
                if self._snapshot_epoch == capture_epoch:
                    self._snapshot = None
            return None

    def clear_snapshot(self) -> None:
        """Release immutable UI names and invalidate all prior element ids."""

        with self._state_lock:
            self._snapshot_epoch += 1
            self._snapshot = None

    def is_snapshot_current(self, snapshot_id: str) -> bool:
        """Check only snapshot/window identity in a fresh UIA thread context."""

        with self._state_lock:
            snapshot = self._snapshot
            snapshot_epoch = self._snapshot_epoch
        if snapshot is None or snapshot_id != snapshot.snapshot_id:
            return False
        try:
            with self._thread_initializer():
                root = self._foreground_root()
                current = bool(
                    root is not None and self._root_id(root) == snapshot.window_id
                )
        except Exception:
            return False
        with self._state_lock:
            return bool(
                current
                and self._snapshot_epoch == snapshot_epoch
                and self._snapshot is snapshot
            )

    def invoke(self, snapshot_id: str, number: int) -> BackendActionResult:
        with self._state_lock:
            snapshot = self._snapshot
            snapshot_epoch = self._snapshot_epoch
        if snapshot is None or snapshot_id != snapshot.snapshot_id:
            return BackendActionResult.blocked("uia", "stale_snapshot")
        expected = snapshot.by_number(number)
        if expected is None:
            return BackendActionResult.not_found("uia", "element_not_found")
        try:
            with self._thread_initializer():
                root = self._foreground_root()
                if root is None or self._root_id(root) != snapshot.window_id:
                    return BackendActionResult.blocked(
                        "uia", "foreground_changed"
                    )

                fresh_control: object | None = None
                for current, control in self._actionable_controls(root):
                    if current.number != number:
                        continue
                    if not self._same_element(expected, current):
                        return BackendActionResult.blocked(
                            "uia", "element_changed"
                        )
                    fresh_control = control
                    break
                if fresh_control is None:
                    return BackendActionResult.blocked(
                        "uia", "element_changed"
                    )
                if not bool(getattr(fresh_control, "IsEnabled", True)):
                    return BackendActionResult.blocked(
                        "uia", "element_disabled"
                    )
                with self._state_lock:
                    if (
                        self._snapshot_epoch != snapshot_epoch
                        or self._snapshot is not snapshot
                    ):
                        return BackendActionResult.blocked(
                            "uia", "stale_snapshot"
                        )

                invoke_pattern = getattr(fresh_control, "GetInvokePattern", None)
                if callable(invoke_pattern):
                    pattern = invoke_pattern()
                    if pattern is not None:
                        pattern.Invoke()
                        return BackendActionResult.success("uia")

                # This fallback uses only the freshly re-enumerated object
                # inside the current initialized UIA thread.
                click = getattr(fresh_control, "Click", None)
                if callable(click):
                    click(simulateMove=False)
                    return BackendActionResult.success("uia_click")
                return BackendActionResult.unsupported(
                    "uia", "invoke_not_supported"
                )
        except Exception:
            return BackendActionResult.failed("uia")

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult:
        try:
            with self._thread_initializer():
                getter = getattr(self._automation, "GetFocusedControl", None)
                control = getter() if callable(getter) else self._foreground_root()
                no_amount = getattr(self._automation, "ScrollAmount_NoAmount", 2)
                increment = getattr(
                    self._automation, "ScrollAmount_SmallIncrement", 4
                )
                decrement = getattr(
                    self._automation, "ScrollAmount_SmallDecrement", 1
                )
                for _ in range(8):
                    if control is None:
                        break
                    get_pattern = getattr(control, "GetScrollPattern", None)
                    pattern = get_pattern() if callable(get_pattern) else None
                    if pattern is not None:
                        horizontal = no_amount
                        vertical = no_amount
                        if direction is ScrollDirection.UP:
                            vertical = decrement
                        elif direction is ScrollDirection.DOWN:
                            vertical = increment
                        elif direction is ScrollDirection.LEFT:
                            horizontal = decrement
                        else:
                            horizontal = increment
                        for _ in range(amount):
                            pattern.Scroll(horizontal, vertical)
                        return BackendActionResult.success("uia")
                    parent = getattr(control, "GetParentControl", None)
                    control = parent() if callable(parent) else None
                return BackendActionResult.unsupported(
                    "uia", "scroll_pattern_unavailable"
                )
        except Exception:
            return BackendActionResult.failed("uia")


def make_optional_uia_backend() -> UIAutomationControlBackend | None:
    if os.name != "nt":
        return None
    try:
        import uiautomation  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None
    try:
        return UiaControlBackend(uiautomation)
    except Exception:
        return None


ULONG_PTR = wintypes.WPARAM


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


_VK = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "SHIFT": 0x10,
    "CTRL": 0x11,
    "ALT": 0x12,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "DELETE": 0x2E,
    "WIN": 0x5B,
    "A": 0x41,
    "C": 0x43,
    "D": 0x44,
    "F": 0x46,
    "L": 0x4C,
    "N": 0x4E,
    "P": 0x50,
    "S": 0x53,
    "T": 0x54,
    "V": 0x56,
    "W": 0x57,
    "X": 0x58,
    "Y": 0x59,
    "Z": 0x5A,
    "F1": 0x70,
    "F4": 0x73,
}


class Win32ControlBackend:
    """Bounded Win32 fallback with protected-target checks."""

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _TOKEN_QUERY = 0x0008
    _TOKEN_ELEVATION_CLASS = 20
    _INPUT_KEYBOARD = 1
    _INPUT_MOUSE = 0
    _KEYEVENTF_KEYUP = 0x0002
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_HWHEEL = 0x01000
    _SW_MINIMIZE = 6
    _SW_MAXIMIZE = 3
    _SW_RESTORE = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 control is available only on Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure()
        # Integrity level is stable for the process lifetime. Unknown fails
        # closed just like an elevated controller.
        self._controller_elevated = self._elevated(os.getpid())

    def _configure(self) -> None:
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = wintypes.UINT

        self._kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL

    def _pid(self, hwnd: int) -> int:
        process_id = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(process_id))
        return int(process_id.value)

    def _open_process(self, pid: int) -> int:
        return int(
            self._kernel32.OpenProcess(
                self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            or 0
        )

    def _process_name(self, pid: int) -> str:
        handle = self._open_process(pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not self._kernel32.QueryFullProcessImageNameW(
                wintypes.HANDLE(handle), 0, buffer, ctypes.byref(size)
            ):
                return ""
            return _executable_name(buffer.value[: size.value])
        finally:
            self._kernel32.CloseHandle(wintypes.HANDLE(handle))

    def _elevated(self, pid: int) -> bool | None:
        process = self._open_process(pid)
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not self._advapi32.OpenProcessToken(
                wintypes.HANDLE(process), self._TOKEN_QUERY, ctypes.byref(token)
            ):
                return None
            elevation = _TOKEN_ELEVATION()
            returned = wintypes.DWORD(0)
            if not self._advapi32.GetTokenInformation(
                token,
                self._TOKEN_ELEVATION_CLASS,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            ):
                return None
            return bool(elevation.TokenIsElevated)
        finally:
            if token:
                self._kernel32.CloseHandle(token)
            self._kernel32.CloseHandle(wintypes.HANDLE(process))

    def _safe_window(self, hwnd: int) -> BackendActionResult | None:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        if not hwnd or not self._user32.IsWindowVisible(wintypes.HWND(hwnd)):
            return BackendActionResult.not_found("win32", "window_not_found")
        pid = self._pid(hwnd)
        name = self._process_name(pid) if pid else ""
        if not pid or not name:
            return BackendActionResult.blocked("win32", "target_identity_unknown")
        if _is_forbidden_executable(name):
            return BackendActionResult.blocked("win32", "terminal_or_admin_prohibited")
        elevated = self._elevated(pid)
        if elevated is not False:
            return BackendActionResult.blocked("win32", "elevated_or_unknown_target")
        return None

    def _controller_blocked(self, backend: str) -> BackendActionResult | None:
        if getattr(self, "_controller_elevated", None) is not False:
            return BackendActionResult.blocked(
                backend, "controller_elevated_or_unknown"
            )
        return None

    def _foreground(self) -> tuple[int, BackendActionResult | None]:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        return hwnd, self._safe_window(hwnd)

    def foreground_gate(
        self,
    ) -> tuple[ForegroundWindowGate | None, BackendActionResult | None]:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return None, controller_blocked
        hwnd, blocked = self._foreground()
        if blocked is not None:
            return None, blocked
        pid = self._pid(hwnd)
        name = self._process_name(pid) if pid else ""
        if not pid or not name:
            return None, BackendActionResult.blocked(
                "win32", "target_identity_unknown"
            )
        return ForegroundWindowGate(_opaque_window_id((hwnd, pid, name))), None

    def _find_window(self, application: KnownApplication) -> int:
        expected = _executable_name(application.executable)
        found = 0
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd: int, _lparam: int) -> bool:
            nonlocal found
            value = int(hwnd or 0)
            if not self._user32.IsWindowVisible(wintypes.HWND(value)):
                return True
            pid = self._pid(value)
            if pid and self._process_name(pid) == expected:
                found = value
                return False
            return True

        callback = callback_type(visit)
        self._user32.EnumWindows(callback, 0)
        return found

    def _target_window(
        self, application: KnownApplication | None
    ) -> tuple[int, BackendActionResult | None]:
        if application is None:
            return self._foreground()
        hwnd = self._find_window(application)
        return hwnd, self._safe_window(hwnd)

    def open_application(self, application: KnownApplication) -> BackendActionResult:
        controller_blocked = self._controller_blocked("process")
        if controller_blocked is not None:
            return controller_blocked
        if _is_forbidden_executable(application.executable):
            return BackendActionResult.blocked("process", "terminal_or_admin_prohibited")
        if not os.path.isfile(application.executable):
            return BackendActionResult.not_found("process", "executable_not_found")
        try:
            subprocess.Popen(
                [application.executable, *application.arguments],
                shell=False,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return BackendActionResult.success("process")
        except (OSError, ValueError):
            return BackendActionResult.failed("process", "launch_failed")

    def switch_application(self, application: KnownApplication) -> BackendActionResult:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        hwnd, blocked = self._target_window(application)
        if blocked is not None:
            return blocked
        self._user32.ShowWindow(wintypes.HWND(hwnd), self._SW_RESTORE)
        if not self._user32.SetForegroundWindow(wintypes.HWND(hwnd)):
            return BackendActionResult.failed("win32", "switch_failed")
        return BackendActionResult.success("win32")

    def window_action(
        self, action: WindowAction, application: KnownApplication | None
    ) -> BackendActionResult:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        hwnd, blocked = self._target_window(application)
        if blocked is not None:
            return blocked
        command = {
            WindowAction.MINIMIZE: self._SW_MINIMIZE,
            WindowAction.MAXIMIZE: self._SW_MAXIMIZE,
            WindowAction.RESTORE: self._SW_RESTORE,
        }[action]
        self._user32.ShowWindow(wintypes.HWND(hwnd), command)
        return BackendActionResult.success("win32")

    def _send(self, events: Sequence[_INPUT]) -> BackendActionResult:
        if not events:
            return BackendActionResult.unsupported("sendinput")
        array_type = _INPUT * len(events)
        array = array_type(*events)
        sent = int(self._user32.SendInput(len(events), array, ctypes.sizeof(_INPUT)))
        return (
            BackendActionResult.success("sendinput")
            if sent == len(events)
            else BackendActionResult.failed("sendinput", "partial_input")
        )

    def scroll(self, direction: ScrollDirection, amount: int) -> BackendActionResult:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        _hwnd, blocked = self._foreground()
        if blocked is not None:
            return blocked
        horizontal = direction in (ScrollDirection.LEFT, ScrollDirection.RIGHT)
        positive = direction in (ScrollDirection.UP, ScrollDirection.RIGHT)
        delta = 120 * amount * (1 if positive else -1)
        event = _INPUT(
            type=self._INPUT_MOUSE,
            mi=_MOUSEINPUT(
                0,
                0,
                ctypes.c_uint32(delta).value,
                self._MOUSEEVENTF_HWHEEL if horizontal else self._MOUSEEVENTF_WHEEL,
                0,
                0,
            ),
        )
        return self._send((event,))

    @staticmethod
    def _key_event(key: str, *, key_up: bool = False) -> _INPUT:
        flags = Win32ControlBackend._KEYEVENTF_KEYUP if key_up else 0
        return _INPUT(
            type=Win32ControlBackend._INPUT_KEYBOARD,
            ki=_KEYBDINPUT(_VK[key], 0, flags, 0, 0),
        )

    def _foreground_for_input(
        self, expected_gate_token: str | None
    ) -> tuple[int, BackendActionResult | None]:
        """Re-check the confirmation-bound foreground immediately before input."""

        hwnd, blocked = self._foreground()
        if blocked is not None or expected_gate_token is None:
            return hwnd, blocked
        pid = self._pid(hwnd)
        name = self._process_name(pid) if pid else ""
        if not pid or not name:
            return hwnd, BackendActionResult.blocked(
                "win32", "target_identity_unknown"
            )
        current = _opaque_window_id((hwnd, pid, name))
        if current != expected_gate_token:
            return hwnd, BackendActionResult.blocked("win32", "foreground_changed")
        return hwnd, None

    def send_key(
        self, key: str, expected_gate_token: str | None = None
    ) -> BackendActionResult:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        if key not in _VK:
            return BackendActionResult.unsupported("sendinput", "key_not_supported")
        events = (self._key_event(key), self._key_event(key, key_up=True))
        _hwnd, blocked = self._foreground_for_input(expected_gate_token)
        if blocked is not None:
            return blocked
        return self._send(events)

    def send_hotkey(
        self, keys: tuple[str, ...], expected_gate_token: str | None = None
    ) -> BackendActionResult:
        controller_blocked = self._controller_blocked("win32")
        if controller_blocked is not None:
            return controller_blocked
        if not keys or any(key not in _VK for key in keys):
            return BackendActionResult.unsupported("sendinput", "hotkey_not_supported")
        events = [self._key_event(key) for key in keys]
        events.extend(self._key_event(key, key_up=True) for key in reversed(keys))
        _hwnd, blocked = self._foreground_for_input(expected_gate_token)
        if blocked is not None:
            return blocked
        return self._send(events)


def make_optional_native_backend() -> NativeControlBackend | None:
    if os.name != "nt":
        return None
    try:
        return Win32ControlBackend()
    except Exception:
        return None


_AUTO_BACKEND = object()
_KEY_ALIASES = {
    "CONTROL": "CTRL",
    "RETURN": "ENTER",
    "ESC": "ESCAPE",
    "DEL": "DELETE",
    "PGUP": "PAGEUP",
    "PGDN": "PAGEDOWN",
}
_MODIFIER_ORDER = ("CTRL", "ALT", "SHIFT", "WIN")
_SAFE_KEYS = frozenset(
    {
        "ESCAPE",
        "TAB",
        "HOME",
        "END",
        "PAGEUP",
        "PAGEDOWN",
        "UP",
        "DOWN",
        "LEFT",
        "RIGHT",
        "F1",
    }
)
_SENSITIVE_KEYS = frozenset({"SPACE", "ENTER", "BACKSPACE", "DELETE"})
_SAFE_HOTKEYS = frozenset(
    {
        ("CTRL", "A"),
        ("CTRL", "C"),
        ("CTRL", "F"),
        ("CTRL", "L"),
        ("CTRL", "N"),
        ("CTRL", "T"),
        ("ALT", "TAB"),
        ("CTRL", "TAB"),
        ("CTRL", "SHIFT", "TAB"),
        ("SHIFT", "TAB"),
        ("WIN", "D"),
        ("WIN", "TAB"),
        ("ALT", "LEFT"),
        ("ALT", "RIGHT"),
    }
)
_SENSITIVE_HOTKEYS = frozenset(
    {
        ("CTRL", "P"),
        ("CTRL", "S"),
        ("CTRL", "V"),
    }
)
_DESTRUCTIVE_HOTKEYS = frozenset(
    {
        ("ALT", "F4"),
        ("CTRL", "F4"),
        ("CTRL", "W"),
        ("CTRL", "X"),
        ("CTRL", "Z"),
        ("CTRL", "Y"),
    }
)


def _canonical_key(value: str | None) -> str:
    key = (
        str(value or "")
        .strip()
        .upper()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )
    return _KEY_ALIASES.get(key, key)


def _canonical_hotkey(values: Sequence[str]) -> tuple[str, ...] | None:
    keys = tuple(_canonical_key(item) for item in values)
    if not keys or any(not item for item in keys) or len(set(keys)) != len(keys):
        return None
    modifiers = tuple(item for item in _MODIFIER_ORDER if item in keys)
    primary = tuple(item for item in keys if item not in _MODIFIER_ORDER)
    if len(primary) != 1:
        return None
    return (*modifiers, primary[0])


@dataclass(frozen=True, slots=True)
class _ConfirmationBinding:
    challenge: ConfirmationChallenge
    identity: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    fingerprint: tuple[object, ...]
    one_shot: _ConfirmationBinding | None
    start_epoch: int


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    fingerprint: tuple[object, ...]
    binding: _ConfirmationBinding
    deadline: float


class WindowsControlExecutor:
    """Policy gate and dispatcher for canonical Windows control requests."""

    def __init__(
        self,
        *,
        registry: ApplicationRegistry | None = None,
        native_backend: NativeControlBackend | None | object = _AUTO_BACKEND,
        uia_backend: UIAutomationControlBackend | None | object = _AUTO_BACKEND,
        confirmation_policy: ConfirmationPolicy | None = None,
        confirmation_ttl_seconds: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry if registry is not None else default_application_registry()
        self.native_backend = (
            make_optional_native_backend()
            if native_backend is _AUTO_BACKEND
            else cast(NativeControlBackend | None, native_backend)
        )
        self.uia_backend = (
            make_optional_uia_backend()
            if uia_backend is _AUTO_BACKEND
            else cast(UIAutomationControlBackend | None, uia_backend)
        )
        self.confirmation_policy = confirmation_policy or DenyConfirmationPolicy()
        self._confirmation_ttl_seconds = max(
            1.0, min(float(confirmation_ttl_seconds), 120.0)
        )
        self._clock = clock
        self._state_lock = threading.RLock()
        self._last_snapshot: NumberedElementSnapshot | None = None
        self._pending_confirmation: _PendingConfirmation | None = None
        self._cancel_epoch = 0
        self._snapshot_epoch = 0

    @staticmethod
    def _result(
        status: ResultStatus,
        intent: CanonicalIntent,
        *,
        backend: str = "none",
        target_id: str = "",
        reason: str = "",
        confirmation: bool = False,
    ) -> ControlResult:
        return ControlResult(status, intent, backend, target_id, reason, confirmation)

    def _from_backend(
        self,
        intent: CanonicalIntent,
        outcome: BackendActionResult,
        target_id: str,
    ) -> ControlResult:
        if outcome.status is BackendStatus.SUCCESS:
            status = ResultStatus.EXECUTED
        elif outcome.status is BackendStatus.BLOCKED:
            status = ResultStatus.REJECTED
        elif outcome.status is BackendStatus.UNSUPPORTED:
            status = ResultStatus.UNAVAILABLE
        elif outcome.status is BackendStatus.NOT_FOUND:
            status = ResultStatus.REJECTED
        else:
            status = ResultStatus.FAILED
        return self._result(
            status,
            intent,
            backend=outcome.backend,
            target_id=target_id,
            reason=outcome.reason_code,
        )

    def _require_confirmation(
        self,
        intent: CanonicalIntent,
        target_id: str,
        sensitivity: Sensitivity,
        reason_code: str,
        *,
        identity: tuple[object, ...],
        context: _ExecutionContext,
    ) -> ControlResult | None:
        challenge = ConfirmationChallenge(intent, sensitivity, target_id, reason_code)
        binding = _ConfirmationBinding(challenge, identity)
        if context.one_shot is not None:
            if context.one_shot != binding or not self._epoch_current(
                context.start_epoch
            ):
                return self._result(
                    ResultStatus.REJECTED,
                    intent,
                    reason="no_matching_pending_confirmation",
                )
            return None
        try:
            approved = bool(self.confirmation_policy.confirm(challenge))
        except Exception:
            approved = False
        if approved:
            return (
                None
                if self._epoch_current(context.start_epoch)
                else self._result(
                    ResultStatus.REJECTED,
                    intent,
                    reason="request_cancelled",
                )
            )
        with self._state_lock:
            if self._cancel_epoch != context.start_epoch:
                return self._result(
                    ResultStatus.REJECTED,
                    intent,
                    reason="request_cancelled",
                )
            self._pending_confirmation = _PendingConfirmation(
                context.fingerprint,
                binding,
                self._clock() + self._confirmation_ttl_seconds,
            )
        return self._result(
            ResultStatus.CONFIRMATION_REQUIRED,
            intent,
            target_id=target_id,
            reason=reason_code,
            confirmation=True,
        )

    def _epoch_current(self, epoch: int) -> bool:
        with self._state_lock:
            return self._cancel_epoch == epoch

    def cancel_pending(self) -> None:
        """Invalidate a pending or in-flight confirmation without backend I/O."""

        with self._state_lock:
            self._cancel_epoch += 1
            self._pending_confirmation = None

    def _foreground_gate(
        self,
    ) -> tuple[ForegroundWindowGate | None, BackendActionResult | None]:
        backend = self.native_backend
        gate_method = getattr(backend, "foreground_gate", None)
        if backend is None or not callable(gate_method):
            return None, BackendActionResult.blocked(
                "win32", "foreground_gate_unavailable"
            )
        try:
            gate, blocked = gate_method()
        except Exception:
            return None, BackendActionResult.blocked(
                "win32", "foreground_gate_failed"
            )
        if blocked is not None:
            return None, blocked
        if gate is None or not gate.token:
            return None, BackendActionResult.blocked(
                "win32", "foreground_identity_unknown"
            )
        return gate, None

    def clear_snapshot(self) -> None:
        """Release executor/backend snapshot metadata without waiting on UIA work."""

        with self._state_lock:
            self._snapshot_epoch += 1
            self._last_snapshot = None
        backend = self.uia_backend
        clear = getattr(backend, "clear_snapshot", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                pass

    def capture_numbered_elements(self) -> NumberedElementSnapshot | None:
        if self.uia_backend is None:
            self.clear_snapshot()
            return None
        gate, blocked = self._foreground_gate()
        if blocked is not None or gate is None:
            self.clear_snapshot()
            return None
        with self._state_lock:
            snapshot_epoch = self._snapshot_epoch
        try:
            snapshot = self.uia_backend.capture_elements(gate.token)
        except Exception:
            snapshot = None
        if snapshot is None:
            self.clear_snapshot()
            return None
        numbers = [item.number for item in snapshot.elements]
        if (
            not snapshot.snapshot_id
            or not snapshot.window_id
            or snapshot.gate_token != gate.token
            or len(numbers) != len(set(numbers))
            or any(number < 1 for number in numbers)
            or len(numbers) > 200
        ):
            self.clear_snapshot()
            return None
        with self._state_lock:
            if self._snapshot_epoch != snapshot_epoch:
                stale = True
            else:
                self._last_snapshot = snapshot
                stale = False
        if stale:
            self.clear_snapshot()
            return None
        return snapshot

    def is_snapshot_current(self, snapshot_id: str) -> bool:
        """Return whether the last numbered snapshot still owns foreground."""

        with self._state_lock:
            snapshot = self._last_snapshot
        if (
            self.uia_backend is None
            or snapshot is None
            or not snapshot_id
            or snapshot.snapshot_id != snapshot_id
        ):
            return False
        gate, blocked = self._foreground_gate()
        if (
            blocked is not None
            or gate is None
            or gate.token != snapshot.gate_token
        ):
            return False
        try:
            current = bool(self.uia_backend.is_snapshot_current(snapshot_id))
        except Exception:
            return False
        with self._state_lock:
            return bool(current and self._last_snapshot is snapshot)

    def _known_app(
        self, request: ControlRequest, *, required: bool
    ) -> tuple[KnownApplication | None, ControlResult | None]:
        if request.app_id is None and not required:
            return None, None
        app = self.registry.resolve(request.app_id)
        if app is None:
            return None, self._result(
                ResultStatus.REJECTED,
                request.intent,
                reason="application_not_allowlisted",
            )
        return app, None

    def _native_unavailable(self, request: ControlRequest) -> ControlResult:
        return self._result(
            ResultStatus.UNAVAILABLE,
            request.intent,
            reason="win32_backend_unavailable",
        )

    def _execute_app(self, request: ControlRequest) -> ControlResult:
        app, rejected = self._known_app(request, required=True)
        if rejected is not None:
            return rejected
        if self.native_backend is None:
            return self._native_unavailable(request)
        assert app is not None
        try:
            outcome = (
                self.native_backend.open_application(app)
                if request.intent is CanonicalIntent.OPEN_APP
                else self.native_backend.switch_application(app)
            )
        except Exception:
            outcome = BackendActionResult.failed("native")
        return self._from_backend(request.intent, outcome, app.app_id)

    def _execute_window(self, request: ControlRequest) -> ControlResult:
        app, rejected = self._known_app(request, required=False)
        if rejected is not None:
            return rejected
        if self.native_backend is None:
            return self._native_unavailable(request)
        action = {
            CanonicalIntent.MINIMIZE_WINDOW: WindowAction.MINIMIZE,
            CanonicalIntent.MAXIMIZE_WINDOW: WindowAction.MAXIMIZE,
            CanonicalIntent.RESTORE_WINDOW: WindowAction.RESTORE,
        }[request.intent]
        try:
            outcome = self.native_backend.window_action(action, app)
        except Exception:
            outcome = BackendActionResult.failed("native")
        return self._from_backend(
            request.intent, outcome, app.app_id if app is not None else "current"
        )

    def _execute_scroll(self, request: ControlRequest) -> ControlResult:
        try:
            direction = (
                request.direction
                if isinstance(request.direction, ScrollDirection)
                else ScrollDirection(str(request.direction or "").casefold())
            )
            amount = int(request.amount)
        except (TypeError, ValueError):
            return self._result(ResultStatus.REJECTED, request.intent, reason="invalid_scroll")
        if not 1 <= amount <= 10:
            return self._result(
                ResultStatus.REJECTED, request.intent, reason="scroll_out_of_bounds"
            )

        if self.uia_backend is not None:
            gate, blocked = self._foreground_gate()
            if blocked is not None or gate is None:
                assert blocked is not None
                return self._from_backend(request.intent, blocked, "current")
            try:
                semantic = self.uia_backend.scroll(direction, amount)
            except Exception:
                semantic = BackendActionResult.failed("uia")
            if semantic.status is BackendStatus.SUCCESS:
                return self._from_backend(request.intent, semantic, "current")
            if semantic.status is BackendStatus.BLOCKED:
                return self._from_backend(request.intent, semantic, "current")

        if self.native_backend is None:
            return self._native_unavailable(request)
        try:
            outcome = self.native_backend.scroll(direction, amount)
        except Exception:
            outcome = BackendActionResult.failed("native")
        return self._from_backend(request.intent, outcome, "current")

    def _execute_named_element(
        self, request: ControlRequest, context: _ExecutionContext
    ) -> ControlResult:
        if self.uia_backend is None:
            return self._result(
                ResultStatus.UNAVAILABLE, request.intent, reason="uia_backend_unavailable"
            )
        expected = _normalize_label(request.element_name or "")
        if not expected:
            return self._result(
                ResultStatus.REJECTED, request.intent, reason="element_name_required"
            )
        if context.one_shot is not None:
            # Confirmation is bound to the original immutable descriptors.
            # Re-capturing here could transfer approval to a same-named control
            # in a different window.
            with self._state_lock:
                snapshot = self._last_snapshot
        else:
            snapshot = self.capture_numbered_elements()
        if snapshot is None:
            status = (
                ResultStatus.REJECTED
                if context.one_shot is not None
                else ResultStatus.UNAVAILABLE
            )
            reason = (
                "no_matching_pending_confirmation"
                if context.one_shot is not None
                else "uia_snapshot_unavailable"
            )
            return self._result(status, request.intent, reason=reason)
        matches = [
            item
            for item in snapshot.elements
            if _normalize_label(item.name) == expected
            and item.enabled
            and not item.offscreen
            and item.invoke_supported
        ]
        if len(matches) != 1:
            reason = "element_not_found" if not matches else "element_name_ambiguous"
            return self._result(ResultStatus.REJECTED, request.intent, reason=reason)
        return self._invoke_element(request, snapshot, matches[0], context)

    def _execute_numbered_element(
        self, request: ControlRequest, context: _ExecutionContext
    ) -> ControlResult:
        with self._state_lock:
            snapshot = self._last_snapshot
        if (
            self.uia_backend is None
            or snapshot is None
            or not request.snapshot_id
            or request.snapshot_id != snapshot.snapshot_id
        ):
            return self._result(
                ResultStatus.REJECTED, request.intent, reason="stale_or_missing_snapshot"
            )
        try:
            number = int(request.element_number or 0)
        except (TypeError, ValueError):
            number = 0
        element = snapshot.by_number(number)
        if element is None:
            return self._result(
                ResultStatus.REJECTED, request.intent, reason="element_not_found"
            )
        if not element.enabled or element.offscreen or not element.invoke_supported:
            return self._result(
                ResultStatus.REJECTED, request.intent, reason="element_not_actionable"
            )
        return self._invoke_element(request, snapshot, element, context)

    @staticmethod
    def _stronger_sensitivity(
        request: ControlRequest, inherent: Sensitivity | None
    ) -> Sensitivity | None:
        requested = (
            Sensitivity(request.sensitivity)
            if request.sensitivity is not None
            else None
        )
        if Sensitivity.DESTRUCTIVE in {requested, inherent}:
            return Sensitivity.DESTRUCTIVE
        if Sensitivity.SENSITIVE in {requested, inherent}:
            return Sensitivity.SENSITIVE
        return None

    @staticmethod
    def _element_identity(
        snapshot: NumberedElementSnapshot, element: NumberedElement
    ) -> tuple[object, ...]:
        # Name is retained only inside the short-lived in-memory binding. It is
        # never copied to ControlResult, ConfirmationChallenge, or logs.
        return (
            "element",
            snapshot.snapshot_id,
            snapshot.window_id,
            snapshot.gate_token,
            element.number,
            element.name,
            element.role,
            element.bounds,
            element.runtime_id,
        )

    def _invoke_element(
        self,
        request: ControlRequest,
        snapshot: NumberedElementSnapshot,
        element: NumberedElement,
        context: _ExecutionContext,
    ) -> ControlResult:
        target_id = f"element:{snapshot.snapshot_id}:{element.number}"
        gate, blocked = self._foreground_gate()
        if blocked is not None or gate is None:
            assert blocked is not None
            return self._from_backend(request.intent, blocked, target_id)
        if gate.token != snapshot.gate_token:
            return self._result(
                ResultStatus.REJECTED,
                request.intent,
                target_id=target_id,
                reason="foreground_changed",
            )
        sensitivity = self._stronger_sensitivity(
            request,
            Sensitivity.DESTRUCTIVE
            if element.destructive
            else Sensitivity.SENSITIVE
            if element.role.casefold() in {"hyperlinkcontrol", "menuitemcontrol"}
            else None,
        )
        if sensitivity is not None:
            confirmation = self._require_confirmation(
                request.intent,
                target_id,
                sensitivity,
                "element_activation_requires_confirmation",
                identity=self._element_identity(snapshot, element),
                context=context,
            )
            if confirmation is not None:
                return confirmation
        gate_after, blocked_after = self._foreground_gate()
        if (
            blocked_after is not None
            or gate_after is None
            or gate_after.token != snapshot.gate_token
            or not self._epoch_current(context.start_epoch)
        ):
            return self._result(
                ResultStatus.REJECTED,
                request.intent,
                target_id=target_id,
                reason="foreground_changed_or_cancelled",
            )
        assert self.uia_backend is not None
        try:
            outcome = self.uia_backend.invoke(snapshot.snapshot_id, element.number)
        except Exception:
            outcome = BackendActionResult.failed("uia")
        return self._from_backend(request.intent, outcome, target_id)

    def _execute_key(
        self, request: ControlRequest, context: _ExecutionContext
    ) -> ControlResult:
        key = _canonical_key(request.key)
        if key not in _SAFE_KEYS and key not in _SENSITIVE_KEYS:
            return self._result(ResultStatus.REJECTED, request.intent, reason="unsafe_key")
        inherent = (
            Sensitivity.DESTRUCTIVE
            if key in {"DELETE", "BACKSPACE"}
            else Sensitivity.SENSITIVE if key in _SENSITIVE_KEYS else None
        )
        sensitivity = self._stronger_sensitivity(request, inherent)
        expected_gate_token: str | None = None
        if sensitivity is not None:
            gate, blocked = self._foreground_gate()
            if blocked is not None or gate is None:
                assert blocked is not None
                return self._from_backend(
                    request.intent, blocked, f"key:{key.casefold()}"
                )
            confirmation = self._require_confirmation(
                request.intent,
                f"key:{key.casefold()}",
                sensitivity,
                "key_requires_confirmation",
                identity=("key", key, gate.token),
                context=context,
            )
            if confirmation is not None:
                return confirmation
            gate_after, blocked_after = self._foreground_gate()
            if (
                blocked_after is not None
                or gate_after is None
                or gate_after.token != gate.token
                or not self._epoch_current(context.start_epoch)
            ):
                return self._result(
                    ResultStatus.REJECTED,
                    request.intent,
                    target_id=f"key:{key.casefold()}",
                    reason="foreground_changed_or_cancelled",
                )
            expected_gate_token = gate.token
        if self.native_backend is None:
            return self._native_unavailable(request)
        try:
            outcome = self.native_backend.send_key(key, expected_gate_token)
        except Exception:
            outcome = BackendActionResult.failed("native")
        return self._from_backend(request.intent, outcome, f"key:{key.casefold()}")

    def _execute_hotkey(
        self, request: ControlRequest, context: _ExecutionContext
    ) -> ControlResult:
        keys = _canonical_hotkey(request.keys)
        if keys is None or (
            keys not in _SAFE_HOTKEYS
            and keys not in _SENSITIVE_HOTKEYS
            and keys not in _DESTRUCTIVE_HOTKEYS
        ):
            return self._result(ResultStatus.REJECTED, request.intent, reason="unsafe_hotkey")
        target_id = "hotkey:" + "+".join(item.casefold() for item in keys)
        inherent = (
            Sensitivity.DESTRUCTIVE
            if keys in _DESTRUCTIVE_HOTKEYS
            else Sensitivity.SENSITIVE if keys in _SENSITIVE_HOTKEYS else None
        )
        sensitivity = self._stronger_sensitivity(request, inherent)
        expected_gate_token: str | None = None
        if sensitivity is not None:
            gate, blocked = self._foreground_gate()
            if blocked is not None or gate is None:
                assert blocked is not None
                return self._from_backend(request.intent, blocked, target_id)
            confirmation = self._require_confirmation(
                request.intent,
                target_id,
                sensitivity,
                "hotkey_requires_confirmation",
                identity=("hotkey", keys, gate.token),
                context=context,
            )
            if confirmation is not None:
                return confirmation
            gate_after, blocked_after = self._foreground_gate()
            if (
                blocked_after is not None
                or gate_after is None
                or gate_after.token != gate.token
                or not self._epoch_current(context.start_epoch)
            ):
                return self._result(
                    ResultStatus.REJECTED,
                    request.intent,
                    target_id=target_id,
                    reason="foreground_changed_or_cancelled",
                )
            expected_gate_token = gate.token
        if self.native_backend is None:
            return self._native_unavailable(request)
        try:
            outcome = self.native_backend.send_hotkey(keys, expected_gate_token)
        except Exception:
            outcome = BackendActionResult.failed("native")
        return self._from_backend(request.intent, outcome, target_id)

    @staticmethod
    def _request_fingerprint(request: ControlRequest) -> tuple[object, ...]:
        """Internal identity used only to bind one confirmation to one request."""

        return (
            str(request.intent),
            request.app_id,
            request.element_name,
            request.element_number,
            request.snapshot_id,
            str(request.sensitivity) if request.sensitivity is not None else None,
            str(request.direction) if request.direction is not None else None,
            request.amount,
            request.key,
            tuple(request.keys),
        )

    def execute_action(
        self,
        action: str,
        args: Mapping[str, object] | None = None,
        *,
        confirmed: bool = False,
    ) -> ControlResult:
        """Small future-facing adapter for ``voice_commands``.

        ``confirmed=True`` is never a blanket override: it is accepted only
        when the same canonical request previously returned
        ``confirmation_required`` from this executor instance.
        """

        try:
            request = ControlRequest.from_action(action, args)
        except (TypeError, ValueError):
            return self._result(
                ResultStatus.REJECTED,
                CanonicalIntent.CANCEL,
                reason="invalid_action_adapter_input",
            )
        return self.execute(request, confirmed=confirmed)

    def execute(
        self, request: ControlRequest, *, confirmed: bool = False
    ) -> ControlResult:
        try:
            intent = CanonicalIntent(request.intent)
        except (TypeError, ValueError):
            return ControlResult(
                ResultStatus.REJECTED,
                CanonicalIntent.CANCEL,
                reason_code="unknown_intent",
            )

        try:
            requested_sensitivity = (
                Sensitivity(request.sensitivity)
                if request.sensitivity is not None
                else None
            )
        except (TypeError, ValueError):
            return self._result(
                ResultStatus.REJECTED,
                intent,
                reason="invalid_sensitivity",
            )
        if requested_sensitivity is not None and intent not in {
            CanonicalIntent.INVOKE_NAMED_ELEMENT,
            CanonicalIntent.INVOKE_NUMBERED_ELEMENT,
            CanonicalIntent.SEND_KEY,
            CanonicalIntent.SEND_HOTKEY,
        }:
            return self._result(
                ResultStatus.REJECTED,
                intent,
                reason="sensitive_intent_not_confirmable",
            )

        fingerprint = self._request_fingerprint(request)
        with self._state_lock:
            if confirmed:
                pending = self._pending_confirmation
                if pending is not None and self._clock() > pending.deadline:
                    self._cancel_epoch += 1
                    self._pending_confirmation = None
                    return self._result(
                        ResultStatus.REJECTED,
                        intent,
                        reason="confirmation_expired",
                    )
                if pending is None or pending.fingerprint != fingerprint:
                    # A mismatched attempt consumes every prior approval
                    # opportunity and cannot create a fresh prompt.
                    self._cancel_epoch += 1
                    self._pending_confirmation = None
                    return self._result(
                        ResultStatus.REJECTED,
                        intent,
                        reason="no_matching_pending_confirmation",
                    )
                one_shot = pending.binding
                self._pending_confirmation = None
                start_epoch = self._cancel_epoch
            else:
                # Every new request invalidates an older pending or late
                # in-flight confirmation, even when the fingerprint matches.
                self._cancel_epoch += 1
                self._pending_confirmation = None
                start_epoch = self._cancel_epoch
                one_shot = None
        context = _ExecutionContext(fingerprint, one_shot, start_epoch)

        if intent is CanonicalIntent.HELP:
            return self._result(ResultStatus.NOOP, intent, reason="help_only")
        if intent is CanonicalIntent.CANCEL:
            return self._result(ResultStatus.NOOP, intent, reason="cancelled")
        if intent in (CanonicalIntent.OPEN_APP, CanonicalIntent.SWITCH_APP):
            return self._execute_app(request)
        if intent in (
            CanonicalIntent.MINIMIZE_WINDOW,
            CanonicalIntent.MAXIMIZE_WINDOW,
            CanonicalIntent.RESTORE_WINDOW,
        ):
            return self._execute_window(request)
        if intent is CanonicalIntent.SCROLL:
            return self._execute_scroll(request)
        if intent is CanonicalIntent.INVOKE_NAMED_ELEMENT:
            return self._execute_named_element(request, context)
        if intent is CanonicalIntent.INVOKE_NUMBERED_ELEMENT:
            return self._execute_numbered_element(request, context)
        if intent is CanonicalIntent.SEND_KEY:
            return self._execute_key(request, context)
        if intent is CanonicalIntent.SEND_HOTKEY:
            return self._execute_hotkey(request, context)
        return self._result(ResultStatus.REJECTED, intent, reason="unsupported_intent")
