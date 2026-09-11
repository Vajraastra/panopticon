"""
Ajustes editables de cherry-dl (A5).

Viven en `~/.cherry-dl/config.toml`, no en el `config.json` de Panopticon: son
del motor, los lee él en cada descarga y los comparte con el standalone. Por eso
se escriben con su propio `save_config` y no con un TOML nuestro — entre otras
cosas, el motor guarda `download_dir` como string literal de TOML (comillas
simples) precisamente para que una ruta de Windows no rompa el parseo por el
backslash.

Los ajustes genéricos de `network` y los workers por template siguen protegidos.
La excepción es la ventana móvil de Pawchive: se expone con defaults prudentes
porque está aislada a ese sitio y se puede ajustar con evidencia real.

Síncrono a propósito: es leer y escribir un archivo de ~15 líneas. Nada de esto
justifica pasar por el runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def config_file() -> Path:
    from third_party.cherry_dl.config import CONFIG_FILE
    return CONFIG_FILE


def load() -> dict:
    """Los ajustes editables, con los valores por defecto del motor si no hay
    archivo todavía."""
    from third_party.cherry_dl.config import load_config

    cfg = load_config()
    return {
        "download_dir": str(cfg.download_dir),
        "workers": int(cfg.workers),
        "timeout": int(cfg.timeout),
        "daily_limit_enabled": bool(cfg.daily_limit_enabled),
        "daily_limit_gb": float(cfg.daily_limit_gb),
        "pawchive_downloads_per_window": int(
            cfg.pawchive.downloads_per_window),
        "pawchive_gigabytes_per_window": float(
            cfg.pawchive.gigabytes_per_window),
        "pawchive_window_hours": float(cfg.pawchive.window_hours),
    }


def save(
    download_dir: str,
    workers: int,
    timeout: int,
    daily_limit_enabled: bool = False,
    daily_limit_gb: float = 100.0,
    pawchive_downloads_per_window: int = 300,
    pawchive_gigabytes_per_window: float = 25.0,
    pawchive_window_hours: float = 3.0,
) -> dict:
    """Guarda los ajustes conservando el resto del TOML.

    Se parte de `load_config()` y se modifica por encima con `model_copy`: así
    `network` y los `templates` que el usuario tenga puestos sobreviven a un
    guardado desde la GUI, en vez de volver a sus valores por defecto.
    """
    from third_party.cherry_dl.config import load_config, save_config

    if float(daily_limit_gb) <= 0:
        raise ValueError("El límite diario debe ser mayor que cero")
    if int(pawchive_downloads_per_window) <= 0:
        raise ValueError("La cuota de Pawchive debe ser mayor que cero")
    if float(pawchive_gigabytes_per_window) <= 0:
        raise ValueError("La cuota en GB de Pawchive debe ser mayor que cero")
    if float(pawchive_window_hours) <= 0:
        raise ValueError("La ventana de Pawchive debe ser mayor que cero")
    from third_party.cherry_dl.config import PawchiveConfig

    cfg = load_config().model_copy(update={
        "download_dir": str(download_dir).strip(),
        "workers": int(workers),
        "timeout": int(timeout),
        "daily_limit_enabled": bool(daily_limit_enabled),
        "daily_limit_gb": float(daily_limit_gb),
        "pawchive": PawchiveConfig(
            downloads_per_window=int(pawchive_downloads_per_window),
            gigabytes_per_window=float(pawchive_gigabytes_per_window),
            window_hours=float(pawchive_window_hours),
        ),
    })
    save_config(cfg)
    log.info("cherry: ajustes guardados en %s", config_file())
    return load()


def download_root() -> Path:
    """Carpeta donde el motor crea los proyectos nuevos."""
    from third_party.cherry_dl.config import load_config
    return load_config().download_path
