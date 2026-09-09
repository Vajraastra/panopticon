"""Certificación viva aislada de Patreon/Pixiv para O5.

Usa sesiones y URLs ya registradas, pero descarga muestras en un temporal y no
modifica index.db, catálogos ni colecciones del usuario.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.config import INDEX_DB, load_config  # noqa: E402
from cherry_dl.engine import DownloadEngine  # noqa: E402
from cherry_dl.live_metrics import SiteRunMetrics, append_metrics  # noqa: E402
from cherry_dl.site_policy import resolve_site_policy  # noqa: E402
from cherry_dl.templates.patreon import PatreonTemplate  # noqa: E402
from cherry_dl.templates.pixiv import PixivTemplate  # noqa: E402


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value or "")


def _enabled_url(site: str) -> str:
    with sqlite3.connect(INDEX_DB) as db:
        rows = db.execute(
            "SELECT url FROM profile_urls WHERE enabled = 1 "
            "AND lower(site) = ? ORDER BY id",
            (site,),
        ).fetchall()
    for row in rows:
        url = _text(row[0])
        if url.startswith(("http://", "https://")):
            return url
    raise RuntimeError(f"No hay una fuente activa y legible de {site}")


async def _certify_patreon(config) -> dict:
    url = _enabled_url("patreon")
    policy = resolve_site_policy(PatreonTemplate, config, config.workers)
    metrics = SiteRunMetrics(
        "patreon", url, policy.transfer.workers,
        policy.transfer.delay_min or 0, policy.transfer.delay_max or 0,
    )
    async with DownloadEngine(config, workers=policy.transfer.workers) as engine:
        template = PatreonTemplate(engine)
        template.set_network_observer(metrics.observe)
        metrics.start_scan()
        artist = await template.get_artist_info(url)
        stream = template.iter_files(artist)
        found = False
        try:
            async for _item in stream:
                found = True
                break
        finally:
            await stream.aclose()
        metrics.finish_scan(1 if found else 0)
    record = append_metrics(metrics)
    if not found:
        raise RuntimeError("Patreon autenticó, pero la fuente no expuso archivos")
    return record


async def _certify_pixiv(config, output: Path) -> dict:
    url = _enabled_url("pixiv")
    policy = resolve_site_policy(PixivTemplate, config, config.workers)
    metrics = SiteRunMetrics(
        "pixiv", url, policy.transfer.workers,
        policy.transfer.delay_min or 0, policy.transfer.delay_max or 0,
    )
    samples = []
    seen = 0
    async with DownloadEngine(config, workers=policy.transfer.workers) as engine:
        template = PixivTemplate(engine)
        template.set_network_observer(metrics.observe)
        metrics.start_scan()
        artist = await template.get_artist_info(url)
        async for item in template.iter_files(artist, since=None):
            seen += 1
            if len(samples) < 6:
                samples.append(item)
        metrics.finish_scan(seen)

        if not samples:
            raise RuntimeError("El historial Pixiv no contiene ilustraciones")

        engine.set_transfer_policy(policy.transfer)
        metrics.start_transfer()

        async def fetch(position: int, item) -> None:
            result = await engine.download(
                item.url, output, f"image_{position}{Path(item.filename).suffix}",
                extra_headers=item.extra_headers,
                on_observation=metrics.observe,
            )
            if not result.ok:
                raise RuntimeError(
                    f"Pixiv image: {result.error_kind}: {result.error}"
                )
            metrics.downloaded(result.file_size)

        await asyncio.gather(*(
            fetch(position, item) for position, item in enumerate(samples, 1)
        ))
        metrics.finish_transfer()

    return append_metrics(metrics)


async def main() -> None:
    config = load_config()
    with tempfile.TemporaryDirectory(prefix="cherry-o5-live-") as tmp:
        patreon = await _certify_patreon(config)
        pixiv = await _certify_pixiv(config, Path(tmp))
    print(
        "PATREON_OK "
        f"scan={patreon['scan_seconds']:.1f}s "
        f"401/403/429={patreon['http_401']}/{patreon['http_403']}/{patreon['http_429']}"
    )
    print(
        "PIXIV_OK "
        f"scan={pixiv['scan_files']} archivos/{pixiv['scan_seconds']:.1f}s "
        f"transfer={pixiv['downloaded_files']} archivos "
        f"{pixiv['files_per_minute']:.1f}/min {pixiv['mib_per_second']:.2f}MiB/s "
        f"401/403/429={pixiv['http_401']}/{pixiv['http_403']}/{pixiv['http_429']} "
        f"stalls={pixiv['stalls']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
