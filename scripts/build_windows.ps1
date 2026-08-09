$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$IconPath = (Resolve-Path (Join-Path $Root "assets\eirven.ico")).Path
$VersionPath = (Resolve-Path (Join-Path $Root "assets\eirven-version.txt")).Path
$BuildName = "EIRVEN-AI-r26"
$Built = Join-Path $Root "dist\$BuildName.exe"
$Target = Join-Path $Root "$BuildName.exe"
$LegacyTarget = Join-Path $Root "EIRVEN-AI.exe"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if (Test-Path "requirements-build.txt") {
    & .\.venv\Scripts\python.exe -m pip install --upgrade -r requirements-build.txt
} else {
    & .\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.10,<7.0"
}
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости сборки" }

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
# Never let a stale spec/resource or old executable survive a rebuild.
Remove-Item -LiteralPath (Join-Path $Root "$BuildName.spec") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "EIRVEN-AI.spec") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Target, $LegacyTarget -Force -ErrorAction SilentlyContinue
& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $BuildName `
    "--icon=$IconPath" `
    --version-file $VersionPath `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $Built)) { throw "PyInstaller did not create $BuildName.exe" }

# Resource sanity only. Exact pixel equality between an extracted PE icon and source ICO
# is intentionally NOT required: Windows/System.Drawing can resample alpha and select a
# different ICO frame, which previously made a perfectly valid build fail at 98%.
Add-Type -AssemblyName System.Drawing
$EmbeddedIcon = [System.Drawing.Icon]::ExtractAssociatedIcon($Built)
if ($null -eq $EmbeddedIcon) { throw "$BuildName.exe has no embedded application icon" }
try {
    $Bitmap = $EmbeddedIcon.ToBitmap()
    try {
        if ($Bitmap.Width -lt 16 -or $Bitmap.Height -lt 16) { throw "Embedded EIRVEN icon is invalid" }
    }
    finally { $Bitmap.Dispose() }
}
finally { $EmbeddedIcon.Dispose() }

Copy-Item -LiteralPath $Built -Destination $Target -Force
Copy-Item -LiteralPath $Built -Destination $LegacyTarget -Force

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EirvenShellNotify {
  [DllImport("shell32.dll")] public static extern void SHChangeNotify(uint eventId, uint flags, IntPtr item1, IntPtr item2);
}
"@ -ErrorAction SilentlyContinue
[EirvenShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Host "Built and resource-verified: $Target" -ForegroundColor Green
