from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass


_WORD_RE = re.compile(r"(?u)\w+(?:[-'’]\w+)*")


def evaluation_words(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _WORD_RE.finditer(text)]


def edit_distance(expected: Sequence[str], actual: Sequence[str]) -> int:
    previous = list(range(len(actual) + 1))
    for row, expected_item in enumerate(expected, start=1):
        current = [row]
        for column, actual_item in enumerate(actual, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (expected_item != actual_item),
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(expected: str, actual: str) -> float:
    expected_words = evaluation_words(expected)
    actual_words = evaluation_words(actual)
    if not expected_words:
        return 0.0 if not actual_words else 1.0
    return edit_distance(expected_words, actual_words) / len(expected_words)


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    wer: float
    protected_total: int
    protected_correct: int

    @property
    def protected_accuracy(self) -> float:
        if not self.protected_total:
            return 1.0
        return self.protected_correct / self.protected_total


def score_transcript(
    expected: str, actual: str, protected: Sequence[str] = ()
) -> EvaluationScore:
    folded_actual = actual.casefold()
    correct = sum(1 for item in protected if item.casefold() in folded_actual)
    return EvaluationScore(
        wer=word_error_rate(expected, actual),
        protected_total=len(protected),
        protected_correct=correct,
    )
