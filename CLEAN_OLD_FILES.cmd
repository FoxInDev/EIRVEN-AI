@echo off
rem Uborka papki EIRVEN: vse, chto ne otnositsya k tekushchey versii,
rem uhodit v _old_files_backup. Dannye, modeli i .venv ne trogayutsya.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\clean_old_files.ps1" %*
echo.
pause
