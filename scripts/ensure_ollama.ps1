# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
param(
    [switch]$InstallIfMissing = $true,
    [switch]$StartServer = $true
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$env:no_proxy = '127.0.0.1,localhost,::1'

function Get-EirvenOllamaExe {
    try {
        $cmd = Get-Command ollama.exe -ErrorAction Stop
        if ($cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) { return $cmd.Source }
    } catch {}
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Add-EirvenOllamaPath([string]$Exe) {
    if (-not $Exe) { return }
    $dir = Split-Path -Parent $Exe
    $parts = @($env:Path -split ';' | Where-Object { $_ })
    if ($parts -notcontains $dir) { $env:Path = "$dir;$env:Path" }
    try {
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $userParts = @($userPath -split ';' | Where-Object { $_ })
        if ($userParts -notcontains $dir) {
            $newUserPath = if ($userPath) { "$userPath;$dir" } else { $dir }
            [Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
        }
    } catch {}
}

function Test-EirvenOllamaApi {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Wait-EirvenOllamaApi([int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-EirvenOllamaApi) { return $true }
        Start-Sleep -Milliseconds 750
    }
    return $false
}

function Format-EirvenBytes([double]$Value) {
    if ($Value -ge 1GB) { return ('{0:N1} ГБ' -f ($Value / 1GB)) }
    if ($Value -ge 1MB) { return ('{0:N1} МБ' -f ($Value / 1MB)) }
    if ($Value -ge 1KB) { return ('{0:N1} КБ' -f ($Value / 1KB)) }
    return ('{0:N0} Б' -f $Value)
}

function Download-EirvenOllamaInstaller([string]$Destination) {
    $urls = @()
    if ($env:EIRVEN_BOOTSTRAP_MIRROR) {
        $urls += ($env:EIRVEN_BOOTSTRAP_MIRROR.TrimEnd('/') + '/OllamaSetup.exe')
    }
    $urls += 'https://ollama.com/download/OllamaSetup.exe'
    $urls = @($urls | Select-Object -Unique)
    $url = $urls[-1]
    foreach ($candidate in $urls) {
        try {
            $probe = Invoke-WebRequest -UseBasicParsing -Method Head -Uri $candidate -TimeoutSec 12
            if ($probe.StatusCode -ge 200 -and $probe.StatusCode -lt 400) { $url = $candidate; break }
        } catch {}
    }
    Write-Host 'EIRVEN: скачиваю официальный OllamaSetup.exe...' -ForegroundColor Cyan
    Write-Host "EIRVEN: источник Ollama: $url" -ForegroundColor DarkGray
    Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue

    $downloaded = $false
    $bitsJob = $null
    try {
        if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) {
            $bitsJob = Start-BitsTransfer -Source $url -Destination $Destination -Asynchronous -DisplayName 'EIRVEN Ollama Runtime' -Description 'Ollama runtime for EIRVEN' -ErrorAction Stop
            $lastBytes = 0.0
            $lastAt = Get-Date
            while ($true) {
                $bitsJob = Get-BitsTransfer -Id $bitsJob.Id -ErrorAction Stop
                $state = [string]$bitsJob.JobState
                $now = Get-Date
                $bytes = [double]$bitsJob.BytesTransferred
                $total = [double]$bitsJob.BytesTotal
                $seconds = [Math]::Max(0.2, ($now - $lastAt).TotalSeconds)
                $rate = [Math]::Max(0.0, ($bytes - $lastBytes) / $seconds)
                $pct = if ($total -gt 0) { [Math]::Min(100.0, 100.0 * $bytes / $total) } else { 0.0 }
                $doneText = Format-EirvenBytes $bytes
                $totalText = if ($total -gt 0) { Format-EirvenBytes $total } else { '?' }
                $rateText = if ($rate -gt 0) { (Format-EirvenBytes $rate) + '/с' } else { 'ожидание' }
                Write-Host -NoNewline ("`rOllama: {0,5:N1}%  {1} / {2}  {3}      " -f $pct,$doneText,$totalText,$rateText)
                $lastBytes = $bytes
                $lastAt = $now

                if ($state -eq 'Transferred') {
                    Complete-BitsTransfer -BitsJob $bitsJob
                    $bitsJob = $null
                    Write-Host "`rOllama: 100.0%  $totalText / $totalText  готово                    " -ForegroundColor Green
                    $downloaded = $true
                    break
                }
                if ($state -in @('Error','TransientError','Cancelled')) {
                    $err = if ($bitsJob.ErrorDescription) { $bitsJob.ErrorDescription } else { $state }
                    throw "BITS: $err"
                }
                Start-Sleep -Milliseconds 750
            }
        }
    } catch {
        Write-Host ''
        Write-Host "EIRVEN: BITS недоступен/оборвался ($($_.Exception.Message)). Переключаюсь на curl с прогрессом..." -ForegroundColor Yellow
        if ($bitsJob) { try { Remove-BitsTransfer -BitsJob $bitsJob -Confirm:$false -ErrorAction SilentlyContinue } catch {} }
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    }

    if (-not $downloaded) {
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if (-not $curl) { throw 'Не найден ни BITS, ни curl.exe для загрузки Ollama.' }
        & $curl.Source -L --fail --retry 3 --retry-delay 2 --progress-bar -o $Destination $url
        if ($LASTEXITCODE -ne 0) { throw "curl не смог скачать OllamaSetup.exe (код $LASTEXITCODE)." }
    }

    if (-not (Test-Path -LiteralPath $Destination) -or (Get-Item -LiteralPath $Destination).Length -lt 10000000) {
        throw 'OllamaSetup.exe скачан не полностью.'
    }
    $sig = Get-AuthenticodeSignature -FilePath $Destination
    if ($sig.Status -ne 'Valid') {
        throw "Не удалось подтвердить цифровую подпись OllamaSetup.exe: $($sig.Status)"
    }
}

function Install-EirvenOllama {
    $installer = Join-Path $env:TEMP 'eirven-OllamaSetup.exe'
    Download-EirvenOllamaInstaller $installer
    try {
        Write-Host 'EIRVEN: устанавливаю Ollama для текущего пользователя...' -ForegroundColor Cyan
        # Official Ollama Windows installer is per-user. Do NOT elevate the process and do
        # NOT use Start-Process -Wait: -Wait can remain blocked by long-lived child app processes.
        $proc = Start-Process -FilePath $installer -ArgumentList @('/SILENT') -PassThru
        $deadline = (Get-Date).AddMinutes(4)
        $exe = $null
        while ((Get-Date) -lt $deadline) {
            $exe = Get-EirvenOllamaExe
            try { $proc.Refresh() } catch {}
            if ($proc.HasExited) {
                if ($proc.ExitCode -ne 0 -and -not $exe) { throw "OllamaSetup.exe завершился с кодом $($proc.ExitCode)." }
                if ($exe) { break }
            }
            if ($exe) {
                # Give the installer a short grace period to finish registry/startup work,
                # but never wait on the persistent Ollama app process tree.
                $grace = (Get-Date).AddSeconds(12)
                while ((Get-Date) -lt $grace) {
                    try { $proc.Refresh() } catch {}
                    if ($proc.HasExited) { break }
                    Start-Sleep -Milliseconds 500
                }
                break
            }
            Start-Sleep -Milliseconds 750
        }
        $exe = Get-EirvenOllamaExe
        if (-not $exe) { throw 'OllamaSetup.exe завершился, но ollama.exe не найден в профиле текущего пользователя.' }
        Write-Host "EIRVEN: Ollama установлена: $exe" -ForegroundColor Green
        return $exe
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

$OllamaExe = Get-EirvenOllamaExe
if ($OllamaExe) {
    Write-Host "EIRVEN: Ollama уже установлена, повторная установка не нужна." -ForegroundColor DarkGray
}
if (-not $OllamaExe -and $InstallIfMissing) {
    $OllamaExe = Install-EirvenOllama
}
if (-not $OllamaExe) { throw 'Ollama не найдена.' }
Add-EirvenOllamaPath $OllamaExe

try {
    $versionText = (& $OllamaExe --version 2>&1 | Out-String).Trim()
    if ($versionText) { Write-Host "EIRVEN: $versionText" }
    $match = [regex]::Match($versionText, '(\d+\.\d+\.\d+)')
    if ($match.Success -and ([version]$match.Value -lt [version]'0.13.0')) {
        Write-Host 'EIRVEN: для Qwen3-VL нужна Ollama 0.13.0 или новее. Обновляю runtime...' -ForegroundColor Cyan
        $OllamaExe = Install-EirvenOllama
        Add-EirvenOllamaPath $OllamaExe
    }
} catch {}

if ($StartServer -and -not (Test-EirvenOllamaApi)) {
    Write-Host 'EIRVEN: запускаю локальный Ollama API...' -ForegroundColor Cyan
    $dir = Split-Path -Parent $OllamaExe
    $appExe = Join-Path $dir 'ollama app.exe'
    if (Test-Path -LiteralPath $appExe) {
        try { Start-Process -FilePath $appExe -WindowStyle Hidden | Out-Null } catch {}
        if (Wait-EirvenOllamaApi 25) {
            Write-Host 'EIRVEN: Ollama API готов.' -ForegroundColor Green
            Write-Output $OllamaExe
            exit 0
        }
    }
    try {
        Start-Process -FilePath $OllamaExe -ArgumentList @('serve') -WindowStyle Hidden | Out-Null
    } catch {
        throw "Ollama установлена, но локальный сервер не запустился: $($_.Exception.Message)"
    }
    if (-not (Wait-EirvenOllamaApi 60)) {
        throw 'Ollama установлена, но API 127.0.0.1:11434 не поднялся автоматически.'
    }
}

Write-Host 'EIRVEN: Ollama готова.' -ForegroundColor Green
Write-Output $OllamaExe
exit 0
