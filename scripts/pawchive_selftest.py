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
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from cherry_dl import site_budget as sb
from cherry_dl.download_service import (                                 # noqa: E402
    ordered_sources,
    site_workers,
    source_priority,
)
from cherry_dl.engine import _ByteBucket, _CompletionGate, _Pacer        # noqa: E402
from cherry_dl.profiles import resolve_artist_name                       # noqa: E402
from cherry_dl.templates._registry import find_template, list_templates   # noqa: E402
from cherry_dl.templates.kemono import KemonoTemplate                     # noqa: E402
from cherry_dl.templates.patreon import PatreonTemplate                   # noqa: E402
from cherry_dl.templates.pawchive import (                                # noqa: E402
    PawchiveTemplate,
    _download_user_agent,
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
    {   # formato legado: preview_only vivía en cada entrada
        "id": "168501262", "title": "Senpai-San", "published": "2026-09-03T16:05:03",
        "file": {"name": "Notice0904.png", "path": f"/51/bf/{H1}.png",
                 "preview_only": True},
        "attachments": [{"name": "NC_Senpai-San.zip", "path": f"/57/1f/{H2}.zip"}],
    },
    {   # formato vivo 2026-09-10: el estado vive en el post
        "id": "168400000", "title": "Solo preview", "published": "2026-09-02T10:00:00",
        "preview_state": "pending", "has_full": False,
        "file": {"name": "a.jpg", "path": "/cf/ea/" + "c" * 64 + ".jpg",
                 },
        "attachments": [{"name": "b.jpg", "path": "/ab/cd/" + "d" * 64 + ".jpg"}],
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
        self.scan_policy = None
        self.pace = None

    def set_scan_policy(self, policy) -> None:
        self.scan_policy = policy

    def set_pace(self, interval) -> None:
        self.pace = interval

    async def get_json(self, url: str):
        self.llamadas.append(url)
        if url.endswith("/creators"):
            return CREATORS
        if "o=0" in url:
            return PAGINA_0
        return []       # segunda página vacía → fin de la paginación

    async def get_text(self, url: str):
        self.llamadas.append(url)
        return '<meta content="BOKABA &amp; Co." name="artist_name">'


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

    print("\n[2] Orden de fuentes: oficiales antes que archivos")
    fuentes = [
        {"id": "paw", "url": "https://pawchive.pw/patreon/user/1"},
        {"id": "patreon-a", "url": "https://www.patreon.com/autor-a"},
        {"id": "kemono", "url": "https://kemono.cr/patreon/user/1"},
        {"id": "pixiv", "url": "https://www.pixiv.net/users/1"},
        {"id": "patreon-b", "url": "https://www.patreon.com/autor-b"},
    ]
    orden = [f["id"] for f in ordered_sources(fuentes)]
    chk(source_priority(fuentes[1]["url"]) < source_priority(fuentes[3]["url"]),
        "Patreon de pago precede al oficial gratuito Pixiv")
    chk(source_priority(fuentes[0]["url"]) < source_priority(fuentes[2]["url"]),
        "Pawchive queda antes que el legado de Kemono")
    chk(find_template(fuentes[2]["url"]).download_enabled is False,
        "Kemono se reconoce, pero queda fuera del circuito de descarga")
    chk(orden == ["patreon-a", "patreon-b", "pixiv", "paw", "kemono"],
        f"orden global correcto y estable ({orden})")

    print("\n[3] Hash desde el path")
    chk(_hash_from_path(f"/51/bf/{H1}.png") == H1,
        "extrae el sha256 con la extensión pegada (formato de pawchive)")
    chk(_hash_from_path("/ad/93/no-es-un-hash.jpg") is None, "rechaza lo que no es hash")
    chk(_hash_from_path("/data/ab/cd/xyz") is None, "rechaza un path con otra forma")

    print("\n[4] Los límites del sitio")
    # El intervalo viene de su respuesta del 2026-09-05, no de afinar
    # rendimiento. Los workers bajaron de 5 a 2 esa misma tarde, después de que
    # la primera descarga real acabara en 774 respuestas 403 seguidas: 5 en
    # vuelo con archivos de 130 MB daban 10,7 MB/s, y el acuerdo no habla de
    # caudal. Si alguien sube cualquiera de las tres, que sea contra un test
    # que falle.
    chk(PawchiveTemplate.max_workers == 2, "max_workers = 2 (tope rígido)")
    chk(PawchiveTemplate.workers == 2, "workers = 2 por defecto")
    chk(PawchiveTemplate.min_request_interval == 1.0,
        "min_request_interval = 1 s (1 petición/s)")
    chk(PawchiveTemplate.post_download_delay == 5.0,
        "espera 5 s desde el final de un archivo antes de iniciar otro")
    chk(PawchiveTemplate.download_window_limit == 500,
        "máximo de 500 transferencias por ventana")
    chk(PawchiveTemplate.download_window_seconds == 3 * 60 * 60,
        "la ventana móvil dura 3 horas")
    chk(PawchiveTemplate.throttle_failure_codes == frozenset(),
        "HTTP directo no arma circuito con fallos ambiguos de red")
    chk(PawchiveTemplate.throttle_failure_streak == 5,
        "cinco fallos consecutivos; uno aislado no pausa")
    chk(PawchiveTemplate.throttle_cooldowns
        == (15 * 60, 30 * 60, 60 * 60, 3 * 60 * 60),
        "enfriamiento progresivo 15/30/60/180 min")
    chk(PawchiveTemplate.scan_page_delay == 0.0,
        "scan_page_delay = 0: lo espacia el pacer, no un sleep propio")
    chk(PawchiveTemplate.provides_file_hashes is True,
        "declara que expone hashes → dedup antes de descargar")
    custom_ua = _download_user_agent(SimpleNamespace(config=SimpleNamespace(
        pawchive=SimpleNamespace(
            downloads_per_window=500,
            resume_headroom=50,
            gigabytes_per_window=25.0,
            window_hours=3.0,
        )
    )))
    chk("max 500 files or 25 GiB per 3 hours" in custom_ua,
        "el User-Agent refleja los límites ajustados por el usuario")

    eng = EngineFalso()
    t = PawchiveTemplate(eng)

    print("\n[5] Info del artista")
    PawchiveTemplate._creators_cache = None
    a = await t.get_artist_info("https://pawchive.pw/patreon/user/30500811")
    chk(a.artist_id == "30500811", "id correcto")
    chk(a.name == "BOKABA & Co.",
        f"resuelve el nombre real desde la página individual ({a.name})")
    chk(a.service == "patreon" and a.site == "pawchive", "service y site correctos")
    chk(sum(1 for u in eng.llamadas if u.endswith("/creators")) == 0,
        "no descarga el índice global cuando la página publica el nombre")

    class EngineSinMeta(EngineFalso):
        async def get_text(self, url: str):
            self.llamadas.append(url)
            return "<html><title>respuesta antigua</title></html>"

    PawchiveTemplate._creators_cache = None
    respaldo = EngineSinMeta()
    a_respaldo = await PawchiveTemplate(respaldo).get_artist_info(
        "https://pawchive.pw/patreon/user/30500811"
    )
    chk(a_respaldo.name == "BOKABA",
        "si falta el meta, conserva el índice global como respaldo")

    print("\n[6] Archivos del artista")
    files = [f async for f in t.iter_files(a)]
    chk(len(files) == 6, f"salen 6 estados de archivo, incluidos previews ({len(files)})")
    nombres = [f.filename for f in files]
    chk("NC_Senpai-San.zip" in nombres, "incluye el adjunto libre")
    previews = {f.filename: f for f in files if f.is_preview}
    chk(set(previews) == {"Notice0904.png", "a.jpg", "b.jpg"},
        f"detecta preview legado y estado nuevo ({set(previews)})")
    chk(all(f.url.startswith("https://file.pawchive.pw/data/")
            for f in previews.values()),
        "la preview conserva la URL final sólo como metadata, sin descargarla")

    z = next(f for f in files if f.filename == "NC_Senpai-San.zip")
    chk(z.url.startswith("https://file.pawchive.pw/data/57/1f/"),
        "la URL apunta al host de archivos, bajo /data/")
    chk(z.url.endswith("?f=NC_Senpai-San.zip"), "lleva el ?f= que usa la propia web")
    chk(z.remote_hash == H2, "el hash sale del path, antes de descargar")
    chk(z.extra_headers.get("Referer") == a.url, "manda Referer del creador")
    user_agent = z.extra_headers.get("User-Agent", "")
    chk(user_agent.startswith("Cherry-DL/"),
        "se identifica como descargador, sin fingir ser navegador")
    chk(("policy: 2 MiB/s sustained bandwidth; max 2 concurrent downloads; "
         "max 500 files or 25 GiB per 3 hours") in user_agent,
        "declara en inglés los límites efectivos por defecto")
    legacy_headers = t.transfer_headers(a, {"Referer": a.url})
    chk(legacy_headers.get("User-Agent") == user_agent,
        "una fila antigua recibe el User-Agent vigente al transferirse")
    chk(z.dedup_key == "pawchive://patreon/30500811/post/168501262/attachment/1",
        "la identidad Pawchive es estable entre preview y original")
    raro = next(f for f in files if f.filename.startswith("y"))
    chk(raro.filename == "y_ raro_.png", f"sanea el nombre ({raro.filename})")

    print("\n[7] Paginación")
    paginas = [u for u in eng.llamadas if "/posts?o=" in u]
    chk(len(paginas) == 2, f"pide páginas hasta la vacía ({len(paginas)})")
    chk(paginas[0].endswith("o=0") and paginas[1].endswith("o=50"),
        "el offset avanza de 50 en 50")
    chk(sum(1 for u in respaldo.llamadas if u.endswith("/creators")) == 1,
        "el respaldo pide el índice de creadores UNA sola vez por sesión")

    resolvedor = EngineFalso()
    nombre_publico = await resolve_artist_name(
        resolvedor, "https://pawchive.pw/patreon/user/30500811"
    )
    chk(nombre_publico == "BOKABA & Co.",
        "el flujo público de alta devuelve el nombre de la página")
    chk(resolvedor.scan_policy is PawchiveTemplate.scan_policy
        and resolvedor.pace == 1.0,
        "el alta usa la política Pawchive y evita la espera genérica")

    print("\n[8] El pool llega al servicio de descarga")
    # Se llama a la función REAL de download_service, no a una copia del
    # cálculo: la copia ya se quedó desfasada una vez y el test pasó igual.
    cls_paw = find_template("https://pawchive.pw/patreon/user/30500811")
    chk(cls_paw is PawchiveTemplate, "el registry resuelve el template")

    for pedidos in (1, 3, 8):
        n = site_workers(cls_paw, pedidos)
        chk(n == 2,
            f"perfil con workers={pedidos} → {n} simultáneas (el tope son 2)")

    # El tope vale en los dos sentidos: sube el ajuste general que se queda
    # corto y recorta el que se pasa. Y no se le contagia a los demás sitios.
    chk(site_workers(PatreonTemplate, 8) == 2,
        "patreon sigue en 2: el 5 de pawchive no se le pega")
    chk(site_workers(KemonoTemplate, 8) == 2,
        "kemono sigue en 2 (su comentario ya era un tope)")
    chk(site_workers(None, 3) == 3, "una URL sin template usa el ajuste general")

    print("\n[9] El ritmo NO se multiplica por worker")
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

    print("\n[10] La pausa entre archivos pequeños")
    reloj["t"] = 10.0
    loop.time = lambda: reloj["t"]                      # type: ignore[method-assign]
    asyncio.sleep = _sleep_falso                        # type: ignore[assignment]
    try:
        gate = _CompletionGate(5.0)
        gate.completed()
        await gate.wait()
        apertura = reloj["t"]
    finally:
        asyncio.sleep = real_sleep                      # type: ignore[assignment]
        loop.time = real_time                           # type: ignore[method-assign]

    chk(apertura == 15.0, f"la compuerta abre 5 s después ({apertura:.1f}s)")
    chk(gate.ready(), "al abrir, el siguiente archivo ya puede pedir turno")

    print("\n[11] El caudal, que es lo que el acuerdo no cubría")
    chk(PawchiveTemplate.max_bytes_per_sec == 2 * 1024**2,
        "techo de 2 MB/s agregado")
    chk(PawchiveTemplate.daily_byte_budget == 0,
        "sin presupuesto diario; mandan caudal, pool y pausa")
    chk(PawchiveTemplate.download_transport == "http",
        "los archivos usan HTTP directo identificable")
    chk(not PawchiveTemplate.browser_origin,
        "Pawchive ya no finge una sesión de navegador")
    # La aritmética que justifica que existan: cumplir 1 petición/s con los
    # archivos de esa tarde daba diez veces el caudal que ahora se permite.
    medido = 3854 * 2**20 / 360          # 3.854 MB en 6 minutos, medidos en disco
    chk(medido / PawchiveTemplate.max_bytes_per_sec > 5,
        f"lo medido el 2026-09-05 ({medido / 2**20:.1f} MB/s) es >5x el techo")
    daily_gib = 86400 * PawchiveTemplate.max_bytes_per_sec / 1024**3
    chk(abs(daily_gib - 168.75) < 0.01,
        "el techo continuo equivale a 168.75 GiB/día, sin corte artificial")

    print("\n[12] El cubo de fichas reparte, no reserva")
    # Mismo reloj falso: se mide el tiempo devengado, no se duerme de verdad.
    reloj["t"] = 0.0
    loop.time = lambda: reloj["t"]                      # type: ignore[method-assign]
    asyncio.sleep = _sleep_falso                        # type: ignore[assignment]
    try:
        # 1 MB/s con cubo de 2 MB: los dos primeros megas salen gratis (es el
        # burst) y a partir de ahí se paga un segundo por mega.
        cubo = _ByteBucket(1024**2, burst=2 * 1024**2)
        await cubo.take(2 * 1024**2)
        libre = reloj["t"]
        await cubo.take(3 * 1024**2)
        tras_3mb = reloj["t"]
    finally:
        asyncio.sleep = real_sleep                      # type: ignore[assignment]
        loop.time = real_time                           # type: ignore[method-assign]

    chk(libre == 0.0, f"el burst inicial no espera ({libre}s)")
    chk(abs(tras_3mb - 3.0) < 0.01,
        f"3 MB más cuestan 3 s a 1 MB/s ({tras_3mb:.2f}s)")

    print("\n[13] El presupuesto diario cuenta por día y por sitio")
    tmp = Path(tempfile.mkdtemp(prefix="paw_budget_")) / "b.json"
    try:
        chk(sb.spent_today("pawchive", tmp) == 0, "empieza en cero")
        sb.add("pawchive", 1024**3, tmp)
        sb.add("pawchive", 1024**3, tmp)
        chk(sb.spent_today("pawchive", tmp) == 2 * 1024**3, "suma entre llamadas")
        chk(sb.spent_today("kemono", tmp) == 0, "cada sitio lleva el suyo")
        chk(sb.remaining("pawchive", 3 * 1024**3, tmp) == 1024**3,
            "lo que queda es el tope menos lo gastado")
        chk(sb.remaining("pawchive", 2 * 1024**3, tmp) == 0,
            "agotado da 0, no un negativo")
        chk(sb.remaining("pawchive", 0, tmp) == -1,
            "sin tope da -1, que no es lo mismo que agotado")
        tmp.write_text("{ esto no es json", encoding="utf-8")
        chk(sb.spent_today("pawchive", tmp) == 0,
            "un sidecar corrupto no tumba la descarga: se empieza de cero")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
