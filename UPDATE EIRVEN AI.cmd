@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto fullinstall
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\repair_release.ps1"
if errorlevel 3 goto fullinstall
if errorlevel 1 (
  echo Update needs attention. See the message above.
  pause
  exit /b 1
)
call "%~dp0scripts\start_windows.bat"
exit /b 0

:fullinstall
call "%~dp0INSTALL EIRVEN AI.cmd"
