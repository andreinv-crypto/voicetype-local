from __future__ import annotations

from voicetype_local.windows_session_editor_probe import (
    run_isolated_session_editor_probe,
)


def main() -> int:
    code, label = run_isolated_session_editor_probe()
    print(f"VOICE_TYPE_SESSION_EDITOR_PROBE={label}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
