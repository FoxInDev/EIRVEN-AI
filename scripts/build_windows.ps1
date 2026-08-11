$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$IconPath = (Resolve-Path (Join-Path $Root "assets\eirven.ico")).Path
$VersionPath = (Resolve-Path (Join-Path $Root "assets\eirven-version.txt")).Path
$BuildName = "EIRVEN-AI"
$Built = Join-Path $Root "dist\$BuildName.exe"
$LegacyTarget = Join-Path $Root "EIRVEN-AI.exe"
$VersionedTarget = Join-Path $Root "EIRVEN-AI-r37.exe"
$StagingDir = Join-Path $Root ".launcher-update"
$StagedTarget = Join-Path $StagingDir "EIRVEN-AI.exe.new"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}
& .\.venv\Scripts\python.exe -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
if ($LASTEXITCODE -ne 0) {
    if (Test-Path "requirements-build.txt") {
        & .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
    } else {
        & .\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.10,<7.0"
    }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости сборки" }
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "$BuildName.spec") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "EIRVEN-AI-r37.spec") -Force -ErrorAction SilentlyContinue

& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $BuildName `
    --collect-all imageio_ffmpeg `
    "--icon=$IconPath" `
    --version-file $VersionPath `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $Built)) { throw "PyInstaller did not create $BuildName.exe" }

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

# Always publish a fresh versioned launcher. This one is also used by shortcuts/autostart.
Copy-Item -LiteralPath $Built -Destination $VersionedTarget -Force

# Keep the historical EIRVEN-AI.exe name synchronized too. If that old executable is
# currently the process that launched this repair, Windows locks it. Stage a replacement
# and copy it immediately after the old process exits instead of leaving the stale EXE.
try {
    Copy-Item -LiteralPath $Built -Destination $LegacyTarget -Force -ErrorAction Stop
    Write-Host "Legacy launcher synchronized: $LegacyTarget" -ForegroundColor Green
} catch {
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null
    Copy-Item -LiteralPath $Built -Destination $StagedTarget -Force
    $ReplaceScript = Join-Path $PSScriptRoot "replace_launcher_when_free.ps1"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
        ('"' + $ReplaceScript + '"'),'-Source',('"' + $StagedTarget + '"'),
        '-Target',('"' + $LegacyTarget + '"'),'-TimeoutSeconds','120'
    ) | Out-Null
    Write-Host "EIRVEN-AI.exe is currently running; replacement scheduled after exit." -ForegroundColor Yellow
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EirvenShellNotify {
  [DllImport("shell32.dll")] public static extern void SHChangeNotify(uint eventId, uint flags, IntPtr item1, IntPtr item2);
}
"@ -ErrorAction SilentlyContinue
[EirvenShellNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Host "Built and resource-verified: $VersionedTarget" -ForegroundColor Green
