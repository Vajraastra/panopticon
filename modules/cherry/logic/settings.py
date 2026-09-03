"""
Ajustes de cherry-dl: `download_dir`, workers y timeout (A5).

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
    }


def save(download_dir: str, workers: int, timeout: int) -> dict:
    """Guarda los tres ajustes conservando el resto del TOML.

    Se parte de `load_config()` y se modifica por encima con `model_copy`: así
    `network` y los `templates` que el usuario tenga puestos sobreviven a un
    guardado desde la GUI, en vez de volver a sus valores por defecto.
    """
    from third_party.cherry_dl.config import load_config, save_config

    cfg = load_config().model_copy(update={
        "download_dir": str(download_dir).strip(),
        "workers": int(workers),
        "timeout": int(timeout),
    })
    save_config(cfg)
    log.info("cherry: ajustes guardados en %s", config_file())
    return load()


def download_root() -> Path:
    """Carpeta donde el motor crea los proyectos nuevos."""
    from third_party.cherry_dl.config import load_config
    return load_config().download_path
