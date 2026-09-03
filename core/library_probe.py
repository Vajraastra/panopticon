"""
LibraryProbe — lector read-only de `panopticon.db` para consultas de contexto.

Existe para responder una pregunta desde fuera del Librarian: *¿esto ya lo
tengo?* La primera en necesitarla es la alta de proyectos de Cherry-DL, que
antes de bajar 9.000 archivos de un artista debería poder decir que ya hay
9.000 en la biblioteca.

**Por qué no se usa `DatabaseManager` directamente:** vive en
`modules/librarian/`, y la regla 1 del proyecto prohíbe que un módulo importe a
otro (`CLAUDE.md`). Ya hay cinco incumplimientos anotados en `TASKS.md` y este
habría sido el sexto. Lo que la regla manda hacer es exactamente esto: lo que
dos módulos necesitan se sube a `core/`. El día que `DatabaseManager` se mueva
a `core/` con inyección, este archivo se puede reemplazar por él sin tocar
ningún módulo.

**Contrato, estrecho a propósito:**
  - NUNCA escribe. La conexión se abre en modo `ro` de SQLite, así que ni un
    bug puede crear el archivo ni modificar una fila. Eso también evita el
    accidente clásico: `sqlite3.connect()` sobre una ruta inexistente crea una
    base vacía, y aquí eso significaría una `panopticon.db` fantasma.
  - NUNCA lanza. Un fallo devuelve el valor vacío. Esto alimenta avisos
    informativos: que la biblioteca no esté disponible no puede impedir dar de
    alta un proyecto.
  - Solo agregados y muestras. No es una API de consulta general; para eso
    está el Librarian.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from core.paths import ProjectPaths

log = logging.getLogger(__name__)

# Se abre y se cierra en cada consulta: son llamadas sueltas desde diálogos,
# no un bucle. Mantener una conexión viva solo añadiría un handle más sobre un
# archivo que ya tienen abierto media docena de módulos.
_TIMEOUT = 5.0


def db_path() -> Path:
    """La misma ruta que usa `DatabaseManager`: raíz del proyecto, no el CWD."""
    return ProjectPaths.root() / "panopticon.db"


def is_available() -> bool:
    """Barato y síncrono. False en una instalación que nunca indexó nada."""
    return db_path().is_file()


def _connect() -> sqlite3.Connection | None:
    path = db_path()
    if not path.is_file():
        return None
    try:
        # `mode=ro` exige que el archivo exista y prohíbe toda escritura.
        return sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=_TIMEOUT)
    except sqlite3.Error as exc:
        log.warning("library_probe: no se pudo abrir la biblioteca: %s", exc)
        return None


def normalize(path: str | Path) -> str:
    r"""Rutas en el formato en que las guarda el Librarian (barras hacia
    delante). Sin esto, `G:\images` y `G:/images` no se parecen en nada."""
    return os.path.normpath(str(path)).replace("\\", "/")


def watched_folders() -> list[str]:
    """Las carpetas que el Librarian vigila, normalizadas."""
    con = _connect()
    if con is None:
        return []
    try:
        rows = con.execute("SELECT path FROM watched_folders").fetchall()
        return [normalize(r[0]) for r in rows]
    except sqlite3.Error as exc:
        log.warning("library_probe: no se pudieron leer las carpetas: %s", exc)
        return []
    finally:
        con.close()


def is_watched(folder: str | Path) -> bool:
    """True si `folder` es una carpeta vigilada o cuelga de una.

    Quien avisa de un cambio en disco lo pregunta antes de publicarlo: el
    Librarian re-indexa lo que le manden sin comprobar si le pertenece, así
    que publicar una carpeta de fuera metería sus archivos en la biblioteca
    sin que nadie la haya dado de alta.
    """
    target = normalize(folder).rstrip("/")
    if not target:
        return False
    low = target.lower()
    for watched in watched_folders():
        w = watched.rstrip("/").lower()
        if low == w or low.startswith(w + "/"):
            return True
    return False


def artist_summary(artist: str, sample_folders: int = 3) -> dict:
    """Qué hay ya en la biblioteca de ese artista.

    Devuelve `{"files": n, "sites": [(sitio, n), …], "folders": [ruta, …]}`.
    Con `files == 0` el resto viene vacío.

    `files.artist` no tiene índice y la tabla ronda las 294K filas, así que
    esto es un recorrido completo de ~60 ms. Es aceptable para un diálogo que
    lo pregunta una vez, y NO lo es para llamarlo por cada tecla escrita.
    """
    empty = {"files": 0, "sites": [], "folders": []}
    name = (artist or "").strip()
    if not name:
        return empty
    con = _connect()
    if con is None:
        return empty
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM files WHERE artist = ?", (name,)).fetchone()[0]
        if not total:
            return empty
        sites = con.execute(
            "SELECT origin_site, COUNT(*) FROM files WHERE artist = ? "
            "GROUP BY origin_site ORDER BY 2 DESC", (name,)).fetchall()
        # Las carpetas se sacan de una muestra, no de un DISTINCT: un artista
        # con 40K archivos vive en un puñado de carpetas y ordenarlas todas
        # costaría más que la cuenta entera.
        folders: list[str] = []
        rows = con.execute(
            "SELECT path FROM files WHERE artist = ? LIMIT 500", (name,)).fetchall()
        for (p,) in rows:
            parent = normalize(Path(p).parent)
            if parent not in folders:
                folders.append(parent)
                if len(folders) >= sample_folders:
                    break
        return {
            "files": total,
            "sites": [(s or "?", n) for s, n in sites],
            "folders": folders,
        }
    except sqlite3.Error as exc:
        log.warning("library_probe: consulta por artista falló: %s", exc)
        return empty
    finally:
        con.close()


def known_artists() -> list[str]:
    """Artistas con al menos un archivo en la biblioteca, ordenados."""
    con = _connect()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT DISTINCT artist FROM files "
            "WHERE artist IS NOT NULL AND artist <> '' ORDER BY artist").fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error as exc:
        log.warning("library_probe: no se pudieron leer los artistas: %s", exc)
        return []
    finally:
        con.close()
