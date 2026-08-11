$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { $PythonW = Join-Path $Root '.venv\Scripts\python.exe' }
if (-not (Test-Path $PythonW)) { throw "EIRVEN Python environment not found" }
$Launcher = Join-Path $Root 'launcher.py'
if (-not (Test-Path $Launcher)) { throw "EIRVEN launcher not found" }
$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'EIRVEN AI.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
# Never target the self-elevating EXE from the Windows Startup folder: a logon-time
# UAC prompt can be suppressed or left unanswered, which makes autostart appear broken.
# The quiet launcher keeps the same port/duplicate-instance recovery as a normal start.
$Shortcut.TargetPath = $PythonW
$Shortcut.Arguments = ('"{0}" --autostart' -f $Launcher)
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 7
$Shortcut.Description = 'EIRVEN AI quiet voice-first autostart'
$Icon = Join-Path $Root 'assets\eirven.ico'
if (Test-Path $Icon) { $Shortcut.IconLocation = $Icon }
$Shortcut.Save()
Write-Host "Autostart enabled: $ShortcutPath"
