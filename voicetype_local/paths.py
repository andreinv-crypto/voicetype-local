from __future__ import annotations

import os
import sys
from pathlib import Path


APP_DIR_NAME = "VoiceTypeLocal"


def resource_root() -> Path:
    """Return the source root or PyInstaller data directory."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """Return a per-user directory for settings only (never transcripts/audio)."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    target = base / APP_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def model_dir(model_name: str) -> Path:
    override = os.environ.get("VOICETYPE_MODEL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return resource_root() / "models" / model_name

