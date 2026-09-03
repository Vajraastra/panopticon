"""
Escrituras en `index.db`: crear perfiles y añadir fuentes.

**Excepción deliberada a la regla del proyecto** (David, 2026-09-02). La regla
dice que Panopticon nunca escribe los archivos de cherry-dl; la excepción es que
dar de alta un proyecto y añadirle URLs *es* la operación central del programa,
y un wrapper que no la tiene obliga a abrir la GUI clásica para lo más básico.

Lo que la regla protege sigue intacto: **cero SQL propio y cero cambios de
schema**. Todo pasa por la API de alto nivel del motor —la misma que llama su
wizard—, que es la puerta por la que el motor ya escribe su índice y su catálogo
en cada descarga lanzada desde aquí. Si el schema cambia upstream, esto sigue
funcionando; ese es justamente el punto.

Async de arriba a abajo: se ejecuta en el loop de `CherryRuntime`.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def site_of(url: str) -> str:
    """Sitio que reconoce la URL, o 'unknown'. Local y sin red."""
    from third_party.cherry_dl.profiles import _site_from_url
    return _site_from_url(url)


def folder_preview(display_name: str) -> Path:
    """Carpeta destino que `create_profile` va a usar para ese nombre.

    Estructura plana `{download_dir}/{nombre}` — la misma que aplican `organize`
    y `reindex`. Se calcula igual que el motor para poder enseñarla ANTES de
    crear nada.
    """
    from third_party.cherry_dl.config import load_config
    from third_party.cherry_dl.profiles import _safe_dirname
    return load_config().download_path / _safe_dirname(display_name)


async def resolve_name(url: str) -> str:
    """Nombre real del artista según la API del sitio (TOCA LA RED).

    Puede lanzar `ValueError` si ningún template reconoce la URL, o
    `NeedsManualAuth`/`NeedsPixivAuth` si el sitio exige sesión. La vista lo
    trata como una comodidad: si falla, el nombre se escribe a mano y el perfil
    se crea igual.
    """
    from third_party.cherry_dl.config import load_config
    from third_party.cherry_dl.engine import DownloadEngine
    from third_party.cherry_dl.profiles import resolve_artist_name

    config = load_config()
    async with DownloadEngine(config) as engine:
        return await resolve_artist_name(engine, url)


async def create_profile(display_name: str, url: str) -> int:
    """Crea el perfil con su primera fuente. Devuelve el id nuevo."""
    from third_party.cherry_dl.config import INDEX_DB, load_config
    from third_party.cherry_dl.index import init_index
    from third_party.cherry_dl.profiles import create_profile as _create

    # Aquí sí se llama a `init_index`: es la única operación que puede
    # encontrarse un índice inexistente (el primer perfil de una instalación
    # nueva), y crear las tablas es parte de darlo de alta. Es idempotente.
    await init_index(INDEX_DB)
    profile_id = await _create(
        db_path=INDEX_DB,
        display_name=display_name,
        primary_url=url,
        base_dir=load_config().download_path,
    )
    log.info("cherry: perfil %s creado (%s)", profile_id, display_name)
    return profile_id


async def add_url(profile_id: int, url: str) -> int:
    """Añade una fuente a un perfil existente. Devuelve el id de la entrada."""
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.profiles import add_url_to_profile

    url_id = await add_url_to_profile(db_path=INDEX_DB, profile_id=profile_id, url=url)
    log.info("cherry: url %s añadida al perfil %s", url_id, profile_id)
    return url_id


async def set_url_enabled(url_id: int, enabled: bool) -> None:
    """Activa o desactiva una fuente.

    Desactivar es lo más cerca de "quitar" que ofrece el motor, y a propósito:
    la entrada guarda `last_synced` y `file_count`, así que borrarla perdería la
    frontera incremental y el siguiente escaneo recorrería el historial entero.
    """
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import set_profile_url_enabled

    await set_profile_url_enabled(INDEX_DB, url_id, enabled)


async def delete_profile(profile_id: int) -> None:
    """Borra un proyecto del índice: sus fuentes (CASCADE) y su artista.

    **No toca el disco.** La carpeta y su `catalog.db` se quedan enteros, así
    que esto no destruye ninguna descarga; lo que se pierde es el `last_synced`
    de cada fuente, es decir, la frontera incremental. Volver a darlo de alta
    funciona, pero su primer escaneo recorrerá el historial completo otra vez.

    Es también la razón de que desactivar una fuente y borrar el proyecto sean
    cosas distintas: para dejar de bajar algo, se desactiva.
    """
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import delete_profile as _delete

    await _delete(INDEX_DB, profile_id)
    log.info("cherry: perfil %s borrado del índice", profile_id)
