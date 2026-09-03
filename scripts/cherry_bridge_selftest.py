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

Sin argumentos NO sale a la red: los siete primeros casos usan eventos
sintéticos, un perfil sin template y lecturas locales de `index.db`. `--scan` y
`--download` sí golpean la API del sitio y descargan de verdad.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

# El script vive en scripts/; la raíz del proyecto es el padre.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QEventLoop  # noqa: E402

from modules.cherry.logic.download_bridge import DownloadBridge  # noqa: E402
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
            "batch_info", "scan_progress", "scan_complete", "worker_start",
            "worker_progress", "worker_done", "worker_idle", "counters",
            "source_done", "unknown_event",
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
    print("\n[3] Los 13 DownloadEvent cruzan de hilo")
    import asyncio
    from third_party.cherry_dl.download_service import (
        BatchInfo, Cooldown, Counters, Log, ScanComplete, ScanProgress,
        SourceDone, SourceStarted, WorkerDone, WorkerIdle, WorkerProgress,
        WorkerStart, WorkersResolved,
    )

    samples = [
        Log("hola", "info"), WorkersResolved(3),
        SourceStarted("http://x", "artista", "kemono"), Cooldown(1.5),
        BatchInfo(10, 2), ScanProgress(25, 4), WorkerStart(0, "a.jpg"),
        WorkerProgress(0, 5, 10), WorkerDone(0, "a.jpg", "✓"),
        WorkerIdle(0), Counters(1, 2, 3, 4), SourceDone(7, 42),
        ScanComplete(9),
    ]
    check("son 13 tipos de evento", len(samples) == 13, f"({len(samples)})")

    rec.events.clear()
    rec.threads.clear()

    async def fire():
        for ev in samples:
            bridge._on_event(ev)
        return threading.get_ident()

    emitter_tid = runtime.run_sync(fire(), timeout=5)
    got_all = pump(app, lambda: len(rec.events) >= 13, timeout=5)

    check("los 13 llegan a la GUI", got_all, f"(llegaron {len(rec.events)})")
    check("se emitieron desde el hilo del loop", emitter_tid != MAIN_THREAD)
    check("se reciben en el hilo GUI (conexión queued)",
          rec.threads == {MAIN_THREAD}, f"(hilos {rec.threads})")
    faltan = {"log_line", "workers_resolved", "source_started", "cooldown",
              "batch_info", "scan_progress", "worker_start", "worker_progress",
              "worker_done", "worker_idle", "counters", "source_done",
              "scan_complete"} - set(rec.names())
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

    app = QCoreApplication(sys.argv)
    runtime = CherryRuntime()
    if not runtime.start():
        print("✗ el runtime no arrancó")
        return 2
    bridge = DownloadBridge(runtime)
    rec = Recorder(bridge)

    try:
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
            test_cancel_mechanics(app, runtime)
            test_auth_roundtrip(app, runtime, bridge, rec)
            test_runner_no_network(app, runtime, bridge, rec)
    finally:
        print("\n[fin] apagando el runtime")
        runtime.stop()
        check("el loop se apagó", not runtime.is_running)

    print(f"\n{_ok} ok, {_fail} fallo(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
