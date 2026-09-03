"""
"¿Esto ya lo tengo?" — lo que se sabe de una URL o un artista ANTES de darlo
de alta (A4).

Dos preguntas distintas, contra dos bases distintas:

  - **¿Esta URL ya está en algún proyecto?** Se consulta `index.db` por la API
    del motor. Importa porque `add_profile_url` es un `INSERT` a pelo: el motor
    no comprueba duplicados, así que pegar dos veces la misma fuente crea dos
    entradas y el escaneo recorre el mismo historial dos veces sin decir nada.

  - **¿Ya hay archivos de este artista en la biblioteca?** Se consulta
    `panopticon.db` a través de `core.library_probe`. Importa porque un
    proyecto nuevo puede ser el mismo artista que ya se bajó por otra vía —
    por eso hay 96.614 filas con `artist` puesto.

Todo es de solo lectura y todo devuelve un valor vacío si algo falla: son
avisos, y ninguno puede impedir dar de alta un proyecto. Async porque la
lectura del índice lo es; la de la biblioteca va a un hilo porque `files.artist`
no tiene índice y recorrer 294K filas en el loop pararía cualquier descarga que
estuviera corriendo en él.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def _key(url: str) -> str:
    """URL comparable: sin espacios, sin barra final, en minúsculas.

    Deliberadamente tosco. No intenta saber que `patreon.com/c/foo` y
    `patreon.com/foo` son el mismo artista: para eso está el `artist_id` que
    resuelve el template contra la API, y esto corre antes de tocar la red.
    Pillar el copiar-pegar literal, que es el caso real, ya basta.
    """
    return (url or "").strip().rstrip("/").lower()


async def find_url(url: str) -> list[dict]:
    """Proyectos que ya tienen esa URL: `[{profile_id, display_name, enabled}]`.

    Lista vacía si no está, si el índice no existe o si la lectura falla.
    """
    target = _key(url)
    if not target:
        return []

    from third_party.cherry_dl.config import INDEX_DB

    if not INDEX_DB.is_file():
        return []  # instalación nueva: nada que comprobar, y NO crear el archivo

    from third_party.cherry_dl.profiles import get_all_profiles

    try:
        profiles = await get_all_profiles(INDEX_DB)
    except Exception:
        log.exception("cherry: no se pudo revisar el índice en busca de la URL")
        return []

    hits = []
    for prof in profiles:
        for entry in prof.urls:
            if entry.url and _key(entry.url) == target:
                hits.append({
                    "profile_id": prof.id,
                    "display_name": prof.display_name,
                    "enabled": bool(entry.enabled),
                })
    return hits


async def find_artist(name: str) -> dict:
    """Qué hay ya en `panopticon.db` de ese artista.

    `{"files": n, "sites": [(sitio, n), …], "folders": [ruta, …]}`; `files` a 0
    si no hay nada o la biblioteca no está disponible.
    """
    clean = (name or "").strip()
    if not clean:
        return {"files": 0, "sites": [], "folders": []}

    from core import library_probe

    return await asyncio.to_thread(library_probe.artist_summary, clean)
