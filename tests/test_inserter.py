import ctypes

import pytest

from voicetype_local.inserter import (
    INPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    TextInsertionError,
    UnicodeTextInserter,
    VK_BACK,
    _unicode_units,
)


def test_input_structure_matches_win32_abi() -> None:
    assert ctypes.sizeof(INPUT) == 40


def test_unicode_units_support_surrogate_pairs() -> None:
    assert _unicode_units("A") == [0x0041]
    assert _unicode_units("😀") == [0xD83D, 0xDE00]


def test_inserter_never_emits_physical_enter_or_tab() -> None:
    inserter = UnicodeTextInserter()
    sent: list[list[INPUT]] = []
    inserter._send = lambda events: sent.append(list(events))  # type: ignore[method-assign]

    inserter.insert("one\r\ntwo\tthree\rfour")

    keydowns = [
        event
        for batch in sent
        for event in batch
        if not event.ki.dwFlags & KEYEVENTF_KEYUP
    ]
    assert [event.ki.wScan for event in keydowns] == [
        ord(char) for char in "one\ntwo\tthree\nfour"
    ]
    assert all(event.ki.wVk == 0 for event in keydowns)
    assert all(event.ki.dwFlags & KEYEVENTF_UNICODE for event in keydowns)
    assert not any(event.ki.wVk in {0x0D, 0x09} for event in keydowns)


@pytest.mark.parametrize("text", ["safe\x00text", "safe\x85text", "safe\ud800text"])
def test_inserter_rejects_unsafe_control_or_unpaired_surrogate_before_send(
    text: str,
) -> None:
    inserter = UnicodeTextInserter()
    sent: list[list[INPUT]] = []
    inserter._send = lambda events: sent.append(list(events))  # type: ignore[method-assign]

    with pytest.raises(TextInsertionError) as captured:
        inserter.insert(text)

    assert captured.value.partial is False
    assert sent == []


def test_inserter_marks_partial_sendinput_as_unsafe_to_retry() -> None:
    inserter = UnicodeTextInserter()
    inserter._send_input = lambda count, _events, _size: count - 1  # type: ignore[method-assign]

    with pytest.raises(TextInsertionError) as captured:
        inserter.insert("текст")

    assert captured.value.partial is True


def test_atomic_inserter_sends_long_unicode_in_one_native_call() -> None:
    inserter = UnicodeTextInserter(chunk_size=1)
    raw = (("😀 Привет\r\nline\t" * 300) + "конец")
    normalized = raw.replace("\r\n", "\n")
    calls: list[list[tuple[int, int, int]]] = []
    order: list[str] = []

    def before_batch() -> bool:
        order.append("guard")
        return True

    def send_input(count, events, _size):
        order.append("native")
        calls.append(
            [
                (events[index].ki.wVk, events[index].ki.wScan, events[index].ki.dwFlags)
                for index in range(count)
            ]
        )
        return count

    inserter._send_input = send_input  # type: ignore[method-assign]

    inserter.insert_atomic(raw, before_batch=before_batch)

    expected_units = [
        unit
        for character in normalized
        for unit in _unicode_units(character)
    ]
    assert order == ["guard", "native"]
    assert len(calls) == 1
    assert len(calls[0]) == len(expected_units) * 2
    assert [event[1] for event in calls[0][::2]] == expected_units
    assert all(event[0] == 0 for event in calls[0])
    assert all(event[2] & KEYEVENTF_UNICODE for event in calls[0])
    assert all(not event[2] & KEYEVENTF_KEYUP for event in calls[0][::2])
    assert all(event[2] & KEYEVENTF_KEYUP for event in calls[0][1::2])


def test_atomic_inserter_guard_rejection_prevents_native_side_effect() -> None:
    inserter = UnicodeTextInserter(chunk_size=1)
    order: list[str] = []

    def reject() -> bool:
        order.append("guard")
        return False

    def send_input(_count, _events, _size):
        order.append("native")
        raise AssertionError("native SendInput must not run after guard rejection")

    inserter._send_input = send_input  # type: ignore[method-assign]

    with pytest.raises(TextInsertionError) as captured:
        inserter.insert_atomic("текст", before_batch=reject)

    assert captured.value.reason_code == "input_guard_rejected"
    assert captured.value.partial is False
    assert order == ["guard"]


@pytest.mark.parametrize(
    ("sent_events", "expected_partial"),
    [(0, False), (1, True)],
)
def test_atomic_inserter_preserves_zero_and_partial_sendinput_contracts(
    sent_events: int,
    expected_partial: bool,
) -> None:
    inserter = UnicodeTextInserter(chunk_size=1)
    calls = 0

    def send_input(_count, _events, _size):
        nonlocal calls
        calls += 1
        return sent_events

    inserter._send_input = send_input  # type: ignore[method-assign]

    with pytest.raises(TextInsertionError) as captured:
        inserter.insert_atomic("ab", before_batch=lambda: True)

    assert calls == 1
    assert captured.value.partial is expected_partial


def test_atomic_inserter_validates_before_guard_or_native_boundary() -> None:
    inserter = UnicodeTextInserter()
    order: list[str] = []
    inserter._send_input = (  # type: ignore[method-assign]
        lambda _count, _events, _size: order.append("native") or 0
    )

    with pytest.raises(TextInsertionError) as captured:
        inserter.insert_atomic(
            "unsafe\x00text",
            before_batch=lambda: order.append("guard") or True,
        )

    assert captured.value.partial is False
    assert order == []


def test_empty_selection_replacement_emits_one_backspace_pair() -> None:
    inserter = UnicodeTextInserter()
    sent: list[list[INPUT]] = []
    inserter._send = lambda events: sent.append(list(events))  # type: ignore[method-assign]

    inserter.replace_selection("")

    assert len(sent) == 1
    assert [event.ki.wVk for event in sent[0]] == [VK_BACK, VK_BACK]
    assert not sent[0][0].ki.dwFlags & KEYEVENTF_KEYUP
    assert sent[0][1].ki.dwFlags & KEYEVENTF_KEYUP


def test_nonempty_selection_replacement_uses_unicode_input() -> None:
    inserter = UnicodeTextInserter()
    sent: list[list[INPUT]] = []
    inserter._send = lambda events: sent.append(list(events))  # type: ignore[method-assign]

    inserter.replace_selection("готово")

    assert sent
    assert all(event.ki.dwFlags & KEYEVENTF_UNICODE for event in sent[0])


def test_guarded_deletion_sends_no_batch_after_target_change() -> None:
    inserter = UnicodeTextInserter()
    sent: list[int] = []
    inserter._send_input = (  # type: ignore[method-assign]
        lambda count, _events, _size: sent.append(count) or count
    )

    with pytest.raises(TextInsertionError) as captured:
        inserter.replace_selection_guarded("", before_batch=lambda: False)

    assert captured.value.reason_code == "input_guard_rejected"
    assert captured.value.partial is False
    assert sent == []


def test_guarded_replacement_rechecks_target_before_every_batch() -> None:
    inserter = UnicodeTextInserter(chunk_size=1)
    sent: list[int] = []
    guard_calls = 0

    def before_batch() -> bool:
        nonlocal guard_calls
        guard_calls += 1
        return guard_calls == 1

    inserter._send_input = (  # type: ignore[method-assign]
        lambda count, _events, _size: sent.append(count) or count
    )

    with pytest.raises(TextInsertionError) as captured:
        inserter.replace_selection_guarded("ab", before_batch=before_batch)

    assert captured.value.reason_code == "input_guard_rejected"
    assert captured.value.partial is True
    assert guard_calls == 2
    assert sent == [2]
