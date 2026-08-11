$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Shell = New-Object -ComObject WScript.Shell
$VersionedExe = Join-Path $Root "EIRVEN-AI-r37.exe"
$LegacyExe = Join-Path $Root "EIRVEN-AI.exe"
if (Test-Path $VersionedExe) {
    $TargetPath = $VersionedExe
} elseif (Test-Path $LegacyExe) {
    $TargetPath = $LegacyExe
} else {
    $TargetPath = Join-Path $Root "EIRVEN AI.cmd"
}
if (-not (Test-Path $TargetPath)) { throw "EIRVEN launch target was not found: $TargetPath" }
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
    $Created += $ShortcutPath
}
Write-Host "Desktop shortcut created and verified: $($Created -join '; ')"
