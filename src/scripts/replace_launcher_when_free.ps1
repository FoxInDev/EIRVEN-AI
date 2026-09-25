# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Target,
    [int]$TimeoutSeconds = 120
)
$ErrorActionPreference = 'SilentlyContinue'
$deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSeconds))
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path -LiteralPath $Source)) { exit 2 }
    try {
        Copy-Item -LiteralPath $Source -Destination $Target -Force -ErrorAction Stop
        exit 0
    } catch {
        Start-Sleep -Milliseconds 600
    }
}
exit 1
