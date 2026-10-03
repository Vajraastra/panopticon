"""Cuota móvil y cortacircuitos persistente para descargas por sitio.

La cuota cuenta archivos y bytes de transferencias terminadas, no intentos.
Cada worker reserva un cupo y el tamaño anunciado antes de pedir para que la
concurrencia no pueda cruzar el límite. El
cortacircuitos abre sólo ante una firma explícita elegida por el template y,
tras el enfriamiento, deja pasar una única sonda.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import CHERRY_DIR


THROTTLE_FILE = CHERRY_DIR / "site_throttle.json"


@dataclass
class Permit:
    probe: bool = False
    active: bool = True
    reserved_bytes: int = 0


@dataclass(frozen=True)
class CircuitTrip:
    seconds: float
    level: int


@dataclass(frozen=True)
class QuotaSnapshot:
    """Lectura puntual de una ventana móvil, incluida su cuota en vuelo."""

    site: str
    used_files: int
    file_limit: int
    used_bytes: int
    byte_limit: int
    active_files: int
    window_seconds: float
    next_release_seconds: float
    recovery_active: bool = False
    resume_at_files: int = 0


class ThrottleDeferred(Exception):
    """Una ejecución batch debe continuar en vez de dormir por esta cuota."""

    def __init__(self, kind: str, seconds: float) -> None:
        super().__init__(kind)
        self.kind = kind
        self.seconds = max(0.0, float(seconds))


WaitFn = Callable[[str, float], None]


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        tmp.replace(path)
    except OSError:
        pass


class SiteThrottle:
    """Coordina workers de un sitio dentro de una ventana móvil persistente."""

    def __init__(
        self,
        site: str,
        *,
        limit: int,
        byte_limit: int = 0,
        resume_headroom: int = 0,
        window_seconds: float,
        failure_threshold: int,
        failure_window_seconds: float,
        cooldowns: tuple[float, ...],
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.site = site
        self.limit = max(0, int(limit))
        self.byte_limit = max(0, int(byte_limit))
        self.resume_headroom = min(
            max(0, int(resume_headroom)), max(0, self.limit - 1),
        )
        self.window_seconds = max(0.0, float(window_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.failure_window_seconds = max(0.0, float(failure_window_seconds))
        self.cooldowns = tuple(max(0.0, float(v)) for v in cooldowns) or (900.0,)
        self.path = path or THROTTLE_FILE
        self._clock = clock
        self._sleep = sleeper
        self._condition = asyncio.Condition()
        self._changed = asyncio.Event()
        self._reservations = 0
        self._reserved_bytes = 0
        self._probe_in_flight = False
        self._last_notice: tuple[str, int] | None = None

    def _read(self) -> tuple[dict, dict]:
        data = _load(self.path)
        state = data.get(self.site)
        if not isinstance(state, dict):
            state = {}
            data[self.site] = state
        state.setdefault("successes", [])
        state.setdefault("transfers", [])
        state.setdefault("failure_streak", 0)
        state.setdefault("last_failure", 0.0)
        state.setdefault("circuit_open", False)
        state.setdefault("cooldown_until", 0.0)
        state.setdefault("cooldown_level", 0)
        state.setdefault("count_recovery", False)
        return data, state

    def _prune(self, state: dict, now: float) -> None:
        floor = now - self.window_seconds
        state["successes"] = [
            float(v) for v in state.get("successes", [])
            if isinstance(v, (int, float)) and float(v) > floor
        ]
        state["transfers"] = [
            {"at": float(v["at"]), "bytes": max(0, int(v["bytes"]))}
            for v in state.get("transfers", [])
            if isinstance(v, dict)
            and isinstance(v.get("at"), (int, float))
            and isinstance(v.get("bytes"), (int, float))
            and float(v["at"]) > floor
        ]

    def _count_quota_wait(
        self, state: dict, now: float,
    ) -> tuple[str, float | None] | None:
        """Aplica histéresis al límite de archivos.

        Una vez alcanzado el techo no basta con que venza el primer archivo:
        la marca persistente se conserva hasta recuperar ``resume_headroom``
        slots reales. Con cero se mantiene el comportamiento anterior.
        """
        if not self.limit:
            state["count_recovery"] = False
            return None

        used = len(state["successes"]) + self._reservations
        if not self.resume_headroom:
            state["count_recovery"] = False
            if used < self.limit:
                return None
            until = (
                min(state["successes"]) + self.window_seconds
                if state["successes"] else None
            )
            return "quota_count", until

        if used >= self.limit:
            state["count_recovery"] = True
        if not state.get("count_recovery"):
            return None

        resume_at = self.limit - self.resume_headroom
        releases_needed = used - resume_at
        if releases_needed <= 0:
            state["count_recovery"] = False
            return None

        successes = sorted(float(value) for value in state["successes"])
        if releases_needed > len(successes):
            return "quota_count_recovery", None
        until = successes[releases_needed - 1] + self.window_seconds
        return "quota_count_recovery", until

    def _quota_wait(
        self, state: dict, now: float, expected_bytes: int,
    ) -> tuple[str, float | None] | None:
        """Devuelve qué cuota bloquea y cuándo podrá abrirse.

        Las reservas sin fecha sólo se liberan al terminar otro worker; en ese
        caso ``None`` obliga a esperar la condición en vez de dormir a ciegas.
        """
        waits: list[tuple[str, float | None]] = []
        count_wait = self._count_quota_wait(state, now)
        if count_wait is not None:
            waits.append(count_wait)

        if self.byte_limit:
            transfers = sorted(state["transfers"], key=lambda item: item["at"])
            used = sum(item["bytes"] for item in transfers)
            projected = used + self._reserved_bytes
            blocked = projected >= self.byte_limit or (
                expected_bytes > 0
                and projected + expected_bytes > self.byte_limit
            )
            if blocked:
                until = None
                remaining = used
                for item in transfers:
                    remaining -= item["bytes"]
                    projected_after = remaining + self._reserved_bytes
                    if (projected_after < self.byte_limit
                            and (expected_bytes <= 0
                                 or projected_after + expected_bytes
                                 <= self.byte_limit)):
                        until = item["at"] + self.window_seconds
                        break
                waits.append(("quota_bytes", until))

        if not waits:
            return None
        if any(until is None for _, until in waits):
            kind = next(kind for kind, until in waits if until is None)
            return kind, None
        return max(waits, key=lambda item: float(item[1]))

    def _notice(self, callback: WaitFn | None, kind: str, until: float, now: float) -> None:
        key = (kind, int(until))
        if callback is not None and key != self._last_notice:
            self._last_notice = key
            callback(kind, max(0.0, until - now))

    def snapshot(self) -> QuotaSnapshot:
        """Devuelve el consumo vigente sin alterar la cuota persistida.

        ``used_bytes`` contiene sólo transferencias terminadas: los bytes que
        todavía viajan se añaden en el evento de progreso del servicio. Esto
        mantiene el medidor auditable contra el JSON y evita contar dos veces
        un archivo cuando pasa de en vuelo a terminado.
        """
        _data, state = self._read()
        now = self._clock()
        self._prune(state, now)
        count_wait = self._count_quota_wait(state, now)
        expirations = [
            float(value) + self.window_seconds
            for value in state["successes"]
        ]
        return QuotaSnapshot(
            site=self.site,
            used_files=len(state["successes"]),
            file_limit=self.limit,
            used_bytes=sum(item["bytes"] for item in state["transfers"]),
            byte_limit=self.byte_limit,
            active_files=self._reservations,
            window_seconds=self.window_seconds,
            next_release_seconds=(
                max(0.0, float(count_wait[1]) - now)
                if count_wait is not None and count_wait[1] is not None
                else (max(0.0, min(expirations) - now) if expirations else 0.0)
            ),
            recovery_active=bool(state.get("count_recovery")),
            resume_at_files=max(0, self.limit - self.resume_headroom),
        )

    async def clear_circuit(self) -> None:
        """Retira un circuito heredado sin alterar la cuota de archivos/bytes."""
        async with self._condition:
            data, state = self._read()
            state["failure_streak"] = 0
            state["last_failure"] = 0.0
            state["circuit_open"] = False
            state["cooldown_until"] = 0.0
            state["cooldown_level"] = 0
            self._probe_in_flight = False
            self._last_notice = None
            _write(self.path, data)
            self._notify_change()

    def _notify_change(self) -> None:
        """Wake indefinite and timed waits; caller holds the condition lock."""
        self._condition.notify_all()
        self._changed.set()
        self._changed = asyncio.Event()

    async def _wait_for_change(self, changed: asyncio.Event, seconds: float) -> None:
        # Subscribe under the admission lock, before another worker can release.
        # Keep the injected clock/sleeper contract used by deterministic tests.
        if changed.is_set():
            return
        timer = asyncio.create_task(self._sleep(seconds))
        notification = asyncio.create_task(changed.wait())
        try:
            done, _ = await asyncio.wait(
                (timer, notification), return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                task.result()
        finally:
            for task in (timer, notification):
                if not task.done():
                    task.cancel()
            await asyncio.gather(timer, notification, return_exceptions=True)

    def _bytes_to_reserve(self, state: dict, expected: int) -> int:
        # Sin tamaño anunciado, reservar el espacio libre y validar el cuerpo
        # HTTP contra esa reserva antes de publicarlo.
        if expected or not self.byte_limit:
            return expected
        return max(0, self.byte_limit
                   - sum(v["bytes"] for v in state["transfers"])
                   - self._reserved_bytes)

    async def acquire(
        self, on_wait: WaitFn | None = None, *, expected_bytes: int | None = None,
        wait: bool = True,
    ) -> Permit:
        """Espera lo necesario y reserva un intento; cancelable con la task."""
        reserved_bytes = max(0, int(expected_bytes or 0))
        if self.byte_limit and reserved_bytes > self.byte_limit:
            raise ValueError("El archivo supera la cuota completa de bytes")
        while True:
            wait_for: float | None = None
            wait_kind = ""
            wait_until = 0.0
            async with self._condition:
                data, state = self._read()
                now = self._clock()
                self._prune(state, now)

                if state.get("circuit_open"):
                    wait_until = float(state.get("cooldown_until", 0.0))
                    if wait_until > now:
                        wait_for = wait_until - now
                        wait_kind = "circuit"
                    else:
                        quota = self._quota_wait(state, now, reserved_bytes)
                        if quota is not None:
                            wait_kind, wait_until_value = quota
                            if wait_until_value is None:
                                await self._condition.wait()
                                continue
                            wait_until = wait_until_value
                            wait_for = max(0.01, wait_until - now)
                        elif not self._probe_in_flight:
                            self._probe_in_flight = True
                            self._reservations += 1
                            reserved_bytes = self._bytes_to_reserve(state, reserved_bytes)
                            self._reserved_bytes += reserved_bytes
                            _write(self.path, data)
                            return Permit(probe=True, reserved_bytes=reserved_bytes)
                        else:
                            await self._condition.wait()
                            continue
                else:
                    quota = self._quota_wait(state, now, reserved_bytes)
                    if quota is not None:
                        wait_kind, wait_until_value = quota
                        if wait_until_value is None:
                            await self._condition.wait()
                            continue
                        wait_until = wait_until_value
                        wait_for = max(0.01, wait_until - now)
                    else:
                        self._reservations += 1
                        reserved_bytes = self._bytes_to_reserve(state, reserved_bytes)
                        self._reserved_bytes += reserved_bytes
                        _write(self.path, data)
                        return Permit(reserved_bytes=reserved_bytes)

                _write(self.path, data)

                changed = self._changed

            if not wait:
                raise ThrottleDeferred(wait_kind, wait_for)
            self._notice(on_wait, wait_kind, wait_until, now)
            await self._wait_for_change(changed, wait_for)

    async def resize(self, permit: Permit, announced_bytes: int) -> int:
        """Revalida la reserva con cabeceras reales, sin esperar con HTTP abierto.

        Cero rechaza el cuerpo: el archivo sigue en cola y una Sync posterior
        puede admitirlo cuando quepa. Un tamaño desconocido conserva la reserva.
        """
        async with self._condition:
            if not permit.active:
                return 0
            if announced_bytes <= 0 or not self.byte_limit:
                return permit.reserved_bytes
            _data, state = self._read()
            self._prune(state, self._clock())
            other_bytes = self._reserved_bytes - permit.reserved_bytes
            used = sum(v["bytes"] for v in state["transfers"])
            if used + other_bytes + announced_bytes > self.byte_limit:
                return 0
            self._reserved_bytes = other_bytes + announced_bytes
            permit.reserved_bytes = announced_bytes
            self._notify_change()
            return announced_bytes

    async def finish(
        self,
        permit: Permit,
        *,
        success: bool,
        throttle_failure: bool = False,
        transferred_bytes: int | None = None,
    ) -> CircuitTrip | None:
        """Cierra una reserva y actualiza cuota/circuito."""
        if not permit.active:
            return None
        trip = None
        async with self._condition:
            permit.active = False
            self._reservations = max(0, self._reservations - 1)
            self._reserved_bytes = max(
                0, self._reserved_bytes - permit.reserved_bytes,
            )
            if permit.probe:
                self._probe_in_flight = False

            data, state = self._read()
            now = self._clock()
            self._prune(state, now)

            if success:
                state["successes"].append(now)
                state["transfers"].append({
                    "at": now,
                    "bytes": max(0, int(transferred_bytes or 0)),
                })
                if (self.resume_headroom and self.limit
                        and len(state["successes"]) + self._reservations
                        >= self.limit):
                    state["count_recovery"] = True
                state["failure_streak"] = 0
                state["last_failure"] = 0.0
                # Un intento normal pudo empezar antes de que otros workers
                # abrieran el circuito. Su éxito tardío cuenta para la cuota,
                # pero no debe deshacer un corte más reciente. Sólo la sonda
                # concedida después del enfriamiento puede cerrarlo.
                if permit.probe or not state.get("circuit_open"):
                    state["circuit_open"] = False
                    state["cooldown_until"] = 0.0
                    state["cooldown_level"] = 0
                    self._last_notice = None
            elif throttle_failure:
                already_open = bool(state.get("circuit_open")) and not permit.probe
                if not already_open:
                    last = float(state.get("last_failure", 0.0))
                    streak = (int(state.get("failure_streak", 0)) + 1
                              if now - last <= self.failure_window_seconds else 1)
                    state["failure_streak"] = streak
                    state["last_failure"] = now
                    if permit.probe or streak >= self.failure_threshold:
                        level = max(0, int(state.get("cooldown_level", 0)))
                        seconds = self.cooldowns[min(level, len(self.cooldowns) - 1)]
                        state["circuit_open"] = True
                        state["cooldown_until"] = now + seconds
                        state["cooldown_level"] = level + 1
                        state["failure_streak"] = 0
                        trip = CircuitTrip(seconds, level + 1)
                        self._last_notice = None
            else:
                state["failure_streak"] = 0
                state["last_failure"] = 0.0
                if permit.probe:
                    state["circuit_open"] = False
                    state["cooldown_until"] = 0.0
                    state["cooldown_level"] = 0
                    self._last_notice = None

            _write(self.path, data)
            self._notify_change()
        return trip

    async def abandon(self, permit: Permit | None) -> None:
        """Libera una reserva cancelada sin convertirla en resultado."""
        if permit is None or not permit.active:
            return
        async with self._condition:
            permit.active = False
            self._reservations = max(0, self._reservations - 1)
            self._reserved_bytes = max(
                0, self._reserved_bytes - permit.reserved_bytes,
            )
            if permit.probe:
                self._probe_in_flight = False
            self._notify_change()
