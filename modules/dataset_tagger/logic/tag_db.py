"""
Resolución, descarga y carga de las bases de datos de tags (CSV) que alimentan
el autocompletado del editor.

- Las rutas viven bajo `models/tags/` (gitignored, como el resto de modelos).
- La descarga reusa `core.ai.model_downloader.download_file` (atómica, errores
  diferenciados). Las URLs/nombres salen de los presets (`CaptionTemplate`).
- La carga al índice en memoria la hace `tag_engine.load_csv`.

`TagDbWorker` (QThread) hace descarga+carga fuera del hilo GUI (Regla de Oro #4);
la descarga del CSV (~6 MB) y el parseo no deben congelar la UI.
"""
import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.paths import CachePaths
from core.ai import model_downloader
from . import tag_engine

log = logging.getLogger(__name__)


def csv_dir() -> Path:
    """Carpeta de los CSV de tags: <models>/tags/ (se crea si falta)."""
    d = CachePaths.get_models_root() / "tags"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_path(csv_name: str) -> Path:
    return csv_dir() / f"{csv_name}.csv"


def is_present(csv_name: str) -> bool:
    return bool(csv_name) and local_path(csv_name).exists()


def is_ready(csv_name: str) -> bool:
    """True si el CSV ya está cargado en el índice en memoria."""
    return bool(csv_name) and tag_engine.is_loaded(csv_name)


def tag_count(csv_name: str) -> int:
    """Nº de tags cargados para ese CSV (0 si no está en memoria)."""
    return tag_engine.tag_count(csv_name)


def ensure_downloaded(csv_name: str, url: str) -> Path:
    """Descarga el CSV si no está en disco. Devuelve la ruta local.
    Bloqueante: el caller debe ejecutarlo en un hilo. Propaga RuntimeError."""
    dest = local_path(csv_name)
    if not dest.exists():
        if not url:
            raise RuntimeError(f"No hay URL de descarga para el CSV '{csv_name}'.")
        model_downloader.download_file(url, dest)
    return dest


def ensure_loaded(csv_name: str, url: str) -> int:
    """Garantiza CSV en disco + cargado en el índice. Devuelve nº de tags.
    Bloqueante: ejecutar en hilo. No recarga si ya está en memoria."""
    if not csv_name:
        return 0
    if tag_engine.is_loaded(csv_name):
        return tag_engine.tag_count(csv_name)
    path = ensure_downloaded(csv_name, url)
    return tag_engine.load_csv(csv_name, path)


class TagDbWorker(QThread):
    """Descarga (si falta) y carga un CSV de tags en segundo plano."""
    ready = Signal(str, int)     # csv_name, nº de tags cargados
    failed = Signal(str, str)    # csv_name, mensaje de error

    def __init__(self, csv_name, url, parent=None):
        super().__init__(parent)
        self.csv_name = csv_name
        self.url = url

    def run(self):
        try:
            n = ensure_loaded(self.csv_name, self.url)
            self.ready.emit(self.csv_name, n)
        except Exception as e:  # noqa: BLE001 — superficie de error para la UI
            log.error("Fallo cargando CSV de tags '%s': %s", self.csv_name, e)
            self.failed.emit(self.csv_name, str(e))
