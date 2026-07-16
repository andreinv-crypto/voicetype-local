from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_install_never_deletes_old_install_before_new_install_is_placed() -> None:
    script = _script("install_candidate.ps1")

    assert "$NewInstallPlaced = $false" in script
    assert "$NewInstallPlaced = $true" in script
    assert "if ($NewInstallPlaced -and (Test-Path -LiteralPath $InstallRoot))" in script
    assert "Invoke-SelfTest $PreviousTarget \"Existing shortcut target\"" in script


def test_first_migration_has_shortcut_manifest_and_verified_rollback() -> None:
    install = _script("install_candidate.ps1")
    rollback = _script("rollback_install.ps1")

    assert "Save-ShortcutManifest" in install
    assert "previous-shortcuts.json" in install
    assert "Restore-ShortcutManifest" in rollback
    assert "Invoke-SelfTest $PreviousExe \"Previous executable\"" in rollback
    assert "Set-InstalledShortcuts" in rollback
    assert "$FreshInstallMoved" in rollback


def test_all_packaging_self_tests_have_timeouts_and_process_guards() -> None:
    for name in ("build.ps1", "install_candidate.ps1", "rollback_install.ps1"):
        script = _script(name)
        assert "WaitForExit(180000)" in script
    assert 'Get-Process -Name "VoiceType Local"' in _script("install_candidate.ps1")
    assert 'Get-Process -Name "VoiceType Local"' in _script("rollback_install.ps1")
