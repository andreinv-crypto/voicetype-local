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
