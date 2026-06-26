#!/usr/bin/env bash
# run.sh — Panopticon launcher (fuente ÚNICA, portable Linux/macOS/Windows-GitBash).
# El run.bat de Windows solo delega aquí. No dupliques lógica de arranque.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_VERSION="3.12"
VENV_DIR="$SCRIPT_DIR/venv"
TOTAL=5

# ── Detección de OS (locale-safe, vía uname) ──────────────────────────────────
# Define el subdirectorio de binarios del venv y el nombre del intérprete según
# la plataforma. En Git-Bash/MSYS el venv usa Scripts/ y python.exe.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) VENV_BIN="Scripts"; VENV_PY="python.exe" ;;
    *)                    VENV_BIN="bin";     VENV_PY="python" ;;
esac
VENV_PY_PATH="$VENV_DIR/$VENV_BIN/$VENV_PY"

# uv se instala en el perfil del usuario (~/.local/bin); lo añadimos al PATH para
# encontrarlo aunque acabe de instalarse en esta misma ejecución.
export PATH="$HOME/.local/bin:$PATH"

# ── Paso 1: uv ────────────────────────────────────────────────────────────────
echo "[1/$TOTAL] Verificando uv..."
if ! command -v uv >/dev/null 2>&1; then
    echo "  -> Instalando uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "  -> uv OK ($(command -v uv))"
fi

# ── Paso 2: Python 3.12 (gestionado por uv, no por el OS) ─────────────────────
echo "[2/$TOTAL] Verificando Python $PYTHON_VERSION..."
if ! uv python list --only-installed 2>/dev/null | grep -q "cpython-${PYTHON_VERSION}"; then
    echo "  -> Descargando Python $PYTHON_VERSION..."
    uv python install "$PYTHON_VERSION"
else
    echo "  -> Python $PYTHON_VERSION OK"
fi

# ── Paso 3: venv (autorepara si es de otra plataforma) ───────────────────────
# Al mover el proyecto entre Linux y Windows el venv viejo apunta a un intérprete
# inexistente. Si falta el python esperado para ESTA plataforma, lo recreamos.
echo "[3/$TOTAL] Verificando entorno virtual..."
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

# ── Paso 4: dependencias ─────────────────────────────────────────────────────
echo "[4/$TOTAL] Instalando dependencias..."
# shellcheck disable=SC1090
source "$VENV_DIR/$VENV_BIN/activate"
uv pip install -q -r requirements.txt
echo "  -> Dependencias OK"

# ── Paso 5: lanzar ───────────────────────────────────────────────────────────
echo "[5/$TOTAL] Iniciando Panopticon..."
python main.py
