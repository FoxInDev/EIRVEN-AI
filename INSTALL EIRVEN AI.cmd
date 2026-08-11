@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\repair_release.ps1"
  if not errorlevel 3 (
    if errorlevel 1 (
      echo Installation repair needs attention. See the message above.
      pause
      exit /b 1
    )
    call "%~dp0scripts\start_windows.bat"
    exit /b 0
  )
)
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\ensure_runtime.ps1"
if errorlevel 1 (
  echo Installation needs attention. See the message above.
  pause
  exit /b 1
)
call "%~dp0scripts\start_windows.bat"
