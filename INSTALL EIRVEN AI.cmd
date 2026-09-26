@echo off
setlocal
cd /d "%~dp0"
REM r60: one linear installer. No self-elevation, no recursive full reinstall.
REM Ollama/Python/runtime are handled by ensure_runtime.ps1 for the CURRENT user.
if exist ".installed-v2.2.0-r70-liquid-orb" if exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy RemoteSigned -File ".\scripts\repair_release.ps1"
  if errorlevel 1 (
    echo EIRVEN repair needs attention. See the message above.
    pause
    exit /b 1
  )
  call "%~dp0scripts\start_windows.bat"
  exit /b 0
)
powershell -NoProfile -ExecutionPolicy RemoteSigned -File ".\scripts\ensure_runtime.ps1"
if errorlevel 1 (
  echo EIRVEN installation stopped on the failed component. Nothing is restarted from zero.
  echo Run this file again after fixing the reported component; completed stages are preserved.
  pause
  exit /b 1
)
call "%~dp0scripts\start_windows.bat"
endlocal
