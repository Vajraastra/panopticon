"""Jerarquía y políticas de red declarativas por sitio."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import UserConfig
    from .templates.base import SiteTemplate


class SourceCategory(IntEnum):
    """Orden semántico estable de las fuentes."""

    PAID_OFFICIAL = 10
    FREE_OFFICIAL = 20
    REPOSITORY = 30
    DISABLED_LEGACY = 40
    UNKNOWN = 90


@dataclass(frozen=True)
class ScanPolicy:
    """Política para identidad, páginas de API y enfriamiento post-scan."""

    delay_min: float | None = None
    delay_max: float | None = None
    retries: int | None = None
    min_request_interval: float = 0.0
    cooldown_threshold: int = 200
    cooldown_seconds: float = 10.0


@dataclass(frozen=True)
class TransferPolicy:
    """Política para el cuerpo de archivos, independiente del scan."""

    workers: int = 3
    max_workers: int | None = None
    delay_min: float | None = None
    delay_max: float | None = None
    retries: int | None = None
    stall_timeout: int | None = None
    min_request_interval: float = 0.0
    post_download_delay: float = 0.0
    max_bytes_per_sec: float = 0.0
    daily_byte_budget: int = 0
    download_window_limit: int = 0
    download_window_seconds: float = 0.0
    throttle_failure_codes: frozenset[str] = frozenset()
    throttle_failure_streak: int = 5
    throttle_failure_window: float = 120.0
    throttle_cooldowns: tuple[float, ...] = (
        900.0, 1800.0, 3600.0, 10800.0,
    )
    transport: str = "http"
    browser_origin: str = ""


@dataclass(frozen=True)
class SitePolicy:
    category: SourceCategory
    scan: ScanPolicy
    transfer: TransferPolicy


def _legacy_scan_policy(cls: type[SiteTemplate]) -> ScanPolicy:
    return ScanPolicy(
        min_request_interval=float(getattr(cls, "min_request_interval", 0.0)),
        cooldown_threshold=int(getattr(cls, "cooldown_threshold", 200)),
        cooldown_seconds=float(getattr(cls, "cooldown_seconds", 10.0)),
    )


def _legacy_transfer_policy(cls: type[SiteTemplate]) -> TransferPolicy:
    return TransferPolicy(
        workers=int(getattr(cls, "workers", 3)),
        max_workers=getattr(cls, "max_workers", None),
        min_request_interval=float(getattr(cls, "min_request_interval", 0.0)),
        post_download_delay=float(getattr(cls, "post_download_delay", 0.0)),
        max_bytes_per_sec=float(getattr(cls, "max_bytes_per_sec", 0.0)),
        daily_byte_budget=int(getattr(cls, "daily_byte_budget", 0)),
        download_window_limit=int(getattr(cls, "download_window_limit", 0)),
        download_window_seconds=float(
            getattr(cls, "download_window_seconds", 0.0)
        ),
        throttle_failure_codes=frozenset(
            getattr(cls, "throttle_failure_codes", frozenset())
        ),
        throttle_failure_streak=int(
            getattr(cls, "throttle_failure_streak", 5)
        ),
        throttle_failure_window=float(
            getattr(cls, "throttle_failure_window", 120.0)
        ),
        throttle_cooldowns=tuple(getattr(
            cls, "throttle_cooldowns", (900.0, 1800.0, 3600.0, 10800.0),
        )),
        transport=str(getattr(cls, "download_transport", "http")),
        browser_origin=str(getattr(cls, "browser_origin", "")),
    )


def declared_site_policy(cls: type[SiteTemplate] | None) -> SitePolicy:
    if cls is None:
        return SitePolicy(
            SourceCategory.UNKNOWN, ScanPolicy(), TransferPolicy(),
        )
    category = getattr(cls, "category", SourceCategory.UNKNOWN)
    scan = (
        cls.__dict__["scan_policy"]
        if "scan_policy" in cls.__dict__ else _legacy_scan_policy(cls)
    )
    transfer = (
        cls.__dict__["transfer_policy"]
        if "transfer_policy" in cls.__dict__ else _legacy_transfer_policy(cls)
    )
    return SitePolicy(SourceCategory(category), scan, transfer)


def resolve_site_policy(
    cls: type[SiteTemplate] | None,
    config: UserConfig,
    requested_workers: int,
) -> SitePolicy:
    """Resuelve defaults, override por template y topes rígidos."""
    declared = declared_site_policy(cls)
    name = getattr(cls, "name", "") if cls else ""
    override = config.templates.get(name)
    if override is not None:
        wanted_workers = override.workers
    elif cls is None:
        wanted_workers = requested_workers
    else:
        wanted_workers = max(requested_workers, declared.transfer.workers)

    cap = declared.transfer.max_workers
    workers = max(1, min(wanted_workers, cap) if cap else wanted_workers)
    scan = replace(
        declared.scan,
        delay_min=(config.network.delay_min
                   if declared.scan.delay_min is None else declared.scan.delay_min),
        delay_max=(config.network.delay_max
                   if declared.scan.delay_max is None else declared.scan.delay_max),
        retries=(config.network.retries_api
                 if declared.scan.retries is None else declared.scan.retries),
    )
    transfer = replace(
        declared.transfer,
        workers=workers,
        delay_min=(config.network.delay_min
                   if declared.transfer.delay_min is None
                   else declared.transfer.delay_min),
        delay_max=(config.network.delay_max
                   if declared.transfer.delay_max is None
                   else declared.transfer.delay_max),
        retries=(config.network.retries_file
                 if declared.transfer.retries is None
                 else declared.transfer.retries),
        stall_timeout=(config.network.stall_timeout
                       if declared.transfer.stall_timeout is None
                       else declared.transfer.stall_timeout),
    )
    return SitePolicy(declared.category, scan, transfer)

