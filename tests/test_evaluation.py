from voicetype_local.evaluation import score_transcript, word_error_rate


def test_word_error_rate() -> None:
    assert word_error_rate("один два три", "один два три") == 0
    assert word_error_rate("один два три", "один четыре три") == 1 / 3


def test_protected_term_accuracy_is_separate_from_wer() -> None:
    score = score_transcript(
        "Открой WordPress и GitHub",
        "Открой вордпресс и GitHub",
        ("WordPress", "GitHub"),
    )
    assert score.protected_total == 2
    assert score.protected_correct == 1
    assert score.protected_accuracy == 0.5
