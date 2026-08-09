$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "EIRVEN AI.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$VersionedExe = Join-Path $Root "EIRVEN-AI-r26.exe"
$LegacyExe = Join-Path $Root "EIRVEN-AI.exe"
if (Test-Path $VersionedExe) {
    $Shortcut.TargetPath = $VersionedExe
} elseif (Test-Path $LegacyExe) {
    $Shortcut.TargetPath = $LegacyExe
} else {
    $Shortcut.TargetPath = Join-Path $Root "EIRVEN AI.cmd"
}
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Local personal AI"
$Icon = Join-Path $Root "assets\eirven.ico"
if (Test-Path $Icon) { $Shortcut.IconLocation = $Icon }
$Shortcut.Save()
# Refresh Explorer's shortcut/icon cache without restarting the desktop shell.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EirvenShortcutNotify {
  [DllImport("shell32.dll")] public static extern void SHChangeNotify(uint eventId, uint flags, IntPtr item1, IntPtr item2);
}
"@ -ErrorAction SilentlyContinue
[EirvenShortcutNotify]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Host "Shortcut created: $ShortcutPath"
