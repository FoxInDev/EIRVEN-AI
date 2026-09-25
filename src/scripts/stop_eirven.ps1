# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
param([switch]$AllInstances)

$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$StopFile = Join-Path $Logs "stop.request"
New-Item -ItemType File -Force -Path $StopFile | Out-Null

$Stopped = 0
$StillRunning = $false
$Targets = @(
    @{ Name = "server.pid"; Marker = "eirven_ai.app" },
    @{ Name = "supervisor.pid"; Marker = "eirven_ai.supervisor" }
)

foreach ($Target in $Targets) {
    $Path = Join-Path $Logs $Target.Name
    if (-not (Test-Path $Path)) { continue }
    $PidValue = Get-Content $Path -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($PidValue -notmatch '^\d+$') {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
        continue
    }
    $TargetPid = [int]$PidValue
    $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$TargetPid" -ErrorAction SilentlyContinue
    if (-not $ProcessInfo) {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
        continue
    }
    if ([string]$ProcessInfo.CommandLine -notlike "*$($Target.Marker)*") {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
        continue
    }
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
    $Stopped++
}

if ($AllInstances) {
    # Old releases could start one copy from the app venv and another from
    # Program Files\Python312. Match only EIRVEN's unique modules; Ollama,
    # unrelated Python work and the installer itself stay running.
    $Pattern = 'eirven_ai\.(supervisor|app|voice_worker|tts_worker)'
    $Processes = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -match $Pattern } |
        Sort-Object ProcessId -Descending
    )
    foreach ($ProcessInfo in $Processes) {
        if (-not (Get-Process -Id $ProcessInfo.ProcessId -ErrorAction SilentlyContinue)) { continue }
        Stop-Process -Id $ProcessInfo.ProcessId -Force -ErrorAction SilentlyContinue
        $Stopped++
    }
}

for ($i = 0; $i -lt 40; $i++) {
    $Remaining = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -match 'eirven_ai\.(supervisor|app|voice_worker|tts_worker)' }
    )
    if (-not $Remaining) { break }
    Start-Sleep -Milliseconds 125
}

if ($AllInstances) {
    $StillRunning = [bool]@(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { [string]$_.CommandLine -match 'eirven_ai\.(supervisor|app|voice_worker|tts_worker)' }
    )
}

if (-not $StillRunning) {
    foreach ($Target in $Targets) {
        Remove-Item (Join-Path $Logs $Target.Name) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
    Write-Host "EIRVEN stopped. Processes: $Stopped" -ForegroundColor Green
    exit 0
}

Write-Host "Some EIRVEN processes are still running." -ForegroundColor Red
exit 5
