"""Banco sin red para la instrumentación operativa de O5."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.live_metrics import SiteRunMetrics, append_metrics  # noqa: E402
from cherry_dl.templates.base import ArtistInfo, SiteTemplate  # noqa: E402


class Clock:
    def __init__(self, *values: float) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class ProbeTemplate(SiteTemplate):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return True

    async def get_artist_info(self, url: str) -> ArtistInfo:
        raise NotImplementedError

    async def iter_files(self, artist: ArtistInfo, since=None):
        if False:
            yield


def main() -> None:
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    metrics = SiteRunMetrics(
        site="pixiv",
        source_url="https://www.pixiv.net/users/1",
        workers=2,
        transfer_delay_min=10,
        transfer_delay_max=30,
        _clock=Clock(10, 22, 30, 60),
    )
    metrics.start_scan()
    metrics.finish_scan(75)
    metrics.start_transfer()
    metrics.downloaded(2**20)
    metrics.downloaded(3 * 2**20)
    metrics.observe("scan_http", 401)
    metrics.observe("scan_http", 403)
    metrics.observe("transfer_http", 429)
    metrics.observe("transfer_http", 429)
    metrics.observe("stall")
    metrics.finish_transfer()
    sample = metrics.snapshot()

    check(sample["scan_files"] == 75, "cuenta archivos vistos")
    check(sample["scan_seconds"] == 12, "mide duración del scan")
    check(sample["transfer_seconds"] == 30, "mide duración de transferencia")
    check(sample["downloaded_files"] == 2, "cuenta descargas reales")
    check(sample["downloaded_bytes"] == 4 * 2**20, "suma bytes reales")
    check(sample["files_per_minute"] == 4, "calcula archivos/min")
    check(abs(sample["mib_per_second"] - 4 / 30) < 1e-9, "calcula MiB/s")
    check(sample["http_401"] == 1, "cuenta 401")
    check(sample["http_403"] == 1, "cuenta 403")
    check(sample["http_429"] == 2, "cuenta cada 429, incluso reintentos")
    check(sample["stalls"] == 1, "cuenta stalls")
    check(sample["workers"] == 2, "registra concurrencia efectiva")
    check(sample["transfer_delay_min"] == 10, "registra ritmo mínimo")
    check(sample["transfer_delay_max"] == 30, "registra ritmo máximo")
    check("_clock" not in sample, "no serializa estado interno")

    summary = metrics.summary()
    check("4.0 archivos/min" in summary, "resumen incluye productividad")
    check("401/403/429=1/1/2" in summary, "resumen incluye rechazos")
    check("stalls=1" in summary, "resumen incluye stalls")

    with tempfile.TemporaryDirectory(prefix="cherry-o5-") as tmp:
        path = Path(tmp) / "metrics.jsonl"
        written = append_metrics(metrics, path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        check(loaded == written, "JSONL conserva la muestra completa")
        append_metrics(metrics, path)
        check(len(path.read_text(encoding="utf-8").splitlines()) == 2,
              "las muestras se añaden sin sobrescribir")

    observed: list[tuple[str, int]] = []
    template = ProbeTemplate(None)
    template.set_network_observer(lambda kind, code: observed.append((kind, code)))
    template._observe_network("scan_http", 429)
    check(observed == [("scan_http", 429)], "el template entrega observaciones")
    template.set_network_observer(None)
    template._observe_network("scan_http", 401)
    check(len(observed) == 1, "el observador se puede desconectar")

    print(f"OK: {checks}/22 comprobaciones")


if __name__ == "__main__":
    main()
