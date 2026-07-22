from __future__ import annotations

import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.windows_chromium_session_editor_probe import (
    run_isolated_chromium_session_editor_probe,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the opt-in isolated Chrome/Edge contenteditable UIA probe."
        )
    )
    parser.add_argument(
        "--browser",
        help="Optional absolute path to an already-installed chrome.exe/msedge.exe.",
    )
    args = parser.parse_args()
    code, reason = run_isolated_chromium_session_editor_probe(args.browser)
    print(f"{code}:{reason}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
