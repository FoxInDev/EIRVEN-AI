@echo off
setlocal
cd /d "%~dp0\.."
set NO_PROXY=127.0.0.1,localhost,::1
set no_proxy=127.0.0.1,localhost,::1
REM Never launch a partially built EXE before the full installer marker exists.
if not exist ".installed-v2.4.0-r72-k5" if not exist ".installed-v2.4.0-r72-k4" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
if exist "EIRVEN.exe" (
  start "EIRVEN AI" "EIRVEN.exe"
  exit /b 0
)
if exist "EIRVEN-AI-r72.exe" (
  start "EIRVEN AI" "EIRVEN-AI-r72.exe"
  exit /b 0
)
start "EIRVEN AI" ".venv\Scripts\pythonw.exe" "launcher.py"
endlocal
