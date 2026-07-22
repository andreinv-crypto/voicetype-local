from __future__ import annotations

import os
from pathlib import Path

import pytest

from voicetype_local.windows_targeted_inserter_probe import (
    run_isolated_targeted_inserter_probe,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows native probe")
def test_isolated_targeted_inserter_probe_round_trips_and_times_out() -> None:
    assert run_isolated_targeted_inserter_probe() == (0, "ok")


def test_isolated_targeted_inserter_probe_never_targets_the_desktop() -> None:
    source = (
        ROOT / "voicetype_local" / "windows_targeted_inserter_probe.py"
    ).read_text(encoding="utf-8")

    for prohibited in (
        "EnumWindows(",
        "FindWindow(",
        "GetForegroundWindow(",
        "GetFocus(",
        "SetFocus(",
        "SetForegroundWindow(",
        "ShowWindow(",
        "uiautomation",
    ):
        assert prohibited not in source
