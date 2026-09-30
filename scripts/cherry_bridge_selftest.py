"""
Prueba de fuego del puente runtime de cherry-dl (A1.5 de TASKS.md).

Valida, sin GUI, que la costura QThread + event loop asyncio + señales Qt hace
lo que promete: que los eventos del motor cruzan de hilo y se entregan EN EL
HILO GUI, que `resolve_auth` puede esperar a un diálogo modal sin bloquear el
loop, y que cancelar mata la task sin dejar el runtime colgado.

Uso (siempre con el Python del venv):

    venv/Scripts/python.exe scripts/cherry_bridge_selftest.py            # sin red
    venv/Scripts/python.exe scripts/cherry_bridge_selftest.py --list     # perfiles
    venv/Scripts/python.exe scripts/cherry_bridge_selftest.py --scan 3   # TOCA LA RED
    venv/Scripts/python.exe scripts/cherry_bridge_selftest.py --download 3 [--cancel-after 20]

Sin argumentos usa eventos sintéticos y bases/configuración temporales; no lee
las bibliotecas del usuario. `--list` consulta el índice del usuario; `--scan` y
`--download` sí golpean la API del sitio y pueden descargar de verdad.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager, ExitStack
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

# El script vive en scripts/; la raíz del proyecto es el padre.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QEventLoop  # noqa: E402

from modules.cherry.logic.download_bridge import DownloadBridge  # noqa: E402
from modules.cherry.logic.download_queue import DownloadQueue  # noqa: E402
from modules.cherry.logic.runtime import CherryRuntime  # noqa: E402

# La consola de Windows es cp1252: sin esto, el primer ✓ mata el script (y los
# logs del motor vienen llenos de ✓ ⏸ ≡).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

MAIN_THREAD = threading.get_ident()

_ok = 0
_fail = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global _ok, _fail
    if condition:
        _ok += 1
        print(f"  ✓ {label}")
    else:
        _fail += 1
        print(f"  ✗ {label}  {detail}")
    return condition


def pump(app: QCoreApplication, predicate, timeout: float = 15.0) -> bool:
    """Bombea el bucle de eventos hasta que se cumpla `predicate`.

    Es lo que hace la GUI real: sin esto, una señal queued nunca se entrega.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.AllEvents, 20)
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Recorder:
    """Receptor de las señales del puente. Vive en el hilo GUI y anota, por
    cada evento, en qué hilo lo recibió — que es el meollo de A1.2."""

    def __init__(self, bridge: DownloadBridge) -> None:
        self.events: list[tuple[str, tuple]] = []
        self.threads: set[int] = set()
        self.auth_tokens: list[tuple[str, int]] = []
        self.summary = None
        self.error: str | None = None
        self.cancelled = False
        self.finished = False

        for name in (
            "log_line", "workers_resolved", "source_started", "cooldown",
            "batch_info", "file_progress", "scan_progress", "scan_complete", "worker_start",
            "worker_progress", "worker_done", "worker_idle", "counters",
            "source_done", "site_metrics", "quota_status", "unknown_event",
        ):
            getattr(bridge, name).connect(self._make_slot(name))

        bridge.auth_required.connect(self._on_auth)
        bridge.finished_signal.connect(self._on_finished)
        bridge.cancelled_signal.connect(self._on_cancelled)
        bridge.failed_signal.connect(self._on_failed)

    def _make_slot(self, name: str):
        def slot(*args):
            self.threads.add(threading.get_ident())
            self.events.append((name, args))
        return slot

    def _on_auth(self, site: str, token: int) -> None:
        self.threads.add(threading.get_ident())
        self.auth_tokens.append((site, token))

    def _on_finished(self, summary) -> None:
        self.threads.add(threading.get_ident())
        self.summary = summary
        self.finished = True

    def _on_cancelled(self) -> None:
        self.cancelled = True
        self.finished = True

    def _on_failed(self, msg: str) -> None:
        self.error = msg
        self.finished = True

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.events if n == name)


# ── casos sin red ───────────────────────────────────────────────────────────

def test_runtime(app, runtime) -> None:
    print("\n[1] Arranque del loop y guard anti doble-inicio")
    import asyncio

    check("start() deja el loop corriendo", runtime.is_running)
    check("start() es idempotente", runtime.start() and runtime.is_running)

    async def ping():
        await asyncio.sleep(0)
        return threading.get_ident()

    tid = runtime.run_sync(ping(), timeout=5)
    check("la corrutina corre en OTRO hilo", tid != MAIN_THREAD, f"(tid={tid})")


def test_index_read(app, runtime) -> list[dict]:
    print("\n[2] Lectura async de index.db desde el hilo GUI")
    from third_party.cherry_dl.config import INDEX_DB
    from third_party.cherry_dl.index import list_profiles

    if not INDEX_DB.exists():
        check("index.db existe", False, f"(no está en {INDEX_DB})")
        return []
    profiles = runtime.run_sync(list_profiles(INDEX_DB), timeout=20)
    check("list_profiles() devuelve filas", isinstance(profiles, list),
          f"(tipo {type(profiles).__name__})")
    print(f"      {len(profiles)} perfil(es) en {INDEX_DB}")
    return profiles


def test_event_bridge(app, runtime, bridge, rec) -> None:
    print("\n[3] Los 16 DownloadEvent cruzan de hilo")
    import asyncio
    from third_party.cherry_dl.download_service import (
        BatchInfo, Cooldown, Counters, FileProgress, Log, QuotaStatus, ScanComplete,
        ScanProgress, SiteMetrics, SourceDone, SourceStarted, WorkerDone, WorkerIdle,
        WorkerProgress, WorkerStart, WorkersResolved,
    )
    from third_party.cherry_dl.site_throttle import QuotaSnapshot

    samples = [
        Log("hola", "info"), WorkersResolved(3),
        SourceStarted("http://x", "artista", "kemono"), Cooldown(1.5),
        BatchInfo(10, 2), FileProgress(3, 10), ScanProgress(25, 4),
        WorkerStart(0, "a.jpg"),
        WorkerProgress(0, 5, 10), WorkerDone(0, "a.jpg", "✓"),
        WorkerIdle(0), Counters(1, 2, 3, 4), SourceDone(7, 42),
        ScanComplete(9), SiteMetrics("pixiv", {"files_per_minute": 4.0}),
        QuotaStatus(QuotaSnapshot(
            "pawchive", 20, 500, 1024, 25 * 1024**3, 1, 10_800, 900,
        ), 512),
    ]
    check("son 16 tipos de evento", len(samples) == 16, f"({len(samples)})")

    rec.events.clear()
    rec.threads.clear()

    async def fire():
        for ev in samples:
            bridge._on_event(ev)
        return threading.get_ident()

    emitter_tid = runtime.run_sync(fire(), timeout=5)
    got_all = pump(app, lambda: len(rec.events) >= 16, timeout=5)

    check("los 16 llegan a la GUI", got_all, f"(llegaron {len(rec.events)})")
    check("se emitieron desde el hilo del loop", emitter_tid != MAIN_THREAD)
    check("se reciben en el hilo GUI (conexión queued)",
          rec.threads == {MAIN_THREAD}, f"(hilos {rec.threads})")
    faltan = {"log_line", "workers_resolved", "source_started", "cooldown",
              "batch_info", "file_progress", "scan_progress", "worker_start", "worker_progress",
              "worker_done", "worker_idle", "counters", "source_done",
              "scan_complete", "site_metrics", "quota_status"} - set(rec.names())
    check("ninguno cae en unknown_event", not faltan and rec.count("unknown_event") == 0,
          f"(faltan {faltan})")


def test_progress_throttle(app, runtime, bridge, rec) -> None:
    print("\n[4] Estrangulado de WorkerProgress")
    from third_party.cherry_dl.download_service import WorkerProgress

    rec.events.clear()

    async def flood():
        for i in range(1, 401):
            bridge._on_event(WorkerProgress(1, i, 400))

    runtime.run_sync(flood(), timeout=10)
    pump(app, lambda: rec.count("worker_progress") > 0, timeout=3)
    n = rec.count("worker_progress")
    check("400 progresos no inundan la GUI", n < 50, f"(pasaron {n})")
    ultimo = [a for nm, a in rec.events if nm == "worker_progress"][-1]
    check("el 100 % siempre pasa", ultimo == (1, 400, 400), f"(último {ultimo})")


def test_large_progress(app, runtime, bridge, rec) -> None:
    """Los bytes conservan precisión al cruzar Qt, incluso por encima de 2 GiB."""
    from third_party.cherry_dl.download_service import WorkerProgress

    samples = [
        (20, 1, 2_379_725_966),
        (21, 2**31, 2**32 + 1),
        (22, 2**32 + 1, 2**32 + 1),
        (23, 2**31 + 1, 0),
    ]
    rec.events.clear()
    rec.threads.clear()

    async def fire():
        for sample in samples:
            bridge._on_event(WorkerProgress(*sample))

    runtime.run_sync(fire(), timeout=5)
    pump(app, lambda: rec.count("worker_progress") == len(samples), timeout=3)
    received = [args for name, args in rec.events if name == "worker_progress"]
    check("bytes >2 GiB llegan intactos, incluido 100 % y total desconocido",
          received == samples, f"({received})")
    check("progreso grande se recibe en el hilo GUI", rec.threads == {MAIN_THREAD})


def test_cancel_mechanics(app, runtime) -> None:
    print("\n[5] Cancelación de una task desde el hilo GUI")
    import asyncio

    holder: dict = {}

    async def make_task():
        async def sleeper():
            await asyncio.sleep(30)
        holder["task"] = asyncio.ensure_future(sleeper())
        return True

    runtime.run_sync(make_task(), timeout=5)
    task = holder["task"]
    check("cancel_task() encola la cancelación", runtime.cancel_task(task))
    ok = pump(app, lambda: task.done(), timeout=5)
    check("la task muere", ok and task.cancelled(), f"(done={task.done()})")
    check("cancelar dos veces no rompe", runtime.cancel_task(task) is False)


def test_shutdown_contract() -> None:
    print("\n[cierre] Descarga activa y descarga del módulo")
    runtime = CherryRuntime()
    check("el runtime auxiliar arranca", runtime.start())
    future = runtime.submit(asyncio.sleep(30))
    started = time.monotonic()
    runtime.stop()
    elapsed = time.monotonic() - started
    check("stop() cancela trabajo activo", future.cancelled() or future.done())
    check("stop() no deja el hilo vivo", not runtime.is_running)
    check("stop() vuelve dentro del límite", elapsed < 6.0, f"({elapsed:.2f}s)")

    from modules.cherry.module import CherryModule

    class Probe:
        def __init__(self):
            self.called = 0

        def shutdown(self):
            self.called += 1

    view = Probe()
    stopped = Probe()
    stopped.stop = stopped.shutdown
    CherryModule.on_unload(type("ModuleProbe", (), {
        "view": view, "runtime": stopped,
    })())
    check("on_unload() cancela primero desde la vista", view.called == 1)
    check("on_unload() no duplica el cierre del runtime", stopped.called == 0)


def test_auth_roundtrip(app, runtime, bridge, rec) -> None:
    print("\n[6] resolve_auth: worker espera al diálogo del hilo GUI")
    import asyncio

    rec.auth_tokens.clear()
    fut = runtime.submit(bridge._resolve_auth("patreon"))

    llego = pump(app, lambda: bool(rec.auth_tokens), timeout=5)
    check("auth_required llega a la GUI", llego, f"({rec.auth_tokens})")
    if not llego:
        fut.cancel()
        return
    site, token = rec.auth_tokens[0]
    check("el sitio es el correcto", site == "patreon", f"({site})")
    check("la corrutina sigue esperando", not fut.done())

    # Aquí, en la app real, se abriría el diálogo modal y esto sería su resultado.
    check("resolve_auth_result() acepta el token",
          bridge.resolve_auth_result(token, True))
    ok = pump(app, lambda: fut.done(), timeout=5)
    check("la corrutina recibe True", ok and fut.result() is True)
    check("token desconocido se rechaza",
          bridge.resolve_auth_result(9999, True) is False)


def test_pixiv_guided_login(app, runtime, bridge, rec) -> None:
    print("\n[6b] Pixiv OAuth: captura, persistencia y reanudación del puente")
    from third_party.cherry_dl.auth import browser_login, pixiv

    captured = {}
    original_capture = browser_login.capture_oauth_code
    original_request = pixiv._request_tokens
    original_save = pixiv.save_pixiv_tokens
    original_load = pixiv.load_pixiv_tokens
    original_restrict = pixiv.pixiv_x_restrict
    original_guided = pixiv.guided_login_pixiv

    async def fake_capture(url, **kwargs):
        captured["url"] = url
        return "synthetic-code"

    async def fake_request(fields):
        captured["fields"] = dict(fields)
        return {
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "_expires_at": 9999999999,
        }

    def fake_save(tokens):
        captured["saved"] = dict(tokens)

    browser_login.capture_oauth_code = fake_capture
    pixiv._request_tokens = fake_request
    pixiv.save_pixiv_tokens = fake_save
    pixiv.load_pixiv_tokens = lambda: None
    pixiv.pixiv_x_restrict = lambda _token: asyncio.sleep(0, result=1)
    try:
        tokens = runtime.run_sync(pixiv.guided_login_pixiv(), timeout=5)
    finally:
        browser_login.capture_oauth_code = original_capture
        pixiv._request_tokens = original_request
        pixiv.save_pixiv_tokens = original_save
        pixiv.load_pixiv_tokens = original_load
        pixiv.pixiv_x_restrict = original_restrict

    check("el login abre OAuth PKCE",
          captured["url"].startswith("https://app-api.pixiv.net/web/v1/login?"))
    check("intercambia el código capturado",
          captured["fields"].get("code") == "synthetic-code")
    check("el refresh_token se persiste",
          captured["saved"].get("refresh_token") == "synthetic-refresh")
    check("el login devuelve los tokens",
          tokens and tokens.get("refresh_token") == "synthetic-refresh")

    async def fake_guided(*, on_status=None):
        if on_status:
            on_status("OAuth sintético autorizado")
        return {"refresh_token": "bridge-refresh"}

    rec.auth_tokens.clear()
    pixiv.guided_login_pixiv = fake_guided
    waiting = runtime.submit(bridge._resolve_auth("pixiv"))
    try:
        arrived = pump(app, lambda: bool(rec.auth_tokens), timeout=5)
        check("Pixiv solicita auth en la GUI", arrived)
        if not arrived:
            waiting.cancel()
            return
        site, token = rec.auth_tokens[0]
        check("el token pertenece a Pixiv", site == "pixiv", f"({site})")
        check("run_site_login acepta Pixiv", bridge.run_site_login(site, token))
        done = pump(app, lambda: waiting.done(), timeout=5)
        check("la descarga se reanuda con sesión válida",
              done and waiting.result() is True)
    finally:
        pixiv.guided_login_pixiv = original_guided


def test_runner_no_network(app, runtime, bridge, rec) -> None:
    print("\n[7] run_profile_download end-to-end (perfil sin template, sin red)")
    rec.events.clear()
    rec.finished = False
    rec.summary = None

    # id=-1 no existe en index.db: el UPDATE final de last_checked afecta 0
    # filas, así que el estado de cherry-dl no se toca.
    fake = {
        "id": -1,
        "folder_path": str(Path(__file__).resolve().parent.parent / "cache" / "_selftest"),
        "urls": [{
            "id": -1, "url": "https://example.invalid/no-template",
            "site": "?", "enabled": 1, "last_synced": None, "file_count": 0,
        }],
    }
    check("start() arranca", bridge.start(fake, workers=2, scan_only=True))
    check("segundo start() se rechaza (guard)", bridge.start(fake) is False)

    done = pump(app, lambda: rec.finished, timeout=30)
    check("la descarga termina", done, f"(error={rec.error})")
    check("no hubo excepción", rec.error is None, f"({rec.error})")
    logs = [a[0] for n, a in rec.events if n == "log_line"]
    check("el motor avisó de la falta de template",
          any("template" in m.lower() for m in logs), f"({logs[:3]})")
    check("finished_signal trae el DownloadSummary",
          rec.summary is not None and getattr(rec.summary, "downloaded", None) == 0)
    check("el puente queda libre", not bridge.is_busy)


# ── casos con red ───────────────────────────────────────────────────────────

def run_live(app, runtime, bridge, rec, profile: dict, *, scan_only: bool,
             cancel_after: float | None) -> None:
    modo = "SCAN (sin descargar)" if scan_only else "DESCARGA REAL"
    print(f"\n[live] {modo} — perfil {profile['id']}: {profile.get('display_name')}")
    print(f"       carpeta: {profile.get('folder_path')}")

    def on_log(msg, level):
        print(f"       {msg}")

    bridge.log_line.connect(on_log)

    # Sin GUI no hay diálogo modal ni login guiado: se responde "no" para que
    # el motor siga (o aborte esa fuente) en vez de esperar para siempre. En
    # la app real, aquí es donde A3 abrirá la ventana de login.
    def on_auth(site, token):
        print(f"       ⚠ {site} pidió autenticación (token {token}) — "
              f"se responde NO: no hay sesión viva o expiró")
        bridge.resolve_auth_result(token, False)

    bridge.auth_required.connect(on_auth)

    t0 = time.monotonic()
    if not bridge.start(profile, workers=4, scan_only=scan_only):
        check("start() arranca", False)
        return
    check("start() arranca", True)

    cancelado = False
    while not rec.finished:
        app.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.01)
        if cancel_after and not cancelado and time.monotonic() - t0 > cancel_after:
            print(f"\n       ⏹ cancelando tras {cancel_after:.0f}s…")
            check("cancel() encola la cancelación", bridge.cancel())
            cancelado = True

    print()
    check("el hilo GUI recibió eventos", bool(rec.events), f"({len(rec.events)})")
    check("todos en el hilo GUI", rec.threads <= {MAIN_THREAD}, f"({rec.threads})")
    if cancelado:
        check("terminó como cancelada", rec.cancelled)
    else:
        check("terminó sin error", rec.error is None, f"({rec.error})")
        if rec.summary is not None:
            s = rec.summary
            print(f"       resumen: ↓{s.downloaded}  skip {s.skipped}  "
                  f"✗{s.errors}  ⏭{s.deferred}")
    check("el puente queda libre", not bridge.is_busy)


def _fake_profile(pid: int, name: str) -> dict:
    """Perfil sin template: recorre el servicio entero sin salir a la red.

    El id negativo no existe en index.db, así que el UPDATE final de
    last_checked afecta 0 filas y el estado de cherry-dl no se toca.
    """
    return {
        "id": pid,
        "display_name": name,
        "folder_path": str(Path(__file__).resolve().parent.parent / "cache" / "_selftest"),
        "urls": [{
            "id": pid, "url": "https://example.invalid/no-template",
            "site": "?", "enabled": 1, "last_synced": None, "file_count": 0,
        }],
    }


def test_queue(app, runtime, bridge, rec) -> None:
    print("\n[8] DownloadQueue: varios perfiles en fila (A3)")
    queue = DownloadQueue(bridge)
    seen: list[tuple[int, int, str]] = []
    done: list[object] = []
    ended: list[tuple] = []
    queue.profile_started.connect(lambda i, n, name: seen.append((i, n, name)))
    queue.profile_finished.connect(lambda prof, summ: ended.append((prof, summ)))
    queue.queue_finished.connect(done.append)

    # `params_for` es lo que hace que cada perfil baje SU filtro de tipos: se
    # anota lo que la cola pide para comprobar que se llamó por perfil y que
    # lo devuelto pisa el valor común.
    asked: list[str] = []

    def params_for(profile: dict) -> dict:
        asked.append(profile["display_name"])
        return {"ext_filter": {".jpg"} if profile["id"] == -1 else {".zip"}}

    check("start() arranca la cola",
          queue.start([_fake_profile(-1, "FALSO A"), _fake_profile(-2, "FALSO B")],
                      params_for=params_for,
                      workers=2, ext_filter=set(), exclude_mode=False,
                      force_full=False, scan_only=True))
    check("un segundo start() se rechaza", queue.start([_fake_profile(-3, "C")]) is False)

    ok = pump(app, lambda: bool(done), timeout=30)
    check("la cola termina sola", ok, f"(perfiles vistos: {len(seen)})")
    if not ok:
        queue.cancel()
        return
    totals = done[0]
    check("corrió los dos perfiles", totals.done == 2, f"({totals.done})")
    check("ninguno falló", not totals.failed, f"({totals.failed})")
    check("el orden se respeta",
          [n for _, _, n in seen] == ["FALSO A", "FALSO B"], f"({seen})")
    check("la posición se numera 1..n",
          [i for i, _, _ in seen] == [1, 2], f"({seen})")
    check("la cola queda libre", not queue.is_running)
    check("el puente queda libre tras la cola", not bridge.is_busy)
    check("params_for se consultó una vez por perfil",
          asked == ["FALSO A", "FALSO B"], f"({asked})")
    # A4: quien reacciona al fin de una descarga necesita el `folder_path` del
    # perfil, no solo su nombre.
    check("profile_finished emite el perfil entero",
          bool(ended) and all(isinstance(p, dict) and "folder_path" in p for p, _ in ended),
          f"({[type(p).__name__ for p, _ in ended]})")


def test_a5() -> None:
    """A5: ajustes, detección de duplicados y exclusiones — todo sin escribir."""
    print("\n[11] A5: ajustes y duplicados")
    from modules.cherry.logic import duplicates as dup
    from modules.cherry.logic import settings

    # ── ajustes ────────────────────────────────────────────────────────────
    cfg = settings.load()
    check("los ajustes traen las nueve claves",
          set(cfg) == {
              "download_dir", "workers", "timeout",
              "daily_limit_enabled", "daily_limit_gb",
              "pawchive_downloads_per_window",
              "pawchive_resume_headroom",
              "pawchive_gigabytes_per_window", "pawchive_window_hours",
          }, f"({sorted(cfg)})")
    check("workers es un entero >= 1",
          isinstance(cfg["workers"], int) and cfg["workers"] >= 1, f"({cfg['workers']})")
    check("timeout es un entero >= 5",
          isinstance(cfg["timeout"], int) and cfg["timeout"] >= 5, f"({cfg['timeout']})")
    check("el límite diario es opcional y tiene una cantidad válida",
          isinstance(cfg["daily_limit_enabled"], bool)
          and isinstance(cfg["daily_limit_gb"], float)
          and cfg["daily_limit_gb"] > 0,
          f"({cfg['daily_limit_enabled']=}, {cfg['daily_limit_gb']=})")
    check("Pawchive conserva la recomendación 500/3 como valores válidos",
          isinstance(cfg["pawchive_downloads_per_window"], int)
          and cfg["pawchive_downloads_per_window"] > 0
          and isinstance(cfg["pawchive_resume_headroom"], int)
          and 0 <= cfg["pawchive_resume_headroom"]
          < cfg["pawchive_downloads_per_window"]
          and isinstance(cfg["pawchive_gigabytes_per_window"], float)
          and cfg["pawchive_gigabytes_per_window"] > 0
          and isinstance(cfg["pawchive_window_hours"], float)
          and cfg["pawchive_window_hours"] > 0,
          f"({cfg['pawchive_downloads_per_window']=}, "
          f"{cfg['pawchive_gigabytes_per_window']=}, "
          f"{cfg['pawchive_window_hours']=})")
    check("la carpeta de descargas coincide con la del motor",
          settings.download_root() == Path(cfg["download_dir"]),
          f"({settings.download_root()} vs {cfg['download_dir']})")
    check("el config vive en ~/.cherry-dl, no en el repo",
          settings.config_file().name == "config.toml"
          and ".cherry-dl" in str(settings.config_file()),
          f"({settings.config_file()})")

    # ── duplicados ─────────────────────────────────────────────────────────
    pairs = asyncio.run(dup.find_pairs())
    check("find_pairs devuelve una lista", isinstance(pairs, list), f"({type(pairs).__name__})")
    tiers = {dup.TIER_SOURCE, dup.TIER_PROBABLE, dup.TIER_POSSIBLE}
    check("todo par trae un tier conocido",
          all(p["tier"] in tiers for p in pairs),
          f"({[p['tier'] for p in pairs if p['tier'] not in tiers]})")
    check("todo par trae los dos lados",
          all({"id_a", "name_a", "folder_a", "id_b", "name_b", "folder_b"} <= set(p)
              for p in pairs))
    check("un par nunca se empareja consigo mismo",
          all(p["id_a"] != p["id_b"] for p in pairs))
    check("no hay pares repetidos en los dos sentidos",
          len({(min(p["id_a"], p["id_b"]), max(p["id_a"], p["id_b"])) for p in pairs})
          == len(pairs))
    order = {dup.TIER_SOURCE: 0, dup.TIER_PROBABLE: 1, dup.TIER_POSSIBLE: 2}
    check("las certezas salen antes que las sospechas",
          [order[p["tier"]] for p in pairs] == sorted(order[p["tier"]] for p in pairs),
          f"({[p['tier'] for p in pairs]})")
    check("los umbrales son los del original",
          (dup.NAME_PROBABLE, dup.NAME_POSSIBLE) == (0.80, 0.60),
          f"({dup.NAME_PROBABLE}, {dup.NAME_POSSIBLE})")
    check("excluded_count no revienta",
          isinstance(asyncio.run(dup.excluded_count()), int))

    # Sin índice no se busca nada — y sobre todo, no se crea el archivo.
    import third_party.cherry_dl.config as _cfg
    real = _cfg.INDEX_DB
    ghost = Path(tempfile.gettempdir()) / "cherry_index_fantasma_selftest.db"
    ghost.unlink(missing_ok=True)
    _cfg.INDEX_DB = ghost
    try:
        check("sin índice, no hay pares", asyncio.run(dup.find_pairs()) == [])
        check("sin índice, no hay exclusiones", asyncio.run(dup.excluded_count()) == 0)
        check("buscar duplicados NO crea el índice", not ghost.exists(), f"({ghost})")
    finally:
        _cfg.INDEX_DB = real
        ghost.unlink(missing_ok=True)


def test_library_probe() -> None:
    """A4: el lector de `panopticon.db` y las consultas de "esto ya lo tengo"."""
    print("\n[10] Biblioteca: library_probe y library_check")
    from core import library_probe as lp
    from modules.cherry.logic import library_check as lc

    win = "G:" + chr(92) + "images" + chr(92) + "x"
    check("normalize deja barras hacia delante",
          lp.normalize(win) == "G:/images/x", f"({lp.normalize(win)})")

    watched = lp.watched_folders()
    if not watched:
        check("hay al menos una carpeta vigilada (se omite el resto)", False,
              "(biblioteca vacía: is_watched no se puede ejercitar)")
    else:
        base = watched[0]
        check("la carpeta vigilada se reconoce a sí misma", lp.is_watched(base))
        check("una subcarpeta cuenta como vigilada", lp.is_watched(base + "/loquesea"))
        check("una hermana con prefijo común NO cuenta",
              not lp.is_watched(base + "-otra"), f"({base}-otra)")
        check("una ruta de fuera no cuenta", not lp.is_watched("C:/definitivamente/fuera"))
    check("una ruta vacía no cuenta como vigilada", not lp.is_watched(""))

    empty = lp.artist_summary("__artista que no existe__")
    check("un artista desconocido da cero", empty["files"] == 0, f"({empty})")
    check("un nombre vacío da cero", lp.artist_summary("")["files"] == 0)

    # La conexión es `mode=ro`: ni un bug puede crear una biblioteca fantasma.
    ghost = Path(tempfile.gettempdir()) / "panopticon_fantasma_selftest.db"
    ghost.unlink(missing_ok=True)
    real_db_path = lp.db_path
    lp.db_path = lambda: ghost
    try:
        check("sin biblioteca, is_available es False", not lp.is_available())
        check("sin biblioteca, no hay carpetas vigiladas", lp.watched_folders() == [])
        check("sin biblioteca, el resumen viene vacío", lp.artist_summary("x")["files"] == 0)
        check("consultar NO crea el archivo", not ghost.exists(), f"({ghost})")
    finally:
        lp.db_path = real_db_path
        ghost.unlink(missing_ok=True)

    check("una URL vacía no busca nada", asyncio.run(lc.find_url("")) == [])
    check("una URL que no está en el índice no da falsos positivos",
          asyncio.run(lc.find_url("https://example.invalid/no-existe")) == [])
    check("un artista vacío da cero", asyncio.run(lc.find_artist(""))["files"] == 0)


def test_ext_filter() -> None:
    """El filtro de tipos: formato del motor, ida y vuelta, y el caso legacy."""
    print("\n[9] Filtro de tipos por perfil")
    from modules.cherry.logic import ext_filter as ef

    gids = [gid for gid, _label, _exts in ef.groups()]
    check("los grupos vienen del motor",
          {"images", "anim", "video", "zip"} <= set(gids), f"({gids})")
    check("cada grupo tiene traducción",
          all(g in ef.GROUP_KEYS for g in gids),
          f"(sin clave: {[g for g in gids if g not in ef.GROUP_KEYS]})")
    project_exts = next(exts for gid, _label, exts in ef.groups() if gid == "project")
    check("proyectos incluye PSD/PSB/CLIP/LIP",
          {"psd", "psb", "clip", "lip"} <= set(project_exts), f"({project_exts})")

    enc = ef.encode(["images", "zip"], "psd, clip")
    groups, custom = ef.decode(enc)
    check("ida y vuelta conserva los grupos", groups == {"images", "zip"}, f"({groups})")
    check("ida y vuelta conserva el campo extra", custom == "psd, clip", f"({custom!r})")

    eff = ef.effective(enc)
    check("las extensiones salen con punto", all(e.startswith(".") for e in eff))
    check("el campo extra entra en el filtro", {".psd", ".clip"} <= eff)
    check("los grupos entran en el filtro", {".jpg", ".zip"} <= eff)
    check("un grupo sin marcar no entra", ".mp4" not in eff)

    check("vacío significa TODO, no nada", ef.effective("") == set())
    check("un ext_filter legacy sigue funcionando",
          ef.effective(".jpg,.png") == {".jpg", ".png"})
    check("un legacy se muestra como texto extra, no como grupos",
          ef.decode(".jpg,.png") == (set(), ".jpg,.png"))
    check("un JSON corrupto no revienta la vista",
          ef.decode("{roto") == (set(), "{roto"))


@contextmanager
def temporary_state():
    from core.paths import ProjectPaths
    from third_party.cherry_dl import config, download_service
    from third_party.cherry_dl.index import init_index, create_profile

    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder)
        cherry = root / '.cherry-dl'
        cherry.mkdir()
        for name, value in {'CHERRY_DIR': cherry, 'INDEX_DB': cherry / 'index.db',
                            'CONFIG_FILE': cherry / 'config.toml',
                            'SESSION_FILE': cherry / 'session.json'}.items():
            stack.enter_context(patch.object(config, name, value))
        stack.enter_context(patch.object(download_service, 'INDEX_DB', config.INDEX_DB))
        stack.enter_context(patch.object(ProjectPaths, 'root', return_value=root))
        config.save_config(config.UserConfig(download_dir=str(root / 'downloads')))
        asyncio.run(init_index(config.INDEX_DB))
        asyncio.run(create_profile(config.INDEX_DB, 'Fixture', root / 'downloads/Fixture', 'fixture'))
        with closing(sqlite3.connect(root / 'panopticon.db')) as db, db:
            db.execute('CREATE TABLE watched_folders(path TEXT)')
            db.execute('INSERT INTO watched_folders VALUES(?)', ((root / 'collection').as_posix(),))
            db.execute('CREATE TABLE files(path TEXT, artist TEXT, origin_site TEXT)')
        yield


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="listar perfiles y salir")
    ap.add_argument("--scan", type=int, metavar="ID",
                    help="scan_only de un perfil (TOCA LA RED)")
    ap.add_argument("--download", type=int, metavar="ID",
                    help="descarga real de un perfil (TOCA LA RED)")
    ap.add_argument("--cancel-after", type=float, metavar="SEG",
                    help="cancelar tras N segundos (prueba de A1.4 en vivo)")
    args = ap.parse_args()

    if args.scan is None and args.download is None and not args.list:
        with temporary_state():
            return run_tests(args)
    return run_tests(args)


def run_tests(args) -> int:

    app = QCoreApplication(sys.argv)
    runtime = CherryRuntime()
    if not runtime.start():
        print("✗ el runtime no arrancó")
        return 2
    bridge = DownloadBridge(runtime)
    rec = Recorder(bridge)

    try:
        test_ext_filter()
        test_library_probe()
        test_a5()
        test_runtime(app, runtime)
        profiles = test_index_read(app, runtime)

        if args.list:
            for p in profiles:
                print(f"  {p['id']:>4}  {p.get('display_name', '?'):<28} "
                      f"{p.get('primary_site', '?'):<8} "
                      f"{p.get('url_count', 0)} url(s)  "
                      f"último: {(p.get('last_checked') or '—')[:16]}")
            return 0

        if args.scan is not None or args.download is not None:
            pid = args.scan if args.scan is not None else args.download
            from third_party.cherry_dl.config import INDEX_DB
            from third_party.cherry_dl.index import get_profile
            profile = runtime.run_sync(get_profile(INDEX_DB, pid), timeout=20)
            if not profile:
                print(f"✗ no existe el perfil {pid}")
                return 2
            run_live(app, runtime, bridge, rec, profile,
                     scan_only=args.scan is not None,
                     cancel_after=args.cancel_after)
        else:
            test_event_bridge(app, runtime, bridge, rec)
            test_progress_throttle(app, runtime, bridge, rec)
            test_large_progress(app, runtime, bridge, rec)
            test_cancel_mechanics(app, runtime)
            test_auth_roundtrip(app, runtime, bridge, rec)
            test_pixiv_guided_login(app, runtime, bridge, rec)
            test_runner_no_network(app, runtime, bridge, rec)
            test_queue(app, runtime, bridge, rec)
    finally:
        print("\n[fin] apagando el runtime")
        runtime.stop()
        check("el loop se apagó", not runtime.is_running)

    if args.scan is None and args.download is None and not args.list:
        test_shutdown_contract()

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
