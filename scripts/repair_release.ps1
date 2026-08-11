$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) { exit 3 }

# Stop the current local core cleanly so both executable names can be replaced.
try {
    $portFile = Join-Path $Root "logs\runtime_port"
    if (Test-Path -LiteralPath $portFile) {
        $port = [int](Get-Content -LiteralPath $portFile -Raw).Trim()
        Invoke-WebRequest -UseBasicParsing -Method Post -Uri "http://127.0.0.1:$port/api/system/shutdown" -TimeoutSec 2 | Out-Null
        Start-Sleep -Milliseconds 900
    }
} catch {}

# This is an update/repair lane, not the first-install bootstrap. It never downloads models.
& $Python -c "import eirven_ai,fastapi,uvicorn,httpx,pydantic; print('runtime-ok')"
if ($LASTEXITCODE -ne 0) { exit 3 }

# Keep the editable package metadata aligned with the newly extracted source without
# reinstalling the full dependency stack.
& $Python -m pip install --disable-pip-version-check --no-deps -e $Root
if ($LASTEXITCODE -ne 0) { exit 3 }
& $Python -m compileall -q (Join-Path $Root "src")
if ($LASTEXITCODE -ne 0) { exit 3 }

& powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_windows.ps1")
if ($LASTEXITCODE -ne 0) { throw "Не удалось пересобрать Windows launcher" }

& powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "create_shortcut.ps1")
if ($LASTEXITCODE -ne 0) { throw "Не удалось обновить ярлык" }
try {
    & powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install_autostart.ps1") | Out-Null
} catch {}

Set-Content -LiteralPath (Join-Path $Root ".installed-v1.7.3-r37") -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding UTF8
Write-Host "EIRVEN repaired without re-downloading models." -ForegroundColor Green
exit 0
