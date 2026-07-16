from voicetype_local.transcriber import normalize_transcript


def test_normalize_transcript_joins_segments_and_punctuation() -> None:
    assert normalize_transcript([" Привет ", " мир ! "]) == "Привет мир!"


def test_normalize_transcript_keeps_unicode() -> None:
    assert normalize_transcript([" España, ", "ёж и café."]) == "España, ёж и café."


def test_normalize_transcript_flattens_newlines_for_safe_chat_insertion() -> None:
    assert normalize_transcript(["Первая строка\nВторая\tстрока"]) == "Первая строка Вторая строка"
