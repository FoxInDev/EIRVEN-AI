param(
    [switch]$InstallIfMissing = $true,
    [switch]$StartServer = $true
)

$ErrorActionPreference = 'Stop'
# Весь вывод — в UTF-8. Установщик читает его как UTF-8, а PowerShell по умолчанию
# пишет в кодировке консоли (cp866). Из-за этого русский текст ошибок у людей
# превращался в "кракозябры" и настоящую причину было не прочитать.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$Root = Split-Path -Parent $PSScriptRoot
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$env:no_proxy = '127.0.0.1,localhost,::1'

function Get-EirvenOllamaExe {
    # Сначала — штатное место установки. Get-Command идёт вторым: в PATH может
    # оказаться чужая или старая копия, а пустые "псевдонимы" из WindowsApps
    # (файлы нулевого размера) запустить нельзя вовсе.
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe')
    )
    try {
        $cmd = Get-Command ollama.exe -ErrorAction Stop
        if ($cmd.Source) { $candidates += $cmd.Source }
    } catch {}
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        try {
            $item = Get-Item -LiteralPath $candidate -ErrorAction Stop
            if ($item.Length -gt 0) { return $item.FullName }
        } catch {}
    }
    return $null
}

function Get-EirvenNativeCode($ErrorRecord) {
    $e = $ErrorRecord.Exception
    while ($e) {
        if ($e -is [System.ComponentModel.Win32Exception]) { return [int]$e.NativeErrorCode }
        $e = $e.InnerException
    }
    # Start-Process теряет исходное исключение и оставляет только текст. Код
    # находим, сравнивая текст с системными сообщениями Windows на языке системы.
    $text = [string]$ErrorRecord.Exception.Message
    foreach ($code in @(1392, 193, 225, 1260, 4551, 5, 2, 3, 32, 740)) {
        try {
            $known = (New-Object System.ComponentModel.Win32Exception($code)).Message
            if ($known -and $text.Contains($known.TrimEnd('.'))) { return $code }
        } catch {}
    }
    return 0
}

function Test-EirvenOllamaFile([string]$Exe) {
    # $null — файл в порядке. Иначе — причина. Подпись Ollama проверяем только на
    # явные признаки повреждения: "не удалось проверить цепочку" бывает на старых
    # Windows без обновлённых корневых сертификатов и поломкой не является.
    try {
        $item = Get-Item -LiteralPath $Exe -ErrorAction Stop
        if ($item.Length -lt 1MB) { return "ollama.exe неполный ($($item.Length) байт)" }
        $sig = Get-AuthenticodeSignature -FilePath $Exe -ErrorAction Stop
        if ($sig.Status -eq 'HashMismatch') { return 'ollama.exe повреждён: содержимое не совпадает с цифровой подписью' }
        return $null
    } catch {
        return "ollama.exe не читается: $($_.Exception.Message)"
    }
}

function Invoke-EirvenOllamaProbe([string]$Exe) {
    # Пробный запуск "ollama --version". Он же проверяет, что Windows вообще даёт
    # запустить файл. Предупреждения Ollama идут в stderr (например, "сервер ещё
    # не запущен") — это нормально, поэтому режим Stop здесь временно выключен.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Exe --version 2>&1 | ForEach-Object { [string]$_ }
        return @{ Ok = $true; Output = (($lines -join "`n").Trim()); Code = 0; Message = '' }
    } catch {
        return @{ Ok = $false; Output = ''; Code = (Get-EirvenNativeCode $_); Message = [string]$_.Exception.Message }
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-EirvenOllamaProbeWithRetry([string]$Exe) {
    # Сразу после установки файл ещё может проверять антивирус или дописывать
    # установщик. Одна неудачная попытка — ещё не поломка: даём несколько секунд.
    $probe = Invoke-EirvenOllamaProbe $Exe
    for ($i = 0; $i -lt 3 -and -not $probe.Ok; $i++) {
        Start-Sleep -Seconds 4
        $probe = Invoke-EirvenOllamaProbe $Exe
    }
    return $probe
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

function Stop-EirvenOllamaProcesses {
    # Перед переустановкой: запущенная Ollama держит свои файлы, и установщик не
    # смог бы их заменить.
    foreach ($name in @('ollama app', 'ollama')) {
        try { Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Milliseconds 800
}

function Install-EirvenOllama {
    $installer = Join-Path $env:TEMP 'eirven-OllamaSetup.exe'
    Download-EirvenOllamaInstaller $installer
    try {
        Write-Host 'EIRVEN: устанавливаю Ollama для текущего пользователя...' -ForegroundColor Cyan
        # Ждём завершения самого установщика, а не появления ollama.exe. Раньше
        # установка считалась законченной через 12 секунд после появления файла,
        # хотя установщик ещё распаковывал сотни мегабайт, — и запуск попадал в
        # недоустановленную папку. Start-Process -Wait не подходит: он ждёт и все
        # дочерние процессы, включая Ollama, которую установщик запускает в конце.
        $proc = Start-Process -FilePath $installer -ArgumentList @('/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -PassThru
        $null = $proc.Handle  # без этого PowerShell 5.1 теряет код завершения
        if (-not $proc.WaitForExit(15 * 60 * 1000)) {
            throw 'Установщик Ollama не завершился за 15 минут.'
        }
        $exe = Get-EirvenOllamaExe
        if (-not $exe) {
            throw "Установщик Ollama завершился (код $($proc.ExitCode)), но ollama.exe не появился в профиле пользователя."
        }
        if ($proc.ExitCode -ne 0) {
            Write-Host "EIRVEN: установщик Ollama вернул код $($proc.ExitCode), но ollama.exe на месте — продолжаю." -ForegroundColor Yellow
        }
        Write-Host "EIRVEN: Ollama установлена: $exe" -ForegroundColor Green
        return $exe
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

function Get-EirvenReason([int]$Code, [string]$Message) {
    switch ($Code) {
        1392 { return 'Windows сообщает, что файл Ollama повреждён или не читается ("Файл или папка повреждены").' }
        193  { return 'Windows не распознаёт ollama.exe как программу — файл повреждён.' }
        225  { return 'Антивирус считает ollama.exe угрозой и не даёт его запустить.' }
        1260 { return 'Запуск ollama.exe запрещён групповой политикой Windows.' }
        4551 { return 'Запуск ollama.exe запрещён политикой целостности кода Windows.' }
        5    { return 'Windows отказала в доступе к ollama.exe (часто так срабатывает антивирус).' }
        2    { return 'ollama.exe не найден на своём месте.' }
        32   { return 'ollama.exe занят другим процессом (идёт установка или обновление Ollama).' }
    }
    return $Message
}

function Write-EirvenFailure([string]$Message) {
    # Одна чистая строка для установщика — без служебных полей PowerShell
    # (CategoryInfo, FullyQualifiedErrorId), которые раньше занимали весь экран.
    $line = 'EIRVEN_ERROR: ' + ($Message -replace "`r?`n", ' ')
    try { [Console]::Error.WriteLine($line) } catch { Write-Output $line }
}

# Коды, при которых сам файл Ollama испорчен или недоставлен: помогает переустановка.
$RepairCodes = @(1392, 193, 2, 3, 32)
$Reinstalled = $false
$LastCode = 0
$LastMessage = ''

try {
    $OllamaExe = Get-EirvenOllamaExe
    if ($OllamaExe) {
        Write-Host "EIRVEN: Ollama уже установлена: $OllamaExe" -ForegroundColor DarkGray
    } elseif ($InstallIfMissing) {
        $OllamaExe = Install-EirvenOllama
        $Reinstalled = $true
    }
    if (-not $OllamaExe) { throw 'Ollama не найдена, а установка отключена.' }

    for ($attempt = 1; $attempt -le 2; $attempt++) {
        Add-EirvenOllamaPath $OllamaExe
        $broken = Test-EirvenOllamaFile $OllamaExe
        $probe = Invoke-EirvenOllamaProbeWithRetry $OllamaExe
        if ($probe.Ok) {
            if ($probe.Output) { Write-Host ('EIRVEN: ' + ($probe.Output -replace "`r?`n", ' · ')) }
            $match = [regex]::Match([string]$probe.Output, '(\d+\.\d+\.\d+)')
            if ($match.Success -and ([version]$match.Value -lt [version]'0.13.0') -and $InstallIfMissing -and -not $Reinstalled) {
                Write-Host 'EIRVEN: для Qwen3-VL нужна Ollama 0.13.0 или новее. Обновляю...' -ForegroundColor Cyan
                Stop-EirvenOllamaProcesses
                $OllamaExe = Install-EirvenOllama
                $Reinstalled = $true
                continue
            }
        } else {
            $LastCode = [int]$probe.Code
            $LastMessage = [string]$probe.Message
            Write-Host "EIRVEN: ollama.exe не запускается (код $LastCode): $LastMessage" -ForegroundColor Yellow
        }
        if ($broken) { Write-Host "EIRVEN: $broken" -ForegroundColor Yellow }

        $needsRepair = [bool]$broken -or ((-not $probe.Ok) -and ($RepairCodes -contains $LastCode))
        if ($needsRepair) {
            if ($InstallIfMissing -and -not $Reinstalled) {
                Write-Host 'EIRVEN: переустанавливаю Ollama поверх повреждённой копии...' -ForegroundColor Cyan
                Stop-EirvenOllamaProcesses
                $OllamaExe = Install-EirvenOllama
                $Reinstalled = $true
                continue
            }
            if (-not $probe.Ok) {
                throw ((Get-EirvenReason $LastCode $LastMessage) + ' Даже свежеустановленная Ollama не запускается.')
            }
        }
        break
    }

    if ($StartServer -and -not (Test-EirvenOllamaApi)) {
        Write-Host 'EIRVEN: запускаю локальный сервер Ollama...' -ForegroundColor Cyan
        $dir = Split-Path -Parent $OllamaExe
        # После свежей установки Ollama запускается сама — сначала даём ей подняться.
        $ready = $false
        if ($Reinstalled) { $ready = Wait-EirvenOllamaApi 20 }
        if (-not $ready) {
            for ($try = 1; $try -le 2 -and -not $ready; $try++) {
                $started = $false
                try {
                    # Именно Start-Process (через оболочку Windows): так сервер не получает
                    # дескрипторы этого процесса. Иначе установщик, читающий наш вывод,
                    # ждал бы завершения сервера Ollama — то есть вечно.
                    Start-Process -FilePath $OllamaExe -ArgumentList @('serve') -WorkingDirectory $dir -WindowStyle Hidden | Out-Null
                    $started = $true
                } catch {
                    $LastCode = Get-EirvenNativeCode $_
                    $LastMessage = [string]$_.Exception.Message
                    Write-Host "EIRVEN: сервер Ollama не стартовал (код $LastCode): $LastMessage" -ForegroundColor Yellow
                }
                if ($started) { $ready = Wait-EirvenOllamaApi 45 }
                elseif ($try -lt 2) { Start-Sleep -Seconds 5 }
            }
        }
        if (-not $ready) {
            # Последняя попытка — через собственное приложение Ollama.
            $appExe = Join-Path $dir 'ollama app.exe'
            if (Test-Path -LiteralPath $appExe) {
                try { Start-Process -FilePath $appExe -WorkingDirectory $dir | Out-Null } catch {}
                $ready = Wait-EirvenOllamaApi 30
            }
        }
        if (-not $ready) {
            $reason = Get-EirvenReason $LastCode $LastMessage
            if (-not $reason) { $reason = 'Сервер запустился, но не ответил на 127.0.0.1:11434.' }
            throw "Ollama установлена, но её сервер не запустился. $reason"
        }
        Write-Host 'EIRVEN: Ollama API готов.' -ForegroundColor Green
    }
} catch {
    Write-EirvenFailure ([string]$_.Exception.Message)
    exit 3
}

Write-Host 'EIRVEN: Ollama готова.' -ForegroundColor Green
Write-Output $OllamaExe
exit 0
