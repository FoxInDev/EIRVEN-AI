$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
& "$PSScriptRoot\ensure_runtime.ps1"
