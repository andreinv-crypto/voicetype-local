param(
    [string]$CandidateRoot = "dist_candidate\VoiceType Local",
    [switch]$SkipShortcutUpdate,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRootFull = [IO.Path]::GetFullPath($ProjectRoot)
$Candidate = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $CandidateRoot))
$ProgramsRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "Programs"))
$InstallRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local"))
$StageRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.staging"))
$BackupRoot = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.previous"))
$ShortcutManifest = [IO.Path]::GetFullPath((Join-Path $ProgramsRoot "VoiceType Local.previous-shortcuts.json"))

foreach ($Path in @($InstallRoot, $StageRoot, $BackupRoot, $ShortcutManifest)) {
    if (-not $Path.StartsWith($ProgramsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside $ProgramsRoot"
    }
}
if (-not $Candidate.StartsWith($ProjectRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Candidate must be inside $ProjectRootFull"
}

$CandidateExe = Join-Path $Candidate "VoiceType Local.exe"
if (-not (Test-Path -LiteralPath $CandidateExe -PathType Leaf)) {
    throw "Candidate executable is missing: $CandidateExe"
}
if (Get-Process -Name "VoiceType Local" -ErrorAction SilentlyContinue) {
    throw "VoiceType Local is running. Exit it before installing the verified candidate."
}

$Shell = New-Object -ComObject WScript.Shell
$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "VoiceType Local.lnk"
$StartupShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "VoiceType Local.lnk"
$ShortcutPaths = @(
    $DesktopShortcutPath,
    $StartupShortcutPath
)

function Save-ShortcutManifest {
    $Records = @()
    foreach ($ShortcutPath in $ShortcutPaths) {
        if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
            $Shortcut = $Shell.CreateShortcut($ShortcutPath)
            $Records += [ordered]@{
                path = $ShortcutPath
                existed = $true
                target_path = $Shortcut.TargetPath
                arguments = $Shortcut.Arguments
                working_directory = $Shortcut.WorkingDirectory
                icon_location = $Shortcut.IconLocation
                description = $Shortcut.Description
                hotkey = $Shortcut.Hotkey
                window_style = $Shortcut.WindowStyle
            }
        }
        else {
            $Records += [ordered]@{ path = $ShortcutPath; existed = $false }
        }
    }
    $Document = [ordered]@{
        schema = 1
        captured_at_utc = [DateTime]::UtcNow.ToString("o")
        shortcuts_updated = $true
        shortcuts = $Records
    }
    $Temporary = "$ShortcutManifest.tmp"
    $Document | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Temporary -Encoding UTF8
    Move-Item -LiteralPath $Temporary -Destination $ShortcutManifest -Force
}

function Save-UnchangedShortcutState {
    # Invalidate any manifest left by an older installation.  Rollback must
    # not touch shortcuts when this installation explicitly skipped them.
    $Document = [ordered]@{
        schema = 1
        captured_at_utc = [DateTime]::UtcNow.ToString("o")
        shortcuts_updated = $false
        shortcuts = @()
    }
    $Temporary = "$ShortcutManifest.tmp"
    $Document | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Temporary -Encoding UTF8
    Move-Item -LiteralPath $Temporary -Destination $ShortcutManifest -Force
}

function Restore-ShortcutManifest {
    if (-not (Test-Path -LiteralPath $ShortcutManifest -PathType Leaf)) {
        return
    }
    $Document = Get-Content -LiteralPath $ShortcutManifest -Raw | ConvertFrom-Json
    if ($Document.schema -ne 1) {
        throw "Unsupported shortcut rollback manifest."
    }
    foreach ($ShortcutPath in $ShortcutPaths) {
        $Record = @($Document.shortcuts) | Where-Object { $_.path -eq $ShortcutPath } | Select-Object -First 1
        if ($null -eq $Record -or -not $Record.existed) {
            if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
                Remove-Item -LiteralPath $ShortcutPath -Force
            }
            continue
        }
        if ([string]::IsNullOrWhiteSpace([string]$Record.target_path)) {
            throw "Previous shortcut target is empty: $ShortcutPath"
        }
        $Shortcut = $Shell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = [string]$Record.target_path
        $Shortcut.Arguments = [string]$Record.arguments
        $Shortcut.WorkingDirectory = [string]$Record.working_directory
        $Shortcut.IconLocation = [string]$Record.icon_location
        $Shortcut.Description = [string]$Record.description
        $Shortcut.Hotkey = [string]$Record.hotkey
        $Shortcut.WindowStyle = [int]$Record.window_style
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

Invoke-SelfTest $CandidateExe "Candidate"

if (Test-Path -LiteralPath $StageRoot) {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $ProgramsRoot -Force | Out-Null
Copy-Item -LiteralPath $Candidate -Destination $StageRoot -Recurse -Force

$StageExe = Join-Path $StageRoot "VoiceType Local.exe"
try {
    Invoke-SelfTest $StageExe "Staged"
}
catch {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force
    throw
}

$InstalledWasMoved = $false
$NewInstallPlaced = $false
$ShortcutManifestCaptured = $false
try {
    if (-not $SkipShortcutUpdate) {
        Save-ShortcutManifest
        $ShortcutManifestCaptured = $true
        $PreviousDocument = Get-Content -LiteralPath $ShortcutManifest -Raw | ConvertFrom-Json
        $PreviousTargets = @($PreviousDocument.shortcuts) |
            Where-Object { $_.existed -and -not [string]::IsNullOrWhiteSpace([string]$_.target_path) } |
            ForEach-Object { [string]$_.target_path } |
            Select-Object -Unique
        foreach ($PreviousTarget in $PreviousTargets) {
            if (-not (Test-Path -LiteralPath $PreviousTarget -PathType Leaf)) {
                throw "Existing shortcut target is unavailable, so rollback cannot be verified: $PreviousTarget"
            }
            Invoke-SelfTest $PreviousTarget "Existing shortcut target"
        }
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        Invoke-SelfTest (Join-Path $InstallRoot "VoiceType Local.exe") "Previous installation"
    }
    if (Test-Path -LiteralPath $BackupRoot) {
        Remove-Item -LiteralPath $BackupRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        Move-Item -LiteralPath $InstallRoot -Destination $BackupRoot
        $InstalledWasMoved = $true
    }
    $NewInstallPlaced = $true
    Move-Item -LiteralPath $StageRoot -Destination $InstallRoot

    $InstalledExe = Join-Path $InstallRoot "VoiceType Local.exe"
    Invoke-SelfTest $InstalledExe "Installed"

    if (-not $SkipShortcutUpdate) {
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
            $Shortcut.TargetPath = $InstalledExe
            $Shortcut.Arguments = [string]$ShortcutSpec.arguments
            $Shortcut.WorkingDirectory = $InstallRoot
            $Shortcut.IconLocation = "$InstalledExe,0"
            $Shortcut.Description = [string]$ShortcutSpec.description
            $Shortcut.Hotkey = ""
            $Shortcut.WindowStyle = 1
            $Shortcut.Save()
        }
    }
    else {
        Save-UnchangedShortcutState
    }

    if (-not $NoLaunch) {
        Start-Process -FilePath $InstalledExe -WorkingDirectory $InstallRoot | Out-Null
    }
    Write-Host "Installed: $InstalledExe"
    if ($InstalledWasMoved) {
        Write-Host "Rollback copy: $BackupRoot"
    }
    elseif ($ShortcutManifestCaptured) {
        Write-Host "Rollback shortcuts: $ShortcutManifest"
    }
}
catch {
    if ($NewInstallPlaced -and (Test-Path -LiteralPath $InstallRoot)) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    if ($InstalledWasMoved -and (Test-Path -LiteralPath $BackupRoot)) {
        Move-Item -LiteralPath $BackupRoot -Destination $InstallRoot
    }
    if ($ShortcutManifestCaptured) {
        Restore-ShortcutManifest
    }
    throw
}
