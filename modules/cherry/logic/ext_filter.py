"""
Filtro de tipos de archivo por perfil — envoltorio del que ya trae el motor.

El motor define los grupos (`EXT_GROUPS`), el formato serializado
(`{"groups": [...], "custom": "psd,clip"}`) y el predicado que decide si un
archivo pasa. Aquí NO se redefine nada de eso: si upstream añade `.jxl` a
"Imágenes" o inventa un grupo, esta vista lo hereda sin tocar una línea. Lo
único propio es la traducción de los nombres de grupo, porque el motor los trae
en español fijo y Panopticon tiene `locales/`.

**Escribe `index.db`**, y por eso vive junto a `profile_writes.py`: es la misma
excepción acordada (David, 2026-09-02), extendida al filtro por la misma razón
—elegir qué tipos bajar es parte de dar de alta un proyecto— y con el mismo límite: se
llama a `index.update_profile_ext_filter`, la API de alto nivel del motor. Cero
SQL propio, cero cambios de schema.

Detalle que importa: el filtro se aplica al **encolar la descarga**, no al
escanear. Cambiarlo nunca invalida el `last_synced` de una fuente, así que
apretar "Comprimidos" después de haber bajado solo imágenes no re-recorre el
historial: los .zip que el escaneo ya vio siguen en `pending_queue` y salen en
la siguiente sync. Ese es el motivo de que el filtro sea del perfil y no un
argumento de la descarga.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Clave de traducción por grupo del motor. Un grupo que upstream añada y no esté
# aquí se muestra con su etiqueta original en español: se degrada, no se pierde.
GROUP_KEYS: dict[str, tuple[str, str]] = {
    "images":  ("cherry.ext_images",  "Images"),
    "anim":    ("cherry.ext_anim",    "Animations"),
    "video":   ("cherry.ext_video",   "Video"),
    "audio":   ("cherry.ext_audio",   "Audio"),
    "zip":     ("cherry.ext_zip",     "Archives"),
    "docs":    ("cherry.ext_docs",    "Documents"),
    "project": ("cherry.ext_project", "Project files"),
}


def groups() -> list[tuple[str, str, list[str]]]:
    """Los grupos del motor como `(id, etiqueta_original, extensiones)`.

    Las extensiones vienen SIN punto (así las guarda el motor) y ordenadas, que
    es lo que quiere un tooltip: el set de `EXT_GROUPS` no tiene orden estable.
    """
    from third_party.cherry_dl.downloads import EXT_GROUPS
    return [
        (gid, label, sorted(exts))
        for gid, (label, exts) in EXT_GROUPS.items()
    ]


def encode(group_ids: list[str], custom: str) -> str:
    """Selección → el JSON que guarda el motor."""
    from third_party.cherry_dl.downloads import _encode_profile_filter
    return _encode_profile_filter(group_ids, custom)


def decode(stored: str | None) -> tuple[set[str], str]:
    """Lo guardado → `(ids de grupo, texto del campo extra)`.

    Es lo que necesita la UI para repintar las casillas, y NO es lo mismo que
    `effective()`: el motor solo sabe deshacer el JSON hasta el set final de
    extensiones, que ya no distingue "el usuario marcó Imágenes" de "el usuario
    escribió jpg,png a mano". Por eso el campo extra se lee del JSON crudo.

    Un valor legacy (extensiones sueltas separadas por coma, de antes de que el
    motor guardara JSON) se devuelve entero como texto extra: se conserva tal
    cual en vez de intentar adivinar a qué grupos pertenecía.
    """
    import json

    if not stored:
        return set(), ""
    if stored.startswith("{"):
        try:
            data = json.loads(stored)
            return set(data.get("groups", [])), str(data.get("custom", "") or "")
        except Exception:
            log.warning("cherry: ext_filter ilegible, se trata como legacy")
    return set(), stored


def effective(stored: str | None) -> set[str]:
    """Lo guardado → el set de extensiones CON punto que espera la descarga.

    Vacío significa "todo", no "nada": así lo interpreta `_passes_ext_filter`.
    """
    from third_party.cherry_dl.downloads import _decode_profile_filter
    _groups, exts = _decode_profile_filter(stored or "")
    return exts


def parse_custom(raw: str) -> set[str]:
    """'jpg, .png; zip' → {'.jpg', '.png', '.zip'}. Para previsualizar el campo
    extra sin pasar por la BD."""
    from third_party.cherry_dl.downloads import _parse_ext_filter
    return _parse_ext_filter(raw.replace(";", ","))


async def save(profile_id: int, group_ids: list[str], custom: str) -> str:
    """Persiste el filtro del perfil. Devuelve lo que quedó guardado.

    `update_profile_ext_filter` además re-escribe el `.cherry-meta.json` de la
    carpeta del perfil (`sync_profile_meta`), que es el respaldo con el que
    `cherry-dl reindex` reconstruye el índice en otra máquina. Si la carpeta no
    existe todavía, el motor lo omite en silencio: por eso guardar el filtro de
    un perfil recién creado no falla.
    """
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import update_profile_ext_filter

    encoded = encode(group_ids, custom)
    await update_profile_ext_filter(INDEX_DB, profile_id, encoded)
    log.info("cherry: filtro del perfil %s → %s", profile_id, encoded)
    return encoded
