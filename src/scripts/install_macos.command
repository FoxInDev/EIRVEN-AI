#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Этот установщик предназначен для macOS."
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Нужен Python 3.11+; установи Homebrew с brew.sh и повтори запуск."
    exit 3
  fi
  brew install python@3.12
  PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
fi

if ! command -v ollama >/dev/null 2>&1 && [[ ! -x /Applications/Ollama.app/Contents/Resources/ollama ]]; then
  if ! command -v brew >/dev/null 2>&1; then
    open "https://ollama.com/download/mac"
    echo "Установи открывшийся бесплатный компонент и повтори запуск."
    exit 4
  fi
  brew install --cask ollama
fi

"$PYTHON_BIN" -m venv .venv-macos
.venv-macos/bin/python -m pip install --upgrade pip wheel setuptools
.venv-macos/bin/python -m pip install -r requirements.txt -r requirements-desktop.txt -r requirements-integrations.txt -r requirements-voice.txt
.venv-macos/bin/python -m pip install -e .
open -gja Ollama || true
sleep 2
MODEL="$(.venv-macos/bin/python -c 'from eirven_ai.hardware import detect_hardware; print(detect_hardware().recommended_main_model)')"
OLLAMA_BIN="$(command -v ollama || true)"
if [[ -z "$OLLAMA_BIN" ]]; then OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"; fi
"$OLLAMA_BIN" pull "$MODEL"

export EIRVEN_ROOT_DIR="$ROOT"
export EIRVEN_OPEN_BROWSER=true
nohup .venv-macos/bin/python -m eirven_ai.supervisor >logs/macos-launch.log 2>&1 &
sleep 2
open "http://127.0.0.1:7860/ui/"
echo "Эрви запущена."
