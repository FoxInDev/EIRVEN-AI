#Requires -Version 5.1
<#
    Перенос Эрви на другой компьютер.

        .\transfer_data.ps1 -Export D:\eirven-backup.zip
        .\transfer_data.ps1 -Import D:\eirven-backup.zip

    Забирает то, что нельзя восстановить: переписку, долговременную память,
    настройки, заметки и календарь, правила Telegram и вход в Telegram.

    Что НЕ переносится и почему: пароль почты и хэш Telegram API зашифрованы
    средствами Windows (DPAPI) и привязаны к учётной записи конкретной машины —
    на другой они физически не расшифруются. Это не недоработка переноса, а
    свойство самого шифрования: именно поэтому пароль и не лежит в открытом виде.
    Их нужно ввести один раз заново. Вход в Telegram при этом сохраняется —
    файл сессии не привязан к машине, повторно подтверждать код не придётся.

    Работает и когда исходная машина не включается: достаточно её диска.
    Экспорт умеет читать данные прямо с чужого диска через -SourceRoot.
#>
param(
    [string]$Export,
    [string]$Import,
    [string]$SourceRoot,   # например E:\Users\Ivan\AppData\Local\EIRVEN AI — с диска мёртвой машины
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $env:LOCALAPPDATA 'EIRVEN AI'

function Write-Step($t) { Write-Host "  $t" -ForegroundColor Cyan }
function Write-Done($t) { Write-Host "  $t" -ForegroundColor Green }
function Write-Note($t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Write-Warn($t) { Write-Host "  $t" -ForegroundColor Yellow }

function Stop-Eirven {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'eirven_ai\.(supervisor|app|tts_worker|voice_worker)' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
    Start-Sleep -Seconds 2
}

# Переносим только личное. Модели сюда не попадают: они одинаковы у всех и
# скачиваются заново, а архив из-за них раздулся бы на несколько гигабайт.
$Payload = @(
    @{ Path = 'data\eirven.db';       Label = 'Переписка, память и настройки'; Required = $true  },
    @{ Path = 'data\eirven.db-wal';   Label = 'Журнал базы';                   Required = $false },
    @{ Path = 'data\eirven.db-shm';   Label = 'Служебный файл базы';           Required = $false },
    @{ Path = 'data\telegram.session';Label = 'Вход в Telegram';               Required = $false },
    @{ Path = 'data\telegram';        Label = 'Данные Telegram';               Required = $false },
    @{ Path = 'data\uploads';         Label = 'Загруженные файлы';             Required = $false },
    @{ Path = 'data\generated';       Label = 'Созданные файлы';               Required = $false },
    @{ Path = 'private';               Label = 'Настройки подключений';         Required = $false },
    @{ Path = 'storage.json';         Label = 'Раскладка дисков';              Required = $false }
)

function Do-Export {
    $source = if ($SourceRoot) { $SourceRoot } else { $AppRoot }
    if (-not (Test-Path -LiteralPath $source)) { throw "Папка Эрви не найдена: $source" }
    Write-Host ''
    Write-Host 'Выгрузка данных Эрви' -ForegroundColor White
    Write-Note "  Источник: $source"
    Write-Host ''

    if (-not $SourceRoot) { Write-Step 'Останавливаю Эрви...'; Stop-Eirven; Write-Done 'Остановлено' }

    $staging = Join-Path $env:TEMP ("eirven-export-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        $copied = 0
        foreach ($item in $Payload) {
            $from = Join-Path $source $item.Path
            if (-not (Test-Path -LiteralPath $from)) {
                if ($item.Required) { throw "$($item.Label) — не найдено ($from). Это основной файл, без него переносить нечего." }
                Write-Note "$($item.Label) — нет, пропускаю"
                continue
            }
            $to = Join-Path $staging $item.Path
            New-Item -ItemType Directory -Path (Split-Path -Parent $to) -Force | Out-Null
            Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
            $copied++
            Write-Done $item.Label
        }

        # Пометка о происхождении: при восстановлении видно, откуда и когда данные.
        $meta = [ordered]@{
            created   = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
            computer  = $env:COMPUTERNAME
            user      = $env:USERNAME
            source    = $source
            items     = $copied
            note      = 'Пароль почты и хэш Telegram API не переносятся: они зашифрованы Windows DPAPI и привязаны к прежней машине.'
        }
        ($meta | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath (Join-Path $staging 'transfer.json') -Encoding UTF8

        $target = [IO.Path]::GetFullPath($Export)
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force -ErrorAction SilentlyContinue | Out-Null
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $target -Force
        $sizeMB = [math]::Round((Get-Item -LiteralPath $target).Length / 1MB, 1)

        Write-Host ''
        Write-Host "  Готово: $target ($sizeMB МБ)" -ForegroundColor Green
        Write-Note '  Перенеси файл на новый компьютер и выполни там:'
        Write-Note "  .\transfer_data.ps1 -Import `"$target`""
        Write-Host ''
    }
    finally { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
}

function Do-Import {
    $archive = [IO.Path]::GetFullPath($Import)
    if (-not (Test-Path -LiteralPath $archive)) { throw "Архив не найден: $archive" }
    if (-not (Test-Path -LiteralPath $AppRoot)) { throw "Эрви не установлена на этом компьютере. Сначала установи её, потом переноси данные." }

    Write-Host ''
    Write-Host 'Восстановление данных Эрви' -ForegroundColor White
    Write-Host ''

    $staging = Join-Path $env:TEMP ("eirven-import-" + [guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $archive -DestinationPath $staging -Force
    try {
        $metaFile = Join-Path $staging 'transfer.json'
        if (Test-Path -LiteralPath $metaFile) {
            $meta = Get-Content -LiteralPath $metaFile -Raw | ConvertFrom-Json
            Write-Note "  Архив от $($meta.created), компьютер $($meta.computer)"
            Write-Host ''
        }

        if (-not $Quiet) {
            Write-Warn '  Текущие переписка и настройки на этом компьютере будут заменены.'
            Write-Note '  Копия текущих данных сохранится рядом, на случай отката.'
            $answer = Read-Host '  Продолжить? (да/нет)'
            if ($answer -notmatch '^(?i)(да|d|y|yes)$') { Write-Host '  Отменено.'; return }
            Write-Host ''
        }

        Write-Step 'Останавливаю Эрви...'; Stop-Eirven; Write-Done 'Остановлено'

        # Откатываемая замена: сначала прячем текущее, потом кладём новое.
        $backup = Join-Path $AppRoot ("data-before-import-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        $currentData = Join-Path $AppRoot 'data'
        if (Test-Path -LiteralPath $currentData) {
            New-Item -ItemType Directory -Path $backup -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $currentData '*') -Destination $backup -Recurse -Force -ErrorAction SilentlyContinue
            Write-Done "Копия текущих данных: $backup"
        }

        $restored = 0
        foreach ($item in $Payload) {
            $from = Join-Path $staging $item.Path
            if (-not (Test-Path -LiteralPath $from)) { continue }
            $to = Join-Path $AppRoot $item.Path
            New-Item -ItemType Directory -Path (Split-Path -Parent $to) -Force | Out-Null
            Copy-Item -LiteralPath $from -Destination $to -Recurse -Force
            $restored++
            Write-Done $item.Label
        }
        if ($restored -eq 0) { throw 'В архиве не оказалось данных для восстановления.' }

        Write-Host ''
        Write-Host '  Данные восстановлены.' -ForegroundColor Green
        Write-Host ''
        Write-Warn '  Нужно ввести заново (их нельзя перенести между компьютерами):'
        Write-Note '    - пароль почты'
        Write-Note '    - API ID и API Hash для Telegram'
        Write-Note '  Оба были зашифрованы Windows и привязаны к прежней машине.'
        Write-Note '  Вход в Telegram сохранён — код подтверждения не потребуется.'
        Write-Host ''
        Write-Note '  Запусти Эрви: переписка, память и настройки будут на месте.'
        Write-Host ''
    }
    finally { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
}

if ($Export) { Do-Export }
elseif ($Import) { Do-Import }
else {
    Write-Host ''
    Write-Host 'Перенос Эрви на другой компьютер' -ForegroundColor White
    Write-Host ''
    Write-Host '  Выгрузить данные:'
    Write-Note '    .\transfer_data.ps1 -Export D:\eirven-backup.zip'
    Write-Host ''
    Write-Host '  Восстановить на новом компьютере:'
    Write-Note '    .\transfer_data.ps1 -Import D:\eirven-backup.zip'
    Write-Host ''
    Write-Host '  Если старый компьютер не включается — подключи его диск и укажи путь:'
    Write-Note '    .\transfer_data.ps1 -Export D:\backup.zip -SourceRoot "E:\Users\Имя\AppData\Local\EIRVEN AI"'
    Write-Host ''
}
if (-not $Quiet -and ($Export -or $Import)) { Read-Host '  Нажми Enter, чтобы закрыть' | Out-Null }
