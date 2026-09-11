"""
Prueba de la fusión de proyectos (A5b) sobre carpetas de juguete.

Fusionar mueve archivos entre carpetas y **no tiene deshacer**, así que no se
prueba contra proyectos reales: este script se monta su propio `index.db` y sus
propias carpetas en un temporal, y se niega a correr si el `INDEX_DB` que ve no
es el suyo. Nada aquí toca `~/.cherry-dl/` ni `G:`.

    venv/Scripts/python.exe scripts/cherry_merge_toytest.py

Cuatro escenarios:

  1. **Fusión normal** — un archivo único que se mueve, uno repetido que queda
     huérfano, uno que el catálogo lista pero no está en disco, y uno suelto que
     el catálogo no conoce. Comprueba que el preview los clasifica igual que la
     migración y que el índice queda con un solo perfil.
  2. **La migración falla** — el destino no tiene catálogo. Comprueba lo que este
     módulo cambió respecto al original: el índice NO se toca, así que los dos
     perfiles siguen vivos y el archivo sigue en su carpeta.
  3. **La fusión no compacta** — el destino conserva su numeración y el
     contador visual queda por delante del archivo migrado.
  4. **Compatibilidad con compactación** — el archivo migrado usa la misma
     convención visual que el destino, por lo que una compactación manual ya no
     deja el contador detrás ni prepara una colisión.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = 0
checks = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global failures, checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        failures += 1
        print(f"  FALLO {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


# ── montaje del escenario ───────────────────────────────────────────────────

async def make_project(
    index_db: Path, root: Path, name: str, files: list[tuple[str, str]],
    counter: int,
) -> int:
    """Crea un proyecto de juguete: carpeta, catálogo, archivos y perfil.

    `files` son pares (nombre, hash). El contenido del archivo es su hash, que
    para esta prueba da igual: la migración compara los hashes del catálogo, no
    recalcula nada.
    """
    from third_party.cherry_dl.catalog import add_file, init_catalog, set_meta_int
    from third_party.cherry_dl.file_routing import LAYOUT_VERSION
    from third_party.cherry_dl.index import add_profile_url, create_profile

    folder = root / name
    await init_catalog(folder)
    for i, (filename, file_hash) in enumerate(files, start=1):
        (folder / filename).write_text(file_hash, encoding="utf-8")
        await add_file(folder, file_hash, filename, None, len(file_hash), i)
    await set_meta_int(folder, "counter", counter)
    await set_meta_int(folder, "counter:visual", counter)
    await set_meta_int(folder, "layout_version", LAYOUT_VERSION)

    pid = await create_profile(index_db, name, folder, "patreon")
    await add_profile_url(index_db, pid, f"https://patreon.com/{name}", "patreon", name)
    return pid


def pair_of(id_a: int, name_a: str, folder_a: Path,
            id_b: int, name_b: str, folder_b: Path) -> dict:
    """Un par con la forma que produce `logic/duplicates.find_pairs`.

    `created_at` va explícito y en orden: `dup_keep_remove` decide con él, y dos
    perfiles creados en el mismo segundo empatan y desempatan por otro camino.
    """
    return {
        "id_a": id_a, "name_a": name_a, "folder_a": str(folder_a),
        "id_b": id_b, "name_b": name_b, "folder_b": str(folder_b),
        "created_at_a": "2026-01-01T00:00:00",
        "created_at_b": "2026-06-01T00:00:00",
        "tier": "source", "reason": "prueba",
    }


async def profile_ids(index_db: Path) -> set[int]:
    from third_party.cherry_dl.index import list_profiles
    return {p["id"] for p in await list_profiles(index_db)}


# ── escenarios ──────────────────────────────────────────────────────────────

async def test_normal(root: Path, index_db: Path) -> None:
    print("\n1. Fusión normal")
    from modules.cherry.logic import merge as mg

    keep_id = await make_project(index_db, root, "artista", [
        ("artista_00001.jpg", "h1"),
        ("artista_00002.jpg", "h2"),
        ("artista_00003.jpg", "h3"),
    ], counter=3)
    rm_id = await make_project(index_db, root, "artista_oficial", [
        ("oficial_00001.jpg", "h2"),   # el destino ya lo tiene: huérfano
        ("oficial_00002.jpg", "h9"),   # único: se mueve
        ("oficial_00003.jpg", "h8"),   # se borrará del disco: ausente
    ], counter=3)

    src = root / "artista_oficial"
    dst = root / "artista"
    (src / "oficial_00003.jpg").unlink()            # catalogado pero no está
    (src / "suelto.txt").write_text("x", encoding="utf-8")  # está pero no catalogado

    pair = pair_of(keep_id, "artista", dst, rm_id, "artista_oficial", src)

    plan = await mg.preview(pair)
    check("el más antiguo absorbe al más nuevo", plan.keep_id == keep_id,
          f"keep={plan.keep_id} esperado {keep_id}")
    check("preview: 1 archivo a mover", plan.movable == 1, f"movable={plan.movable}")
    check("preview: 1 repetido", plan.duplicated == 1, f"duplicated={plan.duplicated}")
    check("preview: 1 ausente del disco", plan.missing == 1, f"missing={plan.missing}")
    check("preview: 1 sin catalogar", plan.untracked == 1, f"untracked={plan.untracked}")
    check("el preview no movió nada", (src / "oficial_00002.jpg").exists())

    seen: list[tuple[int, int]] = []
    result = await mg.merge_pair(
        pair, orphan_action=mg.ORPHAN_DELETE,
        on_progress=lambda done, total: seen.append((done, total)),
    )

    check("la fusión salió bien", result.ok, str(result.errors))
    check("1 archivo movido", result.moved == 1, f"moved={result.moved}")
    check("1 huérfano", result.orphaned == 1, f"orphaned={result.orphaned}")
    check("1 purgado", result.purged == 1, f"purged={result.purged}")
    check("hubo progreso", len(seen) == 3, f"{len(seen)} avisos")
    check("el preview acertó lo que se movería", result.moved == plan.movable)

    check("el único llegó al destino", (dst / "artista_00004.jpg").exists(),
          f"hay {sorted(p.name for p in dst.iterdir())}")
    check("el único ya no está en el origen", not (src / "oficial_00002.jpg").exists())
    check("el huérfano se borró", not (src / "oficial_00001.jpg").exists())
    check("el archivo sin catalogar sobrevive", (src / "suelto.txt").exists(),
          "borrar lo que el catálogo no conoce sería pérdida de datos")

    from third_party.cherry_dl.catalog import get_all_hashes
    hashes = await get_all_hashes(dst)
    check("el catálogo del destino conoce el hash migrado", "h9" in hashes)

    ids = await profile_ids(index_db)
    check("el perfil absorbido desapareció del índice", rm_id not in ids)
    check("el perfil que absorbe sigue ahí", keep_id in ids)

    from third_party.cherry_dl.index import get_profile
    kept = await get_profile(index_db, keep_id)
    urls = {u["url"] for u in kept["urls"]}
    check("la fuente del absorbido pasó al que absorbe",
          "https://patreon.com/artista_oficial" in urls, str(urls))

    check("avisa de las dos carpetas", len(result.touched_folders) == 2,
          str(result.touched_folders))


async def test_migration_fails(root: Path, index_db: Path) -> None:
    print("\n2. La migración falla: el índice no se toca")
    from modules.cherry.logic import merge as mg

    # Destino sin catálogo: `next_counter` no encuentra su fila 'counter' y cada
    # archivo entra en la lista de errores de la migración.
    dst = root / "sin_catalogo"
    dst.mkdir()
    keep_id = await __import__(
        "third_party.cherry_dl.index", fromlist=["create_profile"]
    ).create_profile(index_db, "sin_catalogo", dst, "patreon")
    rm_id = await make_project(index_db, root, "con_archivos", [
        ("con_00001.jpg", "z1"),
    ], counter=1)
    src = root / "con_archivos"

    pair = pair_of(keep_id, "sin_catalogo", dst, rm_id, "con_archivos", src)
    result = await mg.merge_pair(pair, orphan_action=mg.ORPHAN_DELETE)

    check("la fusión se reporta como fallida", not result.ok)
    check("hay errores", bool(result.errors), "se esperaba al menos uno")
    check("el índice NO se fusionó", not result.index_merged,
          "fusionar el índice tras fallar la migración deja archivos sin dueño")

    ids = await profile_ids(index_db)
    check("los dos perfiles siguen vivos", {keep_id, rm_id} <= ids, str(ids))
    check("el archivo sigue en su carpeta", (src / "con_00001.jpg").exists())
    check("no se avisa de ninguna carpeta", result.touched_folders == [])


async def test_no_compaction(root: Path, index_db: Path) -> None:
    print("\n3. La fusión no compacta la numeración")
    from modules.cherry.logic import merge as mg
    from third_party.cherry_dl.catalog import get_meta_int

    keep_id = await make_project(index_db, root, "limpio", [
        ("limpio_00001.jpg", "c1"), ("limpio_00002.jpg", "c2"),
    ], counter=2)
    rm_id = await make_project(index_db, root, "limpio_dos", [
        ("dos_00001.jpg", "c9"),
    ], counter=1)
    dst, src = root / "limpio", root / "limpio_dos"

    pair = pair_of(keep_id, "limpio", dst, rm_id, "limpio_dos", src)
    result = await mg.merge_pair(pair)

    check("fusionó", result.ok, str(result.errors))
    check("los originales conservan su nombre", (dst / "limpio_00001.jpg").exists())
    check("el migrado conserva artista y se llama 00003", (dst / "limpio_00003.jpg").exists(),
          f"hay {sorted(p.name for p in dst.iterdir())}")
    counter = await get_meta_int(dst, "counter:visual")
    check("el contador quedó en 3, por delante del migrado", counter == 3,
          f"counter={counter}")
    check("merge_pair ya no acepta compactar",
          "compact" not in mg.merge_pair.__code__.co_varnames,
          "el parámetro se retiró a propósito: ver la cabecera de merge.py")


async def test_why_no_compaction(root: Path, index_db: Path) -> None:
    print("\n4. La nomenclatura v2 es compatible con compactación")
    from modules.cherry.logic import merge as mg
    from third_party.cherry_dl.catalog import get_meta_int
    from third_party.cherry_dl.dedup import compact_folders

    # Hueco deliberado: falta el 00002.
    keep_id = await make_project(index_db, root, "hueco", [
        ("hueco_00001.jpg", "g1"), ("hueco_00003.jpg", "g3"),
    ], counter=3)
    rm_id = await make_project(index_db, root, "hueco_dos", [
        ("hdos_00001.jpg", "g9"),
    ], counter=1)
    dst, src = root / "hueco", root / "hueco_dos"

    pair = pair_of(keep_id, "hueco", dst, rm_id, "hueco_dos", src)
    result = await mg.merge_pair(pair)
    check("fusionó", result.ok, str(result.errors))

    migrated = dst / "hueco_00004.jpg"
    check("el migrado entró con nombre visual uniforme", migrated.exists(),
          f"hay {sorted(p.name for p in dst.iterdir())}")
    before = await get_meta_int(dst, "counter:visual")
    check("tras fusionar, el contador está por delante del migrado", before == 4,
          f"counter={before}")

    # Lo que haría el original a continuación, ejercido a mano.
    await compact_folders([(keep_id, str(dst))])

    check("compactar cerró el hueco: 00003 pasó a 00002",
          (dst / "hueco_00002.jpg").exists(),
          f"hay {sorted(p.name for p in dst.iterdir())}")
    compacted_migrated = dst / "hueco_00003.jpg"
    check("compactar incluyó al migrado", compacted_migrated.exists(),
          "todos los visuales deben compartir el patrón artista_00000.ext")

    after = await get_meta_int(dst, "counter:visual")
    next_name = f"hueco_{(after or 0) + 1:05d}.jpg"
    check(
        f"el siguiente nombre tras compactar queda libre ({next_name})",
        after == 4 and not (dst / next_name).exists(),
        "la compactación no debe preparar una sobreescritura",
    )


# ── arranque ────────────────────────────────────────────────────────────────

async def run(root: Path) -> None:
    from third_party.cherry_dl import config
    from third_party.cherry_dl.index import init_index

    index_db = root / "index.db"
    config.INDEX_DB = index_db          # aislamiento: ver la guarda de main()
    config.CHERRY_DIR = root
    await init_index(index_db)

    real = Path.home() / ".cherry-dl" / "index.db"
    if config.INDEX_DB == real or str(root) in ("", "/"):
        raise SystemExit("ABORTADO: el aislamiento del temporal no se aplicó.")

    await test_normal(root, index_db)
    await test_migration_fails(root, index_db)
    await test_no_compaction(root, index_db)
    await test_why_no_compaction(root, index_db)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="cherry_merge_toy_"))
    print(f"Carpetas de juguete en {root}")
    try:
        asyncio.run(run(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{checks - failures} ok, {failures} fallo(s) sobre {checks}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
