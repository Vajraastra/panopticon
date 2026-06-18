"""
Configuración central de logging para Panopticon.

Punto único de configuración del sistema de logging (regla de oro: atacar la
raíz, no instancia por instancia). `setup_logging()` se llama UNA sola vez al
arrancar la app (desde main.py) y deja listos dos destinos para TODO el
proyecto:

    1. Consola (stderr)              — nivel configurable, por defecto INFO.
    2. logs/panopticon.log (rotado)  — nivel DEBUG, historial persistente.

Cualquier módulo obtiene su logger con el patrón estándar de la librería:

    import logging
    log = logging.getLogger(__name__)

y hereda automáticamente estos handlers vía el logger raíz. No debe llamar a
`basicConfig` ni añadir handlers por su cuenta.
"""

import logging
from logging.handlers import RotatingFileHandler

from core.paths import ProjectPaths

# Formato compartido: timestamp | nivel | módulo | mensaje
_CONSOLE_FMT = "%(levelname)-7s | %(name)s | %(message)s"
_FILE_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

# Rotación del archivo de log: 2 MB por archivo, 3 backups (~8 MB máx).
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


def setup_logging(console_level=logging.INFO):
    """
    Configura el logger raíz con un handler de consola y otro de archivo
    rotativo en `logs/panopticon.log`. Idempotente: llamadas repetidas no
    duplican handlers.

    Args:
        console_level: nivel mínimo que se muestra en consola (el archivo
                       siempre captura desde DEBUG).
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # el filtrado real lo hacen los handlers

    # --- Consola ---
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT))
    root.addHandler(console)

    # --- Archivo rotativo (best-effort: si falla, no tumba la app) ---
    try:
        logs_dir = ProjectPaths.root() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            logs_dir / "panopticon.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FILE_FMT))
        root.addHandler(file_handler)
    except Exception as e:
        # El logging a archivo es un extra; la consola ya quedó activa.
        root.warning("No se pudo crear el handler de archivo de log: %s", e)

    _configured = True
