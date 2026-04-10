"""
CatalogReader — Lector read-only del catalog.db de cherry-dl.

Responsabilidad única: dado el path de una carpeta de artista descargada
por cherry-dl, devolver la lista de imágenes que realmente existen en disco.

Reglas de oro:
  - NUNCA escribe en catalog.db ni en ningún archivo de cherry-dl.
  - NUNCA lanza excepciones hacia afuera: todos los errores son silenciosos
    y devuelven valores vacíos/None para no interrumpir el indexador.
  - La fuente de verdad es el filesystem, no la DB.
"""

import sqlite3
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Extensiones que Panopticon considera imágenes.
# Los demás archivos de cherry-dl (.psd, .zip, .mp4, etc.) se ignoran.
_IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.avif'})

# Nombre canónico del catálogo de cherry-dl.
_CATALOG_FILENAME = 'catalog.db'

# Ruta del índice central de cherry-dl.
_INDEX_PATH = Path.home() / '.cherry-dl' / 'index.db'


def is_cherry_catalog(folder: str | Path) -> bool:
    """Devuelve True si la carpeta contiene un catalog.db de cherry-dl."""
    return (Path(folder) / _CATALOG_FILENAME).is_file()


def get_image_files(folder: str | Path) -> list[dict]:
    """
    Lee catalog.db y devuelve solo las imágenes que existen en disco.

    Cada elemento de la lista tiene el mismo formato que espera
    IndexerWorker / DatabaseManager.register_files_minimal:
        {
            'path':     str   — path absoluto normalizado,
            'filename': str   — nombre de archivo,
            'size':     int   — tamaño en bytes,
            'created':  float — Unix timestamp (date_added de cherry-dl),
            'cherry_url':  str | None — URL de origen (informativo),
            'cherry_hash': str | None — SHA-256 de cherry-dl (informativo),
        }

    Los campos 'cherry_*' son extras: register_files_minimal los ignora,
    pero están disponibles para uso futuro (dedup por hash, mostrar origen).
    """
    folder = Path(folder)
    catalog_path = folder / _CATALOG_FILENAME

    if not catalog_path.is_file():
        return []

    results = []
    try:
        # URI read-only: nunca abre en modo escritura aunque haya un lock.
        uri = catalog_path.as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT hash, filename, url_source, date_added, file_size FROM files"
        ).fetchall()
        conn.close()

    except sqlite3.OperationalError as e:
        log.warning("[CatalogReader] No se pudo leer %s: %s", catalog_path, e)
        return []

    for row in rows:
        filename = row['filename']
        if not filename:
            continue

        # Filtrar por extensión de imagen antes de tocar el disco.
        ext = Path(filename).suffix.lower()
        if ext not in _IMAGE_EXTENSIONS:
            continue

        full_path = folder / filename

        # El filesystem es la fuente de verdad.
        # Entradas de archivos borrados intencionalmente → se ignoran.
        if not full_path.is_file():
            continue

        # Usar file_size de catalog.db si está disponible; fallback a os.stat.
        size = row['file_size'] or 0
        if not size:
            try:
                size = full_path.stat().st_size
            except OSError:
                size = 0

        results.append({
            'path':        str(full_path),
            'filename':    filename,
            'size':        size,
            'created':     float(row['date_added'] or 0),
            'cherry_url':  row['url_source'],
            'cherry_hash': row['hash'],
        })

    return results


def get_artist_info(folder: str | Path) -> dict | None:
    """
    Busca en el index.db central de cherry-dl (~/.cherry-dl/index.db)
    el perfil de artista correspondiente a esta carpeta.

    Devuelve un dict con:
        {
            'display_name': str,
            'primary_site': str,
            'last_checked': str | None,
        }
    o None si no se encuentra o el índice no existe.
    """
    if not _INDEX_PATH.is_file():
        return None

    folder_norm = str(Path(folder).resolve())

    try:
        uri = _INDEX_PATH.as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row

        # Fase 2+ de cherry-dl usa la tabla 'profiles'.
        row = conn.execute(
            "SELECT display_name, primary_site, last_checked "
            "FROM profiles WHERE folder_path = ?",
            (folder_norm,)
        ).fetchone()
        conn.close()

        if row:
            return {
                'display_name': row['display_name'],
                'primary_site': row['primary_site'],
                'last_checked': row['last_checked'],
            }

    except sqlite3.OperationalError as e:
        log.debug("[CatalogReader] index.db no accesible: %s", e)

    return None
