#!/usr/bin/env bash
set -euo pipefail

# AI Daily Ops Summary — one-command install for demo
# - creates venv
# - installs python deps
# - installs Ollama (macOS/Linux best-effort)
# - pulls small model (phi3:mini)
# - prints run instructions

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
LLM_MODEL="${LLM_MODEL:-phi3:mini}"

echo ""
echo "== AI Daily Ops Summary: install =="
echo ""

# 1) venv
if [ ! -d "${VENV_DIR}" ]; then
  echo "[1/5] Creating venv: ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
else
  echo "[1/5] venv exists: ${VENV_DIR}"
fi

echo "[2/5] Activating venv"
# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

echo "[3/5] Installing Python dependencies"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4) Ollama install (best-effort)
echo "[4/5] Checking Ollama"
if command -v ollama >/dev/null 2>&1; then
  echo " - Ollama: found"
else
  echo " - Ollama: not found. Installing (best-effort)..."
  OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
  if [ "${OS}" = "darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install ollama || true
    else
      echo "   [warn] Homebrew not found. Please install Ollama manually:"
      echo "   https://ollama.com/download"
    fi
  elif [ "${OS}" = "linux" ]; then
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL https://ollama.com/install.sh | sh || true
    else
      echo "   [warn] curl not found. Please install Ollama manually:"
      echo "   https://ollama.com/download"
    fi
  else
    echo "   [warn] Unsupported OS for auto-install. Install Ollama manually:"
    echo "   https://ollama.com/download"
  fi
fi

# 5) pull model (if possible)
echo "[5/5] Preparing demo model: ${LLM_MODEL}"
if command -v ollama >/dev/null 2>&1; then
  # Start server if not running (best-effort)
  if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo " - Starting Ollama server in background..."
    (ollama serve >/dev/null 2>&1 &) || true
    sleep 1
  fi

  echo " - Pulling model: ${LLM_MODEL}"
  ollama pull "${LLM_MODEL}" || {
    echo "   [warn] Failed to pull model. You can try later:"
    echo "   ollama pull ${LLM_MODEL}"
  }
else
  echo " - Ollama still not available. Demo will work with --llm none."
fi

echo ""
echo "[ok] Installed."
echo ""
echo "Next:"
echo "  source ${VENV_DIR}/bin/activate"
echo "  python main.py --input inputs_demo --output outputs --llm none"
echo ""
echo "Ollama mode (recommended):"
echo "  ollama serve"
echo "  python main.py --input inputs_demo --output outputs --llm ollama --ollama-model ${LLM_MODEL}"
echo ""
