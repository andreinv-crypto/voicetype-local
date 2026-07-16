from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.evaluation import score_transcript


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score voluntary VoiceType evaluation transcripts without audio upload."
    )
    parser.add_argument("--cases", type=Path, default=Path("eval/cases.json"))
    parser.add_argument(
        "--results",
        type=Path,
        help="Local JSON object mapping case id to transcript. Keep real text private.",
    )
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    if not args.results:
        print(f"Validated {len(cases)} neutral cases. No transcripts or audio read.")
        return 0
    results = json.loads(args.results.read_text(encoding="utf-8"))
    scores = []
    for case in cases:
        actual = results.get(case["id"], "")
        scores.append(
            score_transcript(case["expected"], actual, case.get("protected", []))
        )
    if not scores:
        raise SystemExit("No evaluation cases")
    print(
        json.dumps(
            {
                "cases": len(scores),
                "mean_wer": sum(score.wer for score in scores) / len(scores),
                "protected_accuracy": sum(
                    score.protected_correct for score in scores
                )
                / max(1, sum(score.protected_total for score in scores)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
