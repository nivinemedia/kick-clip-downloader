#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3.12}"
VENV_DIR=".venv312"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.12 nao encontrado. Instale o python3.12 e tente novamente."
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -r requirements.txt
fi

exec "$VENV_DIR/bin/python" app.py
