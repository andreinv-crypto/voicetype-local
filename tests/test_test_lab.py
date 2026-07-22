from __future__ import annotations

import os
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from voicetype_local.focus import FocusTarget
from voicetype_local.session_editing import (
    SessionEditAction,
    SessionEditCommandParser,
)
from voicetype_local.test_lab import (
    NativeRichEditField,
    QuickTestEvent,
    QuickTestSession,
    QuickTestSessionSnapshot,
    QuickTestWindow,
    _activation_instruction,
    _native_font_pixel_height,
    _normalize_document,
    quick_test_steps,
)


FIELD_HWND = 501


def _event(
    sequence: int,
    generation: int,
    kind: str,
    operation: str,
    *,
    status: str = "passed",
    reason_code: str = "ok",
) -> QuickTestEvent:
    return QuickTestEvent(
        sequence=sequence,
        generation=generation,
        kind=kind,
        status=status,  # type: ignore[arg-type]
        operation=operation,
        reason_code=reason_code,
        backend="test_backend",
        duration_ms=12,
    )


def _snapshot(generation: int, text: str | None, hwnd: int = FIELD_HWND):
    return QuickTestSessionSnapshot(generation, hwnd, text)


def test_quick_test_happy_path_advances_without_manual_buttons() -> None:
    baseline = "Первое предложение. Второе предложение."
    first_sentence = "Первое предложение."
    session = QuickTestSession(language="ru", start_generation=10)

    first = session.handle_event(
        _event(1, 11, "dictation", "insert"),
        _snapshot(11, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    assert first.accepted
    assert not first.completed
    assert session.final_text_match is True
    assert session.route_expectation() == (
        "session_edit",
        SessionEditAction.DELETE_LAST_SENTENCE,
    )

    second = session.handle_event(
        _event(2, 12, "session_edit", "delete_last_sentence"),
        _snapshot(12, first_sentence),
        field_hwnd=FIELD_HWND,
        field_text=first_sentence,
    )
    assert second.accepted
    assert not second.completed
    assert session.route_expectation() == (
        "session_edit",
        SessionEditAction.RESTORE_LAST_EDIT,
    )

    third = session.handle_event(
        _event(3, 13, "session_edit", "restore_last_edit"),
        _snapshot(13, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    assert third.accepted
    assert not third.completed
    assert session.route_expectation() == (
        "session_edit",
        SessionEditAction.REPLACE_LAST_SENTENCE,
    )

    replaced = "Первое предложение. Проверка завершена."
    fourth = session.handle_event(
        _event(4, 14, "session_edit", "replace_last_sentence"),
        _snapshot(14, replaced),
        field_hwnd=FIELD_HWND,
        field_text=replaced,
    )
    assert fourth.accepted
    assert fourth.completed
    assert session.route_expectation() == ("none", None)
    assert all(result.passed for result in session.results)
    assert "ОСНОВА РАБОТАЕТ" in session.summary()


def test_final_text_difference_is_reported_separately_from_insertion() -> None:
    actual = "Первое сообщение. Второе сообщение."
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, actual),
        field_hwnd=FIELD_HWND,
        field_text=actual,
    )

    assert update.accepted
    assert session.final_text_match is False
    assert session.results[0].passed is True
    assert "ASR" not in session.summary()
    assert "Совпадение итогового текста" in session.summary()
    assert "есть отличие" in session.summary()


def test_baseline_without_two_sentences_resets_instead_of_false_pass() -> None:
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, "Только одно предложение."),
        field_hwnd=FIELD_HWND,
        field_text="Только одно предложение.",
    )

    assert update.retry
    assert update.reset_field
    assert not session.results
    assert session.step_index == 0


def test_missing_only_final_punctuation_still_allows_command_check() -> None:
    actual = "Первое предложение. Второе предложение"
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, actual),
        field_hwnd=FIELD_HWND,
        field_text=actual,
    )

    assert update.accepted
    assert session.expected_after_delete == "Первое предложение."


def test_stale_duplicate_and_late_events_never_advance() -> None:
    baseline = "Первое предложение. Второе предложение."
    session = QuickTestSession(language="ru", start_generation=5)

    stale_generation = session.handle_event(
        _event(1, 5, "dictation", "insert"),
        _snapshot(5, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    duplicate = session.handle_event(
        _event(1, 6, "dictation", "insert"),
        _snapshot(6, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    late_snapshot = session.handle_event(
        _event(2, 7, "dictation", "insert"),
        _snapshot(8, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )

    assert not stale_generation.accepted
    assert not duplicate.accepted
    assert not late_snapshot.accepted
    assert session.step_index == 0


def test_wrong_field_is_retry_and_success_mismatch_is_fail_closed() -> None:
    baseline = "Первое предложение. Второе предложение."
    session = QuickTestSession(language="ru")

    wrong_field = session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, baseline, hwnd=999),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    assert wrong_field.retry
    assert not session.blocked

    mismatch = session.handle_event(
        _event(2, 2, "dictation", "insert"),
        _snapshot(2, baseline),
        field_hwnd=FIELD_HWND,
        field_text="Другой текст.",
    )
    assert mismatch.blocked
    assert session.blocked
    assert session.results[-1].passed is False


def test_uncertain_operation_blocks_until_explicit_restart() -> None:
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(
            1,
            1,
            "dictation",
            "insert",
            status="failed",
            reason_code="insertion_result_unknown",
        ),
        _snapshot(1, None),
        field_hwnd=FIELD_HWND,
        field_text="",
    )

    assert update.blocked
    assert session.blocked
    session.reset(start_generation=1, last_sequence=1)
    assert not session.blocked
    assert session.step_index == 0


@pytest.mark.parametrize("language", ("ru", "en", "es"))
def test_guided_commands_are_recognized_in_transient_mixed_mode(language: str) -> None:
    steps = quick_test_steps(language)
    parser = SessionEditCommandParser()

    delete = parser.parse(steps[1].phrase, mode="mixed", language=language)
    restore = parser.parse(steps[2].phrase, mode="mixed", language=language)
    replacement = parser.parse(steps[3].phrase, mode="mixed", language=language)

    assert delete.request is not None
    assert delete.request.action is SessionEditAction.DELETE_LAST_SENTENCE
    assert restore.request is not None
    assert restore.request.action is SessionEditAction.RESTORE_LAST_EDIT
    assert replacement.request is not None
    assert replacement.request.action is SessionEditAction.REPLACE_LAST_SENTENCE


def test_structured_event_and_summary_never_contain_transcript_fields_or_text() -> None:
    forbidden_fields = {"text", "raw_text", "transcript", "audio", "phrase"}
    assert forbidden_fields.isdisjoint({item.name for item in fields(QuickTestEvent)})

    session = QuickTestSession(language="ru")
    summary = session.summary()
    assert "Первое предложение" not in summary
    assert "Команда" not in summary


def test_exact_oracle_normalizes_only_windows_newlines() -> None:
    assert _normalize_document("a\r\nb\rc") == "a\nb\nc"
    assert _normalize_document(" a\xa0b ") == " a\xa0b "

    session = QuickTestSession(language="ru")
    mismatch = session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, "Первое\xa0предложение. Второе предложение."),
        field_hwnd=FIELD_HWND,
        field_text="Первое предложение. Второе предложение.",
    )
    assert mismatch.blocked


@pytest.mark.parametrize(
    "reason_code",
    ("focus_changed", "unexpected_route", "session_editor_busy"),
)
def test_only_explicit_retryable_rejections_keep_current_step(
    reason_code: str,
) -> None:
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(
            1,
            1,
            "route",
            "unknown",
            status="rejected",
            reason_code=reason_code,
        ),
        _snapshot(1, None),
        field_hwnd=FIELD_HWND,
        field_text="",
    )

    assert update.retry
    assert not update.blocked
    assert not session.blocked


@pytest.mark.parametrize(
    "reason_code",
    ("no_session_insertion", "caret_or_text_changed", "text_pattern_unavailable"),
)
def test_nonrecoverable_rejection_blocks_until_restart(reason_code: str) -> None:
    session = QuickTestSession(language="ru")

    update = session.handle_event(
        _event(
            1,
            1,
            "dictation",
            "insert",
            status="rejected",
            reason_code=reason_code,
        ),
        _snapshot(1, None),
        field_hwnd=FIELD_HWND,
        field_text="",
    )

    assert update.blocked
    assert session.blocked
    assert session.results[-1].passed is False


def test_session_clear_scrubs_all_text_and_transcript_free_results() -> None:
    session = QuickTestSession(language="ru")
    baseline = "Первое предложение. Второе предложение."
    session.handle_event(
        _event(1, 1, "dictation", "insert"),
        _snapshot(1, baseline),
        field_hwnd=FIELD_HWND,
        field_text=baseline,
    )
    assert session.baseline_text
    assert session.results

    session.clear()

    assert session.baseline_text == ""
    assert session.expected_after_delete == ""
    assert session.expected_after_replace == ""
    assert session.final_text_match is None
    assert session.results == []
    assert session.blocked
    assert session.completed


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("toggle", "нажмите ещё раз"),
        ("hold", "отпустите"),
    ],
)
def test_activation_instruction_matches_toggle_or_hold(
    mode: str,
    expected: str,
) -> None:
    rendered = _activation_instruction("правый Ctrl", mode).casefold()

    assert expected in rendered
    assert "правый ctrl" in rendered


def test_native_font_height_is_large_and_dpi_bounded() -> None:
    assert _native_font_pixel_height(96 / 72) >= 20
    assert _native_font_pixel_height(2.0) == 32
    assert _native_font_pixel_height(100) <= 48
    assert _native_font_pixel_height("invalid") >= 20


def _window_without_tk() -> QuickTestWindow:
    window = QuickTestWindow.__new__(QuickTestWindow)
    window._closed = False
    window.native_field = SimpleNamespace(hwnd=FIELD_HWND, root_hwnd=700)
    return window


def test_owns_target_requires_stable_matching_identity_when_available() -> None:
    window = _window_without_tk()
    matching = FocusTarget(
        foreground_hwnd=700,
        root_hwnd=700,
        focused_hwnd=FIELD_HWND,
        thread_id=10,
        process_id=os.getpid(),
        focused_class_name="RichEdit50W",
        stable=True,
    )

    assert window.owns_target(matching)
    assert not window.owns_target(replace(matching, stable=False))
    assert not window.owns_target(
        replace(matching, process_id=os.getpid() + 1)
    )
    assert not window.owns_target(replace(matching, root_hwnd=701))
    assert window.owns_target(replace(matching, process_id=0, root_hwnd=0))


class _FakeButton:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.focused = False

    def configure(self, **values: object) -> None:
        self.values.update(values)

    def focus_set(self) -> None:
        self.focused = True


class _FakeNativeField:
    def __init__(self, events: list[str], text: str = "") -> None:
        self.hwnd = FIELD_HWND
        self.root_hwnd = 700
        self._events = events
        self._text = text

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self._events.append("field_clear")
        self._text = ""

    def focus(self) -> None:
        self._events.append("field_focus")

    def transfer_focus(self, _widget: object) -> None:
        self._events.append("field_transfer_focus")

    def destroy(self) -> None:
        self._events.append("field_destroy")
        self.hwnd = 0
        self._text = ""


def _lifecycle_window(events: list[str]) -> QuickTestWindow:
    window = QuickTestWindow.__new__(QuickTestWindow)
    window._closed = False
    window._session_started = True
    window.session = QuickTestSession(language="ru")
    window.native_field = _FakeNativeField(events, "private test text")
    window._on_abort_cycle = lambda restart: events.append(
        f"abort:{'restart' if restart else 'close'}"
    )
    window._on_reset_session = lambda: events.append("reset")
    window._on_close = lambda: events.append("on_close")
    window._snapshot_provider = lambda: _snapshot(9, None)
    window._copy_button = _FakeButton()
    window._restart_button = _FakeButton()
    window._close_button = _FakeButton()
    window._set_status = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    window._refresh_view = lambda: None  # type: ignore[method-assign]
    window.window = SimpleNamespace(
        after=lambda _delay, callback: callback(),
        destroy=lambda: events.append("window_destroy"),
    )
    return window


def test_restart_aborts_before_reset_and_scrubs_previous_session() -> None:
    events: list[str] = []
    window = _lifecycle_window(events)
    window.session.baseline_text = "private baseline"

    window.restart()

    assert events[:3] == ["abort:restart", "reset", "field_clear"]
    assert window.session.baseline_text == ""
    assert not window._session_started


def test_close_aborts_before_reset_destroy_and_scrubs_session() -> None:
    events: list[str] = []
    window = _lifecycle_window(events)
    window.session.baseline_text = "private baseline"

    window.close()

    assert events == [
        "abort:close",
        "reset",
        "field_destroy",
        "window_destroy",
        "on_close",
    ]
    assert window.session.baseline_text == ""
    assert window.session.results == []


def test_actual_lab_event_latches_cleanup_but_route_rejection_does_not() -> None:
    events: list[str] = []
    window = _lifecycle_window(events)
    window._session_started = False
    window.native_field._text = ""
    window._snapshot_provider = lambda: _snapshot(1, None)
    window._status_var = SimpleNamespace(set=lambda _value: None)
    window._status_label = SimpleNamespace(configure=lambda **_values: None)
    window._result_vars = [SimpleNamespace(set=lambda _value: None) for _ in range(4)]
    window._result_labels = [
        SimpleNamespace(configure=lambda **_values: None) for _ in range(4)
    ]

    window.handle_event(
        _event(
            1,
            1,
            "route",
            "unknown",
            status="rejected",
            reason_code="unexpected_route",
        )
    )
    assert not window._session_started

    window.handle_event(
        _event(
            2,
            2,
            "dictation",
            "insert",
            status="failed",
            reason_code="insertion_result_unknown",
        )
    )
    assert window._session_started


def test_blocked_summary_can_be_copied_without_transcript() -> None:
    events: list[str] = []
    window = _lifecycle_window(events)
    window.session.blocked = True
    window.session.results.append(
        SimpleNamespace(step_id="dictation", passed=False)
    )
    clipboard: list[str] = []
    window.window = SimpleNamespace(
        clipboard_clear=lambda: clipboard.clear(),
        clipboard_append=clipboard.append,
        update_idletasks=lambda: None,
    )

    window.copy_summary()

    assert clipboard
    assert "НУЖНА ПРОВЕРКА" in clipboard[0]
    assert "private test text" not in clipboard[0]
    assert window._close_button.focused


def test_internal_error_blocks_followup_and_focuses_restart() -> None:
    events: list[str] = []
    window = _lifecycle_window(events)

    window.stop_with_internal_error()

    assert window.session.blocked
    assert window.session.results[-1].passed is False
    assert window._copy_button.values["state"] == "normal"
    assert window._restart_button.focused


@pytest.mark.parametrize(
    ("message", "key", "shift_down", "expected"),
    [
        (0x0100, 0x09, False, ("tab", True)),
        (0x0100, 0x09, True, ("shift_tab", True)),
        (0x0100, 0x1B, False, ("escape", True)),
        (0x0102, 0x09, False, ("", True)),
        (0x0102, 0x1B, False, ("", True)),
        (0x0101, 0x09, False, ("", False)),
        (0x0100, ord("A"), False, ("", False)),
    ],
)
def test_native_navigation_messages_are_decoded_without_tk_calls(
    message: int,
    key: int,
    shift_down: bool,
    expected: tuple[str, bool],
) -> None:
    assert NativeRichEditField._decode_navigation_message(
        message,
        key,
        shift_down=shift_down,
    ) == expected


def test_native_navigation_request_is_consumed_once() -> None:
    field = NativeRichEditField.__new__(NativeRichEditField)
    field._navigation_request = "tab"

    assert field.take_navigation_request() == "tab"
    assert field.take_navigation_request() == ""


def test_native_transfer_focus_uses_target_widget_hwnd() -> None:
    focused: list[int] = []
    field = NativeRichEditField.__new__(NativeRichEditField)
    field.hwnd = FIELD_HWND
    field._user32 = SimpleNamespace(SetFocus=lambda hwnd: focused.append(hwnd))
    widget = SimpleNamespace(winfo_id=lambda: 90210)

    field.transfer_focus(widget)  # type: ignore[arg-type]

    assert focused == [90210]


def test_action_button_focus_updates_tk_and_native_focus() -> None:
    events: list[str] = []
    window = QuickTestWindow.__new__(QuickTestWindow)
    window.native_field = _FakeNativeField(events)
    button = _FakeButton()

    window._focus_action_button(button)  # type: ignore[arg-type]

    assert button.focused
    assert events == ["field_transfer_focus"]


def test_failed_constructor_cleanup_destroys_native_field_before_window() -> None:
    events: list[str] = []
    window = QuickTestWindow.__new__(QuickTestWindow)
    window._closed = False
    window.window = SimpleNamespace(destroy=lambda: events.append("window"))
    native_field = SimpleNamespace(destroy=lambda: events.append("native"))

    window._cleanup_failed_construction(native_field)  # type: ignore[arg-type]

    assert window._closed
    assert events == ["native", "window"]
