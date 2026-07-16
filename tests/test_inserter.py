import ctypes

from voicetype_local.inserter import INPUT, UnicodeTextInserter, _unicode_units


def test_input_structure_matches_win32_abi() -> None:
    assert ctypes.sizeof(INPUT) == 40


def test_unicode_units_support_surrogate_pairs() -> None:
    assert _unicode_units("A") == [0x0041]
    assert _unicode_units("😀") == [0xD83D, 0xDE00]


def test_inserter_never_emits_physical_enter_or_tab() -> None:
    inserter = UnicodeTextInserter()
    sent: list[list[INPUT]] = []
    inserter._send = lambda events: sent.append(list(events))  # type: ignore[method-assign]

    inserter.insert("one\ntwo\tthree")

    scans = [event.ki.wScan for batch in sent for event in batch if not event.ki.dwFlags & 2]
    assert scans == [ord(char) for char in "one two three"]
