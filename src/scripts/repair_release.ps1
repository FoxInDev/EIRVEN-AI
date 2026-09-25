# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { exit 3 }

# Stop every legacy/global EIRVEN copy, but keep Ollama and its downloads alive.
& powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop_eirven.ps1") -AllInstances | Out-Host
if ($LASTEXITCODE -ne 0) { exit 5 }

# Repair stale model keys without touching Telegram/API/access settings.
& $Python (Join-Path $Root "src\eirven_ai\release_policy.py") --env (Join-Path $Root ".env")
if ($LASTEXITCODE -ne 0) { exit 3 }

$RequiredModels = @(& $Python -c "from eirven_ai.hardware import detect_hardware; p=detect_hardware(); print(p.recommended_main_model); print(p.recommended_vision_model)" | Select-Object -Unique)
$Ollama = Get-Command ollama.exe -ErrorAction SilentlyContinue
$Installed = @()
if ($Ollama) {
    $Installed = @(& $Ollama.Source list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] })
}
$Missing = @($RequiredModels | Where-Object { $_ -notin $Installed })
if ($Missing.Count -gt 0) {
    Write-Host "Эрви завершает подготовку: $($Missing.Count) компонент(а)." -ForegroundColor Cyan
    & $Python (Join-Path $Root "scripts\bootstrap.py")
    exit $LASTEXITCODE
}

# This is an update/repair lane, not the first-install bootstrap. It never downloads models.
& $Python -c "import eirven_ai,fastapi,uvicorn,httpx,pydantic; print('runtime-ok')"
if ($LASTEXITCODE -ne 0) { exit 3 }

# Keep the editable package metadata aligned with the newly extracted source without
# reinstalling the full dependency stack.
& $Python -m pip install --disable-pip-version-check --no-deps -e $Root
if ($LASTEXITCODE -ne 0) { exit 3 }
& $Python -m compileall -q (Join-Path $Root "src")
if ($LASTEXITCODE -ne 0) { exit 3 }

$InstalledExe = Join-Path $Root "EIRVEN.exe"
if (-not (Test-Path -LiteralPath $InstalledExe)) {
    throw "EIRVEN.exe отсутствует; скачайте актуальный файл с официального сайта"
}
$InstalledVersion = (Get-Item -LiteralPath $InstalledExe).VersionInfo.FileVersion.Trim()
if ($InstalledVersion -ne "2.0.0.67") {
    throw "EIRVEN.exe устарел: $InstalledVersion вместо 2.0.0.67. Скачайте актуальный файл."
}

& powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "create_shortcut.ps1")
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить ярлык" }
try {
    & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install_autostart.ps1") | Out-Null
} catch {}

Set-Content -LiteralPath (Join-Path $Root ".installed-v2.0.0-r67-universal-engine") -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding UTF8
Write-Host "EIRVEN repaired without re-downloading models." -ForegroundColor Green
exit 0
