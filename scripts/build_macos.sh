#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Native .app builds must run on macOS."
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m venv .venv-macos-build
.venv-macos-build/bin/python -m pip install --upgrade pip wheel setuptools
.venv-macos-build/bin/python -m pip install -r requirements.txt -r requirements-desktop.txt -r requirements-integrations.txt -r requirements-voice.txt
.venv-macos-build/bin/python -m pip install "pyinstaller>=6.10,<7.0" -e .

.venv-macos-build/bin/python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "EIRVEN AI" \
  --collect-all imageio_ffmpeg \
  --add-data "$ROOT/src/eirven_ai/web:eirven_ai/web" \
  --add-data "$ROOT/assets:assets" \
  scripts/macos_entry.py

mkdir -p release
ditto -c -k --sequesterRsrc --keepParent "dist/EIRVEN AI.app" "release/EIRVEN-macOS-r67.app.zip"
echo "Built: $ROOT/release/EIRVEN-macOS-r67.app.zip"
