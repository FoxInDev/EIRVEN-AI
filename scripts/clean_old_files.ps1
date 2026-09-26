param(
    [switch]$Delete,
    [switch]$Quiet
)
# Уборка папки Эрви: всё, что не относится к текущей версии, уходит в
# _old_files_backup\<дата-время>. Со временем в папке копились модули и скрипты
# прошлых версий, старые exe (r71 и раньше), .spec-файлы и отметки установки, — и
# сборка могла захватить что-то из этого. Список текущих файлов берётся из
# release_manifest.json, который лежит в каждом патче.
#
# Не трогаются никогда: data, .venv, logs, models, ollama-models, .env,
# storage.json и всё прочее, что не перечислено ниже как мусор.
# -Delete — удалить сразу, без папки с копией.

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$Root = Split-Path -Parent $PSScriptRoot
$ManifestPath = Join-Path $Root 'release_manifest.json'
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    Write-Host 'Уборка пропущена: нет release_manifest.json (распакуй патч целиком).' -ForegroundColor Yellow
    exit 0
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$Allowed = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($prop in $Manifest.files.PSObject.Properties) { [void]$Allowed.Add($prop.Name) }
[void]$Allowed.Add('release_manifest.json')

# Отметки установки, которые нужны лончеру: текущая и совместимая прошлая.
$KeepMarkers = @($Manifest.keep_install_markers)
$CurrentExes = @('EIRVEN.exe', 'EIRVEN-AI.exe', 'EIRVEN-AI-r72.exe')

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Backup = Join-Path $Root ("_old_files_backup\" + $Stamp)
$Moved = New-Object System.Collections.Generic.List[string]
$Failed = New-Object System.Collections.Generic.List[string]

function Get-Rel([string]$Path) {
    return $Path.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
}

function Move-Junk([string]$Path) {
    $rel = Get-Rel $Path
    try {
        if ($Delete) {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        } else {
            $target = Join-Path $Backup ($rel.Replace('/', '\'))
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            Move-Item -LiteralPath $Path -Destination $target -Force -ErrorAction Stop
        }
        $Moved.Add($rel)
    } catch {
        $Failed.Add("$rel ($($_.Exception.Message))")
    }
}

# 1) Папки программы: только файлы из манифеста.
foreach ($dir in @('src\eirven_ai', 'scripts', 'assets')) {
    $base = Join-Path $Root $dir
    if (-not (Test-Path -LiteralPath $base)) { continue }
    Get-ChildItem -LiteralPath $base -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
        ForEach-Object {
            if (-not $Allowed.Contains((Get-Rel $_.FullName))) { Move-Junk $_.FullName }
        }
}

# 2) Внутри src — только пакет eirven_ai и служебная папка pip (её пересоздаёт сборка).
$SrcDir = Join-Path $Root 'src'
if (Test-Path -LiteralPath $SrcDir) {
    Get-ChildItem -LiteralPath $SrcDir -Force | ForEach-Object {
        if ($_.Name -ieq 'eirven_ai' -or $_.Name -ieq 'eirven_ai.egg-info') { return }
        Move-Junk $_.FullName
    }
}

# 3) Остатки неоконченной замены папок программы.
foreach ($name in @('src.old', 'src.new', 'scripts.old', 'scripts.new', 'assets.old', 'assets.new')) {
    $p = Join-Path $Root $name
    if (Test-Path -LiteralPath $p) { Move-Junk $p }
}

# 4) Корень: только явный мусор прошлых версий.
Get-ChildItem -LiteralPath $Root -File -Force | ForEach-Object {
    $n = $_.Name
    $junk = $false
    if ($n -like '*.exe' -and ($CurrentExes -notcontains $n)) { $junk = $true }       # EIRVEN-r71.exe и т. п.
    elseif ($n -like '*.spec') { $junk = $true }
    elseif ($n -like '.installed-v*' -and ($KeepMarkers -notcontains $n)) { $junk = $true }
    elseif ($n -like '.payload-*') { $junk = $true }
    elseif ($n -like 'EIRVEN*.zip' -or $n -like 'eirven*.zip') { $junk = $true }       # старые архивы патчей
    if ($junk) { Move-Junk $_.FullName }
}

if (-not $Quiet -or $Moved.Count -or $Failed.Count) {
    if ($Moved.Count) {
        $verb = if ($Delete) { 'Удалено' } else { 'Убрано в ' + (Get-Rel $Backup) }
        Write-Host ("{0}: {1} шт." -f $verb, $Moved.Count) -ForegroundColor Green
        $Moved | Select-Object -First 40 | ForEach-Object { Write-Host "  - $_" -ForegroundColor DarkGray }
        if ($Moved.Count -gt 40) { Write-Host ("  … и ещё {0}" -f ($Moved.Count - 40)) -ForegroundColor DarkGray }
        if (-not $Delete) { Write-Host 'Если всё работает, папку _old_files_backup можно удалить.' -ForegroundColor DarkGray }
    } else {
        Write-Host 'Лишних файлов нет — в папке только текущая версия.' -ForegroundColor Green
    }
    if ($Failed.Count) {
        Write-Host 'Не удалось убрать (заняты другой программой):' -ForegroundColor Yellow
        $Failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
    }
}
exit 0
