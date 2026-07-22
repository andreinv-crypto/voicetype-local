from __future__ import annotations

from contextlib import nullcontext

import pytest

from voicetype_local.focus import FocusMatch, FocusTarget
from voicetype_local.inserter import TextInsertionError, UnicodeTextInserter
from voicetype_local.session_editing import (
    SessionEditAction,
    SessionEditPlan,
    SessionEditRequest,
    plan_session_edit,
)
from voicetype_local.session_editor import SessionEditorStatus, SessionTextEditor
from voicetype_local.targeted_inserter import (
    RICHEDIT_D2DPT_CLASS,
    TargetedInsertionResult,
    TargetedInsertionStatus,
    TargetedRichEditInserter,
)
from voicetype_local.voice_commands import CommandLanguage


class _Document:
    def __init__(self, text: str) -> None:
        self.text = text
        self.selection = (len(text), len(text))


class _Range:
    def __init__(self, document: _Document, start: int, end: int) -> None:
        self.document = document
        self.start = start
        self.end = end

    def Clone(self):
        return _Range(self.document, self.start, self.end)

    def CompareEndpoints(self, source: int, other, target: int) -> int:
        left = self.start if source == 0 else self.end
        right = other.start if target == 0 else other.end
        return (left > right) - (left < right)

    def MoveEndpointByUnit(
        self, endpoint: int, _unit: int, count: int, waitTime: float = 0
    ) -> int:
        del waitTime
        if endpoint == 0:
            old = self.start
            self.start = min(self.end, max(0, self.start + count))
            return self.start - old
        old = self.end
        self.end = max(self.start, min(len(self.document.text), self.end + count))
        return self.end - old

    def MoveEndpointByRange(
        self, endpoint: int, other, target: int, waitTime: float = 0
    ) -> bool:
        del waitTime
        value = other.start if target == 0 else other.end
        if endpoint == 0:
            self.start = value
            if self.end < self.start:
                self.end = self.start
        else:
            self.end = value
            if self.start > self.end:
                self.start = self.end
        return True

    def FindText(self, text: str, backward: bool, ignoreCase: bool):
        haystack = self.document.text[self.start : self.end]
        needle = text
        if ignoreCase:
            haystack = haystack.casefold()
            needle = needle.casefold()
        offset = haystack.rfind(needle) if backward else haystack.find(needle)
        if offset < 0:
            return None
        start = self.start + offset
        return _Range(self.document, start, start + len(text))

    def GetText(self, _max_length: int = -1) -> str:
        return self.document.text[self.start : self.end]

    def GetChildren(self):
        return []

    def Select(self, waitTime: float = 0) -> bool:
        del waitTime
        self.document.selection = (self.start, self.end)
        return True


class _Pattern:
    def __init__(self, document: _Document) -> None:
        self.document = document

    def GetSelection(self):
        start, end = self.document.selection
        return [_Range(self.document, start, end)]

    @property
    def DocumentRange(self):
        return _Range(self.document, 0, len(self.document.text))


class _Control:
    def __init__(self, document: _Document, *, password: bool = False) -> None:
        self.document = document
        self.IsPassword = password

    def GetRuntimeId(self):
        return (7, 8, 9)

    def GetTextPattern(self):
        return _Pattern(self.document)


class _Automation:
    class PatternId:
        TextPattern = 10014

    class TextPatternRangeEndpoint:
        Start = 0
        End = 1

    class TextUnit:
        Character = 0

    def __init__(self, control: _Control) -> None:
        self.control = control

    def UIAutomationInitializerInThread(self):
        return nullcontext()

    def GetFocusedControl(self):
        return self.control


class _Focus:
    def __init__(self, match: FocusMatch = FocusMatch.SAME) -> None:
        self.match = match

    def compare_current(self, _expected: FocusTarget) -> FocusMatch:
        return self.match


class _Inserter:
    def __init__(self, document: _Document) -> None:
        self.document = document

    def replace_selection(self, text: str) -> None:
        start, end = self.document.selection
        self.document.text = (
            self.document.text[:start] + text + self.document.text[end:]
        )
        caret = start + len(text)
        self.document.selection = (caret, caret)


def _target() -> FocusTarget:
    return FocusTarget(
        foreground_hwnd=1,
        root_hwnd=1,
        focused_hwnd=2,
        thread_id=3,
        process_id=4,
        focused_class_name="Edit",
        uia_runtime_id=(7, 8, 9),
        stable=True,
    )


class _ScrollRequiredRange(_Range):
    def __init__(
        self,
        document: _Document,
        start: int,
        end: int,
        *,
        focus: _Focus | None = None,
    ) -> None:
        super().__init__(document, start, end)
        self.focus = focus

    def Clone(self):
        return _ScrollRequiredRange(
            self.document,
            self.start,
            self.end,
            focus=self.focus,
        )

    def FindText(self, text: str, backward: bool, ignoreCase: bool):
        found = super().FindText(text, backward, ignoreCase)
        if found is None:
            return None
        return _ScrollRequiredRange(
            self.document,
            found.start,
            found.end,
            focus=self.focus,
        )

    def ScrollIntoView(self, _align_top: bool, waitTime: float = 0) -> bool:
        del waitTime
        self.document.scrolled = True  # type: ignore[attr-defined]
        if self.focus is not None:
            self.focus.match = FocusMatch.CHANGED
        return True

    def Select(self, waitTime: float = 0) -> bool:
        del waitTime
        if self.start == self.end:
            self.document.selection = (self.start, self.end)
            return True
        self.document.select_calls += 1  # type: ignore[attr-defined]
        select_after = getattr(self.document, "select_after", 1)
        if (
            not self.document.scrolled  # type: ignore[attr-defined]
            or self.document.select_calls < select_after  # type: ignore[attr-defined]
        ):
            return True
        self.document.selection = (self.start, self.end)
        return True


class _ScrollRequiredPattern(_Pattern):
    def __init__(self, document: _Document, *, focus: _Focus | None = None) -> None:
        super().__init__(document)
        self.focus = focus

    def GetSelection(self):
        start, end = self.document.selection
        return [
            _ScrollRequiredRange(
                self.document,
                start,
                end,
                focus=self.focus,
            )
        ]

    @property
    def DocumentRange(self):
        return _ScrollRequiredRange(
            self.document,
            0,
            len(self.document.text),
            focus=self.focus,
        )


class _ScrollRequiredControl(_Control):
    def __init__(self, document: _Document, *, focus: _Focus | None = None) -> None:
        super().__init__(document)
        self.focus = focus

    def GetTextPattern(self):
        return _ScrollRequiredPattern(self.document, focus=self.focus)


def test_editor_scrolls_exact_range_before_chromium_selection() -> None:
    document = _Document("Existing. Voice one. Voice two.")
    document.scrolled = False  # type: ignore[attr-defined]
    document.select_calls = 0  # type: ignore[attr-defined]
    control = _ScrollRequiredControl(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        " Voice one. Voice two.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.scrolled is True  # type: ignore[attr-defined]
    assert document.select_calls == 1  # type: ignore[attr-defined]
    assert document.text == "Existing. Voice one."


def test_editor_never_selects_if_focus_changes_after_scroll() -> None:
    document = _Document("Existing. Voice one. Voice two.")
    document.scrolled = False  # type: ignore[attr-defined]
    document.select_calls = 0  # type: ignore[attr-defined]
    focus = _Focus()
    control = _ScrollRequiredControl(document, focus=focus)
    editor = SessionTextEditor(
        _Inserter(document),
        focus,  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        " Voice one. Voice two.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed"
    assert document.scrolled is True  # type: ignore[attr-defined]
    assert document.select_calls == 0  # type: ignore[attr-defined]
    assert document.text == "Existing. Voice one. Voice two."


def test_editor_retries_one_ignored_chromium_selection_request(monkeypatch) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _value: None)
    document = _Document("Existing. Voice one. Voice two.")
    document.scrolled = False  # type: ignore[attr-defined]
    document.select_calls = 0  # type: ignore[attr-defined]
    document.select_after = 2  # type: ignore[attr-defined]
    control = _ScrollRequiredControl(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        " Voice one. Voice two.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.select_calls == 2  # type: ignore[attr-defined]
    assert document.text == "Existing. Voice one."


def test_compare_endpoints_uses_raw_com_ranges_for_uiautomation_2029() -> None:
    class _Raw:
        def __init__(self, value: int) -> None:
            self.value = value

        def CompareEndpoints(self, _source: int, other, _target: int) -> int:
            assert isinstance(other, _Raw)
            return (self.value > other.value) - (self.value < other.value)

    class _Wrapper:
        def __init__(self, value: int) -> None:
            self.textRange = _Raw(value)

        def CompareEndpoints(self, *_args):  # pragma: no cover - regression guard
            raise AssertionError("broken public wrapper must not be called")

    assert SessionTextEditor._compare_endpoints(_Wrapper(2), 0, _Wrapper(2), 0) == 0


class _AliasRange:
    def __init__(
        self,
        start: int,
        end: int,
        *,
        bridge_text: str = "",
        bridge_children: list[object] | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.bridge_text = bridge_text
        self.bridge_children = [] if bridge_children is None else bridge_children

    def Clone(self):
        return _AliasRange(
            self.start,
            self.end,
            bridge_text=self.bridge_text,
            bridge_children=list(self.bridge_children),
        )

    def CompareEndpoints(self, source: int, other, target: int) -> int:
        left = self.start if source == 0 else self.end
        right = other.start if target == 0 else other.end
        return (left > right) - (left < right)

    def MoveEndpointByRange(
        self, endpoint: int, other, target: int, waitTime: float = 0
    ) -> bool:
        del waitTime
        value = other.start if target == 0 else other.end
        if endpoint == 0:
            self.start = value
            if self.end < self.start:
                self.end = self.start
        else:
            self.end = value
            if self.start > self.end:
                self.start = self.end
        return True

    def GetText(self, _max_length: int = -1) -> str:
        return self.bridge_text if self.start != self.end else ""

    def GetChildren(self):
        return list(self.bridge_children)


def test_endpoint_alias_is_accepted_only_across_empty_childless_bridge() -> None:
    editor = SessionTextEditor(automation_module=_Automation(_Control(_Document(""))))
    leaf_end = _AliasRange(0, 10)
    container_caret = _AliasRange(11, 11)

    assert editor._text_equivalent_endpoints(leaf_end, 1, container_caret, 0)


@pytest.mark.parametrize("bridge_text", ("\n", " ", "\u00a0", "\ufffc"))
def test_endpoint_alias_refuses_any_text_between_positions(bridge_text: str) -> None:
    editor = SessionTextEditor(automation_module=_Automation(_Control(_Document(""))))
    leaf_end = _AliasRange(0, 10, bridge_text=bridge_text)
    container_caret = _AliasRange(11, 11, bridge_text=bridge_text)

    assert not editor._text_equivalent_endpoints(
        leaf_end,
        1,
        container_caret,
        0,
    )


def test_endpoint_alias_refuses_embedded_children_in_empty_bridge() -> None:
    editor = SessionTextEditor(automation_module=_Automation(_Control(_Document(""))))
    leaf_end = _AliasRange(0, 10, bridge_children=[object()])
    container_caret = _AliasRange(11, 11, bridge_children=[object()])

    assert not editor._text_equivalent_endpoints(
        leaf_end,
        1,
        container_caret,
        0,
    )


def test_editor_deletes_only_verified_last_sentence() -> None:
    document = _Document("Первое. Второе.")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.text == "Первое."
    assert document.selection == (len(document.text), len(document.text))


def test_editor_accepts_chromium_alias_caret_across_empty_text_bridge() -> None:
    document = _Document("Первое. Второе.")
    # Model Chromium returning a DOM-container caret one raw AX position after
    # the leaf-text end even though the text stream between them is empty.
    document.selection = (len(document.text) + 1, len(document.text) + 1)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.backend == "uia_text_endpoint_equivalent+sendinput_unicode"
    assert document.text == "Первое."


def test_editor_uses_generic_text_pattern_for_browser_control_wrapper() -> None:
    class _GenericControl:
        IsPassword = False

        def __init__(self, document: _Document) -> None:
            self.document = document
            self.requested_pattern_ids: list[int] = []

        def GetRuntimeId(self):
            return (7, 8, 9)

        def GetPattern(self, pattern_id: int):
            self.requested_pattern_ids.append(pattern_id)
            return _Pattern(self.document)

    document = _Document("Первое. Второе.")
    control = _GenericControl(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert control.requested_pattern_ids == [10014]
    assert document.text == "Первое."


def test_editor_accepts_only_generated_terminal_newline_in_owned_browser_field() -> None:
    document = _Document("Первое. Второе.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        "Первое. Второе.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.backend == "uia_text_owned_document+sendinput_unicode"
    assert document.text == "Первое.\n"


def test_owned_browser_field_accepts_one_leading_chromium_nbsp_alias() -> None:
    tracked = " Первое. Второе."
    document = _Document("\u00a0Первое. Второе.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.backend == "uia_text_owned_document+sendinput_unicode"
    assert document.text == "\u00a0Первое.\n"


@pytest.mark.parametrize(
    ("tracked", "provider_text"),
    (
        ("Первое предложение.", "\u00a0Первое предложение.\n"),
        ("Первое предложение.", "Первое\u00a0предложение.\n"),
        ("  Первое предложение.", "\u00a0 Первое предложение.\n"),
        ("\u00a0Первое предложение.", " Первое предложение.\n"),
    ),
)
def test_owned_browser_field_refuses_broader_nbsp_normalization(
    tracked: str,
    provider_text: str,
) -> None:
    document = _Document(provider_text)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == provider_text


def test_owned_browser_field_normalizes_crlf_in_multiline_tracked_text() -> None:
    tracked = "Первая строка.\r\nВторая строка."
    document = _Document(tracked + "\r\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    pattern = control.GetTextPattern()
    caret = pattern.GetSelection()[0]

    owned = editor._owned_document_range(pattern, tracked, caret)

    assert owned is not None
    assert owned.GetText(-1) == tracked


def test_editor_owned_browser_fallback_refuses_any_untracked_prefix() -> None:
    document = _Document("Чужой текст. Первое. Второе.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        "Первое. Второе.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == "Чужой текст. Первое. Второе.\n"


@pytest.mark.parametrize(
    "terminal",
    ("\n\n", " ", "\u00a0", "\u200b", "\u2028", "\u2029", "\ufffc"),
)
def test_editor_owned_browser_fallback_refuses_non_provider_terminal_text(
    terminal: str,
) -> None:
    tracked = "Первое. Второе."
    document = _Document(tracked + terminal)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == tracked + terminal


def test_editor_owned_browser_fallback_refuses_moved_caret() -> None:
    document = _Document("Первое. Второе.\n")
    document.selection = (len("Первое."), len("Первое."))
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        "Первое. Второе.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == "Первое. Второе.\n"


def test_editor_owned_browser_fallback_requires_exact_uia_runtime_id() -> None:
    document = _Document("Первое. Второе.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        "Первое. Второе.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )
    target = FocusTarget(
        foreground_hwnd=1,
        root_hwnd=1,
        focused_hwnd=2,
        thread_id=3,
        process_id=4,
        focused_class_name="Chrome_RenderWidgetHostHWND",
        uia_runtime_id=None,
        stable=True,
    )

    result = editor.apply(plan, target)

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == "Первое. Второе.\n"


def test_editor_owned_browser_fallback_verifies_full_deletion() -> None:
    document = _Document("Последняя диктовка.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        "Последняя диктовка.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.empty_document_verified is True
    assert document.text == "\n"


def test_editor_waits_for_delayed_owned_document_postcheck(monkeypatch) -> None:
    document = _Document("Последняя диктовка.\n")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    original_probe = editor._owned_document_is_empty
    probe_count = 0

    def delayed_probe(pattern: object) -> bool:
        nonlocal probe_count
        probe_count += 1
        if probe_count <= 6:
            return False
        return original_probe(pattern)

    monkeypatch.setattr(editor, "_owned_document_is_empty", delayed_probe)
    plan = plan_session_edit(
        "Последняя диктовка.",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.empty_document_verified is True
    assert probe_count >= 7
    assert document.text == "\n"


@pytest.mark.parametrize("provider_text", ("", "\n"))
def test_editor_restores_fully_deleted_dictation_only_into_owned_empty_field(
    provider_text: str,
) -> None:
    document = _Document(provider_text)
    # A browser's generated paragraph LF is not user text; its logical caret
    # remains at the empty field's insertion point before that provider LF.
    document.selection = (0, 0)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    restored = "Полностью удалённая диктовка."
    plan = SessionEditPlan(
        action=SessionEditAction.RESTORE_LAST_EDIT,
        original_text="",
        result_text=restored,
        selected_text="",
        replacement_text=restored,
        start=0,
        end=0,
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.empty_document_verified is False
    assert result.backend == "uia_text_owned_empty_document+sendinput_unicode"
    assert document.text == restored + provider_text


def test_editor_refuses_restore_from_empty_transaction_if_field_is_not_empty() -> None:
    document = _Document("Чужой текст")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    restored = "Не вставлять"
    plan = SessionEditPlan(
        action=SessionEditAction.RESTORE_LAST_EDIT,
        original_text="",
        result_text=restored,
        selected_text="",
        replacement_text=restored,
        start=0,
        end=0,
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == "Чужой текст"


def test_editor_refuses_empty_restore_when_exact_runtime_id_changed() -> None:
    document = _Document("")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    restored = "Не вставлять в другое поле"
    plan = SessionEditPlan(
        action=SessionEditAction.RESTORE_LAST_EDIT,
        original_text="",
        result_text=restored,
        selected_text="",
        replacement_text=restored,
        start=0,
        end=0,
    )
    changed_target = FocusTarget(
        foreground_hwnd=1,
        root_hwnd=1,
        focused_hwnd=2,
        thread_id=3,
        process_id=4,
        focused_class_name="Edit",
        uia_runtime_id=(99, 100, 101),
        stable=True,
    )

    result = editor.apply(plan, changed_target)

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed"
    assert document.text == ""


def test_normal_suffix_full_delete_never_upgrades_to_owned_empty_verification() -> None:
    class _CorruptInserter:
        def __init__(self, document: _Document) -> None:
            self.document = document

        def replace_selection(self, _text: str) -> None:
            self.document.text = ""
            self.document.selection = (0, 0)

    tracked = "Последняя диктовка."
    document = _Document("Чужой текст. " + tracked)
    control = _Control(document)
    editor = SessionTextEditor(
        _CorruptInserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_verification_failed"


def test_suffix_deletion_with_foreign_prefix_never_grants_empty_restore() -> None:
    tracked = " Voice input"
    document = _Document("Existing text." + tracked)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert result.empty_document_verified is False
    assert document.text == "Existing text."


def test_normal_suffix_partial_edit_never_upgrades_postcheck_to_owned_document() -> None:
    class _CorruptInserter:
        def __init__(self, document: _Document, result_text: str) -> None:
            self.document = document
            self.result_text = result_text

        def replace_selection(self, _text: str) -> None:
            self.document.text = self.result_text + "\n"
            self.document.selection = (
                len(self.document.text),
                len(self.document.text),
            )

    tracked = "Первое. Второе."
    prefix = "Чужой текст. "
    document = _Document(prefix + tracked + "\n")
    # A Chromium caret can be before its generated paragraph newline.  This
    # makes the pre-edit proof a normal suffix proof, not whole-field ownership.
    document.selection = (len(prefix + tracked), len(prefix + tracked))
    control = _Control(document)
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )
    editor = SessionTextEditor(
        _CorruptInserter(document, plan.result_text),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_verification_failed"


def test_editor_refuses_when_caret_is_not_after_tracked_text() -> None:
    document = _Document("Первое. Второе.")
    document.selection = (3, 3)
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "caret_or_text_changed"
    assert document.text == "Первое. Второе."


def test_editor_refuses_password_fields_before_selection() -> None:
    document = _Document("private")
    control = _Control(document, password=True)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "password_field_prohibited"
    assert document.selection == (len(document.text), len(document.text))


def test_editor_marks_partial_sendinput_as_uncertain() -> None:
    document = _Document("old text")
    control = _Control(document)

    class _PartialInserter:
        def replace_selection(self, _text: str) -> None:
            raise TextInsertionError("partial", partial=True)

    editor = SessionTextEditor(
        _PartialInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_LAST_DICTATION,
            CommandLanguage.EN,
            new_text="new text",
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_may_be_partial"


def test_editor_verifies_full_deletion_against_prefix_anchor() -> None:
    document = _Document("Existing text. Voice input")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        " Voice input",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.text == "Existing text."
    assert document.selection == (len(document.text), len(document.text))


def test_editor_does_not_accept_a_collapsed_noop_as_full_deletion() -> None:
    document = _Document("Voice input")
    control = _Control(document)

    class _NoopInserter:
        def replace_selection(self, _text: str) -> None:
            _start, end = document.selection
            document.selection = (end, end)

    editor = SessionTextEditor(
        _NoopInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_verification_failed"
    assert document.text == "Voice input"


def test_editor_accepts_full_deletion_when_prefix_repeats() -> None:
    document = _Document("one test test")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        " test",
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.text == "one test"


def test_editor_restores_original_caret_if_focus_flips_after_selection() -> None:
    document = _Document("First. Second.")
    control = _Control(document)

    class _FlipFocus:
        def __init__(self) -> None:
            self.calls = 0

        def compare_current(self, _expected: FocusTarget) -> FocusMatch:
            self.calls += 1
            # Initial verification plus the two scroll guards and refreshed
            # runtime-id proof pass; focus flips only after Select().
            return FocusMatch.SAME if self.calls <= 4 else FocusMatch.CHANGED

    editor = SessionTextEditor(
        _Inserter(document),
        _FlipFocus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_SENTENCE,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed_after_selection"
    assert document.text == "First. Second."
    assert document.selection == (len(document.text), len(document.text))


def test_editor_starts_no_sendinput_batch_if_focus_flips_at_input_gate() -> None:
    document = _Document("Voice input")
    control = _Control(document)

    class _FlipAtInputGate:
        def __init__(self) -> None:
            self.calls = 0

        def compare_current(self, _expected: FocusTarget) -> FocusMatch:
            self.calls += 1
            # Stay stable through exact selection read-back, then flip at the
            # immediately-adjacent SendInput authorization gate.
            return FocusMatch.SAME if self.calls <= 6 else FocusMatch.CHANGED

    sent: list[int] = []
    inserter = UnicodeTextInserter()
    inserter._send_input = (  # type: ignore[method-assign]
        lambda count, _events, _size: sent.append(count) or count
    )
    focus = _FlipAtInputGate()
    editor = SessionTextEditor(
        inserter,
        focus,  # type: ignore[arg-type]
        _Automation(control),
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.EN,
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed_after_selection"
    assert focus.calls == 7
    assert sent == []
    assert document.text == "Voice input"
    assert document.selection == (len(document.text), len(document.text))


def test_editor_rejects_same_text_selected_at_a_different_range() -> None:
    document = _Document("same same")

    class _MovedSelectionPattern(_Pattern):
        def __init__(self, value: _Document) -> None:
            super().__init__(value)
            self.calls = 0

        def GetSelection(self):
            self.calls += 1
            if self.calls >= 2:
                return [_Range(self.document, 0, 4)]
            return super().GetSelection()

    pattern = _MovedSelectionPattern(document)

    class _PatternControl(_Control):
        def GetTextPattern(self):
            return pattern

    class _MustNotInsert:
        def replace_selection(self, _text: str) -> None:
            raise AssertionError("input must not run for the wrong range")

    editor = SessionTextEditor(
        _MustNotInsert(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_PatternControl(document)),
    )
    plan = plan_session_edit(
        "same",
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.EN,
            old_text="same",
            new_text="changed",
        ),
    )

    result = editor.apply(plan, _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "selection_changed"
    assert document.text == "same same"
    assert document.selection == (len(document.text), len(document.text))


def test_editor_moves_caret_to_end_after_middle_replacement() -> None:
    document = _Document("prefix alpha beta gamma")
    control = _Control(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )
    tracked = " alpha beta gamma"
    plan = plan_session_edit(
        tracked,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.EN,
            old_text="beta",
            new_text="BETA-LONG",
        ),
    )

    result = editor.apply(plan, _target())

    assert result.succeeded
    assert document.text == "prefix alpha BETA-LONG gamma"
    assert document.selection == (len(document.text), len(document.text))


def _targeted_target(
    *,
    class_name: str = RICHEDIT_D2DPT_CLASS,
    runtime_id: tuple[int, ...] | None = (7, 8, 9),
) -> FocusTarget:
    return FocusTarget(
        foreground_hwnd=1,
        root_hwnd=1,
        focused_hwnd=2,
        thread_id=3,
        process_id=4,
        focused_class_name=class_name,
        uia_runtime_id=runtime_id,
        stable=True,
    )


class _TargetedTestInserter:
    def __init__(
        self,
        document: _Document,
        result: TargetedInsertionResult | None = None,
        *,
        apply_replacement: bool = True,
        caret_override: tuple[int, int] | None = None,
    ) -> None:
        self.document = document
        self.result = result or TargetedInsertionResult.accepted()
        self.apply_replacement = apply_replacement
        self.caret_override = caret_override
        self.calls: list[tuple[str, int, str]] = []
        self.guard_results: list[bool] = []

    @staticmethod
    def normalize_text(text: str) -> str:
        return TargetedRichEditInserter.normalize_text(text)

    def insert(
        self,
        text: str,
        *,
        hwnd: int,
        class_name: str,
        before_call,
    ) -> TargetedInsertionResult:
        allowed = bool(before_call())
        self.guard_results.append(allowed)
        if not allowed:
            return TargetedInsertionResult.rejected("guard_rejected")
        self.calls.append((text, hwnd, class_name))
        if (
            self.result.status is TargetedInsertionStatus.ACCEPTED
            and self.apply_replacement
        ):
            start, end = self.document.selection
            self.document.text = (
                self.document.text[:start] + text + self.document.text[end:]
            )
            caret = start + len(text)
            self.document.selection = (caret, caret)
            if self.caret_override is not None:
                self.document.selection = self.caret_override
        return self.result


def test_targeted_insert_accepts_exact_collapsed_selection() -> None:
    document = _Document("prefix suffix")
    document.selection = (7, 7)
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        "новый ",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.backend == "uia_text+richedit_targeted"
    assert document.text == "prefix новый suffix"
    assert targeted.guard_results == [True]
    assert targeted.calls == [("новый ", 2, RICHEDIT_D2DPT_CLASS)]


def test_targeted_insert_replaces_one_nonempty_selection() -> None:
    document = _Document("before OLD after")
    document.selection = (7, 10)
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        "new\r\nline",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.EXECUTED
    assert document.text == "before new\nline after"
    assert targeted.calls == [("new\nline", 2, RICHEDIT_D2DPT_CLASS)]


def test_targeted_insert_rejects_selection_change_at_native_guard() -> None:
    document = _Document("same same")
    document.selection = (5, 9)

    class _MovedSelectionPattern(_Pattern):
        def __init__(self, value: _Document) -> None:
            super().__init__(value)
            self.calls = 0

        def GetSelection(self):
            self.calls += 1
            if self.calls >= 2:
                return [_Range(self.document, 0, 4)]
            return super().GetSelection()

    pattern = _MovedSelectionPattern(document)

    class _PatternControl(_Control):
        def GetTextPattern(self):
            return pattern

    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_PatternControl(document)),
    )

    result = editor.insert_targeted(
        "changed",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "selection_changed"
    assert targeted.guard_results == [False]
    assert targeted.calls == []
    assert document.text == "same same"


def test_targeted_insert_rejects_focus_change_at_native_guard() -> None:
    document = _Document("safe")

    class _FocusChangesAtGuard:
        def __init__(self) -> None:
            self.calls = 0

        def compare_current(self, _expected: FocusTarget) -> FocusMatch:
            self.calls += 1
            return FocusMatch.SAME if self.calls == 1 else FocusMatch.CHANGED

    focus = _FocusChangesAtGuard()
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        focus,  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed"
    assert focus.calls == 2
    assert targeted.calls == []
    assert document.text == "safe"


def test_targeted_insert_rejects_invalidated_request_at_native_guard() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(document)
    checks = 0

    def is_current() -> bool:
        nonlocal checks
        checks += 1
        # Initial preflight and guard entry are current; the request becomes
        # stale while the guard performs its UIA selection/focus proof.
        return checks < 3

    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
        is_current=is_current,
    )

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "request_cancelled"
    assert checks == 3
    assert targeted.guard_results == [False]
    assert targeted.calls == []
    assert document.text == "safe"


def test_targeted_insert_timeout_is_uncertain() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(
        document,
        TargetedInsertionResult.timeout(),
    )
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "targeted_insert_timeout"
    assert targeted.calls == [(" text", 2, RICHEDIT_D2DPT_CLASS)]


def test_targeted_insert_unsafe_native_rejection_is_uncertain() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(
        document,
        TargetedInsertionResult.rejected(
            "message_rejected",
            postcheck_required=True,
            safe_to_retry=False,
            native_error=5,
        ),
    )
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "targeted_result_unknown"
    assert targeted.calls == [(" text", 2, RICHEDIT_D2DPT_CLASS)]


def test_targeted_insert_postcheck_mismatch_is_uncertain(monkeypatch) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _value: None)
    document = _Document("safe")
    targeted = _TargetedTestInserter(document, apply_replacement=False)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "targeted_postcheck_failed"
    assert document.text == "safe"


def test_targeted_insert_exact_text_with_wrong_caret_is_uncertain(monkeypatch) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _value: None)
    document = _Document("prefix suffix")
    document.selection = (7, 7)
    targeted = _TargetedTestInserter(document, caret_override=(0, 0))
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        "new ",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "targeted_postcheck_failed"
    assert document.text == "prefix new suffix"
    assert document.selection == (0, 0)


def test_targeted_insert_rejects_large_document_before_side_effect() -> None:
    document = _Document("x" * 65_537)
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        "y",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "document_too_large"
    assert targeted.guard_results == []
    assert targeted.calls == []


def test_targeted_insert_counts_non_bmp_text_as_utf16_units() -> None:
    document = _Document("😀" * 32_769)
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        "y",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "document_too_large"
    assert targeted.guard_results == []
    assert targeted.calls == []


def test_targeted_insert_rejects_password_runtime_and_initial_focus() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(document)

    password_editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document, password=True)),
    )
    password = password_editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    class _OtherRuntimeControl(_Control):
        def GetRuntimeId(self):
            return (99,)

    runtime_editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_OtherRuntimeControl(document)),
    )
    runtime = runtime_editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    focus_editor = SessionTextEditor(
        _Inserter(document),
        _Focus(FocusMatch.CHANGED),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )
    focus = focus_editor.insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert (password.status, password.reason_code) == (
        SessionEditorStatus.REJECTED,
        "password_field_prohibited",
    )
    assert (runtime.status, runtime.reason_code) == (
        SessionEditorStatus.REJECTED,
        "focus_changed",
    )
    assert (focus.status, focus.reason_code) == (
        SessionEditorStatus.REJECTED,
        "focus_changed",
    )
    assert targeted.calls == []


def test_targeted_insert_allows_only_exact_richedit_class() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_targeted(
        " text",
        _targeted_target(class_name="richeditd2dpt"),
        targeted,  # type: ignore[arg-type]
    )

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "targeted_class_unsupported"
    assert targeted.calls == []


def test_targeted_insert_requires_uia_and_text_pattern() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(document)
    no_uia = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        None,
    ).insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    no_pattern = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_NoPatternControl(document)),
    ).insert_targeted(
        " text",
        _targeted_target(),
        targeted,  # type: ignore[arg-type]
    )

    assert (no_uia.status, no_uia.reason_code) == (
        SessionEditorStatus.UNAVAILABLE,
        "uia_unavailable",
    )
    assert (no_pattern.status, no_pattern.reason_code) == (
        SessionEditorStatus.UNAVAILABLE,
        "text_pattern_unavailable",
    )
    assert targeted.calls == []


def test_targeted_insert_shares_session_editor_operation_lock() -> None:
    document = _Document("safe")
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )
    assert editor._operation_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        result = editor.insert_targeted(
            " text",
            _targeted_target(),
            targeted,  # type: ignore[arg-type]
        )
    finally:
        editor._operation_lock.release()  # type: ignore[attr-defined]

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "session_editor_busy"
    assert targeted.calls == []


def test_verify_insertion_confirms_exact_suffix_without_modifying_field() -> None:
    document = _Document("Чужой текст. Точная вставка")
    original_selection = document.selection
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.verify_insertion("Точная вставка", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok"
    assert result.backend == "uia_text_postcheck:suffix_exact"
    assert document.text == "Чужой текст. Точная вставка"
    assert document.selection == original_selection


def test_verify_insertion_missing_text_is_uncertain(monkeypatch) -> None:
    monkeypatch.setattr(
        "voicetype_local.session_editor.time.sleep",
        lambda _value: None,
    )
    document = _Document("Другой текст")
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.verify_insertion("Ожидаемая вставка", _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "insertion_verification_failed"
    assert document.text == "Другой текст"


def test_verify_insertion_polls_for_delayed_provider_update(monkeypatch) -> None:
    document = _Document("До вставки: ")
    sleeps = 0

    def publish_update(_value: float) -> None:
        nonlocal sleeps
        sleeps += 1
        document.text = "До вставки: готово"
        document.selection = (len(document.text), len(document.text))

    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", publish_update)
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.verify_insertion("готово", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok"
    assert sleeps == 1
    assert document.text == "До вставки: готово"


def test_verify_insertion_rejects_changed_focus_without_reading_text() -> None:
    document = _Document("Ожидаемая вставка")
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(FocusMatch.CHANGED),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.verify_insertion("Ожидаемая вставка", _target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed"
    assert document.text == "Ожидаемая вставка"


def test_verify_insertion_unavailable_text_pattern_is_uncertain(monkeypatch) -> None:
    monkeypatch.setattr(
        "voicetype_local.session_editor.time.sleep",
        lambda _value: None,
    )
    document = _Document("Ожидаемая вставка")

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_NoPatternControl(document)),
    )

    result = editor.verify_insertion("Ожидаемая вставка", _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "text_pattern_unavailable"
    assert document.text == "Ожидаемая вставка"


def test_verify_insertion_honours_cancellation_during_polling(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "voicetype_local.session_editor.time.sleep",
        lambda value: sleeps.append(value),
    )
    document = _Document("Текст ещё не обновился")
    checks = 0

    def is_current() -> bool:
        nonlocal checks
        checks += 1
        # Initial validation and the first UIA probe are current. The request
        # is cancelled before the second probe.
        return checks < 3

    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.verify_insertion(
        "Ожидаемая вставка",
        _target(),
        is_current=is_current,
    )

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "request_cancelled"
    assert checks == 3
    assert len(sleeps) == 1
    assert document.text == "Текст ещё не обновился"


def test_verify_insertion_nonblocking_lock_reports_uncertain() -> None:
    document = _Document("Ожидаемая вставка")
    editor = SessionTextEditor(
        _Inserter(document),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )
    assert editor._operation_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        result = editor.verify_insertion("Ожидаемая вставка", _target())
    finally:
        editor._operation_lock.release()  # type: ignore[attr-defined]

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "session_editor_busy"
    assert document.text == "Ожидаемая вставка"


class _VerifiedInputInserter:
    def __init__(
        self,
        document: _Document,
        *,
        chunk_size: int | None = None,
        noop: bool = False,
    ) -> None:
        self.document = document
        self.chunk_size = chunk_size
        self.noop = noop
        self.calls: list[str] = []
        self.guard_results: list[bool] = []

    def insert(self, text: str, *, before_batch) -> None:
        self.calls.append(text)
        size = self.chunk_size or max(1, len(text))
        inserted_any = False
        for offset in range(0, len(text), size):
            chunk = text[offset : offset + size]
            allowed = bool(before_batch())
            self.guard_results.append(allowed)
            if not allowed:
                raise TextInsertionError(
                    "guard rejected",
                    partial=inserted_any,
                    reason_code="input_guard_rejected",
                )
            if self.noop:
                continue
            start, end = self.document.selection
            self.document.text = (
                self.document.text[:start]
                + chunk
                + self.document.text[end:]
            )
            caret = start + len(chunk)
            self.document.selection = (caret, caret)
            inserted_any = True

    def replace_selection(self, _text: str) -> None:
        raise AssertionError("insert_verified must use inserter.insert")


class _GuardedAtomicInserter:
    """Model the one-call production inserter without hiding guard timing."""

    def __init__(
        self,
        document: _Document,
        *,
        outcome: str = "success",
        before_guard=None,
        after_input=None,
    ) -> None:
        self.document = document
        self.outcome = outcome
        self.before_guard = before_guard
        self.after_input = after_input
        self.atomic_calls = 0
        self.native_calls = 0
        self.calls: list[str] = []
        self.guard_results: list[bool] = []

    def insert(self, _text: str, *, before_batch) -> None:
        del before_batch
        raise AssertionError("unverified fallback must never use chunked insert")

    def insert_atomic(self, text: str, *, before_batch) -> None:
        self.atomic_calls += 1
        self.calls.append(text)
        if self.before_guard is not None:
            self.before_guard()
        allowed = bool(before_batch())
        self.guard_results.append(allowed)
        if not allowed:
            raise TextInsertionError(
                "guard rejected",
                partial=False,
                reason_code="input_guard_rejected",
            )

        # This counter represents crossing the one native SendInput boundary.
        self.native_calls += 1
        if self.outcome == "zero":
            raise TextInsertionError("zero events accepted", partial=False)
        if self.outcome == "partial":
            start, end = self.document.selection
            self.document.text = (
                self.document.text[:start] + text[:1] + self.document.text[end:]
            )
            self.document.selection = (start + 1, start + 1)
            raise TextInsertionError("partial native result", partial=True)
        if self.outcome == "unknown":
            raise RuntimeError("native result unavailable")
        if self.outcome == "accepted_noop":
            if self.after_input is not None:
                self.after_input()
            return

        start, end = self.document.selection
        self.document.text = (
            self.document.text[:start] + text + self.document.text[end:]
        )
        caret = start + len(text)
        self.document.selection = (caret, caret)
        if self.after_input is not None:
            self.after_input()

    def replace_selection(self, _text: str) -> None:
        raise AssertionError("insert_verified must use insert_atomic")


def test_insert_verified_rejects_identical_preexisting_suffix_noop(
    monkeypatch,
) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _v: None)
    document = _Document("already")
    inserter = _VerifiedInputInserter(document, noop=True)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("already", _target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "insertion_delta_unverified"
    assert document.text == "already"
    assert inserter.calls == ["already"]
    assert inserter.guard_results == [True]


def test_insert_verified_normal_append_succeeds() -> None:
    document = _Document("before ")
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("after", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok"
    assert result.backend == "uia_text_delta+sendinput_unicode:suffix_exact"
    assert document.text == "before after"
    assert document.selection == (len(document.text), len(document.text))


def test_insert_verified_repeated_identical_append_succeeds() -> None:
    document = _Document("echo")
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("echo", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert document.text == "echoecho"
    assert document.selection == (8, 8)


def test_insert_verified_replaces_noncollapsed_selection() -> None:
    document = _Document("before OLD after")
    document.selection = (7, 10)
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("new", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert document.text == "before new after"
    assert document.selection == (10, 10)


def test_insert_verified_handles_long_non_bmp_and_newline_text() -> None:
    document = _Document("prefix:")
    raw = ("😀 строка\r\n" * 80) + "конец"
    normalized = raw.replace("\r\n", "\n")
    inserter = _VerifiedInputInserter(document, chunk_size=73)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(raw, _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert document.text == "prefix:" + normalized
    assert inserter.calls == [normalized]
    assert len(inserter.guard_results) > 1
    assert all(inserter.guard_results)


def test_insert_verified_prefers_single_call_atomic_inserter() -> None:
    document = _Document("prefix:")
    raw = ("😀 строка\r\n" * 80) + "конец"
    normalized = raw.replace("\r\n", "\n")

    class _AtomicInserter(_VerifiedInputInserter):
        atomic_calls = 0

        def insert(self, _text: str, *, before_batch) -> None:
            del before_batch
            raise AssertionError("legacy chunked insert must not run")

        def insert_atomic(self, text: str, *, before_batch) -> None:
            self.atomic_calls += 1
            self.calls.append(text)
            allowed = bool(before_batch())
            self.guard_results.append(allowed)
            if not allowed:
                raise TextInsertionError(
                    "guard rejected",
                    partial=False,
                    reason_code="input_guard_rejected",
                )
            start, end = self.document.selection
            self.document.text = (
                self.document.text[:start] + text + self.document.text[end:]
            )
            caret = start + len(text)
            self.document.selection = (caret, caret)

    inserter = _AtomicInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(raw, _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok"
    assert document.text == "prefix:" + normalized
    assert inserter.calls == [normalized]
    assert inserter.atomic_calls == 1
    assert inserter.guard_results == [True]


def test_insert_verified_atomic_focus_loss_after_input_is_success_unverified() -> None:
    """Live regression: completed input must not enter pending/retry state."""

    document = _Document("safe ")
    focus = _Focus()
    inserter = _GuardedAtomicInserter(
        document,
        after_input=lambda: setattr(focus, "match", FocusMatch.CHANGED),
    )
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        focus,  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("text", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok_unverified"
    assert "guarded_unverified" in result.backend
    assert "focus_changed" in result.backend
    assert document.text == "safe text"
    assert inserter.atomic_calls == 1
    assert inserter.native_calls == 1
    assert inserter.guard_results == [True]


def test_insert_verified_atomic_missing_postcheck_pattern_is_success_unverified(
    monkeypatch,
) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _v: None)
    document = _Document("safe ")

    class _PatternDisappearsControl(_Control):
        def __init__(self, document: _Document) -> None:
            super().__init__(document)
            self.pattern_calls = 0

        def GetTextPattern(self):
            self.pattern_calls += 1
            return _Pattern(self.document) if self.pattern_calls <= 2 else None

    control = _PatternDisappearsControl(document)
    inserter = _GuardedAtomicInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(control),
    )

    result = editor.insert_verified("text", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok_unverified"
    assert "guarded_unverified" in result.backend
    assert "text_pattern_unavailable" in result.backend
    assert document.text == "safe text"
    assert inserter.atomic_calls == 1


def test_insert_verified_atomic_unprovable_delta_is_success_unverified(
    monkeypatch,
) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _v: None)
    document = _Document("safe")
    inserter = _GuardedAtomicInserter(document, outcome="accepted_noop")
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("text", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok_unverified"
    assert "guarded_unverified" in result.backend
    assert inserter.atomic_calls == 1
    assert inserter.native_calls == 1


def test_insert_verified_atomic_postcheck_exception_is_success_unverified() -> None:
    document = _Document("safe ")

    class _PostcheckRaisesFocus(_Focus):
        fail = False

        def compare_current(self, expected: FocusTarget) -> FocusMatch:
            if self.fail:
                raise RuntimeError("UIA focus provider disappeared")
            return super().compare_current(expected)

    focus = _PostcheckRaisesFocus()
    inserter = _GuardedAtomicInserter(
        document,
        after_input=lambda: setattr(focus, "fail", True),
    )
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        focus,  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified("text", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok_unverified"
    assert result.backend.endswith("guarded_unverified:postcheck_exception")
    assert document.text == "safe text"
    assert inserter.atomic_calls == 1


def test_insert_verified_atomic_cancellation_after_input_stays_uncertain() -> None:
    document = _Document("safe ")
    current = True

    def cancel_after_input() -> None:
        nonlocal current
        current = False

    inserter = _GuardedAtomicInserter(document, after_input=cancel_after_input)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(
        "text",
        _target(),
        is_current=lambda: current,
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "request_cancelled_after_input"
    assert document.text == "safe text"
    assert inserter.atomic_calls == 1


def test_insert_verified_no_text_pattern_uses_one_guarded_atomic_call() -> None:
    document = _Document("before ")

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    inserter = _GuardedAtomicInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_NoPatternControl(document)),
    )

    result = editor.insert_verified("after", _target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.reason_code == "ok_unverified"
    assert "guarded_unverified" in result.backend
    assert document.text == "before after"
    assert inserter.calls == ["after"]
    assert inserter.atomic_calls == 1
    assert inserter.native_calls == 1
    assert inserter.guard_results == [True]


@pytest.mark.parametrize("boundary_change", ["focus", "cancel"])
def test_insert_verified_no_text_pattern_rejects_boundary_change_without_input(
    boundary_change: str,
) -> None:
    document = _Document("safe")

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    focus = _Focus()
    current = True

    def change_at_boundary() -> None:
        nonlocal current
        if boundary_change == "focus":
            focus.match = FocusMatch.CHANGED
        else:
            current = False

    inserter = _GuardedAtomicInserter(document, before_guard=change_at_boundary)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        focus,  # type: ignore[arg-type]
        _Automation(_NoPatternControl(document)),
    )

    result = editor.insert_verified(
        " text",
        _target(),
        is_current=lambda: current,
    )

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == (
        "focus_changed" if boundary_change == "focus" else "request_cancelled"
    )
    assert "guarded_unverified" in result.backend
    assert inserter.atomic_calls == 1
    assert inserter.native_calls == 0
    assert inserter.guard_results == [False]
    assert document.text == "safe"


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_reason"),
    [
        ("zero", SessionEditorStatus.FAILED, "insertion_input_failed"),
        ("partial", SessionEditorStatus.UNCERTAIN, "insertion_may_be_partial"),
        ("unknown", SessionEditorStatus.UNCERTAIN, "insertion_result_unknown"),
    ],
)
def test_insert_verified_no_text_pattern_preserves_atomic_error_semantics(
    outcome: str,
    expected_status: SessionEditorStatus,
    expected_reason: str,
) -> None:
    document = _Document("safe")

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    inserter = _GuardedAtomicInserter(document, outcome=outcome)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_NoPatternControl(document)),
    )

    result = editor.insert_verified("text", _target())

    assert result.status is expected_status
    assert result.reason_code == expected_reason
    assert "guarded_unverified" in result.backend
    assert inserter.atomic_calls == 1
    assert inserter.native_calls == 1
    assert inserter.guard_results == [True]
    assert document.text == ("safet" if outcome == "partial" else "safe")


def test_insert_verified_preflight_blocks_without_sendinput() -> None:
    document = _Document("safe")

    class _NoPatternControl(_Control):
        def GetTextPattern(self):
            return None

    class _OtherRuntimeControl(_Control):
        def GetRuntimeId(self):
            return (99,)

    cases = (
        SessionTextEditor(
            _VerifiedInputInserter(document),  # type: ignore[arg-type]
            _Focus(FocusMatch.CHANGED),  # type: ignore[arg-type]
            _Automation(_Control(document)),
        ),
        SessionTextEditor(
            _VerifiedInputInserter(document),  # type: ignore[arg-type]
            _Focus(),  # type: ignore[arg-type]
            _Automation(_Control(document, password=True)),
        ),
        SessionTextEditor(
            _VerifiedInputInserter(document),  # type: ignore[arg-type]
            _Focus(),  # type: ignore[arg-type]
            _Automation(_NoPatternControl(document)),
        ),
        SessionTextEditor(
            _VerifiedInputInserter(document),  # type: ignore[arg-type]
            _Focus(),  # type: ignore[arg-type]
            _Automation(_OtherRuntimeControl(document)),
        ),
    )

    results = [editor.insert_verified(" text", _target()) for editor in cases]

    assert [result.status for result in results] == [
        SessionEditorStatus.REJECTED,
        SessionEditorStatus.REJECTED,
        SessionEditorStatus.UNAVAILABLE,
        SessionEditorStatus.REJECTED,
    ]
    assert [result.reason_code for result in results] == [
        "focus_changed",
        "password_field_prohibited",
        "text_pattern_unavailable",
        "focus_changed",
    ]
    assert all(
        editor.inserter.calls == []  # type: ignore[attr-defined]
        for editor in cases
    )
    assert document.text == "safe"


def test_insert_verified_requires_captured_runtime_id_without_sending() -> None:
    document = _Document("safe")
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(
        " text",
        _targeted_target(class_name="Edit", runtime_id=None),
    )

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "runtime_id_unavailable"
    assert inserter.calls == []
    assert document.text == "safe"


def test_insert_verified_cancelled_preflight_does_not_send() -> None:
    document = _Document("safe")
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(" text", _target(), is_current=lambda: False)

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "request_cancelled"
    assert inserter.calls == []
    assert document.text == "safe"


def test_insert_verified_cancelled_during_batches_is_uncertain() -> None:
    document = _Document("safe")
    inserter = _VerifiedInputInserter(document, chunk_size=2)
    checks = 0

    def is_current() -> bool:
        nonlocal checks
        checks += 1
        # Initial preflight plus both checks around the first native batch pass;
        # cancellation is observed before the second batch.
        return checks < 4

    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )

    result = editor.insert_verified(
        "abcdef",
        _target(),
        is_current=is_current,
    )

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "insertion_may_be_partial"
    assert document.text == "safeab"
    assert inserter.guard_results == [True, False]


def test_insert_verified_partial_and_unknown_errors_are_uncertain() -> None:
    partial_document = _Document("safe")

    class _PartialInserter(_VerifiedInputInserter):
        def insert(self, text: str, *, before_batch) -> None:
            self.calls.append(text)
            assert before_batch()
            start, end = self.document.selection
            self.document.text = (
                self.document.text[:start]
                + text[:1]
                + self.document.text[end:]
            )
            self.document.selection = (start + 1, start + 1)
            raise TextInsertionError("partial", partial=True)

    unknown_document = _Document("safe")

    class _UnknownInserter(_VerifiedInputInserter):
        def insert(self, text: str, *, before_batch) -> None:
            self.calls.append(text)
            assert before_batch()
            raise RuntimeError("unknown native result")

    partial = SessionTextEditor(
        _PartialInserter(partial_document),  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(partial_document)),
    ).insert_verified("text", _target())
    unknown = SessionTextEditor(
        _UnknownInserter(unknown_document),  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(unknown_document)),
    ).insert_verified("text", _target())

    assert (partial.status, partial.reason_code) == (
        SessionEditorStatus.UNCERTAIN,
        "insertion_may_be_partial",
    )
    assert (unknown.status, unknown.reason_code) == (
        SessionEditorStatus.UNCERTAIN,
        "insertion_result_unknown",
    )
    assert partial_document.text == "safet"


def test_insert_verified_shares_nonblocking_operation_lock() -> None:
    document = _Document("safe")
    inserter = _VerifiedInputInserter(document)
    editor = SessionTextEditor(
        inserter,  # type: ignore[arg-type]
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
    )
    assert editor._operation_lock.acquire(blocking=False)  # type: ignore[attr-defined]
    try:
        result = editor.insert_verified(" text", _target())
    finally:
        editor._operation_lock.release()  # type: ignore[attr-defined]

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "session_editor_busy"
    assert inserter.calls == []
    assert document.text == "safe"


class _NeverFallbackInserter:
    def replace_selection(self, _text: str) -> None:
        raise AssertionError("SendInput fallback must not run")

    def replace_selection_guarded(self, _text: str, *, before_batch) -> None:
        del before_batch
        raise AssertionError("SendInput fallback must not run")


def test_apply_uses_targeted_backend_for_full_delete_to_empty() -> None:
    document = _Document("Голосовая вставка")
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.DELETE_LAST_DICTATION,
            CommandLanguage.RU,
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.backend == "uia_text+richedit_targeted"
    assert result.empty_document_verified is True
    assert document.text == ""
    assert document.selection == (0, 0)
    assert targeted.calls == [("", 2, RICHEDIT_D2DPT_CLASS)]


def test_apply_uses_targeted_backend_for_ru_emoji_replacement() -> None:
    document = _Document("Привет мир")
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.RU,
            old_text="мир",
            new_text="мир 🌍",
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.EXECUTED
    assert result.backend == "uia_text+richedit_targeted"
    assert document.text == "Привет мир 🌍"
    assert targeted.calls == [("мир 🌍", 2, RICHEDIT_D2DPT_CLASS)]


def test_apply_targeted_timeout_is_uncertain_without_fallback() -> None:
    document = _Document("Привет мир")
    targeted = _TargetedTestInserter(
        document,
        TargetedInsertionResult.timeout(),
    )
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.RU,
            old_text="мир",
            new_text="земля",
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_result_unknown"
    assert result.backend == "richedit_targeted"
    assert document.text == "Привет мир"
    assert targeted.calls == [("земля", 2, RICHEDIT_D2DPT_CLASS)]


def test_apply_targeted_safe_rejection_restores_original_caret() -> None:
    document = _Document("Привет мир")
    original_caret = document.selection
    targeted = _TargetedTestInserter(
        document,
        TargetedInsertionResult.rejected("backend_unavailable"),
    )
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.RU,
            old_text="мир",
            new_text="земля",
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.UNAVAILABLE
    assert result.reason_code == "backend_unavailable"
    assert result.backend == "richedit_targeted"
    assert document.text == "Привет мир"
    assert document.selection == original_caret
    assert targeted.calls == [("земля", 2, RICHEDIT_D2DPT_CLASS)]


def test_apply_targeted_guard_failure_matches_existing_rejection() -> None:
    document = _Document("Привет мир")

    class _FocusChangesAtInputGate:
        def __init__(self) -> None:
            self.calls = 0

        def compare_current(self, _expected: FocusTarget) -> FocusMatch:
            self.calls += 1
            return FocusMatch.SAME if self.calls <= 6 else FocusMatch.CHANGED

    focus = _FocusChangesAtInputGate()
    targeted = _TargetedTestInserter(document)
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        focus,  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.RU,
            old_text="мир",
            new_text="земля",
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.REJECTED
    assert result.reason_code == "focus_changed_after_selection"
    assert result.backend == "richedit_targeted"
    assert focus.calls == 7
    assert targeted.guard_results == [False]
    assert targeted.calls == []
    assert document.text == "Привет мир"


def test_apply_targeted_accepted_still_requires_existing_postcheck(
    monkeypatch,
) -> None:
    monkeypatch.setattr("voicetype_local.session_editor.time.sleep", lambda _value: None)
    document = _Document("Привет мир")
    targeted = _TargetedTestInserter(document, apply_replacement=False)
    editor = SessionTextEditor(
        _NeverFallbackInserter(),
        _Focus(),  # type: ignore[arg-type]
        _Automation(_Control(document)),
        targeted_inserter=targeted,  # type: ignore[arg-type]
    )
    plan = plan_session_edit(
        document.text,
        SessionEditRequest(
            SessionEditAction.REPLACE_UNIQUE,
            CommandLanguage.RU,
            old_text="мир",
            new_text="земля",
        ),
    )

    result = editor.apply(plan, _targeted_target())

    assert result.status is SessionEditorStatus.UNCERTAIN
    assert result.reason_code == "edit_verification_failed"
    assert "richedit_targeted" in result.backend
    assert document.text == "Привет мир"
    assert targeted.calls == [("земля", 2, RICHEDIT_D2DPT_CLASS)]
