# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& (Join-Path $PSScriptRoot 'build_windows_native.ps1') -PayloadOnly

$BuildRoot = Join-Path $Root 'build\inno-launcher'
if (-not $BuildRoot.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Inno build path escaped the EIRVEN workspace.'
}
if (Test-Path -LiteralPath $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null

$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $Csc)) { $Csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $Csc)) { throw 'Microsoft .NET Framework C# compiler was not found.' }
$framework = Split-Path -Parent $Csc
$runner = Join-Path $BuildRoot 'EIRVEN.exe'
& $Csc /nologo /target:winexe /optimize+ /platform:anycpu "/out:$runner" "/win32icon:$Root\assets\eirven.ico" "/reference:$framework\System.Windows.Forms.dll" (Join-Path $PSScriptRoot 'eirven_runner.cs')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runner)) { throw 'EIRVEN installed launcher build failed.' }

$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 compiler was not found.' }
& $iscc (Join-Path $PSScriptRoot 'eirven_setup.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup build failed.' }

$output = Join-Path $Root 'EIRVEN-Windows-r67.exe'
if (-not (Test-Path -LiteralPath $output)) { throw 'Inno Setup did not create EIRVEN-Windows-r67.exe.' }
$version = (Get-Item -LiteralPath $output).VersionInfo
if ($version.FileVersion.Trim() -ne '2.0.0.67') { throw "Unexpected setup version: $($version.FileVersion)" }
Write-Host "Built Inno Setup installer: $output"
