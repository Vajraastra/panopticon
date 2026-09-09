"""Regresión sin red para jerarquía y políticas efectivas por sitio."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl import config as config_module  # noqa: E402
from cherry_dl.config import (  # noqa: E402
    NetworkConfig,
    TemplateConfig,
    UserConfig,
)
from cherry_dl.download_service import ordered_sources, site_workers  # noqa: E402
from cherry_dl.engine import DownloadEngine  # noqa: E402
from cherry_dl.site_policy import (  # noqa: E402
    SourceCategory,
    declared_site_policy,
    resolve_site_policy,
)
from cherry_dl.templates.kemono import KemonoTemplate  # noqa: E402
from cherry_dl.templates.patreon import PatreonTemplate  # noqa: E402
from cherry_dl.templates.pawchive import PawchiveTemplate  # noqa: E402
from cherry_dl.templates.pixiv import PixivTemplate  # noqa: E402


def main() -> None:
    checks = 0

    def check(condition: bool, message: str) -> None:
        nonlocal checks
        assert condition, message
        checks += 1

    declared = {
        cls.name: declared_site_policy(cls)
        for cls in (
            PatreonTemplate, PixivTemplate, PawchiveTemplate, KemonoTemplate,
        )
    }
    check(
        declared["patreon"].category is SourceCategory.PAID_OFFICIAL,
        "Patreon debe ser oficial de pago",
    )
    check(
        declared["pixiv"].category is SourceCategory.FREE_OFFICIAL,
        "Pixiv debe ser oficial gratuito",
    )
    check(
        declared["pawchive"].category is SourceCategory.REPOSITORY,
        "Pawchive debe ser repositorio",
    )
    check(
        declared["kemono"].category is SourceCategory.DISABLED_LEGACY,
        "Kemono debe ser legado desactivado",
    )

    sources = [
        {"id": "paw-1", "url": "https://pawchive.pw/patreon/user/1"},
        {"id": "pixiv-1", "url": "https://www.pixiv.net/users/1"},
        {"id": "pay-1", "url": "https://www.patreon.com/one"},
        {"id": "pay-2", "url": "https://www.patreon.com/two"},
        {"id": "legacy", "url": "https://kemono.cr/patreon/user/1"},
        {"id": "paw-2", "url": "https://pawchive.pw/fanbox/user/2"},
    ]
    check(
        [row["id"] for row in ordered_sources(sources)] == [
            "pay-1", "pay-2", "pixiv-1", "paw-1", "paw-2", "legacy",
        ],
        "el orden debe respetar categorías y estabilidad interna",
    )

    cfg = UserConfig(
        workers=7,
        network=NetworkConfig(
            delay_min=10, delay_max=30, retries_api=6,
            retries_file=7, stall_timeout=45,
        ),
        templates={
            "patreon": TemplateConfig(workers=1),
            "pawchive": TemplateConfig(workers=8),
        },
    )
    patreon = resolve_site_policy(PatreonTemplate, cfg, cfg.workers)
    pixiv = resolve_site_policy(PixivTemplate, cfg, cfg.workers)
    pawchive = resolve_site_policy(PawchiveTemplate, cfg, cfg.workers)

    check(patreon.transfer.workers == 1,
          "[templates.patreon].workers debe ser efectivo")
    check(site_workers(PatreonTemplate, 7, cfg) == 1,
          "el servicio debe consumir el override de Patreon")
    check(pawchive.transfer.workers == 2,
          "un override no puede superar el tope rígido de Pawchive")
    check(pixiv.transfer.workers == 2,
          "sin override se conserva el default/tope de Pixiv")

    check((patreon.scan.delay_min, patreon.scan.delay_max) == (1.0, 2.5),
          "Patreon conserva su jitter propio de API")
    check((pixiv.scan.delay_min, pixiv.scan.delay_max) == (0.5, 1.5),
          "Pixiv conserva su jitter propio de API")
    check(
        (patreon.transfer.delay_min, patreon.transfer.delay_max) == (10, 30),
        "la transferencia de Patreon conserva el fallback global hasta O5",
    )
    check(patreon.scan.retries == 6 and patreon.transfer.retries == 7,
          "los reintentos de scan y archivo deben resolverse por separado")
    check(patreon.transfer.stall_timeout == 45,
          "stall pertenece a transferencias")

    scan = pawchive.scan
    transfer = pawchive.transfer
    check(scan.min_request_interval == 1.0,
          "Pawchive conserva una petición API por segundo")
    check(transfer.min_request_interval == 1.0,
          "Pawchive conserva una transferencia iniciada por segundo")
    check(transfer.workers == 2 and transfer.max_workers == 2,
          "Pawchive conserva dos workers y su tope")
    check(transfer.max_bytes_per_sec == 2 * 1024**2,
          "Pawchive conserva 2 MiB/s agregados")
    check(transfer.post_download_delay == 5.0,
          "Pawchive conserva cinco segundos post-descarga")
    check(transfer.download_window_limit == 300,
          "Pawchive conserva la ventana recomendada de 300")
    check(transfer.download_window_seconds == 3 * 60 * 60,
          "Pawchive conserva la ventana de tres horas")
    check(transfer.throttle_failure_codes == frozenset({"no_http_failed"}),
          "Pawchive conserva la firma del cortacircuitos")
    check(transfer.transport == "browser_image",
          "Pawchive conserva el transporte de navegador")

    engine = DownloadEngine(cfg)
    engine.set_scan_policy(patreon.scan)
    check((engine._delay_min, engine._delay_max) == (1.0, 2.5),
          "el motor activa el delay de scan")
    check(engine._api_retries == 6,
          "el motor activa reintentos de API")
    engine.set_transfer_policy(patreon.transfer)
    check((engine._delay_min, engine._delay_max) == (10, 30),
          "el motor cambia al delay de transferencias")
    check(engine._file_retries == 7 and engine._stall_timeout == 45,
          "el motor cambia reintentos y stall de archivos")

    old_file = config_module.CONFIG_FILE
    old_dir = config_module.CHERRY_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="cherry-policy-config-") as tmp:
            config_module.CHERRY_DIR = Path(tmp)
            config_module.CONFIG_FILE = Path(tmp) / "config.toml"
            config_module.save_config(cfg)
            loaded = config_module.load_config()
            check(loaded.templates["patreon"].workers == 1,
                  "el override por sitio debe sobrevivir al TOML")
            check(
                resolve_site_policy(PatreonTemplate, loaded, 7).transfer.workers == 1,
                "el override recargado debe seguir siendo efectivo",
            )
    finally:
        config_module.CONFIG_FILE = old_file
        config_module.CHERRY_DIR = old_dir

    print(f"OK: {checks}/29 comprobaciones")


if __name__ == "__main__":
    main()

