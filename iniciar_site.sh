#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

if command -v python3.12 >/dev/null 2>&1; then
  PYTHON_BIN="python3.12"
  VENV_DIR=".venv312"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
  VENV_DIR=".venv"
else
  echo "Python 3 nao encontrado. Instale python3 e tente novamente."
  exit 1
fi

echo "Usando $($PYTHON_BIN --version) em $VENV_DIR"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Criando ambiente virtual..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r requirements.txt
fi

if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti:7860 2>/dev/null || true)
  if [ -n "$PIDS" ]; then
    echo "Matando processo anterior na porta 7860..."
    echo "$PIDS" | xargs kill -9 2>/dev/null || true
  fi
fi

echo "Testando imports..."
if ! "$VENV_DIR/bin/python" -c "import flask, yt_dlp; print('ok')" 2>&1; then
  echo ""
  echo "Dependencias quebradas. Recriando o ambiente virtual..."
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r requirements.txt
fi

echo ""
echo "Abrindo em http://localhost:7860"
echo "---"
exec "$VENV_DIR/bin/python" -u app.py
