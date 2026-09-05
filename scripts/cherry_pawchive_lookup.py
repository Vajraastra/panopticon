"""
Buscar un creador en el índice de Pawchive, por id o por nombre.

Para cerrar a mano lo que la migración automática no puede: cuando el id de un
proyecto no cuadra con el nombre —o cuando directamente no hay URL que
traducir— hace falta ver qué candidatos existen antes de dar de alta la fuente.

**Cuesta una sola petición**, sea cual sea el número de términos buscados: el
índice de creadores (~12,8 MB, 93.572 creadores) se pide una vez y todas las
búsquedas se resuelven en memoria. Esa petición pasa por el pacer de 1/s.

No escribe nada. Lo que salga de aquí se da de alta desde la app, o con el
`--aplicar` de `cherry_kemono_to_pawchive.py` si es una fuente de Kemono.

Uso:
    venv/Scripts/python.exe scripts/cherry_pawchive_lookup.py Hoovesart RuiDX
    venv/Scripts/python.exe scripts/cherry_pawchive_lookup.py --id 566125 --id 4231621
    venv/Scripts/python.exe scripts/cherry_pawchive_lookup.py --proyecto Hoovesart
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from modules.cherry.logic import repoint    # noqa: E402


async def cargar_indice() -> dict[tuple[str, str], str]:
    """El índice de creadores de Pawchive. UNA petición, a su ritmo."""
    from third_party.cherry_dl.config import load_config
    from third_party.cherry_dl.engine import DownloadEngine
    from third_party.cherry_dl.templates.pawchive import PawchiveTemplate

    async with DownloadEngine(load_config()) as engine:
        engine.set_pace(PawchiveTemplate.min_request_interval)
        return await PawchiveTemplate(engine)._get_creators_index()


async def ids_de_proyecto(nombre: str) -> list[tuple[str, str, str]]:
    """Los `artist_id` que el índice local guarda para un proyecto.

    Devuelve `(sitio, artist_id, url)` por cada fuente. Sirve porque una fuente
    de Patreon guarda el id de Patreon, que es el mismo espacio de ids que usa
    Pawchive — así que ese id es un candidato a comprobar aunque la fuente de
    Kemono se haya quedado sin URL.
    """
    from third_party.cherry_dl.index import get_profile, list_profiles

    from modules.cherry.logic.profiles import index_db_path

    db = index_db_path()
    for p in await list_profiles(db):
        if p.get("display_name", "").casefold() != nombre.casefold():
            continue
        detalle = await get_profile(db, p["id"])
        if detalle:
            return [(u["site"], u["artist_id"] or "", u["url"] or "")
                    for u in detalle["urls"]]
    return []


def por_id(indice, creator_id: str) -> list[tuple[str, str]]:
    """`(service, nombre)` de cada servicio donde exista ese id."""
    return [(svc, nom) for (svc, cid), nom in indice.items() if cid == creator_id]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Busca creadores en el índice de Pawchive (1 petición)."
    )
    ap.add_argument("nombres", nargs="*", help="nombres a buscar")
    ap.add_argument("--id", action="append", default=[], dest="ids",
                    help="id de creador a comprobar (repetible)")
    ap.add_argument("--proyecto", action="append", default=[],
                    help="proyecto del índice local: busca por su nombre Y por "
                         "los artist_id de todas sus fuentes (repetible)")
    ap.add_argument("--tope", type=int, default=8,
                    help="cuántas sugerencias por nombre (por defecto 8)")
    args = ap.parse_args()

    if not (args.nombres or args.ids or args.proyecto):
        ap.error("hace falta al menos un nombre, --id o --proyecto")

    async def run() -> int:
        print("Pidiendo el índice de creadores de Pawchive (1 petición)…")
        indice = await cargar_indice()
        print(f"{len(indice):,} creadores en memoria.\n")

        # Los proyectos aportan su nombre y los ids de todas sus fuentes.
        nombres = list(args.nombres)
        ids: list[tuple[str, str]] = [("", i) for i in args.ids]
        for proy in args.proyecto:
            nombres.append(proy)
            fuentes = await ids_de_proyecto(proy)
            if not fuentes:
                print(f"⚠ «{proy}» no está en el índice local.\n")
                continue
            print(f"«{proy}» — fuentes en el índice local:")
            for sitio, aid, url in fuentes:
                print(f"    {sitio:<9} artist_id={aid or '—':<12} {url or '(sin URL)'}")
                if aid:
                    ids.append((f"{proy}/{sitio}", aid))
            print()

        for etiqueta, cid in ids:
            print(f"── id {cid}" + (f"  (de {etiqueta})" if etiqueta else ""))
            hits = por_id(indice, cid)
            if hits:
                for svc, nom in hits:
                    print(f"     ✓ {svc}/{cid} → «{nom}»")
                    print(f"       https://pawchive.pw/{svc}/user/{cid}")
            else:
                print("     ✗ ese id no existe en Pawchive")
            print()

        for nombre in nombres:
            print(f"── por nombre «{nombre}»")
            sug = repoint.buscar_por_nombre(indice, nombre, tope=args.tope)
            if not sug:
                print("     ✗ ningún creador se le parece")
            for s in sug:
                print(f"     {s.score:.2f}  {s.nombre[:30]:<31} {s.url}")
            print()

        print("Nada de esto se ha escrito. Para dar de alta una fuente, usar la app.")
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
