"""Métricas operativas por fuente para ajustar políticas con evidencia."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .config import CHERRY_DIR


METRICS_FILE = CHERRY_DIR / "live_metrics.jsonl"


@dataclass
class SiteRunMetrics:
    site: str
    source_url: str
    workers: int
    transfer_delay_min: float
    transfer_delay_max: float
    started_at: float = field(default_factory=time.time)
    scan_seconds: float = 0.0
    scan_files: int = 0
    transfer_seconds: float = 0.0
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    stalls: int = 0
    http_401: int = 0
    http_403: int = 0
    http_429: int = 0
    _scan_started: float | None = field(default=None, repr=False)
    _transfer_started: float | None = field(default=None, repr=False)
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def start_scan(self) -> None:
        self._scan_started = self._clock()

    def finish_scan(self, files_seen: int) -> None:
        if self._scan_started is not None:
            self.scan_seconds += max(0.0, self._clock() - self._scan_started)
            self._scan_started = None
        self.scan_files = files_seen

    def start_transfer(self) -> None:
        self._transfer_started = self._clock()

    def finish_transfer(self) -> None:
        if self._transfer_started is not None:
            self.transfer_seconds += max(
                0.0, self._clock() - self._transfer_started,
            )
            self._transfer_started = None

    def observe(self, kind: str, status_code: int = 0) -> None:
        if kind == "stall":
            self.stalls += 1
        elif status_code == 401:
            self.http_401 += 1
        elif status_code == 403:
            self.http_403 += 1
        elif status_code == 429:
            self.http_429 += 1

    def downloaded(self, size: int) -> None:
        self.downloaded_files += 1
        self.downloaded_bytes += max(0, size)

    def snapshot(self) -> dict:
        if self._scan_started is not None:
            self.finish_scan(self.scan_files)
        self.finish_transfer()
        raw = asdict(self)
        for private in ("_scan_started", "_transfer_started", "_clock"):
            raw.pop(private, None)
        seconds = self.transfer_seconds
        raw["files_per_minute"] = (
            self.downloaded_files * 60 / seconds if seconds > 0 else 0.0
        )
        raw["mib_per_second"] = (
            self.downloaded_bytes / 2**20 / seconds if seconds > 0 else 0.0
        )
        return raw

    def summary(self) -> str:
        data = self.snapshot()
        return (
            f"{self.site}: scan {self.scan_files} archivo(s) en "
            f"{self.scan_seconds:.1f}s; {self.downloaded_files} descarga(s), "
            f"{data['files_per_minute']:.1f} archivos/min, "
            f"{data['mib_per_second']:.2f} MiB/s; "
            f"401/403/429={self.http_401}/{self.http_403}/{self.http_429}, "
            f"stalls={self.stalls}"
        )


def append_metrics(metrics: SiteRunMetrics, path: Path | None = None) -> dict:
    """Añade una muestra JSONL; un fallo de telemetría nunca rompe la descarga."""
    record = metrics.snapshot()
    target = path or METRICS_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record
