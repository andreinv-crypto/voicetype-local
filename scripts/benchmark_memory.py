from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.memory import AliasInput, MemoryContext, MemoryStore
from voicetype_local.prompt_builder import PromptBuilder


def milliseconds(samples: list[float]) -> dict[str, float]:
    ordered = sorted(value * 1000 for value in samples)
    return {
        "mean": round(statistics.fmean(ordered), 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(max(ordered), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checkpoints = {100, 1_000, 10_000}
    report: dict[str, object] = {"schema": 1, "checkpoints": []}
    with tempfile.TemporaryDirectory(prefix="voicetype-memory-benchmark-") as temp:
        path = Path(temp) / "memory.sqlite3"
        store = MemoryStore(path, app_version="benchmark", enable_fts=True)
        try:
            context = MemoryContext(language="en")
            builder = PromptBuilder(max_tokens=224, reserve_tokens=64)
            start = time.perf_counter()
            for index in range(1, 10_001):
                store.add_term(
                    f"NeutralTerm{index:05d}",
                    language="en",
                    priority=index % 101,
                    confirmed=True,
                    aliases=[
                        AliasInput(f"neutral term {index:05d}", confirmed=True)
                    ],
                )
                if index not in checkpoints:
                    continue
                select_samples: list[float] = []
                search_samples: list[float] = []
                prompt_samples: list[float] = []
                for _ in range(30):
                    tick = time.perf_counter()
                    store.select_terms(context, limit=64)
                    select_samples.append(time.perf_counter() - tick)
                    tick = time.perf_counter()
                    store.search_relevant(
                        f"please use neutral term {index:05d}", context, limit=64
                    )
                    search_samples.append(time.perf_counter() - tick)
                    tick = time.perf_counter()
                    builder.build_pre_asr(store, context)
                    prompt_samples.append(time.perf_counter() - tick)
                report["checkpoints"].append(
                    {
                        "terms": index,
                        "elapsed_insert_seconds": round(time.perf_counter() - start, 3),
                        "database_bytes": path.stat().st_size,
                        "fts_available": store.fts_available,
                        "select_ms": milliseconds(select_samples),
                        "search_ms": milliseconds(search_samples),
                        "prompt_ms": milliseconds(prompt_samples),
                    }
                )
        finally:
            store.close()
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
