#!/usr/bin/env bash
# run.sh — Panopticon launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_VERSION="3.12"
VENV_DIR="$SCRIPT_DIR/venv"
UV_BIN="$HOME/.local/bin/uv"

# ── Paso 1: uv ────────────────────────────────────────────────────────────────
echo "[1/4] Verificando uv..."
if ! command -v uv &>/dev/null && [ ! -x "$UV_BIN" ]; then
    echo "  → Instalando uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    export PATH="$HOME/.local/bin:$PATH"
    echo "  → uv OK"
fi

# ── Paso 2: Python 3.12 ───────────────────────────────────────────────────────
echo "[2/4] Verificando Python $PYTHON_VERSION..."
if ! uv python list 2>/dev/null | grep -q "cpython-${PYTHON_VERSION}"; then
    echo "  → Descargando Python $PYTHON_VERSION..."
    uv python install "$PYTHON_VERSION"
else
    echo "  → Python $PYTHON_VERSION OK"
fi

# ── Paso 3: venv ─────────────────────────────────────────────────────────────
echo "[3/4] Verificando entorno virtual..."
if [ ! -d "$VENV_DIR" ]; then
    echo "  → Creando venv con Python $PYTHON_VERSION..."
    uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
fi

source "$VENV_DIR/bin/activate"
echo "  → Instalando dependencias..."
uv pip install -q -r requirements.txt
echo "  → Dependencias OK"

# ── Paso 4: lanzar ───────────────────────────────────────────────────────────
echo "[4/4] Iniciando Panopticon..."
python main.py
