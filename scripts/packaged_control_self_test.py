from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voicetype_local.packaged_self_test import run_packaged_control_self_test


def main() -> int:
    code, label = run_packaged_control_self_test()
    print(f"VOICE_TYPE_CONTROL_SELF_TEST={label}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
