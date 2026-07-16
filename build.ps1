param(
    [string]$DistRoot = "dist",
    [string]$WorkRoot = "build"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup.ps1 first."
}
if (-not (Test-Path -LiteralPath "models\small\model.bin")) {
    throw "Offline model is missing. Run setup.ps1 first."
}

& $Python scripts\download_model.py --model small --output models\small
if ($LASTEXITCODE -ne 0) { throw "Offline model integrity check failed." }
& $Python -m pytest -q
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
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all av `
    --collect-all sounddevice `
    --hidden-import pystray._win32 `
    --hidden-import pynput.keyboard._win32 `
    voicetype_local\__main__.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$DistDirectory = Join-Path $ProjectRoot $DistRoot
$Exe = Join-Path $DistDirectory "VoiceType Local\VoiceType Local.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Build did not produce $Exe"
}
Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination (Join-Path $DistDirectory "VoiceType Local\THIRD_PARTY_NOTICES.md") -Force
$SelfTest = Start-Process -FilePath $Exe -ArgumentList "--self-test" -PassThru -Wait -WindowStyle Hidden
if ($SelfTest.ExitCode -ne 0) {
    throw "Packaged self-test failed with exit code $($SelfTest.ExitCode)."
}
$UiSmoke = Start-Process -FilePath $Exe -ArgumentList "--ui-smoke-test" -PassThru -WindowStyle Hidden
if (-not $UiSmoke.WaitForExit(15000)) {
    Stop-Process -Id $UiSmoke.Id -Force
    throw "Packaged UI smoke test timed out."
}
if ($UiSmoke.ExitCode -ne 0) {
    throw "Packaged UI smoke test failed with exit code $($UiSmoke.ExitCode)."
}
Write-Host "Built: $Exe"
