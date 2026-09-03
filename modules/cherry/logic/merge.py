"""
Fusionar dos proyectos en uno — fase 2 de A5.

Es la parte destructiva del módulo y la última que quedaba en la GUI clásica:
**mueve archivos entre carpetas de disco y no tiene deshacer**. Todo el trabajo
real lo hacen funciones del motor; aquí solo se orquestan, en un orden distinto
al suyo y por un motivo de seguridad que se explica abajo.

Piezas del motor, en el orden en que se usan:

  - `dedup.dup_keep_remove`        — quién absorbe a quién: el proyecto más antiguo.
  - `catalog.migrate_unique_files` — mueve a destino lo que destino no tiene.
  - `index.merge_profiles`         — reasigna las URLs y borra el perfil vacío.
  - `dedup.handle_orphans`         — borra o aparta lo que quedó repetido.

## Dos cambios de orden respecto al original, ambos por seguridad

**1. Si mover archivos falla, el índice NO se fusiona.** El original fusiona el
índice pase lo que pase (la TUI incluso puede caer con `UnboundLocalError` al
leer el resultado que la excepción dejó sin asignar). Fusionar el índice tras
una migración fallida es el peor resultado posible: el perfil origen desaparece
del índice y sus archivos quedan en disco sin dueño que los reclame, invisibles
para la app y para el motor. Se prefiere parar con las dos carpetas intactas:
eso se reintenta; lo otro se arregla a mano.

**2. Los huérfanos se resuelven DESPUÉS de fusionar el índice**, no antes. Un
huérfano es un archivo que el destino ya tiene con el mismo hash, así que
borrarlo no pierde nada *si la fusión salió bien*. Si el índice falla y ya se
borraron, la carpeta origen queda con su catálogo apuntando a archivos que ya
no están. Al revés no se pierde nada.

## Por qué esto no compacta la numeración, y el original sí

Al terminar, el original ofrece "compactar" el destino —cerrar los huecos de la
secuencia—, que es puramente cosmético. Aquí no se ofrece, porque el único
momento en que la fusión lo propondría es justo el momento en que no es seguro.
Medido con `scripts/cherry_merge_toytest.py` el 2026-09-03:

El motor usa **dos convenciones de nombre distintas**. Lo que descarga se llama
`prefijo_00042.ext` (`downloads.build_filename`) y lo que migra al fusionar se
llama `0042.ext` (`migrate_unique_files`). La compactación solo reconoce la
primera —`get_numbered_files` filtra por el patrón `_(\\d{5})\\.ext`—, así que
ignora a los recién migrados **y además fija el contador de la carpeta según lo
que sí vio**. Resultado medido: un destino con un hueco que acaba de recibir
`0004.jpg` queda con el contador en 3, y la siguiente fusión hacia esa misma
carpeta pediría el 4, generaría otro `0004.jpg` y **sobrescribiría el anterior**
con un `shutil.move`, sin aviso. Fusionar dos veces contra el mismo destino no
es raro: es lo que pasa al limpiar tres proyectos del mismo artista.

Una descarga normal no colisiona (su nombre lleva prefijo), así que compactar
desde la GUI clásica o la TUI, sin fusión de por medio, sigue siendo válido.

## Lo que este módulo NO hace

No escribe SQL propio ni toca el schema —la regla del proyecto sobre cherry-dl—
y tampoco decide por su cuenta lo que el motor ya decide: quién absorbe a quién
sale de `dup_keep_remove`, no de un criterio nuestro. La convención de nombres
tampoco se parchea aquí: es del motor y se re-mide si cambia upstream.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Qué hacer con los archivos que el destino ya tenía. Son los tres valores que
# entiende `dedup.handle_orphans`; el default es el que no destruye nada.
ORPHAN_IGNORE = "ignore"
ORPHAN_RENAME = "rename"
ORPHAN_DELETE = "delete"


@dataclass
class MergePlan:
    """Lo que va a pasar, calculado ANTES de tocar nada.

    El original pregunta qué hacer con los huérfanos a mitad de la operación,
    cuando los archivos ya se movieron y la respuesta ya no cambia nada que se
    pueda deshacer. Con esto la pregunta se hace antes, con los números
    delante.
    """

    keep_id: int
    remove_id: int
    keep_name: str
    remove_name: str
    keep_folder: Path
    remove_folder: Path

    movable: int = 0       # únicos: se mueven al destino
    duplicated: int = 0    # el destino ya los tiene (serán los huérfanos)
    missing: int = 0       # en el catálogo pero no en disco: se omiten
    untracked: int = 0     # en disco pero fuera del catálogo: NADIE los mueve
    readable: bool = True  # se pudo leer el catálogo de origen

    @property
    def total(self) -> int:
        return self.movable + self.duplicated + self.missing


@dataclass
class MergeResult:
    """Lo que pasó de verdad."""

    plan: MergePlan
    moved: int = 0
    orphaned: int = 0
    purged: int = 0
    orphan_action: str = ORPHAN_IGNORE
    index_merged: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.index_merged and not self.errors

    @property
    def touched_folders(self) -> list[Path]:
        """Carpetas cuyo contenido cambió — las dos, si se movió algo.

        El destino ganó archivos y el origen los perdió; el Librarian tiene que
        enterarse de ambas cosas o se queda con filas que apuntan a rutas
        muertas.
        """
        if not self.moved:
            return []
        return [self.plan.keep_folder, self.plan.remove_folder]


def plan_pair(pair: dict) -> MergePlan:
    """Quién absorbe a quién, sin tocar el disco. `pair` viene de `duplicates`."""
    from third_party.cherry_dl.dedup import dup_keep_remove

    keep_id, remove_id, keep_name, remove_name = dup_keep_remove(pair)
    a_is_keep = pair["id_a"] == keep_id
    return MergePlan(
        keep_id=keep_id,
        remove_id=remove_id,
        keep_name=keep_name,
        remove_name=remove_name,
        keep_folder=Path(pair["folder_a"] if a_is_keep else pair["folder_b"]),
        remove_folder=Path(pair["folder_b"] if a_is_keep else pair["folder_a"]),
    )


async def preview(pair: dict) -> MergePlan:
    """Clasifica los archivos del origen igual que hará la migración.

    Misma regla que `migrate_unique_files` —no está en disco: se omite; el hash
    ya está en destino: se queda como huérfano; el resto se mueve— pero leída
    con `get_all_files` y `get_all_hashes`, que es un set en memoria en vez de
    una consulta por archivo. No escribe nada.
    """
    from third_party.cherry_dl.catalog import get_all_files, get_all_hashes

    plan = plan_pair(pair)
    try:
        src_files = await get_all_files(plan.remove_folder)
        dst_hashes = await get_all_hashes(plan.keep_folder)
    except Exception:
        log.exception("cherry: no se pudo leer el catálogo de %s", plan.remove_folder)
        plan.readable = False
        return plan

    known: set[str] = set()
    for f in src_files:
        name = f.get("filename")
        if name:
            known.add(name)
        if not name or not (plan.remove_folder / name).exists():
            plan.missing += 1
        elif f["hash"] in dst_hashes:
            plan.duplicated += 1
        else:
            plan.movable += 1

    # Lo que hay en la carpeta y el catálogo no menciona: la migración lo ignora
    # por completo, así que se quedaría en el origen sin que nada lo diga.
    try:
        plan.untracked = sum(
            1 for p in plan.remove_folder.iterdir()
            if p.is_file() and p.name not in known and p.suffix != ".db"
            and not p.name.startswith("catalog.db")
        )
    except OSError:
        pass

    log.info(
        "cherry: fusión %s → %s · %s a mover, %s repetidos, %s ausentes, %s sin catalogar",
        plan.remove_name, plan.keep_name,
        plan.movable, plan.duplicated, plan.missing, plan.untracked,
    )
    return plan


async def merge_pair(
    pair: dict,
    orphan_action: str = ORPHAN_IGNORE,
    on_progress: Callable[[int, int], None] | None = None,
) -> MergeResult:
    """Ejecuta la fusión. Destructivo y sin deshacer.

    `on_progress(hechos, total)` se llama desde el loop, así que quien la pase
    debe limitarse a emitir una señal — nada de tocar widgets.
    """
    from third_party.cherry_dl.catalog import migrate_unique_files
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.dedup import handle_orphans
    from third_party.cherry_dl.index import merge_profiles

    plan = plan_pair(pair)
    result = MergeResult(plan=plan, orphan_action=orphan_action)
    log.info("cherry: fusionando %s → %s", plan.remove_name, plan.keep_name)

    # ── 1. Mover los archivos únicos ──────────────────────────────────────
    orphan_paths: list[Path] = []
    try:
        moved = await migrate_unique_files(
            plan.remove_folder, plan.keep_folder, on_progress
        )
        result.moved = moved["moved"]
        result.orphaned = moved["orphaned"]
        result.purged = moved["purged"]
        orphan_paths = list(moved["orphaned_paths"])
        result.errors.extend(moved["errors"][:5])
    except Exception as exc:
        log.exception("cherry: la migración de archivos falló")
        result.errors.append(f"{plan.remove_name}: {type(exc).__name__}: {exc}")

    # Si mover falló, parar aquí: ver el porqué en la cabecera del módulo.
    if result.errors:
        log.error(
            "cherry: fusión abortada tras %s error(es); el índice NO se tocó",
            len(result.errors),
        )
        return result

    # ── 2. Fusionar el índice ─────────────────────────────────────────────
    try:
        await merge_profiles(INDEX_DB, plan.keep_id, plan.remove_id)
        result.index_merged = True
    except Exception as exc:
        log.exception("cherry: la fusión del índice falló")
        result.errors.append(f"índice {plan.remove_name}: {type(exc).__name__}: {exc}")
        return result

    # ── 3. Huérfanos, ya con el índice fusionado ──────────────────────────
    if orphan_paths and orphan_action != ORPHAN_IGNORE:
        try:
            await asyncio.to_thread(
                handle_orphans, plan.remove_folder, orphan_paths, orphan_action
            )
        except Exception as exc:
            log.exception("cherry: no se pudieron gestionar los huérfanos")
            result.errors.append(f"huérfanos: {type(exc).__name__}: {exc}")

    log.info(
        "cherry: fusión terminada · %s movidos, %s repetidos (%s), %s ausentes",
        result.moved, result.orphaned, orphan_action, result.purged,
    )
    return result
