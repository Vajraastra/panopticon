"""Bounded maintenance progress; no UI strings or Qt dependencies."""
from dataclasses import dataclass
from time import monotonic
from typing import Callable


@dataclass(frozen=True)
class MaintenanceProgress:
    phase: str
    done: int = 0
    total: int | None = None
    filename: str = ""


class ProgressReporter:
    def __init__(self, callback: Callable[[MaintenanceProgress], None] = lambda _: None):
        self.callback = callback
        self.last = None
        self.last_time = float("-inf")

    def __call__(self, phase, done=0, total=None, filename=""):
        event = MaintenanceProgress(phase, done, total, filename)
        now = monotonic()
        if event == self.last:
            return
        boundary = self.last is None or self.last.phase != phase or done == total
        if boundary or now - self.last_time >= 0.2:
            self.callback(event)
            self.last, self.last_time = event, now
