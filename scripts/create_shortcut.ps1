$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Shell = New-Object -ComObject WScript.Shell
$UnifiedExe = Join-Path $Root "EIRVEN.exe"
$VersionedExe = Join-Path $Root "EIRVEN-AI-r72.exe"
$LegacyExe = Join-Path $Root "EIRVEN-AI.exe"
$ExpectedVersion = "2.4.0.72"
function Test-EirvenLauncher([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try { return ((Get-Item -LiteralPath $Path).VersionInfo.FileVersion.Trim() -eq $ExpectedVersion) }
    catch { return $false }
}
if (Test-EirvenLauncher $UnifiedExe) {
    $TargetPath = $UnifiedExe
} elseif (Test-EirvenLauncher $VersionedExe) {
    $TargetPath = $VersionedExe
} elseif (Test-EirvenLauncher $LegacyExe) {
    $TargetPath = $LegacyExe
} else {
    throw "Не найден актуальный лаунчер EIRVEN $ExpectedVersion. Пересоберите или скачайте EIRVEN.exe заново."
}
$DesktopCandidates = @([Environment]::GetFolderPath("Desktop"), $Shell.SpecialFolders.Item("Desktop")) |
    Where-Object { $_ -and $_.Trim() } |
    ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).Trim() } |
    Select-Object -Unique
if (-not $DesktopCandidates) { throw "Windows did not return the current user Desktop path" }
$Icon = Join-Path $Root "assets\eirven.ico"
$Created = @()
foreach ($Desktop in $DesktopCandidates) {
    New-Item -ItemType Directory -Path $Desktop -Force | Out-Null
    $ShortcutPath = Join-Path $Desktop "EIRVEN AI.lnk"
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $TargetPath
    $Shortcut.WorkingDirectory = $Root
    $Shortcut.Description = "Local personal AI"
    if (Test-Path $Icon) { $Shortcut.IconLocation = $Icon }
    $Shortcut.Save()
    if (-not (Test-Path $ShortcutPath)) { throw "Desktop shortcut was not created: $ShortcutPath" }
    $Verified = $Shell.CreateShortcut($ShortcutPath)
    if ([IO.Path]::GetFullPath($Verified.TargetPath) -ne [IO.Path]::GetFullPath($TargetPath)) { throw "Desktop shortcut points to the wrong target: $ShortcutPath" }
    if (-not (Test-EirvenLauncher $Verified.TargetPath)) { throw "Desktop shortcut points to an outdated launcher: $ShortcutPath" }
    $Created += $ShortcutPath
}
Write-Host "Desktop shortcut created and verified: $($Created -join '; ')"
