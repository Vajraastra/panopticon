"""
CatalogReader — Lector read-only del catalog.db de cherry-dl.

Responsabilidad única: dado el path de una carpeta de artista descargada
por cherry-dl, devolver la lista de imágenes que realmente existen en disco.

Reglas de oro:
  - NUNCA escribe en catalog.db ni en ningún archivo de cherry-dl.
  - El indexador usa read_image_files: errores y cancelación tienen estado
    explícito y jamás se confunden con un catálogo vacío válido.
  - La fuente de verdad es el filesystem, no la DB.
"""

import sqlite3
import logging
import stat
from contextlib import closing
from pathlib import Path
from core.inventory import InventoryResult, InventoryStatus, directory_identity

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
    try:
        return stat.S_ISREG((Path(folder) / _CATALOG_FILENAME).stat().st_mode)
    except FileNotFoundError:
        return False


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

    Los campos 'cherry_*' los ingesta el IndexerWorker (M2 de
    CHERRY_FUSION_DESIGN): cherry_hash → files.hash_original y
    cherry_url → files.origin_url en panopticon.db.
    """
    # Compatibilidad para consumidores de listas. Nunca usar para decidir purgas.
    return read_image_files(folder).files


def read_image_files(folder: str | Path, cancelled=None) -> InventoryResult:
    """Lee sólo en modo ro; conserva archivos válidos y marca inventarios parciales."""
    folder = Path(folder).absolute()
    catalog_path = folder / _CATALOG_FILENAME
    result = InventoryResult()
    cancelled = cancelled or (lambda: False)
    try:
        result.root_identity = directory_identity(folder)
    except OSError as exc:
        result.fail(InventoryStatus.UNAVAILABLE, exc)
        return result

    try:
        uri = catalog_path.as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(cancelled()), 1000)
            rows = conn.execute(
                "SELECT hash, filename, url_source, date_added, file_size FROM files"
            )
            for row in rows:
                if cancelled():
                    result.fail(InventoryStatus.CANCELLED)
                    break
                filename = row['filename']
                if not filename or Path(filename).suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue
                relative = Path(filename)
                if relative.is_absolute() or relative.drive or '..' in relative.parts:
                    result.fail(InventoryStatus.ERROR, f"Ruta fuera del catálogo: {filename}")
                    continue
                full_path = folder / filename
                try:
                    info = full_path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    result.files.append({
                        'path': str(full_path), 'filename': filename,
                        'size': row['file_size'] or info.st_size,
                        'created': float(row['date_added'] or 0),
                        'cherry_url': row['url_source'], 'cherry_hash': row['hash'],
                    })
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError) as exc:
                    result.fail(InventoryStatus.ERROR, exc)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        result.fail(InventoryStatus.ERROR, exc)
        log.warning("[CatalogReader] No se pudo leer %s: %s", catalog_path, exc)

    if cancelled():
        result.fail(InventoryStatus.CANCELLED)
    elif result.complete:
        try:
            if directory_identity(folder) != result.root_identity:
                result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió durante el recorrido")
        except OSError as exc:
            result.fail(InventoryStatus.UNAVAILABLE, exc)
    return result


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
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT display_name, primary_site, last_checked "
                "FROM profiles WHERE folder_path = ?",
                (folder_norm,)
            ).fetchone()

        if row:
            return {
                'display_name': row['display_name'],
                'primary_site': row['primary_site'],
                'last_checked': row['last_checked'],
            }

    except (sqlite3.Error, OSError) as e:
        log.debug("[CatalogReader] index.db no accesible: %s", e)

    return None
