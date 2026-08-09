@echo off
setlocal
cd /d "%~dp0\.."
set NO_PROXY=127.0.0.1,localhost,::1
set no_proxy=127.0.0.1,localhost,::1
if exist "EIRVEN-AI-r26.exe" (
  start "EIRVEN AI" "EIRVEN-AI-r26.exe"
  exit /b 0
)
if not exist ".installed-v1.4.0-r26" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
start "EIRVEN AI" ".venv\Scripts\pythonw.exe" "launcher.py"
endlocal
