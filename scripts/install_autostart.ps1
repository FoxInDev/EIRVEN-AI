$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$VersionedExe = Join-Path $Root 'EIRVEN-AI-r29.exe'
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { $PythonW = Join-Path $Root '.venv\Scripts\python.exe' }
if (-not (Test-Path $PythonW)) { throw "EIRVEN Python environment not found" }
$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'EIRVEN AI.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
if (Test-Path $VersionedExe) {
    $Shortcut.TargetPath = $VersionedExe
    $Shortcut.Arguments = ''
} else {
    $Shortcut.TargetPath = $PythonW
    $Shortcut.Arguments = '-m eirven_ai.supervisor'
}
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'EIRVEN AI voice-first assistant'
$Icon = Join-Path $Root 'assets\eirven.ico'
if (Test-Path $Icon) { $Shortcut.IconLocation = $Icon }
$Shortcut.Save()
Write-Host "Autostart enabled: $ShortcutPath"
