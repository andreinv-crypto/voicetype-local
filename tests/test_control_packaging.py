from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voicetype_local.packaged_self_test import (
    EXPECTED_UIAUTOMATION_VERSION,
    REQUIRED_CONTROL_MODULES,
    run_packaged_control_self_test,
)


ROOT = Path(__file__).resolve().parents[1]


def _fake_modules() -> dict[str, object]:
    return {
        "uiautomation": SimpleNamespace(),
        "voicetype_local.windows_control": SimpleNamespace(
            UiaControlBackend=object,
            WindowsControlExecutor=object,
        ),
        "voicetype_local.voice_control": SimpleNamespace(VoiceControlRouter=object),
        "voicetype_local.voice_commands": SimpleNamespace(VoiceCommandParser=object),
        "voicetype_local.dictation_transform": SimpleNamespace(
            transform_dictation=lambda _text: None
        ),
    }


def test_control_packaging_self_test_is_import_only_and_version_pinned() -> None:
    modules = _fake_modules()
    imported: list[str] = []

    def importer(name: str) -> object:
        imported.append(name)
        return modules[name]

    result = run_packaged_control_self_test(
        importer=importer,
        version_reader=lambda name: (
            EXPECTED_UIAUTOMATION_VERSION if name == "uiautomation" else ""
        ),
    )

    assert result == (0, "ok")
    assert imported == list(REQUIRED_CONTROL_MODULES)


def test_control_packaging_self_test_fails_closed_on_import_or_version() -> None:
    modules = _fake_modules()

    def missing_import(name: str) -> object:
        if name == "voicetype_local.voice_commands":
            raise ImportError("not bundled")
        return modules[name]

    import_failure = run_packaged_control_self_test(
        importer=missing_import,
        version_reader=lambda _name: EXPECTED_UIAUTOMATION_VERSION,
    )
    version_failure = run_packaged_control_self_test(
        importer=modules.__getitem__,
        version_reader=lambda _name: "2.0.28",
    )

    assert import_failure[0] != 0
    assert import_failure[1].startswith("import:voicetype_local.voice_commands:")
    assert version_failure == (51, "version:uiautomation")


def test_pyinstaller_and_scripts_include_control_runtime() -> None:
    spec = (ROOT / "VoiceType Local.spec").read_text(encoding="utf-8")
    build = (ROOT / "build.ps1").read_text(encoding="utf-8")
    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    entrypoint = (ROOT / "voicetype_local" / "__main__.py").read_text(
        encoding="utf-8"
    )

    assert "uiautomation==2.0.29" in requirements.splitlines()
    assert "collect_all('uiautomation')" in spec
    assert "copy_metadata('uiautomation')" in spec
    assert "--collect-all uiautomation" in build
    assert "--copy-metadata uiautomation" in build
    for module_name in REQUIRED_CONTROL_MODULES[1:]:
        assert module_name in spec
        assert f"--hidden-import {module_name}" in build
    assert "scripts\\packaged_control_self_test.py" in build
    assert "scripts\\packaged_control_self_test.py" in setup
    assert entrypoint.index("run_packaged_control_self_test") < entrypoint.index(
        "AudioRecorder.input_devices"
    )


def test_packaging_preflight_contains_no_desktop_actions() -> None:
    helper = (ROOT / "voicetype_local" / "packaged_self_test.py").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "scripts" / "packaged_control_self_test.py").read_text(
        encoding="utf-8"
    )
    source = helper + script

    for prohibited in (
        "GetForegroundControl(",
        "GetFocusedControl(",
        "WindowsControlExecutor(",
        "UiaControlBackend(",
        "subprocess.",
        "Start-Process",
        "tk.Tk(",
    ):
        assert prohibited not in source
