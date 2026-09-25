# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
#Requires -Version 5.1
<#
    Удаление Эрви.

    Установка создаёт больше, чем одну папку: виртуальное окружение, модели на
    возможно другом диске, ярлыки, автозапуск, правило файрвола и запись в списке
    программ Windows. Удаление вручную почти всегда оставляет несколько гигабайт
    моделей и висящее правило файрвола, поэтому всё перечислено здесь явно.

    Личные данные (переписка, память, настройки) по умолчанию сохраняются: их
    потеря необратима, а место они занимают небольшое. Удалить их можно флагом.
#>
param(
    [switch]$RemoveUserData,   # удалить переписку, память и настройки
    [switch]$RemoveOllama,     # удалить саму Ollama и её модели
    [switch]$Quiet             # без вопросов, для автоматического режима
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $env:LOCALAPPDATA 'EIRVEN AI'

function Write-Step($text) { Write-Host "  $text" -ForegroundColor Cyan }
function Write-Done($text) { Write-Host "  $text" -ForegroundColor Green }
function Write-Skip($text) { Write-Host "  $text" -ForegroundColor DarkGray }

function Get-EirvenStorage {
    $file = Join-Path $AppRoot 'storage.json'
    if (-not (Test-Path -LiteralPath $file)) { return @{} }
    try { return (Get-Content -LiteralPath $file -Raw | ConvertFrom-Json) } catch { return @{} }
}

function Test-SafeToDelete($path) {
    # Защита от катастрофы: storage.json редактируется вручную, и опечатка вида
    # "D:\" превратила бы удаление Эрви в удаление всего диска. Отказываемся от
    # корней дисков, корня профиля и слишком коротких путей.
    if (-not $path) { return $false }
    try { $full = [IO.Path]::GetFullPath($path) } catch { return $false }
    $root = [IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $root.TrimEnd('\')) { return $false }
    if ($full.TrimEnd('\').Length -le 3) { return $false }
    foreach ($guard in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:APPDATA, $env:ProgramFiles, $env:SystemRoot)) {
        if ($guard -and ($full.TrimEnd('\') -eq $guard.TrimEnd('\'))) { return $false }
    }
    return $true
}

function Remove-Tree($path, $label) {
    if (-not $path) { Write-Skip "$label — путь не задан"; return }
    if (-not (Test-SafeToDelete $path)) {
        Write-Host "  $label — путь выглядит небезопасным, пропускаю: $path" -ForegroundColor Yellow
        return
    }
    if (-not (Test-Path -LiteralPath $path)) { Write-Skip "$label — уже отсутствует"; return }
    try {
        $size = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
                 Measure-Object Length -Sum).Sum
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
        Write-Done ("$label — удалено{0}" -f $(if ($size) { " ({0:N0} МБ)" -f ($size / 1MB) } else { "" }))
    } catch {
        Write-Host "  $label — не удалось удалить: $_" -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host 'Удаление Эрви' -ForegroundColor White
Write-Host ''

if (-not $Quiet) {
    Write-Host '  Будут удалены: приложение, окружение Python, модели, ярлыки,'
    Write-Host '  автозапуск и правило файрвола.'
    if ($RemoveUserData) {
        Write-Host '  ВНИМАНИЕ: переписка, память и настройки тоже будут удалены.' -ForegroundColor Yellow
    } else {
        Write-Host '  Переписка, память и настройки будут сохранены.' -ForegroundColor DarkGray
    }
    if ($RemoveOllama) { Write-Host '  Ollama и её модели тоже будут удалены.' -ForegroundColor Yellow }
    Write-Host ''
    $answer = Read-Host '  Продолжить? (да/нет)'
    if ($answer -notmatch '^(?i)(да|d|y|yes)$') { Write-Host '  Отменено.'; exit 0 }
    Write-Host ''
}

# 1. Остановить всё, что работает: иначе файлы останутся заблокированными.
Write-Step 'Останавливаю Эрви...'
foreach ($name in @('EIRVEN', 'EIRVEN-AI', 'EIRVEN-AI-r68')) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'eirven_ai\.(supervisor|app|tts_worker|voice_worker)' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
Start-Sleep -Seconds 2
Write-Done 'Процессы остановлены'

# 2. Автозапуск.
Write-Step 'Убираю автозапуск...'
$removedAutostart = $false
foreach ($task in @('EIRVEN', 'EIRVEN AI', 'EIRVEN Autostart')) {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
        $removedAutostart = $true
    }
}
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
foreach ($value in @('EIRVEN', 'EIRVEN AI')) {
    if (Get-ItemProperty -Path $runKey -Name $value -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name $value -ErrorAction SilentlyContinue
        $removedAutostart = $true
    }
}
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'EIRVEN.lnk'
if (Test-Path -LiteralPath $startupLnk) { Remove-Item -LiteralPath $startupLnk -Force -ErrorAction SilentlyContinue; $removedAutostart = $true }
if ($removedAutostart) { Write-Done 'Автозапуск убран' } else { Write-Skip 'Автозапуск не был настроен' }

# 3. Правило файрвола: без этого в системе остаётся открытый порт.
Write-Step 'Убираю правило файрвола...'
$rules = Get-NetFirewallRule -DisplayGroup 'EIRVEN' -ErrorAction SilentlyContinue
if ($rules) {
    $rules | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    Write-Done 'Правило файрвола удалено'
} else {
    Write-Skip 'Правила файрвола нет'
}

# 4. Ярлыки.
Write-Step 'Убираю ярлыки...'
$shortcutRemoved = $false
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
    foreach ($lnk in @('EIRVEN.lnk', 'Эрви.lnk', 'EIRVEN AI.lnk')) {
        $path = Join-Path $dir $lnk
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue; $shortcutRemoved = $true }
    }
}
if ($shortcutRemoved) { Write-Done 'Ярлыки удалены' } else { Write-Skip 'Ярлыков не найдено' }

# 5. Данные и модели. Читаем storage.json: модели могли быть перенесены на другой диск.
$storage = Get-EirvenStorage
$dataRoot = if ($storage.data_root) { $storage.data_root } else { Join-Path $env:LOCALAPPDATA 'EIRVEN' }

Write-Step 'Удаляю модели и данные...'
if ($RemoveUserData) {
    Remove-Tree $dataRoot 'Данные и модели Эрви'
} else {
    # Сохраняем личное, убираем только тяжёлое.
    Remove-Tree (Join-Path $dataRoot 'models') 'Модели Эрви'
    Write-Skip 'Переписка, память и настройки оставлены'
}

if ($RemoveOllama) {
    Write-Step 'Удаляю Ollama...'
    $ollamaModels = if ($storage.ollama_models) { $storage.ollama_models } else { Join-Path $env:USERPROFILE '.ollama\models' }
    Remove-Tree $ollamaModels 'Модели Ollama'
    $ollamaUninstall = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\unins000.exe'
    if (Test-Path -LiteralPath $ollamaUninstall) {
        try {
            Start-Process -FilePath $ollamaUninstall -ArgumentList '/SILENT' -Wait -ErrorAction Stop
            Write-Done 'Ollama удалена'
        } catch { Write-Host "  Ollama — не удалось удалить: $_" -ForegroundColor Yellow }
    } else {
        Remove-Tree (Join-Path $env:LOCALAPPDATA 'Programs\Ollama') 'Ollama'
    }
} else {
    Write-Skip 'Ollama оставлена (её используют и другие программы)'
}

# 6. Приложение и окружение. В последнюю очередь: здесь лежит сам этот скрипт.
Write-Step 'Удаляю приложение...'
Remove-Tree (Join-Path $AppRoot '.venv') 'Окружение Python'
if ($RemoveUserData) {
    Remove-Tree (Join-Path $AppRoot 'data') 'Личные данные'
    Remove-Tree (Join-Path $AppRoot 'private') 'Ключи и секреты'
}
foreach ($item in @('src', 'scripts', 'assets', 'logs', 'video', 'release')) {
    Remove-Tree (Join-Path $AppRoot $item) $item
}
Get-ChildItem -LiteralPath $AppRoot -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch '^(data|private)$' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }

# 7. Запись в «Программы и компоненты».
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\EIRVEN'
if (Test-Path $uninstallKey) {
    Remove-Item -Path $uninstallKey -Recurse -Force -ErrorAction SilentlyContinue
    Write-Done 'Запись в списке программ удалена'
}

Write-Host ''
if ($RemoveUserData) {
    Write-Host '  Эрви удалена полностью.' -ForegroundColor Green
} else {
    Write-Host '  Эрви удалена. Переписка и настройки остались в:' -ForegroundColor Green
    Write-Host "  $AppRoot\data" -ForegroundColor DarkGray
    Write-Host '  Чтобы удалить и их: uninstall.ps1 -RemoveUserData' -ForegroundColor DarkGray
}
Write-Host ''
if (-not $Quiet) { Read-Host '  Нажми Enter, чтобы закрыть' | Out-Null }
