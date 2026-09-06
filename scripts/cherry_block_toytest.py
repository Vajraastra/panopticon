"""
Banco de pruebas de la cortesía del motor: qué hace cuando un sitio le cierra la
puerta, y qué hace para no llegar a ese punto.

El 2026-09-05, la primera descarga real de Pawchive encadenó **774 respuestas
403 en trece minutos**, a una petición por segundo, hasta agotar la cola. Cada
403 se trataba como un problema de ESE archivo, así que el motor pasaba al
siguiente y volvía a llamar. Ningún archivo se perdió —todos siguen en
`pending_queue`— pero el servidor recibió 774 peticiones inútiles después de
habernos dicho que no, que es exactamente la conducta que provocó el bloqueo.

Lo que se verifica aquí, contra un servidor de verdad en 127.0.0.1 que responde
403 a todo:

  1. El motor para al recibir una petición explícita de detenerse, sin recorrer
     la cola entera.
  2. Emite `SiteBlocked`, con el mensaje que el servidor puso en el cuerpo.
  3. Las fuentes siguientes del mismo sitio se saltan sin pedir nada.
  4. Los archivos siguen en la cola: se reintentan otro día, no se pierden.
  5. Una mezcla 50/50 de errores aislados y éxitos NO bloquea.
  6. Un 403 también activa la pausa post-archivo antes del siguiente intento.
  7. El techo de caudal se cumple de verdad, medido con un cronómetro contra
     bytes que viajan por un socket.
  8. El presupuesto del día para la run al agotarse, sin perder nada.
  9. El límite diario global es opcional y corta antes de consultar otra vez.

Se llama al `run_profile_download` real, no a una copia de su lógica: el freno
vive repartido entre el productor, los workers y el bucle de fuentes, y una
reimplementación en el test no probaría que esas tres partes se coordinan.

No toca la red, ni `~/.cherry-dl`, ni `G:`: el índice y el catálogo son
temporales y se borran al terminar.

Uso:  venv/Scripts/python.exe scripts/cherry_block_toytest.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl import download_service as ds
from cherry_dl import site_budget as sb                    # noqa: E402
from cherry_dl.config import NetworkConfig, UserConfig          # noqa: E402
from cherry_dl.templates import _registry                       # noqa: E402
from cherry_dl.templates.base import ArtistInfo, FileInfo, SiteTemplate  # noqa: E402

# La consola de Windows es cp1252: sin esto, el primer ✓ mata el script.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# ── Contadores ────────────────────────────────────────────────────────────────

_ok = 0
_fail = 0


def check(cond: bool, msg: str, extra: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ✓ {msg}")
    else:
        _fail += 1
        print(f"  ✗ {msg} {extra}")


# ── Servidor: dice que no a todo, y cuenta cuántas veces se lo preguntan ──────

# El cuerpo real que devolvía file.pawchive.pw: 143 bytes de texto plano
# escritos por una persona. El motor los tiraba sin leerlos.
MENSAJE = (
    b"stop before I block every single one of your IPs, either download "
    b"reasonably or contact me with your reasoning, but TBs a day is just abusive"
)


class Handler(BaseHTTPRequestHandler):
    """
      /no/<n>       403 con el mensaje en el cuerpo
      /si/<n>       200 con 1 KiB
      /big/<n>/<s>  200 con `s` bytes

    Los cuerpos llevan la ruta al principio: así cada archivo tiene un hash
    distinto y el dedup del catálogo no se traga la mitad del test.
    """

    protocol_version = "HTTP/1.1"
    hits: list[str] = []
    hit_times: list[tuple[str, float]] = []
    lock = threading.Lock()

    def log_message(self, *_args) -> None:  # silencio
        pass

    def do_GET(self) -> None:  # noqa: N802  (nombre impuesto por la stdlib)
        with Handler.lock:
            Handler.hits.append(self.path)
            Handler.hit_times.append((self.path, time.monotonic()))

        if self.path.startswith("/big/"):
            try:
                size = int(self.path.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                size = 1024
            payload = (self.path.encode() + b"\0" * size)[:size]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path.startswith("/si/"):
            payload = self.path.encode() + b"\0" * 1024
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 403 genérico: permite probar la ventana y la pausa sin activar el
        # corte inmediato reservado al mensaje explícito del administrador.
        if self.path.startswith("/generic/"):
            payload = b"forbidden"
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 403 del origen, sin cabeceras de Cloudflare: la forma exacta que
        # tenía el de Pawchive, DDoS-Guard incluido.
        self.send_response(403)
        self.send_header("Server", "ddos-guard")
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(MENSAJE)))
        self.end_headers()
        self.wfile.write(MENSAJE)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ── Template de juguete ──────────────────────────────────────────────────────

BASE = ""          # lo rellena main() con el puerto real
N_ARCHIVOS = 40    # muy por encima del umbral: si no frena, se nota


class ToyTemplate(SiteTemplate):
    """Un sitio con N archivos, todos servidos por el servidor de arriba.

    `toy://<ruta>/<n>` — la ruta decide si el servidor acepta o rechaza, y así
    el mismo template sirve para el caso que bloquea y para el que no.
    """

    name = "toy"
    base_url = "toy://"
    workers = 2
    max_workers = 2
    min_request_interval = 0.0   # el test mide el freno, no el ritmo
    provides_file_hashes = False
    scan_page_delay = 0.0
    cooldown_threshold = 10_000  # que no se interponga un enfriamiento

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toy://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        ruta = url[len("toy://"):]
        return ArtistInfo(artist_id=ruta, name=f"toy_{ruta}", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(N_ARCHIVOS):
            yield FileInfo(
                url=f"{BASE}/{artist.artist_id}/{i}",
                filename=f"{artist.artist_id}_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


class ToyMixTemplate(ToyTemplate):
    """Rechaza los archivos pares y sirve los impares.

    La racha nunca llega al umbral porque un éxito la rompe: el caso que
    distingue «este sitio nos cerró» de «este archivo concreto falló».
    """

    name = "toymix"
    base_url = "toymix://"
    workers = 1
    max_workers = 1

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toymix://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        ruta = url[len("toymix://"):]
        return ArtistInfo(artist_id=ruta, name=f"mix_{ruta}", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(N_ARCHIVOS):
            yield FileInfo(
                url=f"{BASE}/{'generic' if i % 2 == 0 else 'si'}/{i}",
                filename=f"mix_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


class ToyGapTemplate(ToyTemplate):
    """Tres 403 genéricos; verifica la pausa incluso cuando no hubo éxito."""

    name = "toygap"
    base_url = "toygap://"
    workers = 1
    max_workers = 1
    post_download_delay = 0.12
    N = 3

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toygap://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        return ArtistInfo(artist_id="g", name="gap", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(self.N):
            yield FileInfo(
                url=f"{BASE}/generic/{i}",
                filename=f"gap_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


class ToyWindowTemplate(ToyTemplate):
    """Cinco rechazos de siete, con dos éxitos intercalados."""

    name = "toywindow"
    base_url = "toywindow://"
    workers = 1
    max_workers = 1
    N = 7

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toywindow://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        return ArtistInfo(artist_id="w", name="window", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(self.N):
            accepted = i in {3, 5}
            yield FileInfo(
                url=f"{BASE}/{'si' if accepted else 'generic'}/{i}",
                filename=f"window_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


class ToyRateTemplate(ToyTemplate):
    """Seis archivos de 128 KiB con un techo de 256 KiB/s.

    El cubo arranca lleno (burst = 2 s de caudal = 512 KiB), así que los cuatro
    primeros archivos salen sin esperar y los dos últimos cuestan un segundo.
    Números pequeños a propósito: la prueba mide de verdad, y tiene que durar
    un segundo, no un minuto.
    """

    name = "toyrate"
    base_url = "toyrate://"
    max_bytes_per_sec = 256 * 1024
    N = 6
    SIZE = 128 * 1024

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toyrate://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        return ArtistInfo(artist_id="r", name="rate", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(self.N):
            yield FileInfo(
                url=f"{BASE}/big/{i}/{self.SIZE}",
                filename=f"rate_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


class ToyBudgetTemplate(ToyTemplate):
    """Veinte archivos de 1 KiB con un presupuesto diario de 4 KiB.

    Veinte y no tres a propósito: son los archivos de sobra los que destaparon
    que el bloqueo se borraba solo. Al terminar bien, un worker reiniciaba la
    racha de rechazos —correcto para lo que esa racha mide— y de paso levantaba
    el bloqueo que el otro acababa de poner por presupuesto, de forma
    intermitente. Bajar este número volvería el caso demasiado corto para que
    dos workers se pisen, y el test pasaría sin probar nada.
    """

    name = "toybudget"
    base_url = "toybudget://"
    daily_byte_budget = 4 * 1024
    N = 20
    SIZE = 1024

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return url.startswith("toybudget://")

    async def get_artist_info(self, url: str) -> ArtistInfo:
        return ArtistInfo(artist_id="b", name="budget", service="toy",
                          site=self.name, url=url)

    async def iter_files(self, artist: ArtistInfo, since=None):
        for i in range(self.N):
            yield FileInfo(
                url=f"{BASE}/big/{i}/{self.SIZE}",
                filename=f"budget_{i}.bin",
                artist_id=artist.artist_id,
                artist_name=artist.name,
                post_id=str(i),
            )


def toy_config(tmp: Path) -> UserConfig:
    return UserConfig(
        download_dir=str(tmp),
        workers=2,
        timeout=10,
        network=NetworkConfig(
            delay_min=0.0, delay_max=0.0,
            retries_api=2, retries_file=2,
            stall_timeout=10,
        ),
    )


async def correr(
    tmp: Path,
    urls: list[dict],
    nombre: str,
    config: UserConfig | None = None,
):
    """Una run completa; devuelve (summary, eventos)."""
    Handler.hits.clear()
    eventos: list = []
    summ = await ds.run_profile_download(
        {"id": -1, "folder_path": str(tmp / nombre), "urls": urls},
        workers=2,
        ext_filter=set(),
        emit=eventos.append,
        config=config or toy_config(tmp),
    )
    return summ, eventos


# ── Pruebas ───────────────────────────────────────────────────────────────────

async def run() -> None:
    global BASE
    srv, BASE = start_server()
    tmp = Path(tempfile.mkdtemp(prefix="cherry_block_"))
    print(f"servidor en {BASE}\ntemporal   {tmp}\n")

    # El índice va al temporal: ninguna prueba toca ~/.cherry-dl.
    ds.INDEX_DB = tmp / "index.db"
    for t in (ToyTemplate, ToyMixTemplate, ToyGapTemplate, ToyWindowTemplate,
              ToyRateTemplate, ToyBudgetTemplate):
        _registry._TEMPLATES.insert(0, t)
    # El presupuesto va a un sidecar del temporal: ninguna prueba toca el real.
    sb.BUDGET_FILE = tmp / "site_budget.json"

    try:
        # ── [1] Un sitio que rechaza todo se abandona ────────────────────────
        print("[1] 40 archivos, el servidor dice que no a todos")
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toy://no", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso1",
        )
        peticiones = [h for h in Handler.hits if h.startswith("/no/")]
        bloqueos = [e for e in ev if isinstance(e, ds.SiteBlocked)]

        check(len(peticiones) <= ds._BLOCK_STREAK + 2,
              f"para tras {len(peticiones)} peticiones, no {N_ARCHIVOS}",
              f"(umbral {ds._BLOCK_STREAK}; el margen son las que ya estaban "
              f"en vuelo cuando saltó)")
        check(len(bloqueos) == 1, "emite SiteBlocked una sola vez",
              f"({len(bloqueos)})")
        check(bool(bloqueos) and bloqueos[0].site == "toy",
              "el evento nombra el sitio")
        check(bool(bloqueos) and "abusive" in bloqueos[0].detail,
              "el evento trae el mensaje que escribió el servidor",
              f"({bloqueos[0].detail[:60] if bloqueos else ''!r})")
        check(summ.downloaded == 0, "no bajó nada", f"({summ.downloaded})")

        # ── [2] Los archivos siguen en la cola ───────────────────────────────
        print("\n[2] Lo que quedó en la cola")
        from cherry_dl.catalog import pending_count
        quedan = await pending_count(tmp / "caso1", -1)
        check(quedan == N_ARCHIVOS,
              f"los {N_ARCHIVOS} archivos siguen pendientes: no se pierde ninguno",
              f"({quedan})")

        # ── [3] Las demás fuentes del sitio se saltan ────────────────────────
        print("\n[3] Un perfil con tres fuentes del mismo sitio")
        summ, ev = await correr(
            tmp, [{"id": -1, "url": f"toy://no{i}", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}
                  for i in range(3)],
            "caso3",
        )
        # El scan de la fuente 1 no pide nada (iter_files no usa red), así que
        # todo lo que llegue al servidor son intentos de descarga.
        check(len(Handler.hits) <= ds._BLOCK_STREAK + 2,
              f"tres fuentes gastan {len(Handler.hits)} peticiones, "
              f"no {3 * N_ARCHIVOS}")
        saltadas = [e for e in ev if isinstance(e, ds.Log) and "se salta" in e.msg]
        check(len(saltadas) == 2, "las dos fuentes restantes se saltan",
              f"({len(saltadas)})")

        # ── [4] Una mezcla 50/50 no parece un bloqueo de sitio ───────────────
        print("\n[4] El servidor rechaza los pares y sirve los impares")
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toymix://a", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso4",
        )
        bloqueos = [e for e in ev if isinstance(e, ds.SiteBlocked)]
        check(not bloqueos, "NO bloquea: hay solo 4 rechazos en cada ventana de 8",
              f"({len(bloqueos)})")
        check(summ.downloaded == N_ARCHIVOS // 2,
              f"bajó los {N_ARCHIVOS // 2} impares", f"({summ.downloaded})")
        check(summ.errors == N_ARCHIVOS // 2,
              "y contó los pares como error", f"({summ.errors})")

        # ── [5] La pausa también sigue a un rechazo ──────────────────────────
        print("\n[5] Tres 403 genéricos con pausa post-archivo")
        Handler.hit_times.clear()
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toygap://x", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso5_gap",
        )
        tiempos = [t for path, t in Handler.hit_times
                   if path.startswith("/generic/")]
        separaciones = [b - a for a, b in zip(tiempos, tiempos[1:])]
        check(len(tiempos) == ToyGapTemplate.N,
              f"hizo los {ToyGapTemplate.N} intentos genéricos", f"({len(tiempos)})")
        check(all(dt >= ToyGapTemplate.post_download_delay * 0.9
                  for dt in separaciones),
              "cada 403 deja pasar la pausa antes del siguiente intento",
              f"({separaciones})")

        # ── [6] Los éxitos intercalados no esconden un rechazo dominante ────
        print("\n[6] Cinco 403 de siete, con dos éxitos intercalados")
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toywindow://x", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso6_window",
        )
        bloqueos = [e for e in ev if isinstance(e, ds.SiteBlocked)]
        check(len(bloqueos) == 1,
              "la ventana activa un único SiteBlocked", f"({len(bloqueos)})")
        check(bool(bloqueos) and bloqueos[0].streak == ds._BLOCK_STREAK,
              "el corte conserva el conteo de cinco rechazos")

        # ── [7] El techo de caudal, cronometrado ─────────────────────────
        print("\n[7] 6 archivos de 128 KiB con techo de 256 KiB/s")
        t0 = time.monotonic()
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toyrate://x", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso7_rate",
        )
        dt = time.monotonic() - t0
        traido = ToyRateTemplate.N * ToyRateTemplate.SIZE
        sobrante = traido - ToyRateTemplate.max_bytes_per_sec * 2   # el burst
        minimo = sobrante / ToyRateTemplate.max_bytes_per_sec
        check(summ.downloaded == ToyRateTemplate.N,
              f"bajaron los {ToyRateTemplate.N} archivos", f"({summ.downloaded})")
        check(dt >= minimo * 0.9,
              f"tardó {dt:.2f}s: el caudal se cumple (mínimo teórico {minimo:.2f}s)")
        check(dt < minimo + 5,
              f"y no se pasa de frenada ({dt:.2f}s)")

        # ── [8] El presupuesto del día ───────────────────────────────────────
        print("\n[8] 20 archivos de 1 KiB con 4 KiB de presupuesto diario")
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toybudget://x", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso8_budget",
        )
        bloqueos = [e for e in ev if isinstance(e, ds.SiteBlocked)]
        check(bool(bloqueos), "el presupuesto emite SiteBlocked")
        check(bool(bloqueos) and bloqueos[0].streak == 0,
              "con racha 0: no nos rechazaron, paramos nosotros")
        check(summ.downloaded < ToyBudgetTemplate.N,
              f"para antes de bajarlo todo ({summ.downloaded} de {ToyBudgetTemplate.N})")
        check(sb.spent_today("toybudget") >= ToyBudgetTemplate.daily_byte_budget,
              "lo gastado quedó anotado en el sidecar",
              f"({sb.spent_today('toybudget')} B)")

        # Segunda run el mismo día: ni una petición, el tope ya está puesto.
        Handler.hits.clear()
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toybudget://x", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso8_budget",
        )
        check(not Handler.hits,
              "una segunda sync el mismo día no gasta ni una petición",
              f"({len(Handler.hits)})")

        # ── [9] Límite diario global opcional ──────────────────────────────
        print("\n[9] Límite global de 4 KiB, desactivado por defecto")
        check(sb.spent_today(sb.GLOBAL_BUDGET_KEY) == 0,
              "desactivado no cuenta ni limita las descargas anteriores")
        global_cap = 4 * 1024
        global_cfg = toy_config(tmp).model_copy(update={
            "daily_limit_enabled": True,
            "daily_limit_gb": global_cap / 2**30,
        })
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toy://si", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso9_global",
            global_cfg,
        )
        bloqueos = [e for e in ev if isinstance(e, ds.SiteBlocked)]
        check(bool(bloqueos) and bloqueos[0].site == "global",
              "el tope combinado emite un bloqueo global")
        check(bool(bloqueos) and bloqueos[0].streak == 0,
              "el corte global no se confunde con un rechazo del servidor")
        check(summ.downloaded < N_ARCHIVOS,
              f"detiene archivos nuevos ({summ.downloaded} de {N_ARCHIVOS})")
        check(sb.spent_today(sb.GLOBAL_BUDGET_KEY) >= global_cap,
              "el total combinado queda registrado",
              f"({sb.spent_today(sb.GLOBAL_BUDGET_KEY)} B)")

        Handler.hits.clear()
        summ, ev = await correr(
            tmp, [{"id": -1, "url": "toy://si", "site": "toy",
                   "enabled": 1, "last_synced": None, "file_count": 0}],
            "caso9_global",
            global_cfg,
        )
        check(not Handler.hits,
              "otra sync no consulta ningún servidor con el tope agotado",
              f"({len(Handler.hits)})")

        # ── [10] Una fuente retirada se conserva, pero no se consulta ───────
        print("\n[10] Kemono queda fuera del circuito sin borrar su URL")
        summ, ev = await correr(
            tmp, [{"id": -1,
                   "url": "https://kemono.cr/patreon/user/30500811",
                   "site": "kemono", "enabled": 1,
                   "last_synced": None, "file_count": 0}],
            "caso7",
        )
        retiradas = [
            e for e in ev
            if isinstance(e, ds.Log) and "fuera de servicio" in e.msg
        ]
        check(not Handler.hits, "no abre ninguna conexión")
        check(bool(retiradas), "explica que la URL se conserva sin consultarla")
        check(summ.downloaded == 0 and summ.errors == 0,
              "omitir una fuente retirada no cuenta como error")

    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{_ok} ok, {_fail} fallo(s)")


if __name__ == "__main__":
    asyncio.run(run())
    sys.exit(1 if _fail else 0)
