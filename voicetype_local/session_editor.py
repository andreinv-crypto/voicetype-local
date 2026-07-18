from __future__ import annotations

"""Conservative UI Automation editor for VoiceType-owned session text.

The normal path inspects only a bounded range immediately before the current
caret and verifies that this suffix exactly equals VoiceType's last successful
insertion.  Chromium can expose one generated paragraph newline after a rich
editor's visible text; for that provider shape, a bounded whole-field fallback
is allowed only when the field contains exactly the tracked insertion plus that
single provider newline.  The editor selects only the planned subrange and
replaces it through Unicode SendInput.  Any uncertainty is a hard refusal.
"""

import os
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol, cast

from .focus import FocusInspector, FocusMatch, FocusTarget
from .inserter import TextInsertionError, UnicodeTextInserter
from .session_editing import MAX_TRACKED_INSERTION_CHARS, SessionEditPlan


class SessionEditorStatus(StrEnum):
    EXECUTED = "executed"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SessionEditorResult:
    status: SessionEditorStatus
    reason_code: str
    backend: str = "uia_text"
    empty_document_verified: bool = False

    def __post_init__(self) -> None:
        if (
            self.empty_document_verified
            and self.status is not SessionEditorStatus.EXECUTED
        ):
            raise ValueError("empty-document proof requires an executed edit")

    @property
    def succeeded(self) -> bool:
        return self.status is SessionEditorStatus.EXECUTED


@dataclass(frozen=True, slots=True)
class _DeletionAnchor:
    prefix_text: str
    at_document_start: bool
    insertion_start: object


class _SelectionInserter(Protocol):
    def replace_selection(self, text: str) -> None: ...


_AUTO_AUTOMATION = object()
_UIA_POLL_INTERVAL_SECONDS = 0.02
_UIA_SELECTION_VERIFY_ATTEMPTS = 25
_UIA_SELECTION_REQUEST_ATTEMPTS = 2
_UIA_POST_EDIT_VERIFY_ATTEMPTS = 25


def _optional_automation() -> object | None:
    if os.name != "nt":
        return None
    try:
        import uiautomation  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None
    return uiautomation


def _provider_text(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _owned_provider_text_matches(
    actual: str,
    expected: str,
    *,
    allow_terminal_newline: bool,
) -> bool:
    """Match an exact whole-field Chromium representation.

    Chromium contenteditable can expose one leading ASCII space inserted by
    VoiceType as U+00A0.  That provider alias is accepted only by the
    whole-field ownership proof: one first-character substitution, in one
    direction, with every other character still exact.  Suffix, selection and
    endpoint comparisons deliberately remain byte-for-byte strict.
    """

    variants = {expected}
    if expected.startswith(" ") and not expected.startswith("  "):
        variants.add("\u00a0" + expected[1:])
    if allow_terminal_newline:
        variants.update(value + "\n" for value in tuple(variants))
    return actual in variants


class SessionTextEditor:
    """Edit only a verified suffix in the same focused field."""

    def __init__(
        self,
        inserter: _SelectionInserter | None = None,
        focus_inspector: FocusInspector | None = None,
        automation_module: object | None | object = _AUTO_AUTOMATION,
    ) -> None:
        self.inserter = inserter or UnicodeTextInserter()
        self.focus_inspector = focus_inspector or FocusInspector()
        self.automation = (
            _optional_automation()
            if automation_module is _AUTO_AUTOMATION
            else automation_module
        )
        self._operation_lock = threading.Lock()
        # Privacy-safe structural codes only; never text, names or hashes.
        self._match_diagnostic = "not_started"
        self._owned_diagnostic = "not_checked"
        self._empty_diagnostic = "not_checked"
        self._deletion_diagnostic = "not_checked"

    def _thread_initializer(self):
        if self.automation is None:
            return nullcontext()
        initializer = getattr(self.automation, "UIAutomationInitializerInThread", None)
        return initializer() if callable(initializer) else nullcontext()

    @staticmethod
    def _runtime_id(control: object) -> tuple[int, ...] | None:
        getter = getattr(control, "GetRuntimeId", None)
        if not callable(getter):
            return None
        try:
            result = tuple(int(item) for item in getter())
        except Exception:
            return None
        return result or None

    @staticmethod
    def _is_password(control: object) -> bool:
        try:
            return bool(getattr(control, "IsPassword", False))
        except Exception:
            return True

    def _text_pattern(self, control: object) -> object | None:
        """Return TextPattern even for generic UIA control wrappers.

        ``uiautomation`` exposes ``GetTextPattern`` only on a few typed
        Python wrappers.  Browser/Electron editors can be returned as a
        generic GroupControl/EditControl while the underlying UIA element
        still supports TextPattern.  Query the same exact focused element
        through its generic pattern API before declaring it unsupported.
        """

        getter = getattr(control, "GetTextPattern", None)
        if callable(getter):
            try:
                pattern = getter()
            except Exception:
                pattern = None
            if pattern is not None:
                return pattern

        generic_getter = getattr(control, "GetPattern", None)
        pattern_ids = getattr(self.automation, "PatternId", None)
        text_pattern_id = getattr(pattern_ids, "TextPattern", None)
        if not callable(generic_getter) or text_pattern_id is None:
            return None
        try:
            return generic_getter(text_pattern_id)
        except Exception:
            return None

    @staticmethod
    def _selection(pattern: object) -> list[object]:
        getter = getattr(pattern, "GetSelection", None)
        if not callable(getter):
            return []
        try:
            return list(getter())
        except Exception:
            return []

    @staticmethod
    def _endpoints(automation: object) -> tuple[int, int, int]:
        endpoint = getattr(automation, "TextPatternRangeEndpoint", None)
        unit = getattr(automation, "TextUnit", None)
        return (
            int(getattr(endpoint, "Start", 0)),
            int(getattr(endpoint, "End", 1)),
            int(getattr(unit, "Character", 0)),
        )

    @staticmethod
    def _compare_endpoints(
        text_range: object,
        source_endpoint: int,
        other: object,
        target_endpoint: int,
    ) -> int:
        # uiautomation 2.0.29's public TextRange.CompareEndpoints wrapper
        # accidentally passes its Python wrapper to COM instead of the
        # underlying ``.textRange`` object. Prefer the raw call when both
        # objects expose it; keep the public method for fakes/other providers.
        raw_source = getattr(text_range, "textRange", None)
        raw_other = getattr(other, "textRange", None)
        raw_compare = getattr(raw_source, "CompareEndpoints", None)
        if callable(raw_compare) and raw_other is not None:
            return int(raw_compare(source_endpoint, raw_other, target_endpoint))
        compare = getattr(text_range, "CompareEndpoints", None)
        if not callable(compare):
            raise RuntimeError("TextRange endpoint comparison is unavailable")
        return int(compare(source_endpoint, other, target_endpoint))

    def _text_equivalent_endpoints(
        self,
        left: object,
        left_endpoint: int,
        right: object,
        right_endpoint: int,
    ) -> bool:
        """Accept Chromium endpoint aliases only across an empty text bridge.

        Chromium can represent one visible caret offset with two different AX
        positions (for example a DOM container boundary versus its leaf-text
        boundary).  Raw CompareEndpoints then reports a difference even though
        no text or embedded object exists between the positions.  This helper
        keeps raw equality as the primary proof and accepts an alias only when
        a range spanning the two positions contains neither text nor children.
        """

        if self.automation is None:
            return False
        start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
        try:
            comparison = self._compare_endpoints(
                left,
                left_endpoint,
                right,
                right_endpoint,
            )
            if comparison == 0:
                return True
            lower, lower_endpoint, upper, upper_endpoint = (
                (left, left_endpoint, right, right_endpoint)
                if comparison < 0
                else (right, right_endpoint, left, left_endpoint)
            )
            clone = getattr(lower, "Clone", None)
            bridge = clone() if callable(clone) else None
            move = getattr(bridge, "MoveEndpointByRange", None)
            get_text = getattr(bridge, "GetText", None)
            get_children = getattr(bridge, "GetChildren", None)
            if (
                bridge is None
                or not callable(move)
                or not callable(get_text)
                or not callable(get_children)
            ):
                return False
            if not bool(
                move(
                    start_endpoint,
                    lower,
                    lower_endpoint,
                    waitTime=0,
                )
            ):
                return False
            if not bool(
                move(
                    end_endpoint,
                    lower,
                    lower_endpoint,
                    waitTime=0,
                )
            ):
                return False
            if not bool(
                move(
                    end_endpoint,
                    upper,
                    upper_endpoint,
                    waitTime=0,
                )
            ):
                return False
            # Do not normalize here: LF, CR, NBSP, hidden text and object
            # replacement characters must all remain visible to the refusal.
            if get_text(1) != "":
                return False
            children = get_children()
            return children is not None and len(list(children)) == 0
        except Exception:
            return False

    @staticmethod
    def _range_text(text_range: object) -> str:
        getter = getattr(text_range, "GetText", None)
        if not callable(getter):
            raise RuntimeError("TextRange text is unavailable")
        value = getter(-1)
        return _provider_text("" if value is None else str(value))

    def _focused_control(self) -> object | None:
        if self.automation is None:
            return None
        getter = getattr(self.automation, "GetFocusedControl", None)
        return getter() if callable(getter) else None

    def _verified_control(
        self,
        expected_target: FocusTarget,
    ) -> tuple[object | None, SessionEditorResult | None]:
        if self.focus_inspector.compare_current(expected_target) is not FocusMatch.SAME:
            return None, SessionEditorResult(
                SessionEditorStatus.REJECTED, "focus_changed"
            )
        try:
            control = self._focused_control()
        except Exception:
            control = None
        if control is None:
            return None, SessionEditorResult(
                SessionEditorStatus.UNAVAILABLE, "focused_control_unavailable"
            )
        if self._is_password(control):
            return None, SessionEditorResult(
                SessionEditorStatus.REJECTED, "password_field_prohibited"
            )
        expected_runtime = expected_target.uia_runtime_id
        current_runtime = self._runtime_id(control)
        if expected_runtime is not None and current_runtime != expected_runtime:
            return None, SessionEditorResult(
                SessionEditorStatus.REJECTED, "focus_changed"
            )
        return control, None

    def _find_suffix_range(
        self,
        pattern: object,
        expected_text: str,
        *,
        allow_owned_document: bool = False,
    ) -> tuple[object | None, str]:
        if self.automation is None:
            self._match_diagnostic = "uia_unavailable"
            return None, "uia_unavailable"
        selections = self._selection(pattern)
        if len(selections) != 1:
            self._match_diagnostic = "selection_count"
            return None, "selection_unavailable"
        caret = selections[0]
        start_endpoint, end_endpoint, character_unit = self._endpoints(self.automation)
        try:
            if self._compare_endpoints(
                caret, start_endpoint, caret, end_endpoint
            ) != 0:
                self._match_diagnostic = "selection_not_collapsed"
                return None, "selection_changed"
            if not expected_text:
                if allow_owned_document and self._owned_document_is_empty(pattern):
                    self._match_diagnostic = "owned_empty_document"
                    return caret, "ok_owned_empty_document"
                self._match_diagnostic = "empty_document_not_owned"
                return None, "caret_or_text_changed"
            clone = getattr(caret, "Clone", None)
            lookback = clone() if callable(clone) else None
            if lookback is None:
                self._match_diagnostic = "lookback_unavailable"
                return None, "text_range_unavailable"
            move_start = getattr(lookback, "MoveEndpointByUnit", None)
            if not callable(move_start):
                self._match_diagnostic = "move_unavailable"
                return None, "text_range_unavailable"
            lookup_units = min(
                MAX_TRACKED_INSERTION_CHARS * 2,
                max(64, len(expected_text.encode("utf-16-le")) // 2 + 16),
            )
            move_start(start_endpoint, character_unit, -lookup_units, waitTime=0)
            find_text = getattr(lookback, "FindText", None)
            if not callable(find_text):
                self._match_diagnostic = "find_unavailable"
                return None, "text_range_unavailable"
            variants = [expected_text]
            crlf = expected_text.replace("\n", "\r\n")
            if crlf != expected_text:
                variants.append(crlf)
            found = None
            candidate_seen = False
            exact_text_seen = False
            endpoint_alias_used = False
            for variant in variants:
                candidate = find_text(variant, True, False)
                if candidate is None:
                    continue
                candidate_seen = True
                if self._range_text(candidate) != _provider_text(expected_text):
                    continue
                exact_text_seen = True
                raw_comparison = self._compare_endpoints(
                    candidate,
                    end_endpoint,
                    caret,
                    start_endpoint,
                )
                if not self._text_equivalent_endpoints(
                    candidate,
                    end_endpoint,
                    caret,
                    start_endpoint,
                ):
                    continue
                endpoint_alias_used = raw_comparison != 0
                found = candidate
                break
            if found is None:
                owned = (
                    self._owned_document_range(
                        pattern,
                        expected_text,
                        caret,
                    )
                    if allow_owned_document
                    else None
                )
                if owned is not None:
                    self._match_diagnostic = "owned_document"
                    return owned, "ok_owned_document"
                prefix = (
                    "endpoint_gap_nonempty"
                    if exact_text_seen
                    else "candidate_text_mismatch"
                    if candidate_seen
                    else "find_missing"
                )
                self._match_diagnostic = f"{prefix}+{self._owned_diagnostic}"
                return None, "caret_or_text_changed"
            self._match_diagnostic = (
                "suffix_endpoint_equivalent" if endpoint_alias_used else "suffix_exact"
            )
            return found, "ok_endpoint_equivalent" if endpoint_alias_used else "ok"
        except Exception:
            self._match_diagnostic = "text_range_exception"
            return None, "text_range_failed"

    def _owned_document_range(
        self,
        pattern: object,
        expected_text: str,
        caret: object,
    ) -> object | None:
        """Return an exact VoiceType-owned field range for Chromium editors.

        Chromium's UIA text stream may append one generated paragraph newline
        after visible content.  This fallback never treats an arbitrary suffix
        as owned: the bounded DocumentRange must contain *only* the tracked
        insertion and, optionally, that one terminal newline.  The caret must
        still be at the logical or provider end of the field.
        """

        self._owned_diagnostic = "not_checked"
        if self.automation is None or not expected_text:
            self._owned_diagnostic = "owned_unavailable"
            return None
        document_range = getattr(pattern, "DocumentRange", None)
        clone = getattr(document_range, "Clone", None)
        getter = getattr(document_range, "GetText", None)
        if document_range is None or not callable(clone) or not callable(getter):
            self._owned_diagnostic = "owned_range_unavailable"
            return None
        start_endpoint, end_endpoint, character_unit = self._endpoints(
            self.automation
        )
        try:
            bounded = getter(MAX_TRACKED_INSERTION_CHARS + 4)
            document_text = _provider_text("" if bounded is None else str(bounded))
            normalized_expected = _provider_text(expected_text)
            if not _owned_provider_text_matches(
                document_text,
                normalized_expected,
                allow_terminal_newline=True,
            ):
                self._owned_diagnostic = "owned_shape_other"
                return None
            if self._compare_endpoints(
                caret,
                start_endpoint,
                caret,
                end_endpoint,
            ) != 0:
                self._owned_diagnostic = "owned_selection_not_collapsed"
                return None

            owned = clone()
            move_end = getattr(owned, "MoveEndpointByUnit", None)
            if not callable(move_end):
                self._owned_diagnostic = "owned_move_unavailable"
                return None
            # CRLF can be represented as one normalized newline but one or two
            # provider character units.  Four bounded attempts leave room for
            # either representation without scanning unrelated text.
            for _ in range(5):
                if _owned_provider_text_matches(
                    self._range_text(owned),
                    normalized_expected,
                    allow_terminal_newline=False,
                ):
                    break
                moved = move_end(
                    end_endpoint,
                    character_unit,
                    -1,
                    waitTime=0,
                )
                if int(moved) == 0:
                    self._owned_diagnostic = "owned_trim_stopped"
                    return None
            else:
                self._owned_diagnostic = "owned_trim_limit"
                return None

            if not _owned_provider_text_matches(
                self._range_text(owned),
                normalized_expected,
                allow_terminal_newline=False,
            ):
                self._owned_diagnostic = "owned_trim_mismatch"
                return None
            caret_at_logical_end = self._text_equivalent_endpoints(
                caret,
                start_endpoint,
                owned,
                end_endpoint,
            )
            caret_at_provider_end = self._text_equivalent_endpoints(
                caret,
                start_endpoint,
                document_range,
                end_endpoint,
            )
            if not (caret_at_logical_end or caret_at_provider_end):
                self._owned_diagnostic = "owned_endpoint_gap_nonempty"
                return None
            self._owned_diagnostic = "owned_exact"
            return owned
        except Exception:
            self._owned_diagnostic = "owned_exception"
            return None

    def _owned_document_is_empty(self, pattern: object) -> bool:
        """Verify a field has no user text after a full deletion."""

        self._empty_diagnostic = "not_checked"
        if self.automation is None:
            self._empty_diagnostic = "uia_unavailable"
            return False
        document_range = getattr(pattern, "DocumentRange", None)
        getter = getattr(document_range, "GetText", None)
        children_getter = getattr(document_range, "GetChildren", None)
        if (
            document_range is None
            or not callable(getter)
            or not callable(children_getter)
        ):
            self._empty_diagnostic = "range_unavailable"
            return False
        selections = self._selection(pattern)
        if len(selections) != 1:
            self._empty_diagnostic = "selection_count"
            return False
        start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
        try:
            caret = selections[0]
            if self._compare_endpoints(
                caret,
                start_endpoint,
                caret,
                end_endpoint,
            ) != 0:
                self._empty_diagnostic = "selection_not_collapsed"
                return False
            value = getter(3)
            if _provider_text("" if value is None else str(value)) not in {"", "\n"}:
                self._empty_diagnostic = "shape_nonempty"
                return False
            children = children_getter()
            if children is None:
                self._empty_diagnostic = "children_unknown"
                return False
            if len(list(children)) != 0:
                self._empty_diagnostic = "children_nonempty"
                return False
            at_boundary = bool(
                self._text_equivalent_endpoints(
                    caret,
                    start_endpoint,
                    document_range,
                    start_endpoint,
                )
                or self._text_equivalent_endpoints(
                    caret,
                    start_endpoint,
                    document_range,
                    end_endpoint,
                )
            )
            self._empty_diagnostic = (
                "empty_exact" if at_boundary else "caret_not_boundary"
            )
            return at_boundary
        except Exception:
            self._empty_diagnostic = "exception"
            return False

    def _owned_document_matches(self, pattern: object, expected_text: str) -> bool:
        """Verify that the entire focused field is the expected owned text."""

        if not expected_text:
            return self._owned_document_is_empty(pattern)
        selections = self._selection(pattern)
        if len(selections) != 1:
            self._owned_diagnostic = "owned_selection_count"
            return False
        return self._owned_document_range(
            pattern,
            expected_text,
            selections[0],
        ) is not None

    def _find_edit_range(
        self,
        whole: object,
        plan: SessionEditPlan,
    ) -> tuple[object | None, str]:
        if plan.selected_text == plan.original_text:
            return whole, "ok"
        find_text = getattr(whole, "FindText", None)
        if not callable(find_text):
            return None, "text_range_unavailable"
        prefer_last = plan.end == len(plan.original_text)
        variants = [plan.selected_text]
        crlf = plan.selected_text.replace("\n", "\r\n")
        if crlf != plan.selected_text:
            variants.append(crlf)
        try:
            for variant in variants:
                candidate = find_text(variant, prefer_last, False)
                if candidate is None:
                    continue
                if self._range_text(candidate) == _provider_text(plan.selected_text):
                    return candidate, "ok"
        except Exception:
            return None, "text_range_failed"
        return None, "planned_text_changed"

    def _suffix_matches(
        self,
        pattern: object,
        expected_text: str,
        *,
        allow_owned_document: bool = False,
    ) -> bool:
        if not expected_text:
            selections = self._selection(pattern)
            if len(selections) != 1 or self.automation is None:
                return False
            start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
            try:
                return self._compare_endpoints(
                    selections[0], start_endpoint, selections[0], end_endpoint
                ) == 0
            except Exception:
                return False
        found, _reason = self._find_suffix_range(
            pattern,
            expected_text,
            allow_owned_document=allow_owned_document,
        )
        return found is not None

    def _deletion_anchor(
        self,
        pattern: object,
        whole: object,
    ) -> _DeletionAnchor | None:
        """Capture a tiny, non-persistent prefix for verifying an empty result."""

        if self.automation is None:
            return None
        start_endpoint, end_endpoint, character_unit = self._endpoints(self.automation)
        clone = getattr(whole, "Clone", None)
        anchor = clone() if callable(clone) else None
        if anchor is None:
            return None
        collapse = getattr(anchor, "MoveEndpointByRange", None)
        move_start = getattr(anchor, "MoveEndpointByUnit", None)
        if not callable(collapse) or not callable(move_start):
            return None
        try:
            if not bool(
                collapse(
                    end_endpoint,
                    whole,
                    start_endpoint,
                    waitTime=0,
                )
            ):
                return None
            insertion_start = anchor.Clone()
            document_range = getattr(pattern, "DocumentRange", None)
            at_document_start = False
            if document_range is not None:
                at_document_start = self._text_equivalent_endpoints(
                    insertion_start,
                    start_endpoint,
                    document_range,
                    start_endpoint,
                )
            move_start(start_endpoint, character_unit, -64, waitTime=0)
            prefix_text = self._range_text(anchor)
            return _DeletionAnchor(
                prefix_text,
                at_document_start,
                insertion_start,
            )
        except Exception:
            return None

    def _empty_result_matches(
        self,
        pattern: object,
        anchor: _DeletionAnchor | None,
    ) -> bool:
        """Verify a full deletion without treating any collapsed caret as success."""

        self._deletion_diagnostic = "not_checked"
        if anchor is None or self.automation is None:
            self._deletion_diagnostic = "anchor_unavailable"
            return False
        selections = self._selection(pattern)
        document_range = getattr(pattern, "DocumentRange", None)
        if len(selections) != 1:
            self._deletion_diagnostic = "selection_count"
            return False
        start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
        try:
            caret = selections[0]
            if not (
                self._compare_endpoints(caret, start_endpoint, caret, end_endpoint)
                == 0
                and self._text_equivalent_endpoints(
                    caret,
                    start_endpoint,
                    anchor.insertion_start,
                    start_endpoint,
                )
            ):
                self._deletion_diagnostic = "caret_anchor_mismatch"
                return False
            if anchor.prefix_text:
                matched = self._suffix_matches(pattern, anchor.prefix_text)
                self._deletion_diagnostic = (
                    "prefix_exact" if matched else "prefix_mismatch"
                )
                return matched
            matched = bool(
                anchor.at_document_start
                and document_range is not None
                and self._text_equivalent_endpoints(
                    caret,
                    start_endpoint,
                    document_range,
                    start_endpoint,
                )
            )
            self._deletion_diagnostic = (
                "document_start" if matched else "document_start_mismatch"
            )
            return matched
        except Exception:
            self._deletion_diagnostic = "exception"
            return False

    def _caret_at_end(self, text_range: object) -> object | None:
        if self.automation is None:
            return None
        start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
        clone = getattr(text_range, "Clone", None)
        caret = clone() if callable(clone) else None
        move = getattr(caret, "MoveEndpointByRange", None)
        if caret is None or not callable(move):
            return None
        try:
            if not bool(
                move(
                    start_endpoint,
                    text_range,
                    end_endpoint,
                    waitTime=0,
                )
            ):
                return None
            return caret
        except Exception:
            return None

    @staticmethod
    def _restore_caret(caret: object | None) -> bool:
        select = getattr(caret, "Select", None)
        if not callable(select):
            return False
        try:
            return bool(select(waitTime=0))
        except Exception:
            return False

    def _selection_matches_range(self, selected: object, expected: object) -> bool:
        if self.automation is None:
            return False
        start_endpoint, end_endpoint, _unit = self._endpoints(self.automation)
        try:
            return (
                self._range_text(selected) == self._range_text(expected)
                and self._text_equivalent_endpoints(
                    selected,
                    start_endpoint,
                    expected,
                    start_endpoint,
                )
                and self._text_equivalent_endpoints(
                    selected,
                    end_endpoint,
                    expected,
                    end_endpoint,
                )
            )
        except Exception:
            return False

    def _move_caret_to_result_end(
        self,
        pattern: object,
        original_whole: object,
        result_text: str,
    ) -> bool:
        """Re-anchor the caret after a replacement inside the tracked text."""

        if self.automation is None or not result_text:
            return False
        start_endpoint, end_endpoint, character_unit = self._endpoints(self.automation)
        clone = getattr(original_whole, "Clone", None)
        scope = clone() if callable(clone) else None
        move_end = getattr(scope, "MoveEndpointByUnit", None)
        find_text = getattr(scope, "FindText", None)
        if scope is None or not callable(move_end) or not callable(find_text):
            return False
        try:
            move_end(
                end_endpoint,
                character_unit,
                min(MAX_TRACKED_INSERTION_CHARS, len(result_text) + 64),
                waitTime=0,
            )
            variants = [result_text]
            crlf = result_text.replace("\n", "\r\n")
            if crlf != result_text:
                variants.append(crlf)
            for variant in variants:
                candidate = find_text(variant, False, False)
                if candidate is None:
                    continue
                if (
                    self._range_text(candidate) != _provider_text(result_text)
                    or not self._text_equivalent_endpoints(
                        candidate,
                        start_endpoint,
                        original_whole,
                        start_endpoint,
                    )
                ):
                    continue
                return self._restore_caret(self._caret_at_end(candidate))
        except Exception:
            return False
        return False

    def _apply_once(
        self,
        plan: SessionEditPlan,
        expected_target: FocusTarget,
        *,
        is_current: Callable[[], bool] = lambda: True,
    ) -> SessionEditorResult:
        if self.automation is None:
            return SessionEditorResult(
                SessionEditorStatus.UNAVAILABLE, "uia_unavailable"
            )
        if not is_current():
            return SessionEditorResult(SessionEditorStatus.REJECTED, "request_cancelled")
        try:
            with self._thread_initializer():
                control, blocked = self._verified_control(expected_target)
                if blocked is not None or control is None:
                    assert blocked is not None
                    return blocked
                pattern = self._text_pattern(control)
                if pattern is None:
                    return SessionEditorResult(
                        SessionEditorStatus.UNAVAILABLE, "text_pattern_unavailable"
                    )
                allow_owned_document = expected_target.uia_runtime_id is not None
                whole, reason = self._find_suffix_range(
                    pattern,
                    plan.original_text,
                    allow_owned_document=allow_owned_document,
                )
                if whole is None:
                    return SessionEditorResult(
                        SessionEditorStatus.REJECTED,
                        reason,
                        backend=f"uia_text:{self._match_diagnostic}",
                    )
                used_owned_document = reason in {
                    "ok_owned_document",
                    "ok_owned_empty_document",
                }
                used_owned_empty_document = reason == "ok_owned_empty_document"
                used_endpoint_equivalent = reason == "ok_endpoint_equivalent"
                deletion_anchor = (
                    self._deletion_anchor(pattern, whole)
                    if not plan.result_text
                    else None
                )
                if not plan.result_text and deletion_anchor is None:
                    return SessionEditorResult(
                        SessionEditorStatus.UNAVAILABLE,
                        "deletion_verification_unavailable",
                    )
                edit_range, reason = self._find_edit_range(whole, plan)
                if edit_range is None:
                    return SessionEditorResult(SessionEditorStatus.REJECTED, reason)
                restore_caret = self._caret_at_end(whole)
                if restore_caret is None:
                    return SessionEditorResult(
                        SessionEditorStatus.UNAVAILABLE,
                        "caret_restore_unavailable",
                    )
                selection_verified = False
                selection_focus_changed = False
                for selection_request in range(_UIA_SELECTION_REQUEST_ATTEMPTS):
                    if selection_request:
                        # Chromium can return S_OK while ignoring the first
                        # asynchronous selection request.  A caret-restore
                        # request can race that delayed provider action, so do
                        # not queue a competing selection.  Instead re-prove
                        # the unchanged VoiceType range and allow one bounded,
                        # idempotent retry of the same exact selection.
                        actual_whole = self._range_text(whole)
                        whole_matches = (
                            _owned_provider_text_matches(
                                actual_whole,
                                _provider_text(plan.original_text),
                                allow_terminal_newline=False,
                            )
                            if used_owned_document
                            else actual_whole == _provider_text(plan.original_text)
                        )
                        if not whole_matches:
                            return SessionEditorResult(
                                SessionEditorStatus.REJECTED,
                                "planned_text_changed",
                            )
                        retry_control, retry_block = self._verified_control(
                            expected_target
                        )
                        if retry_block is not None or retry_control is None:
                            assert retry_block is not None
                            return retry_block
                        retry_pattern = self._text_pattern(retry_control)
                        if retry_pattern is None:
                            return SessionEditorResult(
                                SessionEditorStatus.UNAVAILABLE,
                                "text_pattern_unavailable",
                            )
                        retry_edit, retry_reason = self._find_edit_range(
                            whole,
                            plan,
                        )
                        if retry_edit is None:
                            return SessionEditorResult(
                                SessionEditorStatus.REJECTED,
                                retry_reason,
                            )
                        pattern = retry_pattern
                        edit_range = retry_edit

                    if (
                        not is_current()
                        or self.focus_inspector.compare_current(expected_target)
                        is not FocusMatch.SAME
                    ):
                        return SessionEditorResult(
                            SessionEditorStatus.REJECTED,
                            "focus_changed",
                        )
                    # Chromium rich contenteditable can acknowledge Select()
                    # while leaving only a collapsed caret unless the exact
                    # range is first brought into the provider's viewport.
                    scroll_into_view = getattr(edit_range, "ScrollIntoView", None)
                    if callable(scroll_into_view):
                        try:
                            scroll_into_view(True, waitTime=0)
                        except Exception:
                            # Unsupported providers keep the established
                            # Select() path; exact read-back remains mandatory.
                            pass
                    if (
                        not is_current()
                        or self.focus_inspector.compare_current(expected_target)
                        is not FocusMatch.SAME
                    ):
                        return SessionEditorResult(
                            SessionEditorStatus.REJECTED,
                            "focus_changed",
                        )
                    refreshed_control, refreshed_block = self._verified_control(
                        expected_target
                    )
                    if refreshed_block is not None or refreshed_control is None:
                        assert refreshed_block is not None
                        return refreshed_block
                    select = getattr(edit_range, "Select", None)
                    try:
                        selected = callable(select) and bool(select(waitTime=0))
                    except Exception:
                        selected = False
                    if not selected:
                        restored = self._restore_caret(restore_caret)
                        return SessionEditorResult(
                            SessionEditorStatus.UNAVAILABLE
                            if restored
                            else SessionEditorStatus.UNCERTAIN,
                            "selection_not_supported"
                            if restored
                            else "selection_restore_failed",
                        )
                    # Scrolling and selection are the only side effects before
                    # this gate.  Neither can change document text.
                    if (
                        not is_current()
                        or self.focus_inspector.compare_current(expected_target)
                        is not FocusMatch.SAME
                    ):
                        restored = self._restore_caret(restore_caret)
                        return SessionEditorResult(
                            SessionEditorStatus.REJECTED
                            if restored
                            else SessionEditorStatus.UNCERTAIN,
                            "focus_changed_after_selection"
                            if restored
                            else "selection_restore_failed",
                        )
                    for attempt in range(_UIA_SELECTION_VERIFY_ATTEMPTS):
                        if (
                            not is_current()
                            or self.focus_inspector.compare_current(expected_target)
                            is not FocusMatch.SAME
                        ):
                            selection_focus_changed = True
                            break
                        selected_now = self._selection(pattern)
                        if (
                            len(selected_now) == 1
                            and self._selection_matches_range(
                                selected_now[0],
                                edit_range,
                            )
                        ):
                            selection_verified = True
                            break
                        if attempt < _UIA_SELECTION_VERIFY_ATTEMPTS - 1:
                            # Chromium selection is asynchronous.  Never
                            # continue until the exact range is observable.
                            time.sleep(_UIA_POLL_INTERVAL_SECONDS)
                    if selection_verified or selection_focus_changed:
                        break
                if not selection_verified:
                    restored = self._restore_caret(restore_caret)
                    return SessionEditorResult(
                        SessionEditorStatus.REJECTED
                        if restored
                        else SessionEditorStatus.UNCERTAIN,
                        "focus_changed_after_selection"
                        if selection_focus_changed and restored
                        else "selection_changed"
                        if restored
                        else "selection_restore_failed",
                    )

                input_guard_reason = "focus_changed_after_selection"
                first_input_batch = True

                def before_input_batch() -> bool:
                    """Re-authorize the target immediately before every SendInput."""

                    nonlocal first_input_batch, input_guard_reason
                    if not is_current():
                        input_guard_reason = "focus_changed_after_selection"
                        return False
                    if first_input_batch:
                        current_selection = self._selection(pattern)
                        if (
                            len(current_selection) != 1
                            or not self._selection_matches_range(
                                current_selection[0],
                                edit_range,
                            )
                        ):
                            input_guard_reason = "selection_changed"
                            return False
                        first_input_batch = False
                    # Keep the focus proof last: UnicodeTextInserter invokes
                    # this guard immediately before the native SendInput call.
                    if (
                        not is_current()
                        or self.focus_inspector.compare_current(expected_target)
                        is not FocusMatch.SAME
                    ):
                        input_guard_reason = "focus_changed_after_selection"
                        return False
                    return True

                try:
                    guarded_replace = getattr(
                        self.inserter,
                        "replace_selection_guarded",
                        None,
                    )
                    if callable(guarded_replace):
                        guarded_replace(
                            plan.replacement_text,
                            before_batch=before_input_batch,
                        )
                    else:
                        # Non-SendInput test/target-bound backends perform one
                        # operation, so one immediately adjacent proof suffices.
                        if not before_input_batch():
                            raise TextInsertionError(
                                "Input target changed before replacement",
                                partial=False,
                                reason_code="input_guard_rejected",
                            )
                        self.inserter.replace_selection(plan.replacement_text)
                except TextInsertionError as exc:
                    if (
                        exc.reason_code == "input_guard_rejected"
                        and not exc.partial
                    ):
                        restored = self._restore_caret(restore_caret)
                        return SessionEditorResult(
                            SessionEditorStatus.REJECTED
                            if restored
                            else SessionEditorStatus.UNCERTAIN,
                            input_guard_reason
                            if restored
                            else "selection_restore_failed",
                            backend="sendinput_unicode",
                        )
                    restored = False
                    if not exc.partial:
                        restored = self._restore_caret(restore_caret)
                    return SessionEditorResult(
                        SessionEditorStatus.UNCERTAIN
                        if exc.partial or not restored
                        else SessionEditorStatus.FAILED,
                        "edit_may_be_partial"
                        if exc.partial
                        else (
                            "edit_input_failed"
                            if restored
                            else "selection_restore_failed"
                        ),
                        backend="sendinput_unicode",
                    )
                except Exception:
                    return SessionEditorResult(
                        SessionEditorStatus.UNCERTAIN,
                        "edit_result_unknown",
                        backend="sendinput_unicode",
                    )

                if plan.end < len(plan.original_text):
                    caret_restored = False
                    for _ in range(_UIA_SELECTION_VERIFY_ATTEMPTS):
                        if not is_current():
                            break
                        if self._move_caret_to_result_end(
                            pattern,
                            whole,
                            plan.result_text,
                        ):
                            caret_restored = True
                            break
                        time.sleep(_UIA_POLL_INTERVAL_SECONDS)
                    if not caret_restored:
                        return SessionEditorResult(
                            SessionEditorStatus.UNCERTAIN,
                            "edit_caret_restore_failed",
                            backend="sendinput_unicode",
                        )

                # UIA providers can update one event-loop tick after SendInput.
                # Verification is bounded and keeps no document text.
                verified = False
                for attempt in range(_UIA_POST_EDIT_VERIFY_ATTEMPTS):
                    if not is_current():
                        break
                    if (
                        (
                            self._owned_document_is_empty(pattern)
                            if used_owned_document
                            else self._empty_result_matches(
                                pattern,
                                deletion_anchor,
                            )
                        )
                        if not plan.result_text
                        else (
                            self._owned_document_matches(
                                pattern,
                                plan.result_text,
                            )
                            if used_owned_document
                            else self._suffix_matches(
                                pattern,
                                plan.result_text,
                                # Never upgrade a normal suffix edit to
                                # whole-field ownership after the side effect.
                                allow_owned_document=False,
                            )
                        )
                    ):
                        verified = True
                        break
                    if attempt < _UIA_POST_EDIT_VERIFY_ATTEMPTS - 1:
                        time.sleep(_UIA_POLL_INTERVAL_SECONDS)
                if not verified:
                    proof_mode = "owned" if used_owned_document else "suffix"
                    diagnostic = (
                        self._empty_diagnostic
                        if not plan.result_text and used_owned_document
                        else self._deletion_diagnostic
                        if not plan.result_text
                        else self._owned_diagnostic
                        if used_owned_document
                        else self._match_diagnostic
                    )
                    return SessionEditorResult(
                        SessionEditorStatus.UNCERTAIN,
                        "edit_verification_failed",
                        backend=f"uia_text_{proof_mode}:postcheck_{diagnostic}",
                    )
                empty_document_verified = False
                empty_capability_candidate = bool(
                    not plan.result_text
                    and expected_target.uia_runtime_id is not None
                    and (
                        used_owned_document
                        or (
                            deletion_anchor is not None
                            and deletion_anchor.at_document_start
                        )
                    )
                )
                if empty_capability_candidate:
                    # A provider may publish the empty DocumentRange slightly
                    # after the deletion itself.  This bounded proof grants an
                    # in-RAM one-shot restore capability; edit success itself
                    # does not depend on obtaining that extra capability.
                    for attempt in range(_UIA_POST_EDIT_VERIFY_ATTEMPTS):
                        if not is_current():
                            break
                        if self._owned_document_is_empty(pattern):
                            empty_document_verified = True
                            break
                        if attempt < _UIA_POST_EDIT_VERIFY_ATTEMPTS - 1:
                            time.sleep(_UIA_POLL_INTERVAL_SECONDS)
                return SessionEditorResult(
                    SessionEditorStatus.EXECUTED,
                    "ok",
                    backend=(
                        "uia_text_owned_empty_document+sendinput_unicode"
                        if used_owned_empty_document
                        else "uia_text_owned_document+sendinput_unicode"
                        if used_owned_document
                        else "uia_text_endpoint_equivalent+sendinput_unicode"
                        if used_endpoint_equivalent
                        else "uia_text+sendinput_unicode"
                    ),
                    empty_document_verified=empty_document_verified,
                )
        except Exception:
            # This boundary surrounds both preflight checks and the native
            # input side effect.  Without a verified phase marker an exception
            # can never be classified as a harmless failure.
            return SessionEditorResult(
                SessionEditorStatus.UNCERTAIN,
                "edit_result_unknown",
            )

    def apply(
        self,
        plan: SessionEditPlan,
        expected_target: FocusTarget,
        *,
        is_current: Callable[[], bool] = lambda: True,
    ) -> SessionEditorResult:
        if not self._operation_lock.acquire(blocking=False):
            return SessionEditorResult(
                SessionEditorStatus.UNAVAILABLE,
                "session_editor_busy",
            )
        try:
            return self._apply_once(
                plan,
                expected_target,
                is_current=is_current,
            )
        finally:
            self._operation_lock.release()


__all__ = [
    "SessionEditorResult",
    "SessionEditorStatus",
    "SessionTextEditor",
]
