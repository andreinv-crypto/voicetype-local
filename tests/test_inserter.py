import ctypes

import pytest

from voicetype_local.inserter import (
    INPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    TextInsertionError,
    UnicodeTextInserter,
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
