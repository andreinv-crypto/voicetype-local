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


def test_desktop_opens_ui_while_startup_is_explicitly_backgrounded() -> None:
    script = _script("install_candidate.ps1")

    assert '$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop"))' in script
    assert '$StartupShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup"))' in script
    assert "path = $DesktopShortcutPath\n                arguments = \"\"" in script
    assert 'path = $StartupShortcutPath\n                arguments = "--background"' in script


def test_update_rollback_restores_exact_shortcut_manifest() -> None:
    rollback = _script("rollback_install.ps1")
    backup_branch = rollback.split(
        'if (Test-Path -LiteralPath $BackupExe -PathType Leaf) {', 1
    )[1].split('if ($null -eq $Manifest) {', 1)[0]

    assert "Restore-ShortcutManifest $Manifest" in backup_branch
    assert "Set-InstalledShortcuts $RestoredCurrentExe" in backup_branch
    assert 'path = $StartupShortcutPath\n            arguments = "--background"' in rollback


def test_skip_shortcut_update_invalidates_stale_manifest_and_rollback_leaves_shortcuts() -> None:
    install = _script("install_candidate.ps1")
    rollback = _script("rollback_install.ps1")

    assert "function Save-UnchangedShortcutState" in install
    assert "shortcuts_updated = $false" in install
    assert "else {\n        Save-UnchangedShortcutState\n    }" in install
    assert 'Properties["shortcuts_updated"]' in rollback
    assert "if ($ShortcutsWereUpdated)" in rollback
