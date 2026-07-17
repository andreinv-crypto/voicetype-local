param(
    [string]$DistRoot = "dist_candidate",
    [string]$WorkRoot = "build_candidate"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

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

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup.ps1 first."
}
& $Python scripts\packaged_control_self_test.py
if ($LASTEXITCODE -ne 0) { throw "VoiceType Control packaging preflight failed." }
if (-not (Test-Path -LiteralPath "models\small\model.bin")) {
    throw "Offline model is missing. Run setup.ps1 first."
}

& $Python scripts\download_model.py --model small --output models\small
if ($LASTEXITCODE -ne 0) { throw "Offline model integrity check failed." }
& $Python -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
& $Python scripts\generate_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --distpath $DistRoot `
    --workpath $WorkRoot `
    --windowed `
    --name "VoiceType Local" `
    --icon "assets\voicetype.ico" `
    --version-file "assets\version_info.txt" `
    --add-data "models\small;models\small" `
    --add-data "assets\packs;assets\packs" `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-all sounddevice `
    --collect-all comtypes `
    --collect-all uiautomation `
    --copy-metadata uiautomation `
    --hidden-import pystray._win32 `
    --hidden-import pynput.keyboard._win32 `
    --hidden-import voicetype_local.packaged_self_test `
    --hidden-import voicetype_local.windows_control `
    --hidden-import voicetype_local.voice_control `
    --hidden-import voicetype_local.voice_commands `
    --hidden-import voicetype_local.dictation_transform `
    --exclude-module IPython `
    --exclude-module _pytest `
    --exclude-module comtypes.test `
    --exclude-module jupyter `
    --exclude-module matplotlib `
    --exclude-module pandas `
    --exclude-module pytest `
    --exclude-module scipy `
    --exclude-module tensorflow `
    --exclude-module torch `
    voicetype_local\__main__.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$DistDirectory = Join-Path $ProjectRoot $DistRoot
$Exe = Join-Path $DistDirectory "VoiceType Local\VoiceType Local.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Build did not produce $Exe"
}
$BundledComtypesTests = Join-Path $DistDirectory "VoiceType Local\_internal\comtypes\test"
if (Test-Path -LiteralPath $BundledComtypesTests) {
    Remove-Item -LiteralPath $BundledComtypesTests -Recurse -Force
}
Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination (Join-Path $DistDirectory "VoiceType Local\THIRD_PARTY_NOTICES.md") -Force
foreach ($Document in @("README.md", "RELEASE_NOTES_0.2.0-rc1.md", "PRIVACY.md", "SECURITY.md", "LICENSE", "CONTRIBUTING.md")) {
    if (Test-Path -LiteralPath $Document) {
        Copy-Item -LiteralPath $Document -Destination (Join-Path $DistDirectory "VoiceType Local\$Document") -Force
    }
}
if (Test-Path -LiteralPath "docs") {
    Copy-Item -LiteralPath "docs" -Destination (Join-Path $DistDirectory "VoiceType Local\docs") -Recurse -Force
}
Invoke-SelfTest $Exe "Packaged"
$UiSmoke = Start-Process -FilePath $Exe -ArgumentList "--ui-smoke-test" -PassThru -WindowStyle Hidden
if (-not $UiSmoke.WaitForExit(15000)) {
    Stop-Process -Id $UiSmoke.Id -Force
    throw "Packaged UI smoke test timed out."
}
if ($UiSmoke.ExitCode -ne 0) {
    throw "Packaged UI smoke test failed with exit code $($UiSmoke.ExitCode)."
}
Write-Host "Built: $Exe"
