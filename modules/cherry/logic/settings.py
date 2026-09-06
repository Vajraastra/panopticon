"""
Ajustes editables de cherry-dl (A5).

Viven en `~/.cherry-dl/config.toml`, no en el `config.json` de Panopticon: son
del motor, los lee él en cada descarga y los comparte con el standalone. Por eso
se escriben con su propio `save_config` y no con un TOML nuestro — entre otras
cosas, el motor guarda `download_dir` como string literal de TOML (comillas
simples) precisamente para que una ruta de Windows no rompa el parseo por el
backslash.

Lo que NO se toca aquí: `network` (delays, reintentos, stall) y los workers por
template. Son los frenos que evitan que el motor martillee un sitio hasta que lo
bloqueen, y no hay ninguna razón para que se ajusten a ojo desde una GUI. Se
siguen editando a mano en el TOML.

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
    }


def save(
    download_dir: str,
    workers: int,
    timeout: int,
    daily_limit_enabled: bool = False,
    daily_limit_gb: float = 100.0,
) -> dict:
    """Guarda los ajustes conservando el resto del TOML.

    Se parte de `load_config()` y se modifica por encima con `model_copy`: así
    `network` y los `templates` que el usuario tenga puestos sobreviven a un
    guardado desde la GUI, en vez de volver a sus valores por defecto.
    """
    from third_party.cherry_dl.config import load_config, save_config

    if float(daily_limit_gb) <= 0:
        raise ValueError("El límite diario debe ser mayor que cero")
    cfg = load_config().model_copy(update={
        "download_dir": str(download_dir).strip(),
        "workers": int(workers),
        "timeout": int(timeout),
        "daily_limit_enabled": bool(daily_limit_enabled),
        "daily_limit_gb": float(daily_limit_gb),
    })
    save_config(cfg)
    log.info("cherry: ajustes guardados en %s", config_file())
    return load()


def download_root() -> Path:
    """Carpeta donde el motor crea los proyectos nuevos."""
    from third_party.cherry_dl.config import load_config
    return load_config().download_path
