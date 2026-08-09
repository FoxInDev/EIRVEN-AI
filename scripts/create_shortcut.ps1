$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "EIRVEN AI.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Exe = Join-Path $Root "EIRVEN-AI.exe"
if (Test-Path $Exe) {
    $Shortcut.TargetPath = $Exe
} else {
    $Shortcut.TargetPath = Join-Path $Root "EIRVEN AI.cmd"
}
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Local personal AI"
$Icon = Join-Path $Root "assets\eirven.ico"
if (Test-Path $Icon) { $Shortcut.IconLocation = $Icon }
$Shortcut.Save()
Write-Host "Shortcut created: $ShortcutPath"
