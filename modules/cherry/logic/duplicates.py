"""
Proyectos que probablemente son el mismo artista — fase 1 (A5).

Dos señales, las mismas que usa el original y calculadas con **sus** helpers:

  - **Misma fuente**: dos proyectos con el mismo `site` + `artist_id`. No es un
    parecido, es una certeza: son literalmente el mismo artista del mismo sitio
    dado de alta dos veces. Pasa al añadir la URL con otra forma (`/c/foo` vs
    `/foo`), que el comparador de texto de `library_check` no pilla y este sí,
    porque compara el `artist_id` que resolvió el template contra la API.
  - **Nombre parecido**: similitud ≥ 0.60 sobre el nombre normalizado. Esto sí
    es una sospecha, y por eso se separa de lo anterior.

**Aquí no se fusiona nada.** Fusionar mueve archivos entre carpetas de disco,
resuelve huérfanos y compacta la numeración del destino; es la parte destructiva
y va en su propia sesión (decisión de David, 2026-09-03). Lo que sí se puede
hacer es lo contrario: marcar un par como "no son el mismo" con `add_exclusion`,
que es información que el motor ya guarda y que se respeta después.

Coste: la comparación es de todos contra todos —46 perfiles son 1.035 pares—
pero sobre datos ya en memoria. Lo caro es traerlos: `get_profile` por perfil,
porque `list_profiles` no devuelve las URLs. Va en el runtime, no en el hilo GUI.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Los mismos umbrales que la vista del original, para que un par que allí sale
# como "probable" no salga aquí como "posible".
NAME_PROBABLE = 0.80
NAME_POSSIBLE = 0.60

TIER_SOURCE   = "source"    # mismo site + artist_id: certeza
TIER_PROBABLE = "probable"  # nombre ≥ 0.80
TIER_POSSIBLE = "possible"  # nombre ≥ 0.60


async def find_pairs() -> list[dict]:
    """Pares sospechosos, los más seguros primero.

    Cada par: `{id_a, name_a, folder_a, id_b, name_b, folder_b, tier, reason}`.
    Los pares que el usuario ya marcó como distintos no salen.
    """
    from third_party.cherry_dl.config import INDEX_DB

    if not INDEX_DB.is_file():
        return []

    from third_party.cherry_dl.dedup import name_similarity, url_overlap
    from third_party.cherry_dl.index import get_exclusions, get_profile, list_profiles

    try:
        excluded = await get_exclusions(INDEX_DB)
        slim = await list_profiles(INDEX_DB)
        full = []
        for row in slim:
            profile = await get_profile(INDEX_DB, row["id"])
            if profile:
                full.append(profile)
    except Exception:
        log.exception("cherry: no se pudieron leer los perfiles para buscar duplicados")
        return []

    pairs: list[dict] = []
    for i, a in enumerate(full):
        for b in full[i + 1:]:
            lo, hi = min(a["id"], b["id"]), max(a["id"], b["id"])
            if (lo, hi) in excluded:
                continue

            base = {
                "id_a": a["id"], "name_a": a["display_name"], "folder_a": a["folder_path"],
                "id_b": b["id"], "name_b": b["display_name"], "folder_b": b["folder_path"],
            }

            match = url_overlap(a["urls"], b["urls"])
            if match:
                pairs.append({**base, "tier": TIER_SOURCE, "reason": match})
                continue

            sim = name_similarity(a["display_name"], b["display_name"])
            if sim >= NAME_POSSIBLE:
                pairs.append({
                    **base,
                    "tier": TIER_PROBABLE if sim >= NAME_PROBABLE else TIER_POSSIBLE,
                    "reason": f"{sim * 100:.0f}%",
                    "similarity": sim,
                })

    order = {TIER_SOURCE: 0, TIER_PROBABLE: 1, TIER_POSSIBLE: 2}
    pairs.sort(key=lambda p: (order[p["tier"]], -p.get("similarity", 1.0)))
    log.info("cherry: %s pares sospechosos sobre %s perfiles", len(pairs), len(full))
    return pairs


async def exclude(id_a: int, id_b: int) -> None:
    """Marca dos proyectos como distintos: el par no vuelve a salir.

    El motor guarda el par en orden canónico, así que da igual cómo se pase.
    """
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import add_exclusion

    await add_exclusion(INDEX_DB, id_a, id_b)
    log.info("cherry: %s y %s marcados como distintos", id_a, id_b)


async def excluded_count() -> int:
    """Cuántos pares hay ya marcados como distintos."""
    from third_party.cherry_dl.config import INDEX_DB

    if not INDEX_DB.is_file():
        return 0
    from third_party.cherry_dl.index import get_exclusions

    try:
        return len(await get_exclusions(INDEX_DB))
    except Exception:
        log.exception("cherry: no se pudieron leer las exclusiones")
        return 0
