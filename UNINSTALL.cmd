REM EIRVEN AI — 2.4.0
REM Copyright (c) 2026 Даниил Павлов. Все права защищены. / All rights reserved.
REM Лицензия: EIRVEN Non-Commercial License — см. файл LICENSE.
REM Обязательна видимая подпись «На базе Эрви». Скрывать её запрещено (см. LICENSE).
REM EIRVEN-LICENSE-HEADER
@echo off
title Uninstall EIRVEN
cd /d "%~dp0"

rem Sborka idyot cherez PyInstaller, a ne Inno Setup, poetomu privychnogo
rem unins000.exe zdes net. Etot fayl vypolnyaet tu zhe rol.
rem Tekst namerenno bez kirillitsy: konsol Windows v raznyh lokalyah
rem otobrazhaet eyo po-raznomu, i imenno na etom lomalas kodirovka.

if not exist "scripts\uninstall.ps1" (
  echo.
  echo   scripts\uninstall.ps1 not found - folder is damaged.
  echo   Download EIRVEN again or remove the folder manually.
  echo.
  pause
  exit /b 1
)

echo.
echo   Udalenie EIRVEN / Uninstall EIRVEN
echo.
echo   Budut udaleny: prilozhenie, okruzhenie Python, modeli,
echo   yarlyki, avtozapusk i pravilo brandmauera.
echo   Perepiska, pamyat i nastroyki po umolchaniyu sohranyatsya.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\uninstall.ps1"
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo   Exit code: %RC%
  echo.
  pause
)
exit /b %RC%
