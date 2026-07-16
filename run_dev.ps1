$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup.ps1 first."
}
Start-Process -FilePath $Python -ArgumentList "-m", "voicetype_local" -WorkingDirectory $ProjectRoot -WindowStyle Hidden

