from __future__ import annotations

"""Reproducible local latency comparison for VoiceType's Whisper settings.

The script prints timings and a transcript hash only.  It never prints or
stores recognized text, which makes it safe to use with private test audio.
"""

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from faster_whisper import WhisperModel


def _run(
    model: WhisperModel,
    audio: Path,
    *,
    language: str | None,
    without_timestamps: bool,
) -> dict[str, object]:
    started = time.perf_counter()
    segments, info = model.transcribe(
        str(audio),
        language=language,
        task="transcribe",
        beam_size=1,
        best_of=1,
        temperature=0,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        condition_on_previous_text=False,
        without_timestamps=without_timestamps,
    )
    text = " ".join(segment.text.strip() for segment in segments).strip()
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {
        "duration_ms": elapsed_ms,
        "detected_language": str(info.language),
        "language_probability": round(float(info.language_probability), 4),
        "characters": len(text),
        "transcript_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--language", choices=("ru", "en", "es"), required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    model = WhisperModel(
        str(args.model),
        device="cpu",
        compute_type="int8",
        cpu_threads=max(1, int(args.threads)),
        local_files_only=True,
    )
    configurations = (
        ("auto_timestamps", None, False),
        ("auto_no_timestamps", None, True),
        ("fixed_timestamps", args.language, False),
        ("fixed_no_timestamps", args.language, True),
    )

    # One unreported warm-up prevents constructor/first-run cost from being
    # attributed to whichever configuration happens to run first.
    _run(model, args.audio, language=args.language, without_timestamps=False)
    samples: dict[str, list[dict[str, object]]] = {
        name: [] for name, _language, _without in configurations
    }
    runs = max(1, min(int(args.runs), 10))
    for round_index in range(runs):
        rotated = configurations[round_index % len(configurations) :] + configurations[: round_index % len(configurations)]
        for name, language, without_timestamps in rotated:
            samples[name].append(
                _run(
                    model,
                    args.audio,
                    language=language,
                    without_timestamps=without_timestamps,
                )
            )

    summary: dict[str, object] = {}
    for name, values in samples.items():
        timings = [int(item["duration_ms"]) for item in values]
        hashes = sorted({str(item["transcript_sha256"]) for item in values})
        summary[name] = {
            "runs": len(values),
            "median_ms": round(statistics.median(timings)),
            "min_ms": min(timings),
            "max_ms": max(timings),
            "stable_transcript": len(hashes) == 1,
            "transcript_hashes": hashes,
            "characters": sorted({int(item["characters"]) for item in values}),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
