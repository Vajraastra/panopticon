"""Regresiones de ciclo de vida: backpressure, standby y parciales."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "third_party")]

from cherry_dl.browser_transport import BrowserDownload, BrowserImageTransport
from cherry_dl.config import NetworkConfig, UserConfig
from cherry_dl.download_service import (
    _enqueue_with_backpressure,
    _gather_transfer_tasks,
)
from cherry_dl.engine import DownloadEngine

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

ok = fail = 0


def check(condition: bool, label: str) -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}")


async def test_backpressure() -> None:
    print("[1] Una cola llena espera al worker sin matar al productor")
    queue = asyncio.Queue(maxsize=1)
    await queue.put("primero")
    pending = asyncio.create_task(_enqueue_with_backpressure(queue, "segundo"))
    await asyncio.sleep(0.03)
    check(not pending.done(), "el productor sigue vivo mientras el worker está ocupado")
    check(await queue.get() == "primero", "el worker consume el elemento anterior")
    await asyncio.wait_for(pending, timeout=0.2)
    check(await queue.get() == "segundo", "el productor continúa cuando vuelve a haber espacio")


async def test_task_group_failure() -> None:
    print("\n[2] Un fallo inesperado no deja compañeros esperando para siempre")
    cancelled = asyncio.Event()

    async def waiting_worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def broken_worker() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("fallo simulado")

    tasks = [
        asyncio.create_task(waiting_worker()),
        asyncio.create_task(broken_worker()),
    ]
    raised = False
    try:
        await asyncio.wait_for(_gather_transfer_tasks(tasks), timeout=0.2)
    except RuntimeError:
        raised = True
    check(raised, "el fallo se propaga al puente")
    check(cancelled.is_set(), "el worker bloqueado se cancela y el lote puede avanzar")


async def test_standby_restart() -> None:
    print("\n[3] Una sesión CDP muerta durante standby se reemplaza")
    transport = BrowserImageTransport("https://origin.invalid", "https://file.invalid/a")

    class DeadTask:
        def done(self):
            return True

    class DeadProcess:
        returncode = 1

    class LiveTask:
        def done(self):
            return False

    class LiveProcess:
        returncode = None

    transport._ws = object()
    transport._recv_task = DeadTask()
    transport._process = DeadProcess()
    transport._configured_rate = 0.0
    calls: list[str] = []

    async def fake_close() -> None:
        calls.append("close")
        transport._ws = None

    async def fake_launch() -> None:
        calls.append("launch")
        transport._ws = object()
        transport._recv_task = LiveTask()
        transport._process = LiveProcess()

    transport.close = fake_close
    transport._launch = fake_launch
    await transport.start(0)
    check(calls == ["close", "launch"], "cierra el CDP obsoleto y lanza uno limpio")


async def test_partial_invalidates_browser() -> None:
    print("\n[4] Un archivo parcial no contamina al siguiente worker")

    class PartialTransport:
        def __init__(self):
            self.closed = False

        async def start(self, _rate):
            return None

        async def download(self, *_args, **_kwargs):
            return BrowserDownload(
                error="respuesta incompleta",
                status=206,
                failure_code="http_rejected",
            )

        async def close(self):
            self.closed = True

    config = UserConfig(
        workers=1,
        timeout=5,
        network=NetworkConfig(delay_min=0, delay_max=0, stall_timeout=10),
    )
    target = Path(tempfile.mkdtemp(prefix="cherry_lifecycle_"))
    try:
        async with DownloadEngine(config, workers=1) as engine:
            partial = PartialTransport()
            engine._browser_transport = partial
            engine._browser_key = ("https://origin.invalid", "file.invalid")
            result = await engine.download(
                "https://file.invalid/a", target, "partial.bin",
                transport="browser_image", browser_origin="https://origin.invalid",
            )
            check(not result.ok, "el parcial se reporta como fallo")
            check(partial.closed, "se cierra la sesión que produjo el parcial")
            check(engine._browser_transport is None,
                  "el siguiente archivo usará una sesión nueva")
    finally:
        shutil.rmtree(target, ignore_errors=True)


async def main() -> int:
    await test_backpressure()
    await test_task_group_failure()
    await test_standby_restart()
    await test_partial_invalidates_browser()
    print(f"\n{ok} ok, {fail} fallo(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
