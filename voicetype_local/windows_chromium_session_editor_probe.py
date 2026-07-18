from __future__ import annotations

"""Opt-in, isolated Chromium UIA probe for :mod:`session_editor`.

The probe launches an already-installed Chrome or Edge executable with a fresh
temporary profile and a local ``file:`` fixture.  The fixture contains only
neutral ``contenteditable`` fields and a restrictive CSP.  No user profile,
account, clipboard, browser automation protocol, remote URL or message-send
surface is used.

A real Chromium TextPattern can only receive Unicode SendInput while its field
owns the Windows input focus.  The probe therefore focuses its own off-screen
app window for the bounded test and restores the previously foreground window
in ``finally``.  It refuses to run if the exact isolated window or field cannot
be proven.
"""

import ctypes
import html
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path


_TITLE_PREFIX = "VoiceType isolated Chromium UIA probe"
_FIELD_SENTENCE = "VoiceType sentence edit fixture"
_FIELD_FULL = "VoiceType full delete fixture"
_FIELD_REPLACE = "VoiceType replace fixture"
_FIELD_FOREIGN = "VoiceType foreign prefix fixture"

_SENTENCE_PREFIX = "Existing sentence."
_SENTENCE_TRACKED = " First voice sentence. Second voice sentence."
_FULL_TRACKED = "Voice input"
_REPLACE_PREFIX = "Existing:"
_REPLACE_TRACKED = " alpha beta gamma"
_FOREIGN_PREFIX = "Foreign prefix."
_FOREIGN_TRACKED = " Voice input"


def _browser_candidates() -> tuple[Path, ...]:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    return (
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files_x86
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
    )


def _find_browser(explicit: str | os.PathLike[str] | None) -> Path | None:
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    override = os.environ.get("VOICETYPE_CHROMIUM_PATH", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    for candidate in _browser_candidates():
        if candidate.is_file():
            return candidate.resolve()
    return None


def _fixture(title: str) -> str:
    fields = (
        (_FIELD_SENTENCE, _SENTENCE_PREFIX + _SENTENCE_TRACKED),
        (_FIELD_FULL, _FULL_TRACKED),
        (_FIELD_REPLACE, _REPLACE_PREFIX + _REPLACE_TRACKED),
        (_FIELD_FOREIGN, _FOREIGN_PREFIX + _FOREIGN_TRACKED),
    )
    controls = "\n".join(
        (
            '<div role="textbox" contenteditable="true" tabindex="0" '
            f'aria-label="{html.escape(label, quote=True)}">'
            f"{html.escape(value)}</div>"
        )
        for label, value in fields
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>{html.escape(title)}</title>
  <style>
    html {{ background: #111827; color: #e5e7eb; font: 18px system-ui; }}
    body {{ margin: 24px; }}
    [role="textbox"] {{
      border: 1px solid #475569; border-radius: 8px; margin: 14px 0;
      min-height: 46px; padding: 12px; white-space: pre-wrap;
    }}
    [role="textbox"]:focus {{ border-color: #60a5fa; outline: 2px solid #1d4ed8; }}
  </style>
</head>
<body aria-label="VoiceType isolated local fixture">
  {controls}
</body>
</html>
"""


def _window_with_title_and_pid(title: str, pid: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = (
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    )
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    )
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    matches: list[int] = []

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if int(process_id.value) != int(pid):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if title in buffer.value:
            matches.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return matches[0] if len(matches) == 1 else 0


def _wait_for_window(title: str, process: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return 0
        hwnd = _window_with_title_and_pid(title, process.pid)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    return 0


def _field_controls(
    editor: object,
    root: object,
    labels: tuple[str, ...],
) -> dict[str, object] | None:
    # Chromium versions expose contenteditable as Edit, Document or Group.
    # Walk only the already-proven isolated window, match exact fixture-owned
    # aria-labels, and require one unique runtime id plus TextPattern per field.
    # This avoids the third-party library's process-global search cache.
    wanted = frozenset(labels)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        matches: dict[str, dict[tuple[int, ...], object]] = {
            label: {} for label in labels
        }
        stack: list[tuple[object, int]] = [(root, 0)]
        visited = 0
        while stack and visited < 512:
            candidate, depth = stack.pop()
            visited += 1
            try:
                name = str(getattr(candidate, "Name", ""))
            except Exception:
                name = ""
            if name in wanted:
                runtime_id = editor._runtime_id(candidate)  # type: ignore[attr-defined]
                if (
                    runtime_id is not None
                    and editor._text_pattern(candidate) is not None  # type: ignore[attr-defined]
                ):
                    matches[name][runtime_id] = candidate
            if depth >= 24:
                continue
            getter = getattr(candidate, "GetChildren", None)
            if not callable(getter):
                continue
            try:
                children = tuple(getter())
            except Exception:
                children = ()
            stack.extend((child, depth + 1) for child in reversed(children))
        if all(len(matches[label]) == 1 for label in labels):
            return {
                label: next(iter(matches[label].values())) for label in labels
            }
        time.sleep(0.1)
    return None


def _pattern_text(editor: object, control: object) -> str | None:
    pattern = editor._text_pattern(control)  # type: ignore[attr-defined]
    if pattern is None:
        return None
    document = getattr(pattern, "DocumentRange", None)
    getter = getattr(document, "GetText", None)
    if not callable(getter):
        return None
    try:
        value = getter(12_004)
    except Exception:
        return None
    return str("" if value is None else value).replace("\r\n", "\n").replace(
        "\r", "\n"
    )


def _visible_text_matches(editor: object, control: object, expected: str) -> bool:
    actual = _pattern_text(editor, control)
    return actual in {expected, expected + "\n"}


def _fixture_shape(editor: object, control: object, expected: str) -> str:
    """Privacy-safe shape code for the fixed neutral fixture only."""

    actual = _pattern_text(editor, control)
    if actual is None:
        return "pattern_unavailable"
    return (
        f"length_{len(actual)}_{len(expected)}"
        f"+exact_{int(actual == expected)}"
        f"+suffix_{int(actual.endswith(expected))}"
        f"+terminal_lf_{int(actual.endswith(expected + chr(10)))}"
        f"+lf_count_{actual.count(chr(10))}"
        f"+nbsp_count_{actual.count(chr(160))}"
    )


def _focus_at_end(
    automation: object,
    editor: object,
    inspector: object,
    control: object,
) -> object | None:
    set_focus = getattr(control, "SetFocus", None)
    if not callable(set_focus):
        return None
    try:
        if not bool(set_focus()):
            return None
    except Exception:
        return None
    pattern = editor._text_pattern(control)  # type: ignore[attr-defined]
    if pattern is None:
        return None
    document = getattr(pattern, "DocumentRange", None)
    if document is None:
        return None
    expected_runtime = editor._runtime_id(control)  # type: ignore[attr-defined]
    if expected_runtime is None:
        return None
    start_endpoint, end_endpoint, _unit = editor._endpoints(  # type: ignore[attr-defined]
        automation
    )
    for _ in range(20):
        # SetFocus() can asynchronously publish a default caret at the start.
        # Re-apply and then prove exactly one collapsed selection at the
        # logical/provider document end before capturing the target.
        caret = editor._caret_at_end(document)  # type: ignore[attr-defined]
        if caret is None or not editor._restore_caret(caret):  # type: ignore[attr-defined]
            return None
        time.sleep(0.05)
        try:
            focused = automation.GetFocusedControl()
        except Exception:
            focused = None
        selections = editor._selection(pattern)  # type: ignore[attr-defined]
        selection_at_end = bool(
            len(selections) == 1
            and editor._compare_endpoints(  # type: ignore[attr-defined]
                selections[0],
                start_endpoint,
                selections[0],
                end_endpoint,
            )
            == 0
            and editor._text_equivalent_endpoints(  # type: ignore[attr-defined]
                selections[0],
                start_endpoint,
                document,
                end_endpoint,
            )
        )
        if (
            focused is not None
            and editor._runtime_id(focused) == expected_runtime  # type: ignore[attr-defined]
            and selection_at_end
        ):
            target = inspector.capture()
            if target.stable and target.uia_runtime_id == expected_runtime:
                return target
    return None


def _restore_plan(before: str, after: str, action: object) -> object:
    from .session_editing import SessionEditPlan

    return SessionEditPlan(
        action=action,
        original_text=after,
        result_text=before,
        selected_text=after,
        replacement_text=before,
        start=0,
        end=len(after),
    )


def run_isolated_chromium_session_editor_probe(
    browser_path: str | os.PathLike[str] | None = None,
) -> tuple[int, str]:
    """Exercise session editing against a real, isolated Chromium TextPattern."""

    if os.name != "nt":
        return 80, "platform:not_windows"
    browser = _find_browser(browser_path)
    if browser is None:
        return 81, "browser:not_found"

    import uiautomation as automation

    from .focus import FocusInspector
    from .session_editing import (
        SessionEditAction,
        SessionEditRequest,
        plan_session_edit,
    )
    from .session_editor import SessionEditorStatus, SessionTextEditor
    from .voice_commands import CommandLanguage

    class ProbeSessionTextEditor(SessionTextEditor):
        """Record structural fixture-only selection proof diagnostics."""

        probe_selection_diagnostic = "not_checked"

        def _selection_matches_range(self, selected: object, expected: object) -> bool:
            try:
                start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
                selected_text = self._range_text(selected)
                expected_text = self._range_text(expected)
                raw_start = self._compare_endpoints(
                    selected, start_endpoint, expected, start_endpoint
                )
                raw_end = self._compare_endpoints(
                    selected, end_endpoint, expected, end_endpoint
                )
                alias_start = self._text_equivalent_endpoints(
                    selected, start_endpoint, expected, start_endpoint
                )
                alias_end = self._text_equivalent_endpoints(
                    selected, end_endpoint, expected, end_endpoint
                )
                self.probe_selection_diagnostic = (
                    f"length_{len(selected_text)}_{len(expected_text)}"
                    f"+text_{int(selected_text == expected_text)}"
                    f"+raw_{raw_start}_{raw_end}"
                    f"+alias_{int(alias_start)}_{int(alias_end)}"
                )
            except Exception as exc:
                self.probe_selection_diagnostic = (
                    f"exception_{type(exc).__name__}"
                )
            return super()._selection_matches_range(selected, expected)

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )
    user32.PostMessageW.restype = wintypes.BOOL

    previous_foreground = int(user32.GetForegroundWindow() or 0)
    token = uuid.uuid4().hex[:12]
    title = f"{_TITLE_PREFIX} {token}"
    work_dir = Path(tempfile.mkdtemp(prefix="voicetype-chromium-probe-"))
    profile = work_dir / "profile"
    fixture = work_dir / "fixture.html"
    profile.mkdir()
    fixture.write_text(_fixture(title), encoding="utf-8")
    command = (
        str(browser),
        f"--user-data-dir={profile}",
        f"--app={fixture.resolve().as_uri()}",
        "--force-renderer-accessibility",
        "--window-position=-32000,-32000",
        "--window-size=900,720",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-domain-reliability",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--safebrowsing-disable-auto-update",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--no-proxy-server",
        "--disable-features=AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,OptimizationHints,PrivacySandboxSettings4,Translate",
    )
    process: subprocess.Popen[bytes] | None = None
    hwnd = 0
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        hwnd = _wait_for_window(title, process)
        if not hwnd:
            return 82, "window:not_found"
        # Chromium does not expose a usable real TextPattern+SendInput target
        # on a headless/non-input desktop.  Focus only this unique off-screen
        # app window and verify every field by its UIA runtime id.
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE; already off-screen.
        root = automation.ControlFromHandle(hwnd)
        if root is None:
            return 83, "uia:root"

        # Chromium builds the renderer accessibility subtree lazily on some
        # releases even with --force-renderer-accessibility.  Activate only
        # the unique off-screen fixture window before discovering fields.
        user32.SetForegroundWindow(hwnd)
        root_focus = getattr(root, "SetFocus", None)
        if callable(root_focus):
            try:
                root_focus()
            except Exception:
                pass
        time.sleep(0.25)

        inspector = FocusInspector()
        editor = ProbeSessionTextEditor(focus_inspector=inspector)
        field_labels = (
            _FIELD_SENTENCE,
            _FIELD_FULL,
            _FIELD_REPLACE,
            _FIELD_FOREIGN,
        )
        controls = _field_controls(editor, root, field_labels)
        if controls is None:
            return 84, "uia:fields_not_unique"

        # 1. Delete the final sentence while preserving an earlier portion of
        # the same tracked insertion, then restore the one-shot transaction.
        sentence_control = controls[_FIELD_SENTENCE]
        sentence_target = _focus_at_end(
            automation, editor, inspector, sentence_control
        )
        if sentence_target is None:
            return 85, "focus:sentence"
        sentence_plan = plan_session_edit(
            _SENTENCE_TRACKED,
            SessionEditRequest(
                SessionEditAction.DELETE_LAST_SENTENCE,
                CommandLanguage.EN,
            ),
        )
        sentence_result = editor.apply(sentence_plan, sentence_target)
        if not sentence_result.succeeded:
            return 86, (
                f"edit:sentence:{sentence_result.reason_code}:"
                f"{sentence_result.backend}:"
                f"{_fixture_shape(editor, sentence_control, _SENTENCE_PREFIX + _SENTENCE_TRACKED)}:"
                f"{editor.probe_selection_diagnostic}"
            )
        sentence_after = _SENTENCE_PREFIX + sentence_plan.result_text
        if not _visible_text_matches(editor, sentence_control, sentence_after):
            return 87, "result:sentence"
        sentence_restore = editor.apply(
            _restore_plan(
                _SENTENCE_TRACKED,
                sentence_plan.result_text,
                SessionEditAction.RESTORE_LAST_EDIT,
            ),
            sentence_target,
        )
        if not sentence_restore.succeeded:
            return 88, f"restore:sentence:{sentence_restore.reason_code}"
        if not _visible_text_matches(
            editor,
            sentence_control,
            _SENTENCE_PREFIX + _SENTENCE_TRACKED,
        ):
            return 89, "result:sentence_restore"

        # 2. Full-field deletion must grant the typed empty-document capability
        # and the exact same empty field must accept a restore.
        full_control = controls[_FIELD_FULL]
        full_target = _focus_at_end(automation, editor, inspector, full_control)
        if full_target is None:
            return 90, "focus:full"
        full_plan = plan_session_edit(
            _FULL_TRACKED,
            SessionEditRequest(
                SessionEditAction.DELETE_LAST_DICTATION,
                CommandLanguage.EN,
            ),
        )
        full_result = editor.apply(full_plan, full_target)
        if not full_result.succeeded:
            return 91, f"edit:full:{full_result.reason_code}:{full_result.backend}"
        if not full_result.empty_document_verified:
            return 92, "proof:empty_document"
        if not _visible_text_matches(editor, full_control, ""):
            return 93, "result:full"
        full_restore = editor.apply(
            _restore_plan(
                _FULL_TRACKED,
                "",
                SessionEditAction.RESTORE_LAST_EDIT,
            ),
            full_target,
        )
        if not full_restore.succeeded:
            return 94, f"restore:full:{full_restore.reason_code}"
        if not _visible_text_matches(editor, full_control, _FULL_TRACKED):
            return 95, "result:full_restore"

        # 3. Replace a unique word inside the tracked suffix, then restore the
        # whole tracked suffix exactly as the application transaction does.
        replace_control = controls[_FIELD_REPLACE]
        replace_target = _focus_at_end(
            automation, editor, inspector, replace_control
        )
        if replace_target is None:
            return 96, "focus:replace"
        replace_plan = plan_session_edit(
            _REPLACE_TRACKED,
            SessionEditRequest(
                SessionEditAction.REPLACE_UNIQUE,
                CommandLanguage.EN,
                old_text="beta",
                new_text="BETA",
            ),
        )
        replace_result = editor.apply(replace_plan, replace_target)
        if not replace_result.succeeded:
            return 97, (
                f"edit:replace:{replace_result.reason_code}:"
                f"{replace_result.backend}"
            )
        if not _visible_text_matches(
            editor,
            replace_control,
            _REPLACE_PREFIX + replace_plan.result_text,
        ):
            return 98, "result:replace"
        replace_restore = editor.apply(
            _restore_plan(
                _REPLACE_TRACKED,
                replace_plan.result_text,
                SessionEditAction.RESTORE_LAST_EDIT,
            ),
            replace_target,
        )
        if not replace_restore.succeeded:
            return 99, f"restore:replace:{replace_restore.reason_code}"
        if not _visible_text_matches(
            editor,
            replace_control,
            _REPLACE_PREFIX + _REPLACE_TRACKED,
        ):
            return 100, "result:replace_restore"

        # 4. Deleting a tracked suffix after foreign text may succeed, but it
        # must never mint the typed full-empty restore capability.
        foreign_control = controls[_FIELD_FOREIGN]
        foreign_target = _focus_at_end(
            automation, editor, inspector, foreign_control
        )
        if foreign_target is None:
            return 101, "focus:foreign"
        foreign_plan = plan_session_edit(
            _FOREIGN_TRACKED,
            SessionEditRequest(
                SessionEditAction.DELETE_LAST_DICTATION,
                CommandLanguage.EN,
            ),
        )
        foreign_result = editor.apply(foreign_plan, foreign_target)
        if not foreign_result.succeeded:
            return 102, (
                f"edit:foreign:{foreign_result.reason_code}:"
                f"{foreign_result.backend}"
            )
        if foreign_result.empty_document_verified:
            return 103, "proof:foreign_prefix"
        if not _visible_text_matches(editor, foreign_control, _FOREIGN_PREFIX):
            return 104, "result:foreign"

        # 5. A target captured for one virtual field must be rejected after a
        # focus switch, before SessionTextEditor can select or alter text.
        switched_target = _focus_at_end(
            automation, editor, inspector, sentence_control
        )
        if switched_target is None:
            return 105, "focus:switch_source"
        switch_plan = plan_session_edit(
            _SENTENCE_TRACKED,
            SessionEditRequest(
                SessionEditAction.DELETE_LAST_WORD,
                CommandLanguage.EN,
            ),
        )
        if _focus_at_end(automation, editor, inspector, full_control) is None:
            return 106, "focus:switch_target"
        switch_result = editor.apply(switch_plan, switched_target)
        if switch_result.status is not SessionEditorStatus.REJECTED:
            return 107, "proof:field_switch"
        if not _visible_text_matches(
            editor,
            sentence_control,
            _SENTENCE_PREFIX + _SENTENCE_TRACKED,
        ):
            return 108, "result:field_switch"
        return 0, "ok"
    except Exception as exc:
        return 109, f"exception:{type(exc).__name__}"
    finally:
        if hwnd:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        if process is not None:
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3.0)
        if previous_foreground and user32.IsWindow(previous_foreground):
            user32.SetForegroundWindow(previous_foreground)
        shutil.rmtree(work_dir, ignore_errors=True)


__all__ = ["run_isolated_chromium_session_editor_probe"]
