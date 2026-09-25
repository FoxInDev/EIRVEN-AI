# EIRVEN AI — 2.4.0
# Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
# Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
# Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
# EIRVEN-LICENSE-HEADER
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BuildRoot = Join-Path $Root 'build\native-launcher'
$Stage = Join-Path $BuildRoot 'payload'
$Payload = Join-Path $BuildRoot 'eirven-payload.zip'
$Output = Join-Path $Root 'EIRVEN-Windows-r67.exe'

if (-not $BuildRoot.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Native build path escaped the EIRVEN workspace.'
}
if (Test-Path -LiteralPath $BuildRoot) { Remove-Item -LiteralPath $BuildRoot -Recurse -Force }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

$directories = @('src', 'scripts', 'assets')
foreach ($directory in $directories) {
    $sourceRoot = Join-Path $Root $directory
    Get-ChildItem -LiteralPath $sourceRoot -Recurse -File | Where-Object {
        $_.Extension -ne '.pyc' -and $_.FullName -notmatch '[\\/]__pycache__[\\/]'
    } | ForEach-Object {
        $relative = $_.FullName.Substring($Root.Length).TrimStart('\', '/')
        $destination = Join-Path $Stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}

$files = @(
    '.env.example', 'launcher.py', 'pyproject.toml', 'requirements.txt',
    'requirements-desktop.txt', 'requirements-integrations.txt', 'requirements-voice.txt',
    'requirements-build.txt', 'BUILD_INFO.json', 'EIRVEN_VERSION.txt', 'LICENSE',
    'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md', 'INSTALL EIRVEN AI.cmd',
    'EIRVEN-Mobile.apk'
)
foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $Root $file) -Destination (Join-Path $Stage $file) -Force
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($Stage, $Payload, [IO.Compression.CompressionLevel]::Optimal, $false)

$Csc = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $Csc)) { $Csc = 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $Csc)) { throw 'Microsoft .NET Framework C# compiler was not found.' }
$framework = Split-Path -Parent $Csc
$arguments = @(
    '/nologo', '/target:winexe', '/optimize+', '/platform:anycpu',
    ('/out:' + $Output),
    ('/win32icon:' + (Join-Path $Root 'assets\eirven.ico')),
    ('/resource:' + $Payload + ',EirvenPayload.zip'),
    ('/reference:' + (Join-Path $framework 'System.IO.Compression.dll')),
    ('/reference:' + (Join-Path $framework 'System.IO.Compression.FileSystem.dll')),
    ('/reference:' + (Join-Path $framework 'System.Windows.Forms.dll')),
    (Join-Path $PSScriptRoot 'native_launcher.cs')
)
& $Csc @arguments
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Output)) { throw 'Native Windows launcher build failed.' }

$version = (Get-Item -LiteralPath $Output).VersionInfo
if ($version.FileVersion -ne '2.0.0.67') { throw "Unexpected native launcher version: $($version.FileVersion)" }
Write-Host "Built native launcher: $Output"
