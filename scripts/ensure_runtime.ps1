$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"

# Upgrade/repair always begins from one clean EIRVEN runtime. This does not stop
# Ollama or an in-progress model download.
& powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop_eirven.ps1') -AllInstances | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'Не удалось завершить старые копии EIRVEN.' }

# r42 deliberately does not self-elevate. All normal installation is per-user.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($elevated) {
    Write-Host 'EIRVEN: установщик запущен с повышенными правами. Это не требуется; EIRVEN не будет запрашивать UAC самостоятельно.' -ForegroundColor Yellow
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user;$env:LOCALAPPDATA\Programs\Ollama;$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts;$env:USERPROFILE\.local\bin;$env:APPDATA\npm"
}

function Get-Python312Exe {
    try {
        $resolved = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved.Trim())) { return $resolved.Trim() }
    } catch {}
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "${env:ProgramFiles(x86)}\Python312\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $version = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null | Select-Object -Last 1)
            if ($LASTEXITCODE -eq 0 -and $version.Trim() -eq '3.12') { return $candidate }
        } catch {}
    }
    return $null
}

function Install-Python312CurrentUser {
    $pythonVersion = '3.12.10'
    $fileName = "python-$pythonVersion-amd64.exe"
    $urls = @()
    if ($env:EIRVEN_BOOTSTRAP_MIRROR) {
        $urls += ($env:EIRVEN_BOOTSTRAP_MIRROR.TrimEnd('/') + "/$fileName")
    }
    $urls += "https://www.python.org/ftp/python/$pythonVersion/$fileName"
    $urls = @($urls | Select-Object -Unique)
    $installer = Join-Path $env:TEMP "eirven-python-$pythonVersion-amd64.exe"
    Write-Host "EIRVEN: Python 3.12 не найден. Скачиваю официальный установщик для текущего пользователя..." -ForegroundColor Cyan
    try {
        $downloaded = $false
        foreach ($url in $urls) {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
            try {
                Write-Host "EIRVEN: источник Python: $url" -ForegroundColor DarkGray
                $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
                if ($curl) {
                    & $curl.Source -L --fail --retry 2 --retry-delay 2 --connect-timeout 15 --progress-bar -o $installer $url
                    if ($LASTEXITCODE -ne 0) { throw "curl завершился с кодом $LASTEXITCODE" }
                } else {
                    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $installer -TimeoutSec 600
                }
                if ((Test-Path -LiteralPath $installer) -and (Get-Item -LiteralPath $installer).Length -ge 10000000) {
                    $downloaded = $true
                    break
                }
            } catch {
                Write-Host "EIRVEN: источник не ответил, пробую следующий." -ForegroundColor Yellow
            }
        }
        if (-not $downloaded) { throw 'Python installer не скачан с настроенного зеркала или python.org.' }
        $sig = Get-AuthenticodeSignature -FilePath $installer
        if ($sig.Status -ne 'Valid' -or -not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw "Подпись Python installer не подтверждена ($($sig.Status))."
        }
        $proc = Start-Process -FilePath $installer -ArgumentList @(
            '/quiet','InstallAllUsers=0','PrependPath=1','Include_launcher=1','Include_test=0','Shortcuts=0'
        ) -PassThru
        if (-not $proc.WaitForExit(300000)) { throw 'Python installer не завершился за 5 минут.' }
        if ($proc.ExitCode -ne 0) { throw "Python installer завершился с кодом $($proc.ExitCode)." }
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    Refresh-Path
}

function Ensure-VCRedist {
    # NumPy, OpenCV, onnxruntime and ctranslate2 all ship Windows wheels linked
    # against the Microsoft Visual C++ runtime. Python's own installer does not
    # include it, so on a clean machine importing NumPy dies with
    # "DLL load failed while importing _multiarray_umath" -- which reads like a
    # NumPy problem but is a missing system library.
    $marker = Join-Path $env:SystemRoot 'System32\vcruntime140_1.dll'
    if (Test-Path -LiteralPath $marker) { return }
    Write-Host 'Устанавливаю Visual C++ Runtime (нужен для NumPy и распознавания)...'
    $installer = Join-Path $env:TEMP 'eirven-vc_redist.x64.exe'
    try {
        Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' `
            -OutFile $installer -UseBasicParsing -TimeoutSec 300
        $proc = Start-Process -FilePath $installer -ArgumentList @('/install','/quiet','/norestart') -PassThru
        if (-not $proc.WaitForExit(300000)) { throw 'Установщик Visual C++ Runtime не завершился за 5 минут.' }
        # 3010 means success but a reboot is pending; the DLLs are already in place.
        if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010 -and $proc.ExitCode -ne 1638) {
            throw "Visual C++ Runtime завершился с кодом $($proc.ExitCode)."
        }
    } catch {
        # Not fatal on its own: many machines already have a compatible runtime from
        # another program. bootstrap.py verifies the actual NumPy import afterwards
        # and reports a precise cause if it still fails.
        Write-Host "Не удалось установить Visual C++ Runtime автоматически: $_"
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

Refresh-Path
$PythonExe = Get-Python312Exe
if (-not $PythonExe) {
    Install-Python312CurrentUser
    $PythonExe = Get-Python312Exe
}
if (-not $PythonExe) { throw 'Не удалось подготовить Python 3.12 для текущего пользователя.' }
& $PythonExe --version | Out-Host
Ensure-VCRedist

function Select-EirvenStorage {
    # Спрашиваем про диск один раз, при первой установке. Модели весят несколько
    # гигабайт, и у части людей на C: их просто некуда положить — а узнают они об
    # этом обычно уже посреди скачивания.
    $storageFile = Join-Path $Root 'storage.json'
    if (Test-Path -LiteralPath $storageFile) { return }

    $systemFreeGB = [math]::Round((Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'))).Free / 1GB, 1)
    Write-Host ''
    Write-Host '  Куда сохранять модели (около 5 ГБ)?' -ForegroundColor White
    Write-Host "  Системный диск $env:SystemDrive — свободно $systemFreeGB ГБ" -ForegroundColor DarkGray
    $drives = Get-PSDrive -PSProvider FileSystem |
        Where-Object { $_.Free -ne $null -and $_.Free -gt 6GB } |
        Sort-Object Name
    foreach ($d in $drives) {
        Write-Host ("    {0}:  свободно {1} ГБ" -f $d.Name, [math]::Round($d.Free / 1GB, 1))
    }
    Write-Host '  Enter — оставить на системном диске.' -ForegroundColor DarkGray
    # Спрашиваем только если ввод действительно доступен. Установку часто
    # запускают из окна без консоли: тогда Read-Host не может ничего прочитать
    # и обрывает весь сценарий. В журнале остаётся одна строка заголовка, а
    # человек видит «не получилось запустить» — ровно этот случай и был.
    $choice = ''
    $interactive = $true
    try {
        if ([Console]::IsInputRedirected) { $interactive = $false }
        if (-not [Environment]::UserInteractive) { $interactive = $false }
    } catch { $interactive = $false }

    if (-not $interactive) {
        Write-Host '  Ввод недоступен — оставляю на системном диске.' -ForegroundColor DarkGray
        return
    }

    try {
        $choice = Read-Host '  Буква диска'
    } catch {
        Write-Host '  Не удалось прочитать ответ — оставляю на системном диске.' -ForegroundColor DarkGray
        return
    }
    # Пустой ответ — это Enter, он означает «оставить как есть».
    if (-not $choice) { Write-Host '  Оставляю на системном диске.' -ForegroundColor DarkGray; return }
    $choice = ([string]$choice).Trim().TrimEnd(':').ToUpper()

    if (-not $choice -or $choice -eq $env:SystemDrive.TrimEnd(':')) {
        Write-Host '  Оставляю на системном диске.' -ForegroundColor DarkGray
        return
    }
    if (-not (Test-Path -LiteralPath "$choice`:\\")) {
        Write-Host "  Диск $choice не найден, оставляю на системном." -ForegroundColor Yellow
        return
    }
    $target = "$choice`:\EIRVEN"
    $payload = [ordered]@{
        data_root     = (Join-Path $target 'data')
        ollama_models = (Join-Path $target 'ollama-models')
    }
    New-Item -ItemType Directory -Path $payload.data_root -Force | Out-Null
    New-Item -ItemType Directory -Path $payload.ollama_models -Force | Out-Null
    ($payload | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $storageFile -Encoding UTF8
    # Ollama берёт расположение моделей отсюда — это её штатный механизм.
    [Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $payload.ollama_models, 'User')
    $env:OLLAMA_MODELS = $payload.ollama_models
    Write-Host "  Модели будут в $target" -ForegroundColor Green
}
try {
    Select-EirvenStorage
} catch {
    # Выбор диска — удобство, а не необходимость. Любая ошибка здесь не должна
    # останавливать установку: модели просто лягут на системный диск.
    Write-Host "  Выбор диска пропущен: $($_.Exception.Message)" -ForegroundColor DarkGray
}

function Register-EirvenUninstall {
    # Без этой записи Эрви не видно в «Приложения и возможности», и удалить её
    # штатным способом нельзя — остаются гигабайты моделей и правило файрвола.
    try {
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\EIRVEN'
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
        # Ведём на видимый файл в корне: тот же путь, что при ручном удалении,
        # поэтому поведение из «Программ и компонентов» и из папки совпадает.
        $entry = Join-Path $Root 'UNINSTALL.cmd'
        if (Test-Path -LiteralPath $entry) {
            $uninstall = "`"$entry`""
        } else {
            $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\uninstall.ps1`""
        }
        New-ItemProperty -Path $key -Name 'DisplayName'     -Value 'Эрви (EIRVEN AI)' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'DisplayVersion'  -Value '2.4.0' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'Publisher'       -Value 'Даниил Павлов' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'URLInfoAbout'    -Value 'https://foxyhosty.ru' -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'InstallLocation' -Value $Root -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'UninstallString' -Value $uninstall -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $key -Name 'NoModify'        -Value 1 -PropertyType DWord -Force | Out-Null
        New-ItemProperty -Path $key -Name 'NoRepair'        -Value 1 -PropertyType DWord -Force | Out-Null
        $icon = Join-Path $Root 'assets\eirven.ico'
        if (Test-Path -LiteralPath $icon) {
            New-ItemProperty -Path $key -Name 'DisplayIcon' -Value $icon -PropertyType String -Force | Out-Null
        }
    } catch {
        Write-Host "  Не удалось зарегистрировать удаление: $_" -ForegroundColor Yellow
    }
}
Register-EirvenUninstall

# Open the installer GUI before Ollama/model work. bootstrap.py owns the idempotent
# Ollama setup and exposes exact byte progress for both required DeepSeek models.
& $PythonExe "$Root\scripts\bootstrap.py"
if ($LASTEXITCODE -ne 0) { throw 'EIRVEN bootstrap остановился на ошибочном компоненте. Уже завершённые этапы сохранены.' }
