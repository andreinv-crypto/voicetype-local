param([switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$ProgramsRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$InstallRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local"))
$BackupRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.previous"))
$FailedRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.failed"))
$ShortcutManifest = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.previous-shortcuts.json"))

foreach ($Path in @($InstallRoot, $BackupRoot, $FailedRoot, $ShortcutManifest)) {
    if (-not $Path.StartsWith($ProgramsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside $ProgramsRoot"
    }
}
if (Get-Process -Name "VoiceType Local" -ErrorAction SilentlyContinue) {
    throw "VoiceType Local is running. Exit it before rollback."
}

$Shell = New-Object -ComObject WScript.Shell
$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "VoiceType Local.lnk"
$StartupShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "VoiceType Local.lnk"
$ShortcutPaths = @(
    $DesktopShortcutPath,
    $StartupShortcutPath
)

function Read-ShortcutManifest {
    if (-not (Test-Path -LiteralPath $ShortcutManifest -PathType Leaf)) {
        return $null
    }
    $Document = Get-Content -LiteralPath $ShortcutManifest -Raw | ConvertFrom-Json
    if ($Document.schema -ne 1) {
        throw "Unsupported shortcut rollback manifest."
    }
    return $Document
}

function Restore-ShortcutManifest($Document) {
    $UpdatedProperty = $Document.PSObject.Properties["shortcuts_updated"]
    if ($null -ne $UpdatedProperty -and -not [bool]$UpdatedProperty.Value) {
        return
    }
    foreach ($ShortcutPath in $ShortcutPaths) {
        $Record = @($Document.shortcuts) | Where-Object { $_.path -eq $ShortcutPath } | Select-Object -First 1
        if ($null -eq $Record -or -not $Record.existed) {
            if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
                Remove-Item -LiteralPath $ShortcutPath -Force
            }
            continue
        }
        $Target = [string]$Record.target_path
        if ([string]::IsNullOrWhiteSpace($Target) -or -not (Test-Path -LiteralPath $Target -PathType Leaf)) {
            throw "Previous shortcut target is unavailable: $Target"
        }
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = $Target
        $Shortcut.Arguments = [string]$Record.arguments
        $Shortcut.WorkingDirectory = [string]$Record.working_directory
        $Shortcut.IconLocation = [string]$Record.icon_location
        $Shortcut.Description = [string]$Record.description
        $Shortcut.Hotkey = [string]$Record.hotkey
        $Shortcut.WindowStyle = [int]$Record.window_style
        $Shortcut.Save()
    }
}

function Set-InstalledShortcuts([string]$Executable) {
    $WorkingDirectory = Split-Path -Parent $Executable
    $InstalledShortcutSpecs = @(
        [ordered]@{
            path = $DesktopShortcutPath
            arguments = ""
            description = "VoiceType Local - open settings"
        },
        [ordered]@{
            path = $StartupShortcutPath
            arguments = "--background"
            description = "VoiceType Local - background startup"
        }
    )
    foreach ($ShortcutSpec in $InstalledShortcutSpecs) {
        $Shortcut = $Shell.CreateShortcut([string]$ShortcutSpec.path)
        $Shortcut.TargetPath = $Executable
        $Shortcut.Arguments = [string]$ShortcutSpec.arguments
        $Shortcut.WorkingDirectory = $WorkingDirectory
        $Shortcut.IconLocation = "$Executable,0"
        $Shortcut.Description = [string]$ShortcutSpec.description
        $Shortcut.Hotkey = ""
        $Shortcut.WindowStyle = 1
        $Shortcut.Save()
    }
}

function Invoke-SelfTest([string]$FilePath, [string]$Label) {
    $Process = Start-Process -FilePath $FilePath -ArgumentList "--self-test" -PassThru -WindowStyle Hidden
    if (-not $Process.WaitForExit(180000)) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $Process.Id /T /F | Out-Null
        throw "$Label self-test timed out."
    }
    if ($Process.ExitCode -ne 0) {
        throw "$Label self-test failed with exit code $($Process.ExitCode)."
    }
}

$Manifest = Read-ShortcutManifest
$ShortcutsWereUpdated = $false
if ($null -ne $Manifest) {
    $UpdatedProperty = $Manifest.PSObject.Properties["shortcuts_updated"]
    $ShortcutsWereUpdated = $null -eq $UpdatedProperty -or [bool]$UpdatedProperty.Value
}
$BackupExe = Join-Path $BackupRoot "VoiceType Local.exe"
if (Test-Path -LiteralPath $BackupExe -PathType Leaf) {
    Invoke-SelfTest $BackupExe "Previous installation"
    if (Test-Path -LiteralPath $FailedRoot) {
        Remove-Item -LiteralPath $FailedRoot -Recurse -Force
    }
    try {
        if (Test-Path -LiteralPath $InstallRoot) {
            Move-Item -LiteralPath $InstallRoot -Destination $FailedRoot
        }
        Move-Item -LiteralPath $BackupRoot -Destination $InstallRoot
        $Exe = Join-Path $InstallRoot "VoiceType Local.exe"
        Invoke-SelfTest $Exe "Rollback executable"
        if ($ShortcutsWereUpdated) {
            Restore-ShortcutManifest $Manifest
        }
    }
    catch {
        if (Test-Path -LiteralPath $InstallRoot) {
            Move-Item -LiteralPath $InstallRoot -Destination $BackupRoot
        }
        if (Test-Path -LiteralPath $FailedRoot) {
            Move-Item -LiteralPath $FailedRoot -Destination $InstallRoot
        }
        $RestoredCurrentExe = Join-Path $InstallRoot "VoiceType Local.exe"
        if ($ShortcutsWereUpdated -and (Test-Path -LiteralPath $RestoredCurrentExe -PathType Leaf)) {
            Set-InstalledShortcuts $RestoredCurrentExe
        }
        throw
    }
    if (-not $NoLaunch) {
        Start-Process -FilePath $Exe -WorkingDirectory $InstallRoot | Out-Null
    }
    Write-Host "Rolled back to: $Exe"
    return
}

if ($null -eq $Manifest) {
    throw "No previous installation or shortcut rollback manifest exists."
}
$PreviousTargets = @($Manifest.shortcuts) |
    Where-Object { $_.existed -and -not [string]::IsNullOrWhiteSpace([string]$_.target_path) } |
    ForEach-Object { [string]$_.target_path } |
    Select-Object -Unique
$PreviousExe = $PreviousTargets | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($PreviousExe)) {
    $HadPreviousShortcut = @($Manifest.shortcuts) | Where-Object { $_.existed } | Select-Object -First 1
    if ($null -ne $HadPreviousShortcut) {
        throw "The previous executable recorded in the shortcut manifest is unavailable."
    }
    if (Test-Path -LiteralPath $FailedRoot) {
        Remove-Item -LiteralPath $FailedRoot -Recurse -Force
    }
    $FreshInstallMoved = $false
    try {
        if (Test-Path -LiteralPath $InstallRoot) {
            Move-Item -LiteralPath $InstallRoot -Destination $FailedRoot
            $FreshInstallMoved = $true
        }
        Restore-ShortcutManifest $Manifest
    }
    catch {
        if ($FreshInstallMoved -and (Test-Path -LiteralPath $FailedRoot)) {
            Move-Item -LiteralPath $FailedRoot -Destination $InstallRoot
            Set-InstalledShortcuts (Join-Path $InstallRoot "VoiceType Local.exe")
        }
        throw
    }
    Write-Host "Rolled back to the previous state: VoiceType Local was not installed."
    return
}
Invoke-SelfTest $PreviousExe "Previous executable"
if (Test-Path -LiteralPath $FailedRoot) {
    Remove-Item -LiteralPath $FailedRoot -Recurse -Force
}
if (Test-Path -LiteralPath $InstallRoot) {
    Move-Item -LiteralPath $InstallRoot -Destination $FailedRoot
}
try {
    Restore-ShortcutManifest $Manifest
}
catch {
    if (Test-Path -LiteralPath $FailedRoot) {
        Move-Item -LiteralPath $FailedRoot -Destination $InstallRoot
        Set-InstalledShortcuts (Join-Path $InstallRoot "VoiceType Local.exe")
    }
    throw
}
if (-not $NoLaunch) {
    Start-Process -FilePath $PreviousExe -WorkingDirectory (Split-Path -Parent $PreviousExe) | Out-Null
}
Write-Host "Rolled back to shortcut target: $PreviousExe"
