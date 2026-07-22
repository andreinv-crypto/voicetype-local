from __future__ import annotations

import ctypes
from collections.abc import Callable

import pytest

from voicetype_local.targeted_inserter import (
    EM_REPLACESEL,
    ERROR_TIMEOUT,
    RICHEDIT_D2DPT_CLASS,
    SMTO_ABORTIFHUNG,
    SMTO_BLOCK,
    SMTO_ERRORONEXIT,
    RichEditMessageBackend,
    TargetedInsertionResult,
    TargetedInsertionStatus,
    TargetedRichEditInserter,
    Win32RichEditMessageBackend,
)


class RecordingBackend(RichEditMessageBackend):
    def __init__(
        self,
        result: TargetedInsertionResult | None = None,
    ) -> None:
        self.result = result or TargetedInsertionResult.accepted()
        self.calls: list[tuple[int, str, int]] = []
        self.guard_results: list[bool] = []

    def replace_selection(
        self,
        hwnd: int,
        text: str,
        *,
        timeout_ms: int,
        before_call: Callable[[], bool],
    ) -> TargetedInsertionResult:
        allowed = bool(before_call())
        self.guard_results.append(allowed)
        if not allowed:
            return TargetedInsertionResult.rejected("guard_rejected")
        self.calls.append((hwnd, text, timeout_ms))
        return self.result


def test_policy_accepts_only_exact_richedit_d2dpt_class() -> None:
    backend = RecordingBackend()
    inserter = TargetedRichEditInserter(backend)

    accepted = inserter.insert(
        "текст",
        hwnd=123,
        class_name=RICHEDIT_D2DPT_CLASS,
        before_call=lambda: True,
    )

    assert accepted.status is TargetedInsertionStatus.ACCEPTED
    assert backend.calls == [(123, "текст", 500)]

    for rejected_class in (
        "RichEditD2DPT1",
        "richeditd2dpt",
        "RichEditD2D",
        " RichEditD2DPT",
        "",
    ):
        result = inserter.insert(
            "текст",
            hwnd=123,
            class_name=rejected_class,
            before_call=lambda: True,
        )
        assert result.status is TargetedInsertionStatus.REJECTED
        assert result.reason_code == "class_not_allowlisted"

    assert backend.calls == [(123, "текст", 500)]


@pytest.mark.parametrize("hwnd", [0, -1, True, 1.5, "123"])
def test_policy_rejects_invalid_window_handle_without_backend_call(
    hwnd: object,
) -> None:
    backend = RecordingBackend()
    inserter = TargetedRichEditInserter(backend)

    result = inserter.insert(
        "safe",
        hwnd=hwnd,  # type: ignore[arg-type]
        class_name=RICHEDIT_D2DPT_CLASS,
        before_call=lambda: True,
    )

    assert result.status is TargetedInsertionStatus.REJECTED
    assert result.reason_code == "invalid_window_handle"
    assert result.safe_to_retry is True
    assert result.postcheck_required is False
    assert backend.calls == []


@pytest.mark.parametrize("text", ["bad\x00text", "bad\x85text", "bad\ud800text"])
def test_policy_rejects_unsafe_unicode_without_backend_call(text: str) -> None:
    backend = RecordingBackend()
    inserter = TargetedRichEditInserter(backend)

    result = inserter.insert(
        text,
        hwnd=123,
        class_name=RICHEDIT_D2DPT_CLASS,
        before_call=lambda: True,
    )

    assert result.status is TargetedInsertionStatus.REJECTED
    assert result.reason_code == "invalid_text"
    assert backend.calls == []


def test_policy_normalizes_windows_line_endings_before_dispatch() -> None:
    backend = RecordingBackend()
    inserter = TargetedRichEditInserter(backend, timeout_ms=250)

    result = inserter.insert(
        "one\r\ntwo\rthree",
        hwnd=456,
        class_name=RICHEDIT_D2DPT_CLASS,
        before_call=lambda: True,
    )

    assert result.status is TargetedInsertionStatus.ACCEPTED
    assert backend.calls == [(456, "one\ntwo\nthree", 250)]
    assert inserter.normalize_text("one\r\ntwo") == "one\ntwo"


@pytest.mark.parametrize(
    "timeout_ms",
    [0, -1, 5001, True, 1.5, float("inf"), float("nan"), "500"],
)
def test_policy_rejects_unbounded_or_noninteger_timeout(timeout_ms: object) -> None:
    with pytest.raises(ValueError, match="timeout_ms"):
        TargetedRichEditInserter(
            RecordingBackend(), timeout_ms=timeout_ms  # type: ignore[arg-type]
        )


def test_native_backend_uses_undoable_em_replacesel_and_bounded_flags() -> None:
    calls: list[tuple[int, int, int, str, int, int]] = []
    order: list[str] = []

    def fake_send(
        hwnd: int,
        message: int,
        can_undo: int,
        text_address: int,
        flags: int,
        timeout_ms: int,
        _message_result: object,
    ) -> int:
        order.append("send")
        calls.append(
            (
                hwnd,
                message,
                can_undo,
                ctypes.wstring_at(text_address),
                flags,
                timeout_ms,
            )
        )
        return 1

    def guard() -> bool:
        order.append("guard")
        return True

    backend = Win32RichEditMessageBackend(fake_send)
    result = backend.replace_selection(
        987,
        "Привет 😀",
        timeout_ms=375,
        before_call=guard,
    )

    assert result == TargetedInsertionResult.accepted()
    assert calls == [
        (
            987,
            EM_REPLACESEL,
            1,
            "Привет 😀",
            SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT,
            375,
        )
    ]
    assert order == ["guard", "send"]


def test_native_backend_guard_rejection_sends_nothing() -> None:
    sent: list[bool] = []
    backend = Win32RichEditMessageBackend(
        lambda *_args: sent.append(True) or 1
    )

    result = backend.replace_selection(
        123,
        "safe",
        timeout_ms=500,
        before_call=lambda: False,
    )

    assert result.status is TargetedInsertionStatus.REJECTED
    assert result.reason_code == "guard_rejected"
    assert result.postcheck_required is False
    assert result.safe_to_retry is True
    assert sent == []


def test_native_backend_guard_exception_sends_nothing() -> None:
    sent: list[bool] = []
    backend = Win32RichEditMessageBackend(
        lambda *_args: sent.append(True) or 1
    )

    def broken_guard() -> bool:
        raise RuntimeError("private diagnostic")

    result = backend.replace_selection(
        123,
        "safe",
        timeout_ms=500,
        before_call=broken_guard,
    )

    assert result.status is TargetedInsertionStatus.REJECTED
    assert result.reason_code == "guard_error"
    assert sent == []


def test_native_backend_returns_typed_timeout_and_forbids_retry() -> None:
    def fake_timeout(*_args: object) -> int:
        ctypes.set_last_error(ERROR_TIMEOUT)
        return 0

    backend = Win32RichEditMessageBackend(fake_timeout)
    result = backend.replace_selection(
        123,
        "safe",
        timeout_ms=500,
        before_call=lambda: True,
    )

    assert result.status is TargetedInsertionStatus.TIMEOUT
    assert result.reason_code == "message_timeout"
    assert result.native_error == ERROR_TIMEOUT
    assert result.postcheck_required is True
    assert result.safe_to_retry is False


def test_native_backend_returns_typed_rejection_for_native_failure() -> None:
    def fake_rejection(*_args: object) -> int:
        ctypes.set_last_error(5)
        return 0

    backend = Win32RichEditMessageBackend(fake_rejection)
    result = backend.replace_selection(
        123,
        "safe",
        timeout_ms=500,
        before_call=lambda: True,
    )

    assert result.status is TargetedInsertionStatus.REJECTED
    assert result.reason_code == "message_rejected"
    assert result.native_error == 5
    assert result.postcheck_required is True
    assert result.safe_to_retry is False


def test_accepted_dispatch_still_requires_router_postcheck() -> None:
    result = TargetedInsertionResult.accepted()

    assert result.status is TargetedInsertionStatus.ACCEPTED
    assert result.postcheck_required is True
    assert result.safe_to_retry is False
