@echo off
setlocal
cd /d "%~dp0\.."
set NO_PROXY=127.0.0.1,localhost,::1
set no_proxy=127.0.0.1,localhost,::1
if exist "EIRVEN-AI-r37.exe" (
  start "EIRVEN AI" "EIRVEN-AI-r37.exe"
  exit /b 0
)
if not exist ".installed-v1.7.3-r37" (
  if exist ".venv\Scripts\python.exe" (
    > ".installed-v1.7.3-r37" echo repaired-on-start %date% %time%
  ) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
    if errorlevel 1 exit /b %errorlevel%
  )
)
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_runtime.ps1"
  if errorlevel 1 exit /b %errorlevel%
)
start "EIRVEN AI" ".venv\Scripts\pythonw.exe" "launcher.py"
endlocal
