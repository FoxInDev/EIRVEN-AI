$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "EIRVEN-AI" `
    --icon "assets\eirven.ico" `
    launcher.py

$Built = Join-Path $Root "dist\EIRVEN-AI.exe"
$Target = Join-Path $Root "EIRVEN-AI.exe"
if (-not (Test-Path $Built)) { throw "PyInstaller did not create EIRVEN-AI.exe" }
Copy-Item -LiteralPath $Built -Destination $Target -Force
Write-Host "Built: $Target" -ForegroundColor Green
