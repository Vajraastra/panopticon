"""
Purga de la cola de pendientes que apunta a un host que ya no responde.

## El caso que la motiva

Los proyectos que empezaron en Kemono conservan en su `pending_queue` las URLs
de `kemono.cr`, aunque su fuente viva de hoy ya sea Patreon. Medido el
2026-09-05 sobre la biblioteca real: **98.295 de 98.307 filas de cola** son de
ese host, repartidas por casi todos los proyectos.

Y no se limpian solas. Kemono no devuelve 404 —eso lo purgaría el motor por su
cuenta desde 3b97dc0—: **no conecta**, da timeout de TCP. Un timeout es
`DEFERRABLE`, así que cada fila se reintenta, pasa a la cola diferida y se
vuelve a intentar, gastando un ciclo de conexión completo cada vez. Con casi
cien mil filas eso es el grueso del tiempo de cada sync, empleado en algo que no
puede terminar.

## Qué borra y qué no

Solo filas de `pending_queue`, y solo las cuyo host esté en la lista de muertos.
**Nunca toca un archivo del disco ni el catálogo de lo ya descargado**: lo que
se pierde es la anotación «esto está pendiente», no una descarga. Si el host
revive, un re-scan la repuebla entera — es justo de donde salió.

Como cualquier operación destructiva del módulo, va en dos fases: `plan()` solo
cuenta y `aplicar()` solo borra lo que el plan enseñó. Los números se ven antes.

## Por la puerta del motor

Se borra con `catalog.remove_pending()`, una fila a la vez, que es la API de
alto nivel del motor — cero SQL propio contra sus tablas, como manda la regla
del proyecto. Es más lento que un `DELETE ... WHERE`, y se acepta a sabiendas:
es una operación puntual, y la alternativa es exactamente lo que la regla
prohíbe. El listado sí se lee con `get_pending_files()`, que tampoco es SQL
nuestro.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Hosts que ya no sirven descargas. Kemono ha cambiado de dominio varias veces y
# las colas viejas conservan el de su época, así que están todos.
HOSTS_MUERTOS = ("kemono.cr", "kemono.su", "kemono.party", "kemono.re")


def _host(url: str) -> str:
    """Host de una URL http(s), o cadena vacía.

    Solo http y https a propósito. Las claves de dedup de Patreon son URIs
    canónicas —`patreon://media/179166692`— y partir por `//` a secas les
    sacaría «media» como si fuera un host. Hoy ninguna lista de hosts muertos
    contiene algo así, pero una función que devuelve un host inventado es una
    trampa esperando a que alguien añada el host equivocado a la lista.
    """
    bajo = (url or "").lower()
    for esquema in ("http://", "https://"):
        if bajo.startswith(esquema):
            return bajo[len(esquema):].split("/", 1)[0]
    return ""


@dataclass
class ProyectoPurga:
    """Lo que se va a purgar de un proyecto."""
    profile_id: int
    nombre: str
    folder: Path
    total: int = 0                       # filas en su cola
    claves: list[str] = field(default_factory=list)   # url_source a borrar

    @property
    def a_purgar(self) -> int:
        return len(self.claves)

    @property
    def quedan(self) -> int:
        return self.total - self.a_purgar


@dataclass
class Plan:
    proyectos: list[ProyectoPurga]

    @property
    def total(self) -> int:
        return sum(p.total for p in self.proyectos)

    @property
    def a_purgar(self) -> int:
        return sum(p.a_purgar for p in self.proyectos)

    @property
    def afectados(self) -> list[ProyectoPurga]:
        return [p for p in self.proyectos if p.a_purgar]


async def plan(
    hosts: tuple[str, ...] = HOSTS_MUERTOS,
    db_path=None,
) -> Plan:
    """Qué filas de cola apuntan a un host muerto. **No escribe nada.**

    Mira tanto `download_url` como `url_source`: la primera es la que se pediría
    y la segunda la clave de dedup, y en las colas viejas no siempre coinciden.
    """
    from third_party.cherry_dl.catalog import get_pending_files
    from third_party.cherry_dl.index import get_profile, list_profiles

    from .profiles import index_db_path

    db = db_path or index_db_path()
    salida: list[ProyectoPurga] = []

    for p in await list_profiles(db):
        detalle = await get_profile(db, p["id"])
        if not detalle:
            continue
        folder = Path(detalle["folder_path"])
        if not (folder / "catalog.db").exists():
            continue

        entrada = ProyectoPurga(
            profile_id=p["id"],
            nombre=detalle.get("display_name", ""),
            folder=folder,
        )
        for fila in await get_pending_files(folder):
            entrada.total += 1
            candidatas = (fila.get("download_url") or "", fila.get("url_source") or "")
            if any(_host(u) in hosts for u in candidatas if u):
                entrada.claves.append(fila["url_source"])
        salida.append(entrada)

    plan_ = Plan(salida)
    log.info(
        "cherry/purge: %s filas de cola, %s en hosts muertos",
        plan_.total, plan_.a_purgar,
    )
    return plan_


async def aplicar(
    plan_: Plan,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> int:
    """Borra las filas que el plan marcó. Devuelve cuántas se fueron.

    `on_progress(nombre_proyecto, hechas, total)` se llama durante el borrado:
    con decenas de miles de filas esto tarda, y una barra parada asusta más que
    el borrado.
    """
    from third_party.cherry_dl.catalog import remove_pending

    hechas = 0
    for proyecto in plan_.afectados:
        for i, clave in enumerate(proyecto.claves, 1):
            await remove_pending(proyecto.folder, clave)
            hechas += 1
            if on_progress and (i % 250 == 0 or i == proyecto.a_purgar):
                on_progress(proyecto.nombre, i, proyecto.a_purgar)
        log.info(
            "cherry/purge: %s — %s filas fuera de la cola",
            proyecto.nombre, proyecto.a_purgar,
        )
    return hechas
