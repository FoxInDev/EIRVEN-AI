REM EIRVEN AI — 2.4.0
REM Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
REM Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
REM Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
REM EIRVEN-LICENSE-HEADER
@echo off
setlocal

echo ==========================================
echo EIRVEN AI PATCH INSTALLER
echo ==========================================

set "ROOT=%~dp0"
for %%I in ("%ROOT%..") do set "TARGET=%%~fI"

echo Project:
echo %TARGET%
echo.

choice /M "Create backup before install"
if errorlevel 2 goto install
if errorlevel 1 goto backup

:backup
set "BACKUP=%TARGET%_backup_r68"

echo Creating backup:
echo %BACKUP%

if not exist "%BACKUP%" mkdir "%BACKUP%"

for %%D in (data config src .eirven_override) do (
    if exist "%TARGET%\%%D" (
        echo Copying %%D
        xcopy "%TARGET%\%%D" "%BACKUP%\%%D" /E /I /H /Y >nul
    )
)

goto install

:install
echo.
echo Installing patch...

if exist "%ROOT%eirven_ai" (
    xcopy "%ROOT%eirven_ai" "%TARGET%\.eirven_override\eirven_ai" /E /I /H /Y
)

if exist "%ROOT%src" (
    xcopy "%ROOT%src" "%TARGET%\src" /E /I /H /Y
)

del /s /q "%TARGET%\*.pyc" >nul 2>&1

echo.
echo PATCH COMPLETE
echo Restart EIRVEN AI.
pause
endlocal
