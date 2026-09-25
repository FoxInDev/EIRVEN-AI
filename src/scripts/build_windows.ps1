# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$IconPath = (Resolve-Path (Join-Path $Root "assets\eirven.ico")).Path
$VersionPath = (Resolve-Path (Join-Path $Root "assets\eirven-version.txt")).Path
$BuildName = "EIRVEN-AI"
$Built = Join-Path $Root "dist\$BuildName.exe"
$LegacyTarget = Join-Path $Root "EIRVEN-AI.exe"
$VersionedTarget = Join-Path $Root "EIRVEN-AI-r72.exe"
$UnifiedTarget = Join-Path $Root "EIRVEN.exe"
$StagingDir = Join-Path $Root ".launcher-update"
$StagedTarget = Join-Path $StagingDir "EIRVEN-AI.exe.new"
$StagedUnifiedTarget = Join-Path $StagingDir "EIRVEN.exe.new"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
}

# A bare python.exe error (e.g. "PyInstaller not installed yet" on first run) gets
# reinterpreted by PowerShell as a terminating NativeCommandError under
# $ErrorActionPreference=Stop, killing the script before it reaches the branch that
# would have installed PyInstaller. Scope Continue + *> $null (not 2>$null) to just
# this detection call so it can't do that, then restore Stop immediately after.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& .\.venv\Scripts\python.exe -c "import PyInstaller; print(PyInstaller.__version__)" *> $null
$pyInstallerCheckExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($pyInstallerCheckExit -ne 0) {
    if (Test-Path "requirements-build.txt") {
        & .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
    } else {
        & .\.venv\Scripts\python.exe -m pip install "pyinstaller>=6.10,<7.0"
    }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости сборки" }
}

# PyInstaller packages exactly what is physically installed in this venv -- not what
# the app needs. A venv that only ever received requirements-build.txt (or was just
# created by the step above) silently produces a smaller, broken EXE instead of
# failing here where the real cause would be visible. This list is copied from
# scripts/bootstrap.py's own install loop (read from its source, not inferred from a
# log) plus torch, which no requirements*.txt lists -- bootstrap.py always installs
# it as its own separate step because Silero/Baya depends on it directly. Running
# this unconditionally every time is deliberate: pip is fast and idempotent when a
# package is already satisfied, and that's cheaper than trusting an import-check
# that could itself miss the next thing that turns out not to be in requirements.txt.
foreach ($req in @("requirements.txt", "requirements-voice.txt", "requirements-desktop.txt", "requirements-integrations.txt")) {
    if (-not (Test-Path $req)) { continue }
    if ($req -eq "requirements-desktop.txt") {
        # Mirrors bootstrap.py: opencv-python and opencv-contrib-python can both end up
        # installed across venv rebuilds and fight over the same cv2 namespace. This is
        # best-effort cleanup -- "not installed" is expected and fine on a fresh venv,
        # not an error -- but PowerShell's Stop mode still turns pip's own warning text
        # into a fatal NativeCommandError unless Continue is scoped around the call,
        # same fix as the PyInstaller detection above.
        $prevEAP3 = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & .\.venv\Scripts\python.exe -m pip uninstall -y opencv-python opencv-python-headless opencv-contrib-python opencv-contrib-python-headless mediapipe *> $null
        $ErrorActionPreference = $prevEAP3
    }
    & .\.venv\Scripts\python.exe -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости приложения из $req" }
}
& .\.venv\Scripts\python.exe -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить пакет eirven-ai (pip install -e .)" }
& .\.venv\Scripts\python.exe -m pip install --upgrade --no-cache-dir torch
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить PyTorch (нужен для голоса Baya)" }

$HooksDir = Join-Path $Root "pyinstaller-hooks"
$WebrtcvadHook = Join-Path $HooksDir "hook-webrtcvad.py"
if (-not (Test-Path $WebrtcvadHook)) {
    New-Item -ItemType Directory -Path $HooksDir -Force | Out-Null
    # The installed distribution is webrtcvad-wheels (prebuilt Windows wheels), but
    # the community PyInstaller hook hardcodes copy_metadata('webrtcvad') and crashes
    # with PackageNotFoundError since no distribution is registered under that exact
    # name. Nothing in this codebase reads webrtcvad's own package metadata at
    # runtime, so overriding the copy to a no-op is safe; the module itself still
    # gets bundled normally through PyInstaller's regular import analysis.
    "datas = []`n" | Set-Content -LiteralPath $WebrtcvadHook -Encoding utf8
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "$BuildName.spec") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $Root "EIRVEN-AI-r38.spec") -Force -ErrorAction SilentlyContinue

& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name $BuildName `
    --additional-hooks-dir "$Root\pyinstaller-hooks" `
    --collect-all imageio_ffmpeg `
    --collect-all torch `
    --hidden-import numpy `
    --hidden-import eirven_ai.app `
    --hidden-import eirven_ai.tts_worker `
    --hidden-import eirven_ai.voice_worker `
    --add-data "$Root\src;src" `
    --add-data "$Root\scripts;scripts" `
    --add-data "$Root\assets;assets" `
    --add-data "$Root\.env.example;." `
    --add-data "$Root\launcher.py;." `
    --add-data "$Root\pyproject.toml;." `
    --add-data "$Root\requirements.txt;." `
    --add-data "$Root\requirements-desktop.txt;." `
    --add-data "$Root\requirements-integrations.txt;." `
    --add-data "$Root\requirements-voice.txt;." `
    --add-data "$Root\requirements-build.txt;." `
    --add-data "$Root\UNINSTALL.cmd;." `
    --add-data "$Root\BUILD_INFO.json;." `
    --add-data "$Root\EIRVEN_VERSION.txt;." `
    --add-data "$Root\LICENSE;." `
    --add-data "$Root\README.md;." `
    --add-data "$Root\SECURITY.md;." `
    --add-data "$Root\THIRD_PARTY_NOTICES.md;." `
    "--icon=$IconPath" `
    --version-file $VersionPath `
    --noupx `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
if (-not (Test-Path $Built)) { throw "PyInstaller did not create $BuildName.exe" }

$BuiltVersion = (Get-Item -LiteralPath $Built).VersionInfo.FileVersion.Trim()
if ($BuiltVersion -ne "2.4.0.72") {
    throw "Unexpected launcher version: $BuiltVersion (expected 2.4.0.72)"
}

$BuiltSizeMB = [math]::Round((Get-Item -LiteralPath $Built).Length / 1MB, 1)
Write-Host "Built size: $BuiltSizeMB MB" -ForegroundColor Cyan
if ($BuiltSizeMB -lt 100) {
    throw "Built EXE is only $BuiltSizeMB MB (expected roughly 120-135 MB). A dependency is almost certainly missing from .venv even though PyInstaller did not error -- do not ship this file. Re-run this script; if the size is still low, check the foreach install loop above ran without skipping."
}

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

# Real launch smoke-test: start the built EXE and wait for its own web UI to actually
# answer, instead of just checking the process hasn't exited (a crash dialog from
# PyInstaller's windowed traceback handler would leave the process "running" forever
# while doing nothing useful, which a HasExited check alone would miss entirely).
# This is what would have caught both the missing-PIL and missing-torch builds
# automatically, before anyone had to download and run them by hand to find out.
# Run the smoke test on a throwaway copy in TEMP. Launching the real EXE here made
# a live EIRVEN write its bundled src/ over the install folder and hold file locks on
# the very targets the publish step below needs to overwrite -- so a build could
# silently leave the old EIRVEN-AI.exe in place.
$SmokeExe = Join-Path ([System.IO.Path]::GetTempPath()) "EIRVEN-smoke-$([guid]::NewGuid().ToString('N')).exe"
Copy-Item -LiteralPath $Built -Destination $SmokeExe -Force
Write-Host "Launch smoke-test: starting $SmokeExe ..." -ForegroundColor Cyan
# Перед проверкой — остановить открытую Эрви. Иначе на порту 7860 отвечает она,
# а не только что собранный exe, и проверка оценивает не ту программу. Новый
# лончер всё равно закрывает прежнюю копию при запуске, так что для владельца
# ничего не меняется, а проверка становится однозначной. Ollama не трогаем.
$StopScript = Join-Path $PSScriptRoot "stop_eirven.ps1"
if (Test-Path -LiteralPath $StopScript) {
    Write-Host "Stopping any running EIRVEN before the smoke-test ..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $StopScript -AllInstances | Out-Null
    for ($w = 0; $w -lt 20; $w++) {
        $busy = $false
        try {
            $probe = New-Object System.Net.Sockets.TcpClient
            $probe.Connect("127.0.0.1", 7860)
            $busy = $probe.Connected
            $probe.Close()
        } catch { $busy = $false }
        if (-not $busy) { break }
        Start-Sleep -Milliseconds 500
    }
}
$ExpectedBuild = (Get-Content -LiteralPath (Join-Path $Root "BUILD_INFO.json") -Raw | ConvertFrom-Json).build
$SawOtherBuild = $false
Write-Host "Launch smoke-test expects build $ExpectedBuild" -ForegroundColor Cyan
$Proc = Start-Process -FilePath $SmokeExe -PassThru
$Ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if ($Proc.HasExited) { break }
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:7860/api/ping" -UseBasicParsing -TimeoutSec 2
        # Отвечать может открытая старая Эрви на том же порту и той же версии.
        # Засчитываем ответ, только если он от сборки, которую мы сейчас собрали.
        if ($resp.StatusCode -eq 200) {
            $pong = $resp.Content | ConvertFrom-Json
            if ($pong.build -eq $ExpectedBuild) { $Ready = $true; break }
            elseif (-not $SawOtherBuild) {
                Write-Host "Port 7860 is answered by another EIRVEN build ($($pong.build)); waiting for $ExpectedBuild ..." -ForegroundColor Yellow
                $SawOtherBuild = $true
            }
        }
    } catch { }
}
# Эрви теперь только приложение: интерфейс открывается лишь с ключом сеанса.
# Проверяем это на настоящем собранном exe, пока сервер жив. Если ключи сломаны,
# окно Эрви не откроется вовсе — такой exe выпускать нельзя.
$KeyProblem = $null
if ($Ready) {
    $KeyFile = Join-Path $Root "data\app.key"
    $Key = $null
    for ($k = 0; $k -lt 20; $k++) {
        if (Test-Path -LiteralPath $KeyFile) { $Key = (Get-Content -LiteralPath $KeyFile -Raw).Trim(); if ($Key) { break } }
        Start-Sleep -Milliseconds 250
    }
    if (-not $Key) {
        $KeyProblem = "server did not write data\app.key"
    } else {
        $Blocked = $false
        try { Invoke-WebRequest -Uri "http://127.0.0.1:7860/ui/" -UseBasicParsing -TimeoutSec 3 | Out-Null }
        catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) { $Blocked = $true } }
        if (-not $Blocked) { $KeyProblem = "/ui/ opened WITHOUT the key - interface is not protected" }
        else {
            try {
                $withKey = Invoke-WebRequest -Uri "http://127.0.0.1:7860/api/preferences" -Headers @{ "X-Eirven-Key" = $Key } -UseBasicParsing -TimeoutSec 5
                if ($withKey.StatusCode -ne 200) { $KeyProblem = "/api/preferences with key returned $($withKey.StatusCode)" }
            } catch { $KeyProblem = "/api/preferences with key failed: $($_.Exception.Message)" }
        }
    }
    if (-not $KeyProblem) { Write-Host "App-key check passed: browser blocked, app window allowed" -ForegroundColor Green }
}
if (-not $Proc.HasExited) {
    # Kill the whole tree: the launcher spawns the API server and voice workers, and
    # a surviving child keeps port 7860 and the file lock held.
    Start-Process -FilePath "taskkill" -ArgumentList @("/PID", "$($Proc.Id)", "/T", "/F") -NoNewWindow -Wait -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 3
Remove-Item -LiteralPath $SmokeExe -Force -ErrorAction SilentlyContinue
if (-not $Ready) {
    $ExitNote = if ($Proc.HasExited) { "process exited with code $($Proc.ExitCode)" } else { "process was still running but never answered" }
    throw "Launch smoke-test failed ($ExitNote): http://127.0.0.1:7860/api/ping never came up within 60s. Do not ship this EXE. Run '.venv\Scripts\python.exe launcher.py' directly in this same folder to see the real traceback."
}
Write-Host "Launch smoke-test passed: EIRVEN answered at http://127.0.0.1:7860/api/ping" -ForegroundColor Green
if ($KeyProblem) { throw "App-key check failed: $KeyProblem. Do not ship this EXE." }

# Always publish a fresh versioned launcher. This one is also used by shortcuts/autostart.
Copy-Item -LiteralPath $Built -Destination $VersionedTarget -Force

# The public and installed entry point has one stable name: EIRVEN.exe.
try {
    Copy-Item -LiteralPath $Built -Destination $UnifiedTarget -Force -ErrorAction Stop
    Write-Host "Unified launcher synchronized: $UnifiedTarget" -ForegroundColor Green
} catch {
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null
    Copy-Item -LiteralPath $Built -Destination $StagedUnifiedTarget -Force
    $ReplaceScript = Join-Path $PSScriptRoot "replace_launcher_when_free.ps1"
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
        ('"' + $ReplaceScript + '"'),'-Source',('"' + $StagedUnifiedTarget + '"'),
        '-Target',('"' + $UnifiedTarget + '"'),'-TimeoutSeconds','120'
    ) | Out-Null
    Write-Host "EIRVEN.exe is currently running; replacement scheduled after exit." -ForegroundColor Yellow
}

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
Write-Host "Built, resource-verified, and launch-tested: $VersionedTarget ($BuiltSizeMB MB)" -ForegroundColor Green
