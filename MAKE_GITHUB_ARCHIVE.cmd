@echo off
rem Arhiv tekushchey versii EIRVEN dlya GitHub - na rabochiy stol.
cd /d "%~dp0"
set PYEXE=
if exist ".venv\Scripts\python.exe" set PYEXE=.venv\Scripts\python.exe
if not defined PYEXE (
  where py >nul 2>nul && set PYEXE=py -3
)
if not defined PYEXE set PYEXE=python
%PYEXE% make_github_archive.py %*
echo.
pause
