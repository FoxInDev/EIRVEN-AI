$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$IconPath = (Resolve-Path (Join-Path $Root "assets\eirven.ico")).Path
$VersionPath = (Resolve-Path (Join-Path $Root "assets\eirven-version.txt")).Path
$Built = Join-Path $Root "dist\EIRVEN-AI.exe"
$Target = Join-Path $Root "EIRVEN-AI.exe"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip pyinstaller

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
# Never let an old purple executable survive a rebuild/copy failure.
Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "EIRVEN-AI" `
    --icon $IconPath `
    --version-file $VersionPath `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

if (-not (Test-Path $Built)) { throw "PyInstaller did not create EIRVEN-AI.exe" }

# Confirm that the generated Windows binary actually exposes an embedded icon resource.
Add-Type -AssemblyName System.Drawing
$EmbeddedIcon = [System.Drawing.Icon]::ExtractAssociatedIcon($Built)
if ($null -eq $EmbeddedIcon) { throw "EIRVEN-AI.exe has no embedded application icon" }
try {
    $Bitmap = $EmbeddedIcon.ToBitmap()
    if ($Bitmap.Width -lt 16 -or $Bitmap.Height -lt 16) {
        throw "Embedded EIRVEN icon is invalid"
    }
    $Bitmap.Dispose()
}
finally {
    $EmbeddedIcon.Dispose()
}

Copy-Item -LiteralPath $Built -Destination $Target -Force
Write-Host "Built: $Target" -ForegroundColor Green
