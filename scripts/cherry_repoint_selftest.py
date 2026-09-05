"""
Selftest de la migración Kemono → Pawchive (`logic/repoint` y `logic/pending_purge`).

Lo que de verdad protege es una decisión, no una función: **cuándo se da por
bueno que dos nombres son el mismo creador.** El id de Pawchive es el del
servicio original, así que la traducción parece mecánica — pero el proyecto
`𝕮𝖗𝖔𝖘𝖘` apuntaba a `patreon/61047323`, que allí es «视野环球财经», confirmado a
mano por David el 2026-09-05. Aceptarlo habría metido obra de otro autor en esa
carpeta, y como la biblioteca deduplica por hash del contenido, deshacerlo
después no es quitar una fuente: es separar archivos ya mezclados.

Por eso los casos de abajo son **los nombres reales de la biblioteca**, con el
resultado que se verificó a mano, y no ejemplos inventados. Si alguien afloja el
umbral, aquí se entera.

**No toca la red, ni `~/.cherry-dl`, ni `G:`.** El índice de creadores se
sustituye por un diccionario en memoria; `plan()` y `apply()`, que son los
únicos que salen fuera, no se ejercitan aquí.

Uso:  venv/Scripts/python.exe scripts/cherry_repoint_selftest.py
"""

from __future__ import annotations

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

from modules.cherry.logic import pending_purge          # noqa: E402
from modules.cherry.logic import repoint                # noqa: E402

_ok = 0
_fail = 0


def chk(cond: bool, msg: str) -> None:
    global _ok, _fail
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if cond:
        _ok += 1
    else:
        _fail += 1


# Índice de creadores recortado: los nombres reales que devolvió Pawchive el
# 2026-09-05, más los que aparecieron como falsas sugerencias de «Cross».
INDICE = {
    ("patreon", "7998148"):   "BadSpider",
    ("patreon", "7788275"):   "Cubed Coconut",
    ("patreon", "79784210"):  "Mereoleon",
    ("patreon", "305523"):    "TwistedScarlett60",
    ("patreon", "93665351"):  "Umbra.",
    ("patreon", "69132994"):  "YourDigimonGirl",
    ("patreon", "126855042"): "HONKIBOOTY",
    ("patreon", "61047323"):  "视野环球财经",     # el id de 𝕮𝖗𝖔𝖘𝖘, ocupado por otro
    ("patreon", "9098078"):   "crossrealm",
    ("patreon", "46701683"):  "Artcross",
    ("patreon", "135057761"): "r",
    ("fanbox",  "38332767"):  "RO",
    ("fanbox",  "30462830"):  "マックス",
    ("fanbox",  "389873"):    "shift",
    ("fanbox",  "560126"):    "cross-kan",
}


def main() -> int:
    print("[1] Traducción de URL de Kemono")
    chk(repoint.traducir("https://kemono.cr/patreon/user/12345") == ("patreon", "12345"),
        "extrae servicio e id")
    chk(repoint.traducir("https://kemono.su/fanbox/user/99") == ("fanbox", "99"),
        "acepta el dominio viejo (.su) — las fuentes guardan el de su alta")
    chk(repoint.traducir("https://pawchive.pw/patreon/user/1") is None,
        "no toca lo que ya es de Pawchive")
    chk(repoint.traducir("") is None, "una URL vacía no revienta")

    print("\n[2] Normalización de nombres")
    chk(repoint.normalizar("𝕮𝖗𝖔𝖘𝖘") == "cross",
        "descompone las variantes matemáticas de Unicode (𝕮𝖗𝖔𝖘𝖘 → cross)")
    chk(repoint.normalizar("Cubed Coconut") == "cubedcoconut",
        "quita espacios y mayúsculas")
    chk(repoint.normalizar("Umbra.") == "umbra", "quita la puntuación")
    chk(repoint.normalizar("マックス") == "マックス", "no destruye el katakana")

    print("\n[3] Los pares reales que SÍ son el mismo creador")
    # Todos verificados contra la biblioteca: son las fuentes que se migraron.
    pares_ok = [
        ("MereoleonArt",    "Mereoleon"),
        ("cubedcoconut",    "Cubed Coconut"),
        ("TwistedScarlett", "TwistedScarlett60"),
        ("Umbra",           "Umbra."),
        ("yourdigimongirl", "YourDigimonGirl"),
        ("honkibooty",      "HONKIBOOTY"),
        ("マックス",          "マックス"),
        ("shift",           "shift"),
    ]
    for proyecto, pawchive in pares_ok:
        s = repoint.parecido(proyecto, pawchive)
        chk(s >= repoint.UMBRAL,
            f"{proyecto} ≈ {pawchive}  ({s:.2f} >= {repoint.UMBRAL})")

    print("\n[4] El par real que NO lo es")
    s = repoint.parecido("𝕮𝖗𝖔𝖘𝖘", "视野环球财经")
    chk(s < repoint.UMBRAL,
        f"𝕮𝖗𝖔𝖘𝖘 ≠ 视野环球财经  ({s:.2f} < {repoint.UMBRAL}) — el caso que lo motivó todo")

    print("\n[5] Una subcadena corta no es una identidad")
    # Sin esto, «RO» y «r» puntuaban 1.00 contra «Cross» (están dentro de
    # c-r-o-s-s) y llenaban de ruido justo la lista que hay que revisar a mano.
    for corto in ("RO", "r", "os"):
        s = repoint.parecido("Cross", corto)
        chk(s < repoint.UMBRAL, f"«Cross» vs «{corto}» no es identidad ({s:.2f})")
    chk(repoint.parecido("Mereoleon", "MereoleonArt") == 1.0,
        "pero una contención sustancial sí lo es")

    print("\n[6] Sugerencias por nombre")
    sug = repoint.buscar_por_nombre(INDICE, "𝕮𝖗𝖔𝖘𝖘", "patreon")
    nombres = [s.nombre for s in sug]
    chk(bool(sug), f"propone alternativas para 𝕮𝖗𝖔𝖘𝖘 ({len(sug)})")
    chk("crossrealm" in nombres and "Artcross" in nombres,
        f"y son creíbles: {nombres}")
    chk("RO" not in nombres and "r" not in nombres,
        "sin el ruido de las subcadenas cortas")
    chk("视野环球财经" not in nombres,
        "el ocupante del id no se cuela como sugerencia")

    sug_svc = repoint.buscar_por_nombre(INDICE, "shift", "fanbox")
    chk(bool(sug_svc) and sug_svc[0].nombre == "shift",
        "el nombre exacto encabeza la lista")
    chk(sug_svc[0].url == "https://pawchive.pw/fanbox/user/389873",
        f"y su URL se arma bien ({sug_svc[0].url})")
    chk(repoint.buscar_por_nombre(INDICE, "") == [],
        "un nombre vacío no propone nada")

    print("\n[7] Estados y resumen")
    def cand(estado: str, nombre: str = "X"):
        return repoint.Candidato(
            profile_id=1, profile_name=nombre, url_id=1, kemono_url="",
            service="patreon", creator_id="1", pawchive_url="", estado=estado,
        )
    chk(cand(repoint.LISTO).listo, "solo el estado `listo` se añade")
    for otro in (repoint.OTRO_NOMBRE, repoint.NO_ESTA,
                 repoint.OTRO_SERVICIO, repoint.SIN_URL, repoint.YA_ESTA):
        chk(not cand(otro).listo, f"`{otro}` no se añade")
    r = repoint.resumen([cand(repoint.LISTO), cand(repoint.LISTO),
                         cand(repoint.OTRO_NOMBRE)])
    chk(r == {repoint.LISTO: 2, repoint.OTRO_NOMBRE: 1}, f"el resumen cuenta bien ({r})")

    print("\n[8] Qué host se considera muerto")
    chk(pending_purge._host("https://kemono.cr/b1/0b/abc.png") == "kemono.cr",
        "saca el host de una URL de descarga")
    chk(pending_purge._host("patreon://media/179166692") == "",
        "una URI canónica de Patreon no tiene host — y no se purga")
    chk(pending_purge._host("HTTPS://KEMONO.CR/x") == "kemono.cr",
        "el host se compara en minúsculas")
    for h in ("kemono.cr", "kemono.su", "kemono.party"):
        chk(h in pending_purge.HOSTS_MUERTOS, f"{h} está en la lista")
    chk("pawchive.pw" not in pending_purge.HOSTS_MUERTOS
        and "file.pawchive.pw" not in pending_purge.HOSTS_MUERTOS,
        "Pawchive NO está en la lista: purgar su cola sería borrar el destino")
    chk("c10.patreonusercontent.com" not in pending_purge.HOSTS_MUERTOS,
        "el CDN de Patreon tampoco — sus 12 filas se quedan")

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
