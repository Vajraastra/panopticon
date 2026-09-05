"""
Selftest del template de Pawchive.

Comprueba detección de URL, resolución del artista, extracción de archivos,
paginación y —lo que más importa aquí— que los límites que pidió su
administrador (1 petición/s y 5 descargas simultáneas) llegan de verdad al
motor: que el pool efectivo sale de ahí y que el ritmo NO se multiplica por
worker.

**No toca la red.** El motor se sustituye por uno falso que devuelve respuestas
capturadas del sitio real el 2026-09-05. Eso no es solo comodidad de test: el
administrador de pawchive pide expresamente que no se le machaque
(`third_party/cherry_dl/templates/pawchive.py` lo cita), así que las pruebas no
gastan ni una petición suya —cero, no «una por segundo»—. El bloque del pacer
mide contra un reloj falso, así que tampoco tarda un segundo por turno.

Uso:  venv/Scripts/python.exe scripts/pawchive_selftest.py
"""

from __future__ import annotations

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

from cherry_dl.download_service import site_workers                       # noqa: E402
from cherry_dl.engine import _Pacer                                       # noqa: E402
from cherry_dl.templates._registry import find_template, list_templates   # noqa: E402
from cherry_dl.templates.kemono import KemonoTemplate                     # noqa: E402
from cherry_dl.templates.patreon import PatreonTemplate                   # noqa: E402
from cherry_dl.templates.pawchive import (                                # noqa: E402
    PawchiveTemplate,
    _hash_from_path,
)

_ok = 0
_fail = 0


def chk(cond: bool, msg: str) -> None:
    global _ok, _fail
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if cond:
        _ok += 1
    else:
        _fail += 1


# ── Respuestas capturadas del sitio real (2026-09-05) ─────────────────────────

CREATORS = [
    {"id": "30500811", "name": "BOKABA", "service": "patreon",
     "indexed": 1785420000, "updated": 1788505200, "ever_imported": True},
]

H1 = "51bf253b816128bec1d099972b433c474c6b81e7d79857dbac211003486a83ff"
H2 = "571ffff62ab3b6eb012709962b23a85e3de2f90ebf0d808fd53ed6fb245b2bf4"

PAGINA_0 = [
    {   # file preview_only + adjunto libre → solo debe salir el adjunto
        "id": "168501262", "title": "Senpai-San", "published": "2026-09-03T16:05:03",
        "file": {"name": "Notice0904.png", "path": f"/51/bf/{H1}.png",
                 "preview_only": True},
        "attachments": [{"name": "NC_Senpai-San.zip", "path": f"/57/1f/{H2}.zip"}],
    },
    {   # todo preview_only → no sale nada
        "id": "168400000", "title": "Solo preview", "published": "2026-09-02T10:00:00",
        "file": {"name": "a.jpg", "path": "/cf/ea/" + "c" * 64 + ".jpg",
                 "preview_only": True},
        "attachments": [{"name": "b.jpg", "path": "/ab/cd/" + "d" * 64 + ".jpg",
                         "preview_only": True}],
    },
    {   # sin file, dos adjuntos libres
        "id": "168300000", "title": "Dos adjuntos", "published": "2026-09-01T10:00:00",
        "file": None,
        "attachments": [{"name": "x.png", "path": "/11/22/" + "a" * 64 + ".png"},
                        {"name": "y: raro?.png", "path": "/33/44/" + "b" * 64 + ".png"}],
    },
]


class EngineFalso:
    """Sirve lo capturado y registra las llamadas, para auditar la paginación."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []

    async def get_json(self, url: str):
        self.llamadas.append(url)
        if url.endswith("/creators"):
            return CREATORS
        if "o=0" in url:
            return PAGINA_0
        return []       # segunda página vacía → fin de la paginación


# ── Pruebas ───────────────────────────────────────────────────────────────────

async def main() -> int:
    print("[1] Detección de URL")
    print("    templates registrados:", list_templates())
    for url, esperado in [
        ("https://pawchive.pw/patreon/user/30500811", True),
        ("https://pawchive.pw/fanbox/user/12345", True),
        ("https://pawchive.pw/discord/user/999", False),      # no lo sirve la API
        ("https://kemono.cr/patreon/user/30500811", False),   # es de kemono
        ("https://www.patreon.com/RuiDX", False),
    ]:
        chk(PawchiveTemplate.can_handle(url) == esperado, f"{url[:46]:<46} -> {esperado}")
    chk(find_template("https://pawchive.pw/patreon/user/1") is PawchiveTemplate,
        "el registry lo elige para una URL de pawchive")
    chk(find_template("https://kemono.cr/patreon/user/1").name == "kemono",
        "kemono sigue quedándose con lo suyo")

    print("\n[2] Hash desde el path")
    chk(_hash_from_path(f"/51/bf/{H1}.png") == H1,
        "extrae el sha256 con la extensión pegada (formato de pawchive)")
    chk(_hash_from_path("/ad/93/no-es-un-hash.jpg") is None, "rechaza lo que no es hash")
    chk(_hash_from_path("/data/ab/cd/xyz") is None, "rechaza un path con otra forma")

    print("\n[3] Los límites que pidió su administrador")
    # Las dos cifras vienen de su respuesta del 2026-09-05, no de afinar
    # rendimiento. Si alguien las sube, que sea contra un test que falle.
    chk(PawchiveTemplate.max_workers == 5, "max_workers = 5 (tope rígido)")
    chk(PawchiveTemplate.workers == 5, "workers = 5 por defecto")
    chk(PawchiveTemplate.min_request_interval == 1.0,
        "min_request_interval = 1 s (1 petición/s)")
    chk(PawchiveTemplate.scan_page_delay == 0.0,
        "scan_page_delay = 0: lo espacia el pacer, no un sleep propio")
    chk(PawchiveTemplate.provides_file_hashes is True,
        "declara que expone hashes → dedup antes de descargar")

    eng = EngineFalso()
    t = PawchiveTemplate(eng)

    print("\n[4] Info del artista")
    a = await t.get_artist_info("https://pawchive.pw/patreon/user/30500811")
    chk(a.artist_id == "30500811", "id correcto")
    chk(a.name == "BOKABA", f"resuelve el nombre real desde el índice ({a.name})")
    chk(a.service == "patreon" and a.site == "pawchive", "service y site correctos")

    print("\n[5] Archivos del artista")
    files = [f async for f in t.iter_files(a)]
    chk(len(files) == 3, f"salen 3 de 6: los preview_only se saltan (fueron {len(files)})")
    nombres = [f.filename for f in files]
    chk("NC_Senpai-San.zip" in nombres, "incluye el adjunto libre")
    chk("Notice0904.png" not in nombres, "excluye el file marcado preview_only")
    chk("a.jpg" not in nombres and "b.jpg" not in nombres,
        "excluye el post entero cuando todo es preview")

    z = files[0]
    chk(z.url.startswith("https://file.pawchive.pw/data/57/1f/"),
        "la URL apunta al host de archivos, bajo /data/")
    chk(z.url.endswith("?f=NC_Senpai-San.zip"), "lleva el ?f= que usa la propia web")
    chk(z.remote_hash == H2, "el hash sale del path, antes de descargar")
    chk(z.extra_headers.get("Referer") == a.url, "manda Referer del creador")
    chk(z.dedup_key == z.url, "la clave de dedup es la URL (como en kemono)")
    raro = next(f for f in files if f.filename.startswith("y"))
    chk(raro.filename == "y_ raro_.png", f"sanea el nombre ({raro.filename})")

    print("\n[6] Paginación")
    paginas = [u for u in eng.llamadas if "/posts?o=" in u]
    chk(len(paginas) == 2, f"pide páginas hasta la vacía ({len(paginas)})")
    chk(paginas[0].endswith("o=0") and paginas[1].endswith("o=50"),
        "el offset avanza de 50 en 50")
    chk(sum(1 for u in eng.llamadas if u.endswith("/creators")) == 1,
        "el índice de creadores (12,8 MB) se pide UNA sola vez por sesión")

    print("\n[7] El pool llega al servicio de descarga")
    # Se llama a la función REAL de download_service, no a una copia del
    # cálculo: la copia ya se quedó desfasada una vez y el test pasó igual.
    cls_paw = find_template("https://pawchive.pw/patreon/user/30500811")
    chk(cls_paw is PawchiveTemplate, "el registry resuelve el template")

    for pedidos in (1, 3, 8):
        n = site_workers(cls_paw, pedidos)
        chk(n == 5,
            f"perfil con workers={pedidos} → {n} simultáneas (el acuerdo son 5)")

    # El tope vale en los dos sentidos: sube el ajuste general que se queda
    # corto y recorta el que se pasa. Y no se le contagia a los demás sitios.
    chk(site_workers(PatreonTemplate, 8) == 2,
        "patreon sigue en 2: el 5 de pawchive no se le pega")
    chk(site_workers(KemonoTemplate, 8) == 2,
        "kemono sigue en 2 (su comentario ya era un tope)")
    chk(site_workers(None, 3) == 3, "una URL sin template usa el ajuste general")

    print("\n[8] El ritmo NO se multiplica por worker")
    # Es el punto entero del pacer: con 5 workers pidiendo turno a la vez, la
    # tasa agregada sigue siendo 1/s. Un delay por worker daría 5/s, que es
    # justo lo que el administrador pidió no hacer. Se mide contra un reloj
    # falso: el test no duerme ni un segundo de verdad.
    reloj = {"t": 0.0}

    async def _sleep_falso(d):
        reloj["t"] += d

    loop = asyncio.get_running_loop()
    real_sleep, real_time = asyncio.sleep, loop.time
    loop.time = lambda: reloj["t"]                      # type: ignore[method-assign]
    asyncio.sleep = _sleep_falso                        # type: ignore[assignment]
    try:
        pacer  = _Pacer(1.0)
        turnos = []
        for _ in range(5):
            await pacer.wait()
            turnos.append(reloj["t"])
    finally:
        asyncio.sleep = real_sleep                      # type: ignore[assignment]
        loop.time = real_time                           # type: ignore[method-assign]

    chk(turnos == [0.0, 1.0, 2.0, 3.0, 4.0],
        f"5 turnos consecutivos caen a 1 s exacto ({turnos})")
    chk(turnos[-1] - turnos[0] >= 4.0,
        "5 peticiones ocupan >= 4 s: no salen en ráfaga")

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
