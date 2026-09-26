#Requires -Version 5.1
<#
    Перенос данных Эрви на другой диск.

    Переносится то, что занимает место: модели Ollama (несколько гигабайт) и
    голосовая модель. Само приложение и окружение Python остаются на системном
    диске — они привязаны к профилю пользователя, и попытка увести их в сторону
    ломает пути внутри venv.

    Перенос делается копированием с последующей проверкой и только потом удалением
    исходника: при обрыве на середине останется рабочая копия, а не половина файлов
    в каждом из двух мест.
#>
param(
    [Parameter(Mandatory = $true)][string]$Target,   # например D:\EIRVEN
    [switch]$IncludeOllama = $true,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$AppRoot = Join-Path $env:LOCALAPPDATA 'EIRVEN AI'
$StorageFile = Join-Path $AppRoot 'storage.json'

function Write-Step($t) { Write-Host "  $t" -ForegroundColor Cyan }
function Write-Done($t) { Write-Host "  $t" -ForegroundColor Green }

function Get-Storage {
    if (-not (Test-Path -LiteralPath $StorageFile)) { return [ordered]@{} }
    try { return (Get-Content -LiteralPath $StorageFile -Raw | ConvertFrom-Json) } catch { return [ordered]@{} }
}

function Save-Storage($obj) {
    $dir = Split-Path -Parent $StorageFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($obj | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $StorageFile -Encoding UTF8
}

function Get-FolderSizeMB($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
    if (-not $sum) { return 0 }
    return [math]::Round($sum / 1MB, 0)
}

function Move-Safely($source, $destination, $label) {
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Host "  $label — нечего переносить" -ForegroundColor DarkGray
        return $true
    }
    $sizeMB = Get-FolderSizeMB $source
    Write-Step "$label — переношу ($sizeMB МБ)..."
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    # robocopy умеет докачивать и корректно работает с длинными путями; /MT ускоряет
    # на тысячах мелких файлов, которых у моделей много.
    $null = robocopy $source $destination /E /R:2 /W:2 /MT:8 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "$label — копирование не удалось (robocopy $LASTEXITCODE)." }
    $copiedMB = Get-FolderSizeMB $destination
    if ($sizeMB -gt 0 -and $copiedMB -lt ($sizeMB * 0.98)) {
        throw "$label — скопировано $copiedMB МБ из $sizeMB МБ, исходник не трогаю."
    }
    Remove-Item -LiteralPath $source -Recurse -Force -ErrorAction SilentlyContinue
    Write-Done "$label — перенесено ($copiedMB МБ)"
    return $true
}

Write-Host ''
Write-Host 'Перенос данных Эрви' -ForegroundColor White
Write-Host ''

$targetRoot = [IO.Path]::GetFullPath($Target)
$targetDrive = [IO.Path]::GetPathRoot($targetRoot)
if (-not (Test-Path -LiteralPath $targetDrive)) { throw "Диск $targetDrive не найден." }

$storage = Get-Storage
$currentData = if ($storage.data_root) { $storage.data_root } else { Join-Path $env:LOCALAPPDATA 'EIRVEN' }
$currentOllama = if ($storage.ollama_models) { $storage.ollama_models } else { Join-Path $env:USERPROFILE '.ollama\models' }

$needMB = (Get-FolderSizeMB $currentData) + $(if ($IncludeOllama) { Get-FolderSizeMB $currentOllama } else { 0 })
$freeMB = [math]::Round((Get-PSDrive -Name $targetDrive.TrimEnd(':\')).Free / 1MB, 0)
Write-Host "  Нужно: $needMB МБ   Свободно на $targetDrive : $freeMB МБ"
if ($freeMB -lt ($needMB * 1.1)) { throw "На $targetDrive недостаточно места." }
Write-Host ''

if (-not $Quiet) {
    $answer = Read-Host "  Перенести данные в $targetRoot ? (да/нет)"
    if ($answer -notmatch '^(?i)(да|d|y|yes)$') { Write-Host '  Отменено.'; exit 0 }
    Write-Host ''
}

Write-Step 'Останавливаю Эрви и Ollama...'
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'eirven_ai\.(supervisor|app|tts_worker|voice_worker)' } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
Get-Process -Name 'ollama', 'ollama app' -ErrorAction SilentlyContinue |
    ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue } catch { } }
Start-Sleep -Seconds 3
Write-Done 'Остановлено'

$newData = Join-Path $targetRoot 'data'
Move-Safely $currentData $newData 'Модели и данные Эрви' | Out-Null

$newOllama = $currentOllama
if ($IncludeOllama) {
    $newOllama = Join-Path $targetRoot 'ollama-models'
    Move-Safely $currentOllama $newOllama 'Модели Ollama' | Out-Null
    # Ollama читает расположение моделей из OLLAMA_MODELS. Это её штатный механизм —
    # надёжнее, чем переносить папку и надеяться, что она найдётся сама.
    [Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $newOllama, 'User')
    Write-Done "OLLAMA_MODELS -> $newOllama"
}

$updated = [ordered]@{
    data_root      = $newData
    ollama_models  = $newOllama
    ollama_program = $(if ($storage.ollama_program) { $storage.ollama_program } else { '' })
}
Save-Storage $updated
Write-Done "Пути записаны в storage.json"

Write-Host ''
Write-Host '  Готово. Перезапусти Эрви — она подхватит новое расположение.' -ForegroundColor Green
Write-Host ''
if (-not $Quiet) { Read-Host '  Нажми Enter, чтобы закрыть' | Out-Null }
