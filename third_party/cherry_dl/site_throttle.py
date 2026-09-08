"""Cuota móvil y cortacircuitos persistente para descargas por sitio.

La cuota cuenta transferencias terminadas, no intentos. Cada worker reserva un
cupo antes de pedir para que la concurrencia no pueda cruzar el límite. El
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


@dataclass(frozen=True)
class CircuitTrip:
    seconds: float
    level: int


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
        self.window_seconds = max(0.0, float(window_seconds))
        self.failure_threshold = max(1, int(failure_threshold))
        self.failure_window_seconds = max(0.0, float(failure_window_seconds))
        self.cooldowns = tuple(max(0.0, float(v)) for v in cooldowns) or (900.0,)
        self.path = path or THROTTLE_FILE
        self._clock = clock
        self._sleep = sleeper
        self._condition = asyncio.Condition()
        self._reservations = 0
        self._probe_in_flight = False
        self._last_notice: tuple[str, int] | None = None

    def _read(self) -> tuple[dict, dict]:
        data = _load(self.path)
        state = data.get(self.site)
        if not isinstance(state, dict):
            state = {}
            data[self.site] = state
        state.setdefault("successes", [])
        state.setdefault("failure_streak", 0)
        state.setdefault("last_failure", 0.0)
        state.setdefault("circuit_open", False)
        state.setdefault("cooldown_until", 0.0)
        state.setdefault("cooldown_level", 0)
        return data, state

    def _prune(self, state: dict, now: float) -> None:
        floor = now - self.window_seconds
        state["successes"] = [
            float(v) for v in state.get("successes", [])
            if isinstance(v, (int, float)) and float(v) > floor
        ]

    def _notice(self, callback: WaitFn | None, kind: str, until: float, now: float) -> None:
        key = (kind, int(until))
        if callback is not None and key != self._last_notice:
            self._last_notice = key
            callback(kind, max(0.0, until - now))

    async def acquire(self, on_wait: WaitFn | None = None) -> Permit:
        """Espera lo necesario y reserva un intento; cancelable con la task."""
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
                    elif (self.limit and self.window_seconds
                          and len(state["successes"]) + self._reservations
                          >= self.limit):
                        if state["successes"]:
                            wait_until = min(state["successes"]) + self.window_seconds
                            wait_for = max(0.01, wait_until - now)
                            wait_kind = "quota"
                        else:
                            await self._condition.wait()
                            continue
                    elif not self._probe_in_flight:
                        self._probe_in_flight = True
                        self._reservations += 1
                        _write(self.path, data)
                        return Permit(probe=True)
                    else:
                        await self._condition.wait()
                        continue
                elif (self.limit and self.window_seconds
                      and len(state["successes"]) + self._reservations >= self.limit):
                    if state["successes"]:
                        wait_until = min(state["successes"]) + self.window_seconds
                        wait_for = max(0.01, wait_until - now)
                        wait_kind = "quota"
                    else:
                        await self._condition.wait()
                        continue
                else:
                    self._reservations += 1
                    _write(self.path, data)
                    return Permit()

                _write(self.path, data)

            self._notice(on_wait, wait_kind, wait_until, now)
            await self._sleep(wait_for)

    async def finish(
        self,
        permit: Permit,
        *,
        success: bool,
        throttle_failure: bool = False,
    ) -> CircuitTrip | None:
        """Cierra una reserva y actualiza cuota/circuito."""
        if not permit.active:
            return None
        trip = None
        async with self._condition:
            permit.active = False
            self._reservations = max(0, self._reservations - 1)
            if permit.probe:
                self._probe_in_flight = False

            data, state = self._read()
            now = self._clock()
            self._prune(state, now)

            if success:
                state["successes"].append(now)
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
            self._condition.notify_all()
        return trip

    async def abandon(self, permit: Permit | None) -> None:
        """Libera una reserva cancelada sin convertirla en resultado."""
        if permit is None or not permit.active:
            return
        async with self._condition:
            permit.active = False
            self._reservations = max(0, self._reservations - 1)
            if permit.probe:
                self._probe_in_flight = False
            self._condition.notify_all()
