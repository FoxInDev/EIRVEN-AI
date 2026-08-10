@echo off
setlocal
cd /d "%~dp0"

set "EIRVEN_DEFAULT_MODEL=gemma4:e2b"
set "EIRVEN_CLAUDE_MODEL="
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if /i "%%A"=="EIRVEN_MODEL" set "EIRVEN_DEFAULT_MODEL=%%B"
  if /i "%%A"=="EIRVEN_CLAUDE_CODE_MODEL" set "EIRVEN_CLAUDE_MODEL=%%B"
)
if not defined EIRVEN_CLAUDE_MODEL set "EIRVEN_CLAUDE_MODEL=%EIRVEN_DEFAULT_MODEL%"

set "ANTHROPIC_AUTH_TOKEN=ollama"
set "ANTHROPIC_API_KEY="
set "ANTHROPIC_BASE_URL=http://127.0.0.1:11434"
set "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
set "NO_PROXY=127.0.0.1,localhost"
set "no_proxy=127.0.0.1,localhost"

where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code CLI is not installed yet. Run INSTALL EIRVEN AI.cmd first.
  pause
  exit /b 1
)

claude --model "%EIRVEN_CLAUDE_MODEL%"
if errorlevel 1 pause
endlocal
