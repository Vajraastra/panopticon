"""
Gestión de configuración de usuario.
Lee/escribe ~/.cherry-dl/config.toml y administra rutas del sistema.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ── Rutas del sistema ──────────────────────────────────────────────────────────

CHERRY_DIR = Path.home() / ".cherry-dl"
CONFIG_FILE = CHERRY_DIR / "config.toml"
SESSION_FILE = CHERRY_DIR / "session.json"   # cookies DDG persistentes
INDEX_DB = CHERRY_DIR / "index.db"

# ── Modelos Pydantic ───────────────────────────────────────────────────────────

class NetworkConfig(BaseModel):
    delay_min: float = Field(10.0, ge=0)
    delay_max: float = Field(30.0, ge=0)
    retries_api: int = Field(6, ge=1)
    retries_file: int = Field(7, ge=1)
    stall_timeout: int = Field(45, ge=10)   # segundos sin datos → stall

    @field_validator("delay_max")
    @classmethod
    def max_gte_min(cls, v: float, info: Any) -> float:
        if "delay_min" in info.data and v < info.data["delay_min"]:
            raise ValueError("delay_max debe ser >= delay_min")
        return v


class TemplateConfig(BaseModel):
    workers: int = Field(3, ge=1)


class PawchiveConfig(BaseModel):
    """Frenos ajustables del archivo público Pawchive.

    El techo de caudal permanece en el template: 2 MiB/s entre todos los
    workers. Solo la ventana móvil se expone porque es el dato que podremos
    afinar con observaciones reales sin alterar el comportamiento de los demás
    servicios.
    """

    downloads_per_window: int = Field(300, ge=1, le=100_000)
    window_hours: float = Field(3.0, gt=0, le=168)


class UserConfig(BaseModel):
    download_dir: str = str(Path.home() / "cherry-dl-collections")
    workers: int = Field(3, ge=1)
    timeout: int = Field(30, ge=5)
    daily_limit_enabled: bool = False
    daily_limit_gb: float = Field(100.0, gt=0)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    pawchive: PawchiveConfig = Field(default_factory=PawchiveConfig)
    templates: dict[str, TemplateConfig] = Field(default_factory=dict)

    @property
    def download_path(self) -> Path:
        return Path(self.download_dir)


# ── Carga y guardado ───────────────────────────────────────────────────────────

def _load_toml(path: Path) -> dict:
    """Carga un archivo TOML compatible con Python 3.10 y 3.11+."""
    if sys.version_info >= (3, 11):
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    else:
        import tomli
        with open(path, "rb") as f:
            return tomli.load(f)


def load_config() -> UserConfig:
    """Carga configuración desde disco. Si no existe o falla, retorna defaults."""
    if not CONFIG_FILE.exists():
        return UserConfig()

    try:
        raw = _load_toml(CONFIG_FILE)

        # Aplanar sección [general] si existe (TOML crea dict anidado)
        general = raw.pop("general", {})
        raw.update(general)

        # Aplanar sección [templates.kemono] → templates: {"kemono": {...}}
        templates_raw = raw.pop("templates", {})
        templates = {k: TemplateConfig(**v) for k, v in templates_raw.items()}

        network_raw = raw.pop("network", {})
        network = NetworkConfig(**network_raw)

        pawchive_raw = raw.pop("pawchive", {})
        pawchive = PawchiveConfig(**pawchive_raw)

        return UserConfig(
            **raw, network=network, pawchive=pawchive, templates=templates,
        )

    except Exception as e:
        # Si el TOML está corrupto, retornar defaults y no crashear
        import sys
        print(f"[cherry-dl] ERROR cargando config: {e} — usando valores por defecto", file=sys.stderr)
        return UserConfig()


def save_config(config: UserConfig) -> None:
    """Guarda configuración en disco en formato TOML."""
    CHERRY_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "[general]\n",
        # String literal de TOML (comillas simples): no procesa escapes, así las
        # rutas de Windows (G:\images\…) no rompen el parseo por el backslash.
        f"download_dir = '{config.download_dir}'\n",
        f"workers = {config.workers}\n",
        f"timeout = {config.timeout}\n",
        f"daily_limit_enabled = {str(config.daily_limit_enabled).lower()}\n",
        f"daily_limit_gb = {config.daily_limit_gb}\n",
        "\n[network]\n",
        f"delay_min = {config.network.delay_min}\n",
        f"delay_max = {config.network.delay_max}\n",
        f"retries_api = {config.network.retries_api}\n",
        f"retries_file = {config.network.retries_file}\n",
        f"stall_timeout = {config.network.stall_timeout}\n",
        "\n[pawchive]\n",
        f"downloads_per_window = {config.pawchive.downloads_per_window}\n",
        f"window_hours = {config.pawchive.window_hours}\n",
    ]

    for name, tmpl in config.templates.items():
        lines.append(f"\n[templates.{name}]\n")
        lines.append(f"workers = {tmpl.workers}\n")

    CONFIG_FILE.write_text("".join(lines), encoding="utf-8")


def set_config_value(key: str, value: str) -> UserConfig:
    """Actualiza una clave de configuración de primer nivel y guarda."""
    config = load_config()

    match key:
        case "download_dir":
            config = config.model_copy(update={"download_dir": value})
        case "workers":
            config = config.model_copy(update={"workers": int(value)})
        case "timeout":
            config = config.model_copy(update={"timeout": int(value)})
        case "daily_limit_enabled":
            enabled = value.strip().casefold()
            if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("daily_limit_enabled debe ser true o false")
            config = config.model_copy(update={
                "daily_limit_enabled": enabled in {"true", "1", "yes", "on"},
            })
        case "daily_limit_gb":
            amount = float(value)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("daily_limit_gb debe ser mayor que cero")
            config = config.model_copy(update={"daily_limit_gb": amount})
        case "pawchive_downloads_per_window":
            limit = int(value)
            if limit <= 0:
                raise ValueError("pawchive_downloads_per_window debe ser mayor que cero")
            config = config.model_copy(update={
                "pawchive": config.pawchive.model_copy(update={
                    "downloads_per_window": limit,
                }),
            })
        case "pawchive_window_hours":
            hours = float(value)
            if not math.isfinite(hours) or hours <= 0:
                raise ValueError("pawchive_window_hours debe ser mayor que cero")
            config = config.model_copy(update={
                "pawchive": config.pawchive.model_copy(update={
                    "window_hours": hours,
                }),
            })
        case _:
            raise ValueError(
                f"Clave desconocida: '{key}'. Válidas: download_dir, workers, "
                "timeout, daily_limit_enabled, daily_limit_gb, "
                "pawchive_downloads_per_window, pawchive_window_hours"
            )

    save_config(config)
    return config


# ── Sesión DDG ─────────────────────────────────────────────────────────────────

def load_session() -> dict[str, str]:
    """Carga cookies DDG persistidas en disco."""
    if not SESSION_FILE.exists():
        return {}
    return json.loads(SESSION_FILE.read_text(encoding="utf-8"))


def save_session(cookies: dict[str, str]) -> None:
    """Persiste cookies DDG en disco para reutilizarlas en la siguiente sesión."""
    CHERRY_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


# ── Bootstrap ──────────────────────────────────────────────────────────────────

def ensure_dirs(config: UserConfig) -> None:
    """Crea directorios necesarios si no existen."""
    CHERRY_DIR.mkdir(parents=True, exist_ok=True)
    config.download_path.mkdir(parents=True, exist_ok=True)
