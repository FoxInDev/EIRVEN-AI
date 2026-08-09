$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
$StopFile = Join-Path $Logs "stop.request"
New-Item -ItemType File -Force -Path $StopFile | Out-Null

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

    # A PID file is not a process identity. Windows can reuse an old EIRVEN PID for an
    # unrelated process; never kill it unless the command line still belongs to EIRVEN.
    $CommandLine = [string]$ProcessInfo.CommandLine
    if ($CommandLine -notlike "*$($Target.Marker)*") {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
        continue
    }

    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 100
    }
    if (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {
        $StillRunning = $true
    } else {
        Remove-Item $Path -Force -ErrorAction SilentlyContinue
    }
}

# If both EIRVEN processes are gone, the marker has done its job. A stale stop.request
# must not poison the next launcher run.
if (-not $StillRunning) {
    Remove-Item $StopFile -Force -ErrorAction SilentlyContinue
}
Write-Host "EIRVEN stopped." -ForegroundColor Green
