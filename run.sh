#!/usr/bin/env bash
# run.sh — launcher best-effort para Linux/macOS. NO es la plataforma soportada:
# el launcher principal y testeado es run.bat (Windows-first). Sin garantía aquí.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_VERSION="3.12"
VENV_DIR="$SCRIPT_DIR/venv"
TOTAL=3

# Detección del subdirectorio de binarios del venv (bin/ en Unix).
VENV_PY_PATH="$VENV_DIR/bin/python"

# uv vive en el perfil del usuario; lo añadimos al PATH para encontrarlo aunque
# acabe de instalarse en esta misma ejecución. only-managed: no engancha Pythons
# ajenos del sistema (paridad con run.bat).
export PATH="$HOME/.local/bin:$PATH"
export UV_PYTHON_PREFERENCE="only-managed"

# ── Paso 1: uv ────────────────────────────────────────────────────────────────
echo "[1/$TOTAL] Verificando uv..."
if ! command -v uv >/dev/null 2>&1; then
    echo "  -> Instalando uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "  -> uv OK ($(command -v uv))"
fi

# ── Paso 2: venv (autorepara si es de otra plataforma) ───────────────────────
# uv venv descarga el Python 3.12 gestionado si falta, sin crear shims en
# ~/.local/bin. Si falta el python esperado, el venv es de otro OS o no existe.
echo "[2/$TOTAL] Verificando entorno virtual..."
if [ ! -f "$VENV_PY_PATH" ]; then
    if [ -d "$VENV_DIR" ]; then
        echo "  -> venv incompatible con esta plataforma, recreando..."
        rm -rf "$VENV_DIR"
    else
        echo "  -> Creando venv con Python $PYTHON_VERSION..."
    fi
    uv venv "$VENV_DIR" --python "$PYTHON_VERSION"
else
    echo "  -> venv OK"
fi

# ── Paso 3: dependencias e inicio ────────────────────────────────────────────
echo "[3/$TOTAL] Instalando dependencias..."
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
uv pip install -q -r requirements.txt
echo "  -> Dependencias OK"

echo "[LAUNCH] Iniciando Panopticon..."
python main.py
