"""Pruebas sin red de la cuota móvil y el cortacircuitos de Cherry-DL."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.site_throttle import SiteThrottle, ThrottleDeferred  # noqa: E402
from cherry_dl.download_service import (  # noqa: E402
    Log,
    _confirm_failed_probe_once,
)
from cherry_dl.engine import DownloadResult, ErrorKind  # noqa: E402


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
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


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make(
    path: Path, fake: FakeTime, *, limit: int = 300, byte_limit: int = 0,
) -> SiteThrottle:
    return SiteThrottle(
        "pawchive", limit=limit, byte_limit=byte_limit,
        window_seconds=10_800,
        failure_threshold=5, failure_window_seconds=120,
        cooldowns=(900, 1800, 3600, 10_800), path=path,
        clock=fake.clock, sleeper=fake.sleep,
    )


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cherry_throttle_"))
    state = tmp / "throttle.json"
    fake = FakeTime()
    notices: list[tuple[str, float]] = []
    try:
        print("[1] Cuota móvil exacta y persistente")
        gate = make(state, fake, limit=3)
        permits = [await gate.acquire(notices.append) for _ in range(3)]
        for permit in permits:
            await gate.finish(permit, success=True)
        fourth = await gate.acquire(lambda kind, secs: notices.append((kind, secs)))
        check(fake.sleeps == [10_800], "la cuarta espera hasta vencer la ventana")
        check(notices[-1][0] == "quota_count",
              "la espera se identifica como cuota de archivos")
        await gate.finish(fourth, success=False)

        persisted = json.loads(state.read_text(encoding="utf-8"))
        check(len(persisted["pawchive"]["successes"]) == 0,
              "al vencer la ventana se podan los éxitos antiguos")

        reserve_state = tmp / "reservations.json"
        reserve_gate = make(reserve_state, fake, limit=1)
        first = await reserve_gate.acquire()
        waiting = asyncio.create_task(reserve_gate.acquire())
        await asyncio.sleep(0)
        check(not waiting.done(), "una reserva en vuelo ocupa el último cupo")
        await reserve_gate.finish(first, success=False)
        second = await waiting
        check(second.active, "un intento fallido devuelve el cupo al siguiente worker")
        await reserve_gate.abandon(second)

        print("\n[2] La primera cuota entre archivos o bytes detiene la ventana")
        byte_state = tmp / "bytes.json"
        byte_gate = make(byte_state, fake, limit=50, byte_limit=100)
        first = await byte_gate.acquire(expected_bytes=60)
        await byte_gate.finish(first, success=True, transferred_bytes=60)
        second = await byte_gate.acquire(
            lambda kind, secs: notices.append((kind, secs)), expected_bytes=50,
        )
        check(fake.sleeps[-1] == 10_800,
              "el tamaño anunciado espera antes de superar los bytes")
        check(notices[-1][0] == "quota_bytes",
              "la espera distingue la cuota de bytes")
        await byte_gate.finish(second, success=True, transferred_bytes=50)
        persisted = json.loads(byte_state.read_text(encoding="utf-8"))
        check(persisted["pawchive"]["transfers"][-1]["bytes"] == 50,
              "el tamaño real queda persistido")

        reserved_state = tmp / "byte-reservations.json"
        reserved_gate = make(reserved_state, fake, limit=50, byte_limit=100)
        large = await reserved_gate.acquire(expected_bytes=70)
        waiting_bytes = asyncio.create_task(
            reserved_gate.acquire(expected_bytes=40)
        )
        await asyncio.sleep(0)
        check(not waiting_bytes.done(),
              "dos workers no pueden reservar juntos más bytes que el tope")
        await reserved_gate.finish(large, success=False)
        released = await waiting_bytes
        check(released.reserved_bytes == 40,
              "un fallo libera la reserva de bytes")
        await reserved_gate.abandon(released)

        batch_state = tmp / "batch-bytes.json"
        batch_gate = make(batch_state, fake, limit=50, byte_limit=100)
        completed = await batch_gate.acquire(expected_bytes=100)
        await batch_gate.finish(
            completed, success=True, transferred_bytes=100,
        )
        before = list(fake.sleeps)
        try:
            await batch_gate.acquire(expected_bytes=1, wait=False)
            deferred = None
        except ThrottleDeferred as exc:
            deferred = exc
        check(deferred is not None and deferred.kind == "quota_bytes",
              "el batch recibe un diferido al alcanzar los GB")
        check(fake.sleeps == before,
              "el batch continúa sin dormir durante la ventana")

        print("\n[3] Sólo la firma específica arma la racha")
        state.unlink(missing_ok=True)
        fake.now += 1
        gate = make(state, fake)
        for _ in range(4):
            permit = await gate.acquire()
            trip = await gate.finish(permit, success=False, throttle_failure=True)
            check(trip is None, "un fallo aislado aún no abre el circuito")
        permit = await gate.acquire()
        await gate.finish(permit, success=False, throttle_failure=False)
        for _ in range(4):
            permit = await gate.acquire()
            trip = await gate.finish(permit, success=False, throttle_failure=True)
        check(trip is None, "un 404/error distinto rompe la racha")

        print("\n[4] Cinco fallos, sonda única y espera progresiva")
        permit = await gate.acquire()
        trip = await gate.finish(permit, success=False, throttle_failure=True)
        check(trip is not None and trip.seconds == 900,
              "el quinto fallo abre 15 minutos")
        probe = await gate.acquire(lambda kind, secs: notices.append((kind, secs)))
        check(probe.probe and fake.sleeps[-1] == 900,
              "tras enfriar sólo concede una sonda")
        trip = await gate.finish(probe, success=False, throttle_failure=True)
        check(trip is not None and trip.seconds == 1800,
              "una sonda fallida sube a 30 minutos")
        probe = await gate.acquire()
        await gate.finish(probe, success=True)
        normal = await gate.acquire()
        check(not normal.probe, "una sonda exitosa cierra el circuito")
        await gate.abandon(normal)

        print("\n[4b] Retirar la firma limpia sólo el circuito heredado")
        legacy_state = tmp / "legacy-circuit.json"
        legacy_state.write_text(json.dumps({"pawchive": {
            "successes": [fake.now],
            "transfers": [{"at": fake.now, "bytes": 1234}],
            "failure_streak": 0,
            "last_failure": fake.now,
            "circuit_open": True,
            "cooldown_until": fake.now + 10_800,
            "cooldown_level": 4,
        }}), encoding="utf-8")
        legacy_gate = make(legacy_state, fake)
        await legacy_gate.clear_circuit()
        persisted = json.loads(legacy_state.read_text(encoding="utf-8"))["pawchive"]
        check(not persisted["circuit_open"] and persisted["cooldown_until"] == 0,
              "el enfriamiento obsoleto desaparece de inmediato")
        check(len(persisted["successes"]) == 1
              and persisted["transfers"][0]["bytes"] == 1234,
              "la ventana de archivos y bytes permanece intacta")

        print("\n[5] Un éxito anterior no deshace un corte nuevo")
        stale_state = tmp / "stale-success.json"
        stale_gate = make(stale_state, fake)
        in_flight = [await stale_gate.acquire() for _ in range(6)]
        for permit in in_flight[:5]:
            trip = await stale_gate.finish(
                permit, success=False, throttle_failure=True,
            )
        check(trip is not None, "cinco fallos abren el circuito")
        await stale_gate.finish(in_flight[5], success=True)
        persisted = json.loads(stale_state.read_text(encoding="utf-8"))
        check(persisted["pawchive"]["circuit_open"],
              "un éxito que ya estaba en vuelo no lo cierra")
        probe = await stale_gate.acquire()
        check(probe.probe, "después del enfriamiento sigue exigiendo una sonda")
        await stale_gate.finish(probe, success=True)

        print("\n[6] El estado sobrevive a otra instancia")
        reloaded = make(state, fake)
        permit = await reloaded.acquire()
        check(not permit.probe, "la instancia nueva lee un circuito cerrado")
        await reloaded.abandon(permit)

        print("\n[7] Una sonda CDP Failed exige confirmación independiente")
        events = []
        retry_calls = 0

        async def retry_ok():
            nonlocal retry_calls
            retry_calls += 1
            return DownloadResult("https://file.test/ok", "ok.jpg")

        failed = DownloadResult(
            "https://file.test/fail", "fail.jpg",
            error="sin estado HTTP · reason=Failed",
            error_kind=ErrorKind.NETWORK,
            failure_code="no_http_failed",
        )
        probe = type("Probe", (), {"probe": True})()
        confirmed = await _confirm_failed_probe_once(
            probe, failed, retry_ok, events.append,
        )
        check(confirmed.ok and retry_calls == 1,
              "la sonda se repite una sola vez y acepta el segundo resultado")
        check(any(isinstance(event, Log) for event in events),
              "la confirmación queda visible en el log")

        normal = type("Permit", (), {"probe": False})()
        unchanged = await _confirm_failed_probe_once(
            normal, failed, retry_ok, events.append,
        )
        check(unchanged is failed and retry_calls == 1,
              "un fallo ordinario no duplica todas las descargas")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{ok} ok, {fail} fallo(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
