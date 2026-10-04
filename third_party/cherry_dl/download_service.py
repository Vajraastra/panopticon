"""
Servicio de descarga compartido, agnóstico de UI.

Encapsula el flujo canónico de descarga en dos fases:

    scan API (iter_files, since=last_synced)  →  pending_queue
      → cooldown si el scan fue agresivo (politeness)
      → descarga productor/workers (dedup atómico, remove_pending por éxito)
      → errores temporales al fondo para la próxima sesión
      → completed discovery commits its UTC start watermark; downloads update file_count

La interfaz no conoce asyncio: pasa un callback `emit(evento)` y recibe eventos
tipados (ver más abajo). Todo corre en un único event loop,
por lo que `emit` es síncrono y seguro para tocar widgets directamente.

Cancelación: cancelar la task que await-ea `run_profile_download` — el flujo
propaga CancelledError limpiamente (cancela productor + workers y re-lanza).
"""

from __future__ import annotations

import asyncio
from .url_receipts import remember_downloaded_url
import json
import logging
import time
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from .config import INDEX_DB, load_config
from .engine import DownloadEngine, ErrorKind
from .downloads import (
    _build_local_hash_map,
    _passes_ext_filter,
)
from .file_routing import (
    COUNTER_NAMESPACES,
    LAYOUT_VERSION,
    prepare_selected_folders,
    relative_filename,
)
from .archives import organize_project
from .catalog import (
    ARCHIVE_RECHECK_BATCH,
    ScanCatalogBatch,
    add_content_alias,
    add_file,
    defer_pending_to_next_session,
    defer_until_archived,
    clean_pending_catalog_overlap,
    content_alias_hash,
    get_meta_int,
    get_pending_files,
    hash_exists,
    init_catalog,
    ensure_layout_version,
    reserve_destination as _reserve_destination,
    pending_count,
    reactivate_awaiting_archive,
    remove_pending,
    set_meta_int,
    url_exists,
)
from .index import (
    get_or_create_artist,
    get_or_create_site,
    init_index,
    set_profile_url_availability,
    update_profile_last_checked,
    update_profile_url_sync,
)
from .site_budget import (
    GLOBAL_BUDGET_KEY,
    add as budget_add,
    remaining as budget_remaining,
    spent_today as budget_spent_today,
)
from .site_throttle import SiteThrottle, ThrottleDeferred
from .transfer_status import TransferStatus, WorkerState
from .site_policy import SourceCategory, declared_site_policy, resolve_site_policy
from .live_metrics import SiteRunMetrics, append_metrics
from .templates._registry import find_template, get_template
from .templates.base import FileInfo, parse_date_utc
from .auth.patreon import NeedsManualAuth
from .auth.pixiv import NeedsPixivAuth


log = logging.getLogger(__name__)


def _failure_detail(error: str | None, limit: int = 320) -> str:
    """Una línea legible para UI y log, sin esconder la causa concreta."""
    compact = " ".join((error or "sin detalle").split())
    return compact if len(compact) <= limit else compact[:limit - 1] + "…"


def _publish_site_metrics(metrics: SiteRunMetrics, emit: "EmitFn") -> None:
    values = append_metrics(metrics)
    emit(SiteMetrics(metrics.site, values))
    emit(Log(f"◫ {metrics.summary()}", "dim"))


# ── Eventos ─────────────────────────────────────────────────────────────────────
# La UI hace match/isinstance sobre estos y actualiza sus widgets.

@dataclass(frozen=True)
class Log:
    """Línea de log. level ∈ {info, dim, ok, warn, error}."""
    msg: str
    level: str = "info"


async def _confirm_failed_probe_once(
    permit,
    result,
    retry: Callable[[], Awaitable],
    emit: "EmitFn",
):
    """Confirma un ``CDP Failed`` de sonda en una sesión nueva del navegador."""
    if not (
        permit is not None
        and permit.probe
        and not result.ok
        and getattr(result, "failure_code", None) == "no_http_failed"
    ):
        return result
    emit(Log(
        "↻ La sonda falló sin respuesta HTTP; se confirma una vez con "
        "una sesión nueva del navegador",
        "warn",
    ))
    return await retry()


@dataclass(frozen=True)
class WorkersResolved:
    """Número efectivo de workers tras aplicar el cap del template."""
    count: int


@dataclass(frozen=True)
class SourceStarted:
    url: str
    artist: str
    site: str


@dataclass(frozen=True)
class Cooldown:
    seconds: float


@dataclass(frozen=True)
class BatchInfo:
    """Total de la tanda actual y offset si se está retomando."""
    total: int
    offset: int


@dataclass(frozen=True)
class FileProgress:
    """Archivos resueltos en la tanda activa y total teórico de la tanda."""
    done: int
    total: int


@dataclass(frozen=True)
class WorkerStart:
    slot: int
    filename: str


@dataclass(frozen=True)
class WorkerProgress:
    slot: int
    done: int
    total: int


@dataclass(frozen=True)
class WorkerDone:
    slot: int
    filename: str
    icon: str


@dataclass(frozen=True)
class WorkerIdle:
    slot: int


@dataclass(frozen=True)
class Counters:
    downloaded: int
    skipped: int
    errors: int
    deferred: int


@dataclass(frozen=True)
class SourceDone:
    url_id: int
    file_count: int


@dataclass(frozen=True)
class ScanComplete:
    """Emitido al final en modo scan_only."""
    pending: int


@dataclass(frozen=True)
class ScanProgress:
    """Progreso de la Fase 1 (escaneo). La Fase 1 puede tardar minutos sin
    descargar nada; esto da señal de vida a la UI durante el paginado."""
    seen: int     # posts/archivos vistos en la API hasta ahora
    queued: int   # nuevos encolados en pending_queue hasta ahora


@dataclass(frozen=True)
class SiteBlocked:
    """El sitio cerró la puerta: se abandona, no se insiste.

    `detail` es lo que dijera el servidor en el cuerpo del rechazo, cuando
    dijo algo. Los archivos siguen en la cola: no se pierde ninguno."""
    site: str
    streak: int
    detail: str = ""


@dataclass(frozen=True)
class SiteMetrics:
    """Resumen operativo medido al terminar una fuente."""
    site: str
    values: dict


@dataclass(frozen=True)
class QuotaStatus:
    """Estado visual de la ventana móvil de un sitio.

    El snapshot persistido sólo contiene transferencias terminadas;
    ``in_flight_bytes`` hace visible lo recibido por los workers actuales sin
    adelantarlo en la contabilidad definitiva.
    """
    snapshot: object
    in_flight_bytes: int = 0


DownloadEvent = Union[
    Log, WorkersResolved, SourceStarted, Cooldown, BatchInfo, FileProgress,
    WorkerStart, WorkerProgress, WorkerDone, WorkerIdle, WorkerState,
    Counters, SourceDone, ScanComplete, ScanProgress, SiteBlocked, SiteMetrics,
    QuotaStatus,
]

EmitFn = Callable[[DownloadEvent], None]
# resolve_auth(site) -> True si el usuario se autenticó y se puede reintentar.
AuthResolver = Callable[[str], Awaitable[bool]]


def _auth_site(exc: Exception) -> str:
    return "pixiv" if isinstance(exc, NeedsPixivAuth) else "patreon"


async def _resolve_required_auth(
    template,
    exc: Exception,
    resolve_auth: Optional[AuthResolver],
    emit: EmitFn,
) -> bool:
    """Suelta el cliente rechazado antes de capturar y usar una sesión nueva."""
    site = _auth_site(exc)
    discard = getattr(template, "discard_auth_client", None)
    if discard is not None:
        await discard()
    emit(Log(f"⚠ {site.capitalize()} requiere autenticación", "warn"))
    if resolve_auth is None or not await resolve_auth(site):
        emit(Log("✗ Autenticación cancelada", "error"))
        return False
    return True


# Rechazos seguidos que se toman por «este sitio nos cerró la puerta».
#
# El 2026-09-05 una descarga de Pawchive encadenó 774 respuestas 403 en trece
# minutos, a una petición por segundo, hasta agotar la cola: el motor trataba
# cada 403 como problema de ESE archivo y pasaba al siguiente. Ningún archivo se
# perdió —siguen en la cola— pero el servidor recibió 774 peticiones inútiles
# después de habernos dicho que no, que es justo la conducta que provocó el
# bloqueo. El aviso explícito del administrador corta al primero; sin ese texto,
# cinco rechazos entre los últimos ocho resultados ya son el sitio hablando.
_BLOCK_STREAK = 5
_BLOCK_WINDOW = 8

# Pawchive devuelve este texto cuando el bloqueo no es un fallo de un archivo,
# sino una petición humana de detener toda la sesión. No se esperan otros cuatro
# golpes después de leerlo.
_HARD_STOP_MARKERS = (
    "stop before i block every single one of your ips",
    "tbs a day is just abusive",
)

# Qué cuenta como «el servidor nos dice que no». Un 404 no entra (el archivo ya
# no existe, y eso no dice nada del resto), ni un timeout (puede ser la red de
# aquí), ni una firma vencida (es legítima y llega en masa tras un scan viejo).
_REJECTIONS = frozenset({
    ErrorKind.AUTH, ErrorKind.CLOUDFLARE, ErrorKind.RATE_LIMIT,
})


def site_workers(cls, requested: int, config=None) -> int:
    """
    Descargas simultáneas para un sitio concreto.

    El usuario fija el pool general; el template lo acota por arriba con
    `max_workers`, que es un tope que no es nuestro sino del sitio. Y si el
    template recomienda MÁS de lo que pide el ajuste general, ese sitio puede
    usarlo: el template sabe del sitio lo que un número global no —el caso real
    es Pawchive, cuyo administrador dio 5 por escrito—.

    `cls` puede ser None (URL sin template): entonces manda lo pedido.
    """
    if config is not None:
        return resolve_site_policy(cls, config, requested).transfer.workers
    declared = declared_site_policy(cls).transfer
    if cls is None:
        return max(1, requested)
    want = max(requested, declared.workers)
    cap = declared.max_workers
    return max(1, min(want, cap) if cap else want)


def source_priority(url: str) -> int:
    """Categoría semántica de una URL, usada como prioridad estable."""
    cls = find_template(url) if url else None
    return int(declared_site_policy(cls).category)


def source_category(url: str) -> SourceCategory:
    """Categoría declarada de una URL, compartida con el planificador batch."""
    cls = find_template(url) if url else None
    return declared_site_policy(cls).category


def _wait_on_site_throttle(
    source_categories: set[SourceCategory] | None,
) -> bool:
    """Espera cuotas normales y las de la pasada final de repositorios.

    Las categorías oficiales todavía pueden diferirse para dejar avanzar el
    batch. Repositorios es la última pasada global: diferir Pawchive ahí
    terminaría falsamente el lote y exigiría otra Sync manual.
    """
    return (
        source_categories is None
        or SourceCategory.REPOSITORY in source_categories
    )


async def _remember_content_alias(
    folder: Path, file_info: FileInfo, file_hash: str, *, url_recorded: bool = False,
) -> bool:
    """Conserva la identidad original sólo cuando ya existe un hash probado."""
    if file_info.is_preview:
        return False
    receipt = url_recorded or await remember_downloaded_url(folder, file_info.dedup_key, file_hash)
    if not receipt:
        raise ValueError('download_url_history_unproven')
    alias = await add_content_alias(
        folder,
        file_hash,
        file_info.content_service,
        file_info.post_id,
        file_info.original_filename or file_info.filename,
        file_info.content_role,
        file_info.content_position,
        file_info.remote_size,
    )
    return receipt or alias


def ordered_sources(sources: list[dict]) -> list[dict]:
    """Orden estable para que los mirrors solo rellenen lo que falte."""
    return sorted(
        sources,
        key=lambda source: source_priority(source.get("url") or ""),
    )


async def _enqueue_with_backpressure(queue: asyncio.Queue, item) -> None:
    """Espera espacio real; una descarga lenta no equivale a cola rota."""
    await queue.put(item)


async def _gather_transfer_tasks(tasks: list[asyncio.Task]) -> list:
    """Reúne productor/workers y corta el grupo si uno muere inesperadamente."""
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass
class DownloadSummary:
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    deferred: int = 0
    dropped: int = 0    # retirados de la cola por no existir ya en el origen
    awaiting_archive: int = 0  # Pawchive lo anuncia, pero aún no tiene original


# ── Servicio ────────────────────────────────────────────────────────────────────

def _scan_watermark() -> str:
    # Whole UTC seconds overlap boundary posts rather than skip them.
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


async def _close_scan_catalog(catalog) -> None:
    """Drain the owned partial-batch flush even if closure itself is cancelled."""
    closing = asyncio.create_task(catalog.close(),name='cherry-scan-catalog-close')
    cancelled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve failures before reporting any complete scan; no detached close task.
    closing.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_profile_download(
    profile: dict,
    *,
    workers: int,
    ext_filter: set[str],
    exclude_mode: bool = False,
    force_full: bool = False,
    scan_only: bool = False,
    validate_sources: bool = False,
    source_categories: set[SourceCategory] | None = None,
    finalize_profile: bool = True,
    emit: EmitFn,
    config=None,
    resolve_auth: Optional[AuthResolver] = None,
) -> DownloadSummary:
    """
    Ejecuta el flujo de descarga de dos fases para todas las fuentes activas
    de un perfil. Ver el docstring del módulo para el flujo y la cancelación.

    Parámetros:
      profile       — dict con 'id', 'folder_path', 'urls' (cada url con
                      'id', 'url', 'enabled', 'last_synced', 'file_count').
      workers       — pool deseado; se reduce al max_workers del template.
      ext_filter    — set de extensiones con punto ({'.jpg', '.zip'}).
      exclude_mode  — False = incluir solo esas extensiones; True = excluirlas.
      force_full    — ignora last_synced y escanea desde el inicio (Rescan).
      scan_only     — solo puebla pending_queue, no descarga.
      validate_sources — desactiva fuentes que no responden al inicio del scan.
      source_categories — limita esta pasada a esas categorías semánticas.
      finalize_profile — actualiza last_checked al cerrar la última pasada.
      emit          — callback síncrono que recibe DownloadEvent.
      resolve_auth  — callback async opcional para resolver auth interactiva.
    """
    summ = DownloadSummary()
    progress = {"done": 0, "total": 0}

    def _file_finished() -> None:
        if not progress["total"]:
            return
        progress["done"] = min(progress["done"] + 1, progress["total"])
        emit(FileProgress(progress["done"], progress["total"]))

    def _counters() -> None:
        emit(Counters(summ.downloaded, summ.skipped, summ.errors, summ.deferred))

    async def _not_found_action(template, artist, file_info) -> str:
        """Clasifica sin permitir que un fallo del diagnóstico pierda la fila."""
        try:
            return await template.classify_not_found(artist, file_info)
        except Exception:
            log.exception(
                "cherry: fallo clasificando 404 | sitio=%s | archivo=%r",
                template.name, file_info.filename,
            )
            return "unknown"

    config = config or load_config()
    folder = Path(profile["folder_path"])

    if not scan_only:
        folder.mkdir(parents=True, exist_ok=True)
        await init_catalog(folder)
        if not await ensure_layout_version(
            folder, COUNTER_NAMESPACES, LAYOUT_VERSION,
        ):
            raise RuntimeError(
                "El proyecto usa el esquema de nombres anterior y debe pasar "
                "la migración de organización antes de descargar."
            )
        if ext_filter and not exclude_mode:
            prepare_selected_folders(folder, ext_filter)
        try:
            repaired = await organize_project(folder)
            if repaired.organized:
                emit(Log(
                    f"🗂 {repaired.organized} archivo(s) anterior(es) "
                    "reubicados automáticamente por tipo",
                    "dim",
                ))
        except Exception as exc:
            log.exception(
                "cherry: no se pudo reparar la organización | proyecto=%s",
                folder,
            )
            emit(Log(
                f"⚠ No se pudo reparar la organización anterior: "
                f"{type(exc).__name__}: {exc}. La descarga continuará.",
                "warn",
            ))

    global_budget = (
        int(config.daily_limit_gb * 2**30)
        if config.daily_limit_enabled else 0
    )
    global_state = {
        "reached": budget_remaining(GLOBAL_BUDGET_KEY, global_budget) == 0,
        "notified": False,
    }

    def _record_global_download(nbytes: int) -> None:
        """Cuenta bytes combinados y cierra la llave al llegar al tope."""
        if not global_budget or nbytes <= 0:
            return
        spent = budget_add(GLOBAL_BUDGET_KEY, nbytes)
        if spent < global_budget:
            return
        global_state["reached"] = True
        if global_state["notified"]:
            return
        global_state["notified"] = True
        detail = (
            f"límite diario global agotado ({config.daily_limit_gb:g} GB)"
        )
        emit(SiteBlocked("global", 0, detail))
        emit(Log(
            f"⛔ Límite diario global: {spent / 2**30:.1f} GB hoy, "
            f"tope {config.daily_limit_gb:g} GB. Sigue mañana; los archivos "
            "que ya estaban en curso terminarán.",
            "warn",
        ))

    # El engine arranca con el pool más grande que alguna fuente vaya a pedir;
    # `set_workers` lo ajusta al entrar en cada una.
    selected_urls = [
        pu for pu in profile.get("urls", [])
        if source_categories is None
        or source_category(pu.get("url") or "") in source_categories
    ]
    def _eligible(pu: dict) -> bool:
        return bool(
            pu.get("enabled")
            or (force_full and pu.get("auto_disabled"))
        )

    pool_max = max(
        [site_workers(find_template(pu["url"]), workers, config)
         for pu in selected_urls
         if _eligible(pu) and pu.get("url")]
        or [workers]
    )

    # Sitios que se abandonan durante ESTA run, y por qué: o nos cerraron la
    # puerta, o se acabó su presupuesto del día. El bucle de fuentes es
    # secuencial, así que basta un dict: en cuanto uno entra, las fuentes
    # siguientes del mismo sitio se saltan sin gastar una sola petición.
    blocked_sites: dict[str, str] = {}
    throttles: dict[str, SiteThrottle] = {}
    throttle_meta: dict[str, dict] = {}

    def _throttle_wait(site: str, kind: str, until: float | None) -> None:
        meta = throttle_meta[site]
        if kind == "quota_bytes":
            reason = f"esperando cupo de {meta['gigabytes']:g} GiB por volumen"
        elif kind == "quota_count_recovery":
            reason = (
                f"esperando recuperar {meta['headroom']} cupos del límite "
                f"de {meta['limit']} archivos"
            )
        elif kind in {"quota", "quota_count"}:
            reason = f"esperando cupo del límite de {meta['limit']} archivos"
        else:
            reason = "esperando una sonda del servidor"
        if until is None:
            release = "se reevalúa al liberar otra transferencia"
        else:
            seconds = max(0, math.ceil(until - time.time()))
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            duration = f"{hours} h {minutes} min {secs} s" if hours else (
                f"{minutes} min {secs} s" if minutes else f"{secs} s"
            )
            release = f"próxima revisión en {duration} ({time.strftime('%H:%M:%S', time.localtime(until))})"
        msg = f"⏸ {site}: transferencias en espera; {reason}; {release}"
        log.warning(msg)
        emit(Log(msg, "warn"))

    def _report_throttle_trip(site: str, seconds: float) -> None:
        meta = throttle_meta[site]
        msg = (
            f"⏸ {site}: {meta['streak']} fallos consecutivos sin respuesta "
            "HTTP; descargas pausadas. "
            f"Se probará un solo archivo en {seconds / 60:.0f} min"
        )
        log.warning(msg)
        emit(Log(msg, "warn"))

    def _defer_throttled_batch(site: str, exc: ThrottleDeferred) -> None:
        if site in blocked_sites:
            return
        meta = throttle_meta[site]
        if exc.kind == "quota_bytes":
            reason = f"espera cupo de {meta['gigabytes']:g} GiB por consumo o reservas"
        elif exc.kind in {"quota_count", "quota_count_recovery"}:
            reason = f"espera cupo del límite de {meta['limit']} archivos"
        else:
            reason = "su sonda de archivos sigue en enfriamiento"
        blocked_sites[site] = reason
        emit(SiteBlocked(site, 0, reason))
        emit(Log(
            f"⏭ {site}: {reason}; esta pasada continúa sin esperar. La cola se conserva.",
            "warn",
        ))

    sources = ordered_sources(selected_urls)
    if source_categories is None and sources != profile["urls"]:
        emit(Log(
            "↕ Fuentes reordenadas: pago → gratuitas → repositorios → legado",
            "dim",
        ))

    async with DownloadEngine(config, workers=pool_max) as engine:
        for pu in sources:
            if not _eligible(pu) or not pu["url"]:
                continue

            async def _set_unavailable(detail: str) -> None:
                if not (validate_sources or force_full):
                    return
                if not isinstance(pu.get("id"), int) or pu["id"] <= 0:
                    return
                reason = _failure_detail(detail)
                await set_profile_url_availability(
                    INDEX_DB, pu["id"], available=False, reason=reason,
                )
                pu["enabled"] = False
                pu["auto_disabled"] = True
                pu["auto_disabled_reason"] = reason
                emit(Log(
                    f"⏭ Fuente desactivada automáticamente: {reason}. "
                    "Se volverá a probar en un Rescan completo.",
                    "warn",
                ))

            async def _set_available() -> None:
                if not pu.get("auto_disabled"):
                    return
                if not isinstance(pu.get("id"), int) or pu["id"] <= 0:
                    return
                await set_profile_url_availability(
                    INDEX_DB, pu["id"], available=True,
                )
                pu["enabled"] = True
                pu["auto_disabled"] = False
                pu["auto_disabled_reason"] = None
                emit(Log("✓ Fuente reactivada automáticamente", "ok"))

            if global_state["reached"]:
                if not global_state["notified"]:
                    global_state["notified"] = True
                    spent = budget_spent_today(GLOBAL_BUDGET_KEY)
                    detail = (
                        f"límite diario global agotado "
                        f"({config.daily_limit_gb:g} GB)"
                    )
                    emit(SiteBlocked("global", 0, detail))
                    emit(Log(
                        f"⛔ Límite diario global agotado: "
                        f"{spent / 2**30:.1f} de {config.daily_limit_gb:g} GB "
                        "hoy. No se consultarán servidores hasta mañana.",
                        "warn",
                    ))
                break

            template = get_template(pu["url"], engine)
            if not template:
                emit(Log(f"Sin template para: {pu['url']}", "error"))
                await _set_unavailable("URL sin proveedor compatible")
                continue

            policy = resolve_site_policy(type(template), config, workers)
            source_metrics = SiteRunMetrics(
                site=template.name,
                source_url=pu["url"],
                workers=policy.transfer.workers,
                transfer_delay_min=policy.transfer.delay_min or 0.0,
                transfer_delay_max=policy.transfer.delay_max or 0.0,
            )
            set_network_observer = getattr(
                template, "set_network_observer", None,
            )
            if set_network_observer is not None:
                set_network_observer(source_metrics.observe)
            source_metrics.start_scan()

            if not getattr(template, "download_enabled", True):
                emit(Log(
                    f"⏭ {template.name}: fuera de servicio; se conserva la URL "
                    f"pero no se consulta",
                    "warn",
                ))
                await _set_unavailable(f"{template.name}: servicio fuera de línea")
                continue

            if template.name in blocked_sites:
                emit(Log(
                    f"⏭ {pu['url']}  [{blocked_sites[template.name]} — se salta]",
                    "warn",
                ))
                continue

            # Presupuesto del día. Se mira ANTES de escanear: un scan que
            # encola trabajo que no se va a hacer gasta peticiones para nada.
            transfer_policy = policy.transfer
            scan_policy = policy.scan
            day_budget = transfer_policy.daily_byte_budget or 0
            left = budget_remaining(template.name, day_budget)
            if left == 0:
                blocked_sites[template.name] = (
                    f"{template.name}: presupuesto de hoy agotado "
                    f"({day_budget / 2**30:.0f} GB)"
                )
                emit(SiteBlocked(template.name, 0, blocked_sites[template.name]))
                emit(Log(
                    f"⛔ {template.name}: se llegó al tope de hoy "
                    f"({day_budget / 2**30:.0f} GB). Sigue mañana.", "warn",
                ))
                continue

            # El pool pertenece a transferencias; durante API/scan se activa
            # una política separada para que sus pausas y reintentos no se
            # filtren a los cuerpos de archivos (ni a la inversa).
            src_workers = transfer_policy.workers
            engine.set_workers(src_workers)
            emit(WorkersResolved(src_workers))

            engine.set_scan_policy(scan_policy)
            engine.set_pace(scan_policy.min_request_interval)
            engine.set_rate(0)
            engine.set_completion_gap(0)
            if scan_policy.min_request_interval:
                emit(Log(
                    f"⏱ {template.name}: API cada "
                    f"{scan_policy.min_request_interval:g}s", "dim",
                ))

            pace = transfer_policy.min_request_interval
            rate = transfer_policy.max_bytes_per_sec or 0.0
            completion_gap = transfer_policy.post_download_delay or 0.0
            download_transport = transfer_policy.transport
            browser_origin = transfer_policy.browser_origin
            window_limit = transfer_policy.download_window_limit or 0
            window_seconds = transfer_policy.download_window_seconds or 0
            if template.name == "pawchive":
                window_limit = config.pawchive.downloads_per_window
                window_seconds = config.pawchive.window_hours * 60 * 60
            byte_window_limit = (
                int(config.pawchive.gigabytes_per_window * 1024**3)
                if template.name == "pawchive" else 0
            )
            failure_codes = transfer_policy.throttle_failure_codes
            throttle = None
            quota_progress: dict[int, int] = {}
            quota_notice_at = 0.0

            def _emit_quota_status(*, force: bool = False) -> None:
                """Publica Pawchive como máximo cuatro veces por segundo.

                Los callbacks de progreso llegan por chunk; sin este freno el
                contador visual podría atascar el bucle de Qt igual que antes
                lo hacían las barras de cada worker.
                """
                nonlocal quota_notice_at
                if template.name != "pawchive" or throttle is None:
                    return
                now = time.monotonic()
                if not force and now - quota_notice_at < 0.25:
                    return
                quota_notice_at = now
                emit(QuotaStatus(
                    throttle.snapshot(),
                    sum(max(0, value) for value in quota_progress.values()),
                ))

            if window_limit or failure_codes:
                throttle_meta[template.name] = {
                    "limit": window_limit,
                    "window": window_seconds,
                    "gigabytes": (
                        config.pawchive.gigabytes_per_window
                        if template.name == "pawchive" else 0
                    ),
                    "headroom": (
                        config.pawchive.resume_headroom
                        if template.name == "pawchive" else 0
                    ),
                    "streak": transfer_policy.throttle_failure_streak,
                    "codes": failure_codes,
                }
                throttle = throttles.get(template.name)
                if throttle is None:
                    throttle = SiteThrottle(
                        template.name,
                        limit=window_limit,
                        byte_limit=byte_window_limit,
                        resume_headroom=(
                            config.pawchive.resume_headroom
                            if template.name == "pawchive" else 0
                        ),
                        window_seconds=window_seconds,
                        failure_threshold=transfer_policy.throttle_failure_streak,
                        failure_window_seconds=transfer_policy.throttle_failure_window,
                        cooldowns=transfer_policy.throttle_cooldowns,
                    )
                    if not failure_codes:
                        # La política pudo retirar un cortacircuitos anterior
                        # (Pawchive dejó CDP en 2026-09-16). Su enfriamiento
                        # persistido ya no debe bloquear el transporte nuevo;
                        # las cuotas de archivos y bytes se conservan.
                        await throttle.clear_circuit()
                    throttles[template.name] = throttle
                _emit_quota_status(force=True)

            # ── Resolver artista (puede disparar lazy auth / modal) ─────────
            scan_started_at = _scan_watermark()
            pixiv_auth_refreshed = False
            try:
                artist_info = await template.get_artist_info(pu["url"])
            except (NeedsManualAuth, NeedsPixivAuth) as exc:
                if not await _resolve_required_auth(
                    template, exc, resolve_auth, emit,
                ):
                    _publish_site_metrics(source_metrics, emit)
                    continue
                pixiv_auth_refreshed = template.name == "pixiv"
                if pixiv_auth_refreshed:
                    emit(Log(
                        "Sesión Pixiv renovada; se comprobará el historial completo.",
                        "dim",
                    ))
                try:
                    artist_info = await template.get_artist_info(pu["url"])
                except (NeedsManualAuth, NeedsPixivAuth) as exc2:
                    discard = getattr(template, "discard_auth_client", None)
                    if discard is not None:
                        await discard()
                    emit(Log(f"✗ Error tras auth: {exc2}", "error"))
                    await _set_unavailable(str(exc2))
                    _publish_site_metrics(source_metrics, emit)
                    continue
                except Exception as exc2:
                    emit(Log(f"✗ Error tras auth: {exc2}", "error"))
                    await _set_unavailable(str(exc2))
                    _publish_site_metrics(source_metrics, emit)
                    continue
            except Exception as exc:
                emit(Log(
                    f"✗ Error resolviendo fuente ({type(exc).__name__}): {exc}",
                    "error",
                ))
                await _set_unavailable(f"{type(exc).__name__}: {exc}")
                _publish_site_metrics(source_metrics, emit)
                continue

            emit(SourceStarted(pu["url"], artist_info.name, pu["site"]))
            emit(Log(f"▶ {artist_info.name} ({pu['site']})", "info"))

            folder.mkdir(parents=True, exist_ok=True)
            await init_catalog(folder)
            await init_index(INDEX_DB)
            site_id = await get_or_create_site(INDEX_DB, artist_info.site)
            await get_or_create_artist(
                db_path=INDEX_DB,
                site_id=site_id,
                artist_id=artist_info.artist_id,
                name=artist_info.name,
                folder_path=folder,
            )
            await update_profile_url_sync(
                INDEX_DB, pu["id"], artist_id=artist_info.artist_id
            )

            # Frontera incremental: usa last_synced salvo Rescan forzado.
            url_since: datetime | None = None
            if not force_full and pu.get("last_synced"):
                url_since = parse_date_utc(pu["last_synced"])
                if url_since:
                    emit(Log(f"Sync desde {pu['last_synced'][:16]}", "dim"))
            elif force_full:
                emit(Log("Rescan completo desde el inicio…", "dim"))

            dl_before = summ.downloaded
            local_hashes = await _build_local_hash_map(folder)
            pu_id: int | None = pu.get("id")

            # Limpiar pendientes que ya están en el catálogo.
            cleaned = await clean_pending_catalog_overlap(folder, pu_id)
            if cleaned:
                emit(Log(f"⊘ {cleaned} ya descargadas eliminadas de la cola", "dim"))

            if template.name == "pawchive":
                due = await reactivate_awaiting_archive(
                    folder, pu_id, limit=ARCHIVE_RECHECK_BATCH, due_only=True,
                )
                if due:
                    emit(Log(
                        f"↺ {due} original(es) de Pawchive vuelven a revisión semanal",
                        "dim",
                    ))

            existing_pending = await pending_count(folder, pu_id)

            new_this_scan = 0
            preview_markers = 0

            # ── Fase 1: escaneo ─────────────────────────────────────────────
            # El filtro de extensión NO influye en el escaneo: el scan puebla
            # pending_queue con TODOS los tipos y el filtro se aplica al
            # descargar (producer). Por eso cambiar el filtro nunca re-escanea
            # el historial — los tipos ya descubiertos siguen en pending_queue,
            # con su extensión en filename_hint, y se descargan sin tocar la red.
            #
            # Alcance del scan:
            #   • completo (desde el inicio) si nunca terminó uno con éxito
            #     (primera vez sin last_synced, o un completo previo que quedó a
            #     medias), o si el usuario forzó Rescan;
            #   • incremental (since=last_synced) en caso normal — Patreon corta
            #     la paginación al cruzar la fecha, así que es barato y no
            #     martillea al servidor.
            full_done = await get_meta_int(folder, "full_scan_done")
            # A legacy timestamp alone cannot prove full historical coverage:
            # older artist/count writes advanced it before discovery completed.
            need_full = force_full or pixiv_auth_refreshed or not full_done
            scan_since = None if need_full else url_since
            emit(Log(
                "Escaneo completo del historial…" if need_full
                else "Buscando novedades (incremental)…",
                "dim",
            ))
            if existing_pending > 0:
                emit(Log(f"↺ {existing_pending} en cola de sesiones previas", "dim"))

            seen_scan: set[str] = set()
            scan_files_seen = 0
            scan_ok = True
            auth_retried = False
            scan_catalog = await ScanCatalogBatch.open(folder)
            try:
                while True:
                    try:
                        async for fi in template.iter_files(artist_info, since=scan_since):
                            scan_files_seen += 1
                            # Señal de vida durante el paginado (Fase 1 no descarga).
                            if scan_files_seen % 25 == 0:
                                emit(ScanProgress(scan_files_seen, new_this_scan))
                            key = fi.dedup_key
                            if key in seen_scan:
                                continue
                            seen_scan.add(key)
                            matched_hash = scan_catalog.find_downloaded_hash(
                                key,
                                fi.remote_hash,
                                service=fi.content_service or artist_info.service,
                                post_id=fi.post_id,
                                original_filename=(
                                    fi.original_filename or fi.filename
                                ),
                                position=fi.content_position,
                                remote_size=fi.remote_size,
                            )
                            if fi.is_preview:
                                if matched_hash:
                                    scan_catalog.resolve_preview_only(key)
                                elif await scan_catalog.stage_preview_only(
                                        url_source=key,
                                        filename_hint=fi.filename,
                                        post_id=fi.post_id,
                                        post_published=fi.date_published,
                                        observed_hash=fi.remote_hash,
                                        content_service=(
                                            fi.content_service or artist_info.service
                                        ),
                                        original_filename=(
                                            fi.original_filename or fi.filename
                                        ),
                                        content_role=fi.content_role,
                                        content_position=fi.content_position,
                                        profile_url_id=pu_id,
                                ):
                                    preview_markers += 1
                                continue
                            if matched_hash:
                                scan_catalog.resolve_preview_only(key)
                                await scan_catalog.stage_downloaded_url(key, matched_hash)
                                await scan_catalog.stage_content_alias(
                                    matched_hash,
                                    fi.content_service or artist_info.service,
                                    fi.post_id,
                                    fi.original_filename or fi.filename,
                                    fi.content_role,
                                    fi.content_position,
                                    fi.remote_size,
                                )
                                continue
                            if await scan_catalog.stage_pending(
                                    url_source=key,
                                    download_url=fi.url,
                                    filename_hint=fi.filename,
                                    post_id=fi.post_id,
                                    post_published=fi.date_published,
                                    remote_hash=fi.remote_hash,
                                    content_service=(
                                        fi.content_service or artist_info.service
                                    ),
                                    original_filename=(
                                        fi.original_filename or fi.filename
                                    ),
                                    content_role=fi.content_role,
                                    content_position=fi.content_position,
                                    remote_size=fi.remote_size,
                                    extra_headers=(
                                        json.dumps(fi.extra_headers)
                                        if fi.extra_headers else None
                                    ),
                                    profile_url_id=pu_id,
                            ):
                                new_this_scan += 1
                    except asyncio.CancelledError:
                        raise
                    except (NeedsManualAuth, NeedsPixivAuth) as scan_auth:
                        if auth_retried or not await _resolve_required_auth(
                            template, scan_auth, resolve_auth, emit,
                        ):
                            scan_ok = False
                            if auth_retried and scan_files_seen == 0:
                                await _set_unavailable(str(scan_auth))
                            emit(Log("⚠ Scan parcial — usá Sync para continuar.", "warn"))
                            break
                        auth_retried = True
                        emit(Log("↺ Sesión renovada; retomando el escaneo…", "info"))
                        continue
                    except Exception as scan_exc:
                        scan_ok = False
                        if scan_files_seen == 0:
                            await _set_unavailable(
                                f"{type(scan_exc).__name__}: {scan_exc}"
                            )
                        emit(Log(
                            f"✗ Error en escaneo ({type(scan_exc).__name__}): {scan_exc}",
                            "error",
                        ))
                        emit(Log("⚠ Scan parcial — usá Sync para continuar.", "warn"))
                        break
                    else:
                        break
            finally:
                # Conserva hasta el último bloque parcial ante error o cancelación.
                try:
                    await _close_scan_catalog(scan_catalog)
                finally:
                    source_metrics.finish_scan(scan_files_seen)

            if scan_ok:
                await _set_available()

            # Every catalog batch is durable and closed before advancing discovery.
            # This frontier belongs to discovery, independently of later downloads.
            if scan_ok:
                if need_full:
                    await set_meta_int(folder, "full_scan_done", 1)
                await update_profile_url_sync(INDEX_DB,pu["id"],last_synced=scan_started_at)
            emit(Log(f"Scan: {new_this_scan} nuevo(s) en cola", "dim"))
            if preview_markers:
                emit(Log(
                    f"Scan: {preview_markers} preview(s) marcada(s), sin descarga",
                    "dim",
                ))

            threshold = scan_policy.cooldown_threshold
            cooldown = scan_policy.cooldown_seconds
            if new_this_scan >= threshold:
                emit(Cooldown(cooldown))
                emit(Log(f"⏸ Enfriamiento {cooldown:.0f}s antes de descargar…", "warn"))
                try:
                    await asyncio.sleep(cooldown)
                finally:
                    emit(Cooldown(0))

            if scan_only:
                count = await pending_count(folder, pu_id)
                emit(Log(
                    f"⏳ {count} archivo(s) en cola" if count else "✓ Ya está al día",
                    "info" if count else "ok",
                ))
                _publish_site_metrics(source_metrics, emit)
                continue

            # ── Fase 2: descargar desde la cola ─────────────────────────────
            pending_list = await get_pending_files(folder, pu_id)
            if ext_filter:
                pending_list = [
                    item for item in pending_list
                    if _passes_ext_filter(
                        item.get("filename_hint") or "",
                        ext_filter,
                        exclude_mode,
                    )
                ]
            if not pending_list:
                emit(Log("✓ Sin archivos nuevos para el filtro activo", "ok"))
                await update_profile_url_sync(
                    INDEX_DB, pu["id"], file_count=pu.get("file_count") or 0,
                )
                continue

            # Una fila que ya falló en otra sesión se vuelve a intentar, pero
            # su anomalía conocida no prueba que el sitio completo esté caído.
            known_deferred = {
                item["url_source"] for item in pending_list
                if int(item.get("deferred_attempts") or 0) > 0
            }

            engine.set_transfer_policy(transfer_policy)
            engine.set_pace(pace)
            engine.set_rate(rate)
            engine.set_completion_gap(completion_gap)
            if pace:
                emit(Log(
                    f"⏱ {template.name}: transferencias cada {pace:g}s "
                    f"entre {src_workers} workers", "dim",
                ))
            if rate:
                queda = (f", {left / 2**30:.1f} GB de presupuesto hoy"
                         if left >= 0 else "")
                emit(Log(
                    f"⏱ {template.name}: techo de {rate / 2**20:.1f} MB/s "
                    f"entre todas las descargas{queda}", "dim",
                ))
            if completion_gap:
                emit(Log(
                    f"⏳ {template.name}: {completion_gap:.0f}s entre terminar "
                    f"un archivo y empezar el siguiente", "dim",
                ))

            # Progreso "X / N" con resume.
            batch_key = f"pending_batch_{pu_id}"
            if ext_filter or existing_pending == 0:
                batch_total = len(pending_list)
                batch_offset = 0
                await set_meta_int(folder, batch_key, batch_total)
            else:
                stored = await get_meta_int(folder, batch_key)
                if stored and stored >= len(pending_list):
                    batch_total = stored
                    batch_offset = stored - len(pending_list)
                else:
                    batch_total = len(pending_list)
                    batch_offset = 0
            emit(BatchInfo(batch_total, batch_offset))
            progress.update(done=batch_offset, total=batch_total)
            emit(FileProgress(batch_offset, batch_total))

            resume_note = f" — retomando desde {batch_offset} / {batch_total}" if batch_offset else ""
            emit(Log(f"Descargando {len(pending_list)} archivo(s){resume_note}", "info"))

            file_queue: asyncio.Queue = asyncio.Queue(maxsize=src_workers * 3)
            source_metrics.start_transfer()

            # Ventana de rechazos y su motivo. Un dict y no un `nonlocal` porque
            # lo comparten el productor y N workers, todos closures de aquí.
            reject = {
                "recent": deque(maxlen=_BLOCK_WINDOW),
                "detail": "",
            }

            def _blocked() -> bool:
                # `blocked_sites` manda. Si el bloqueo viviera únicamente en la
                # ventana, otro worker podría alterarla con un resultado que ya
                # venía en vuelo y la run seguiría como si nada.
                return (global_state["reached"]
                        or template.name in blocked_sites
                        or sum(reject["recent"]) >= _BLOCK_STREAK)

            async def producer() -> None:
                """Alimenta la cola de workers; ext_filter se aplica aquí para
                dejar lo no-coincidente en pending_queue para futuras sesiones."""
                try:
                    for pf in pending_list:
                        if _blocked():
                            break
                        extra: dict = {}
                        if pf.get("extra_headers"):
                            try:
                                extra = json.loads(pf["extra_headers"])
                            except Exception:
                                pass
                        fi = FileInfo(
                            url=pf["download_url"],
                            url_source=pf["url_source"],
                            filename=pf["filename_hint"],
                            artist_id=artist_info.artist_id,
                            artist_name=artist_info.name,
                            post_id=pf.get("post_id") or "",
                            date_published=pf.get("post_published") or "",
                            remote_hash=pf.get("remote_hash") or "",
                            content_service=(
                                pf.get("content_service") or artist_info.service
                            ),
                            original_filename=(
                                pf.get("original_filename")
                                or pf["filename_hint"]
                            ),
                            content_role=pf.get("content_role") or "",
                            content_position=(
                                pf.get("content_position")
                                if pf.get("content_position") is not None else -1
                            ),
                            remote_size=pf.get("remote_size"),
                            is_preview=bool(pf.get("is_preview")),
                            extra_headers=template.transfer_headers(
                                artist_info, extra,
                            ),
                        )
                        # La cola llena es backpressure normal: un archivo grande
                        # de Pawchive puede ocupar ambos workers durante bastante
                        # más de dos minutos. Expirar este put mataba al productor
                        # sin centinelas; al terminar, los workers esperaban para
                        # siempre y el lote no avanzaba al proyecto siguiente.
                        await _enqueue_with_backpressure(file_queue, fi)
                    # Terminación normal: un centinela None por worker para que
                    # cada bucle corte. Solo aquí — en cancelación NO se encolan:
                    # los workers ya fueron cancelados por el gather padre y no
                    # consumen, así que un put() en cola llena se colgaría sin
                    # consumidor (deadlock). El except deja propagar el cancel.
                    for _ in range(src_workers):
                        await file_queue.put(None)
                except asyncio.CancelledError:
                    raise

            in_progress_hashes: set[str] = set()
            transfer_status = TransferStatus(src_workers)
            for state in transfer_status.workers.values():
                emit(state)

            def _worker_state(
                slot: int, phase: str, reason: str = "", until: float | None = None,
            ) -> None:
                state = WorkerState(slot, phase, reason, until)
                emit(state)
                transition = transfer_status.update(state)
                if transition is None:
                    return
                action, waiting = transition
                if action == "paused":
                    _throttle_wait(template.name, waiting.reason, waiting.until)
                else:
                    msg = f"▶ {template.name}: espera terminada; transferencias reanudadas"
                    log.info(msg)
                    emit(Log(msg, "info"))

            async def worker_task(slot_id: int) -> None:
                while True:
                    fi = await file_queue.get()
                    if fi is None:
                        _worker_state(slot_id, "idle")
                        emit(WorkerIdle(slot_id))
                        break

                    # Sitio bloqueado: se vacía la cola SIN tocar la red. Salir
                    # del bucle aquí dejaría al productor colgado en un put()
                    # sobre una cola llena sin consumidor; drenarla lo deja
                    # terminar y el cierre sigue siendo el normal. Los archivos
                    # no se tocan: siguen en pending_queue para la próxima sync.
                    if _blocked():
                        continue

                    if fi.remote_hash and fi.remote_hash in in_progress_hashes:
                        summ.skipped += 1
                        emit(Log(f"— {fi.filename[:60]}  [hash en progreso]", "dim"))
                        _counters()
                        _file_finished()
                        continue
                    if fi.remote_hash:
                        in_progress_hashes.add(fi.remote_hash)

                    try:
                        throttle_permit = None
                        if (fi.remote_hash and await hash_exists(
                                folder, fi.remote_hash, include_previews=False)):
                            summ.skipped += 1
                            await _remember_content_alias(folder, fi, fi.remote_hash)
                            emit(Log(f"— {fi.filename[:60]}  [hash en catálogo]", "dim"))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue
                        logical_hash = await content_alias_hash(
                            folder,
                            fi.content_service,
                            fi.post_id,
                            fi.original_filename or fi.filename,
                            fi.content_position,
                            fi.remote_size,
                        )
                        if logical_hash:
                            summ.skipped += 1
                            emit(Log(
                                f"— {fi.filename[:60]}  [identidad ya catalogada]",
                                "dim",
                            ))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue
                        if await url_exists(
                                folder, fi.dedup_key, include_previews=False):
                            summ.skipped += 1
                            emit(Log(f"— {fi.filename[:60]}  [URL en catálogo]", "dim"))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue

                        if (throttle is not None and throttle.byte_limit
                                and int(fi.remote_size or 0) > throttle.byte_limit):
                            await defer_pending_to_next_session(folder, fi.dedup_key)
                            summ.deferred += 1
                            emit(WorkerDone(slot_id, fi.filename, "⏸"))
                            emit(Log(
                                f"⏸ {fi.filename[:55]}  [el archivo supera la cuota "
                                "completa; aumentar la cuota para descargarlo]", "warn",
                            ))
                            _counters()
                            continue

                        counter, final_name, download_folder, catalog_name = await _reserve_destination(
                            folder, artist_info.name, fi.filename)
                        emit(WorkerStart(slot_id, fi.filename))

                        def make_cb(s: int) -> Callable[[int, int], None]:
                            def cb(done: int, total: int) -> None:
                                if template.name == "pawchive":
                                    quota_progress[s] = max(0, int(done))
                                    _emit_quota_status()
                                emit(WorkerProgress(s, done, total))
                            return cb

                        if throttle is not None:
                            throttle_permit = await throttle.acquire(
                                expected_bytes=fi.remote_size,
                                wait=_wait_on_site_throttle(source_categories),
                                on_state=lambda kind, until: _worker_state(
                                    slot_id, "waiting", kind, until,
                                ),
                            )
                            _emit_quota_status(force=True)

                        _worker_state(slot_id, "downloading")

                        async def _reserve_http_size(size: int) -> int:
                            return await throttle.resize(throttle_permit, size)

                        async def _download_current_file():
                            return await asyncio.wait_for(
                                engine.download(
                                    url=fi.url,
                                    dest_dir=download_folder,
                                    filename=final_name,
                                    on_progress=make_cb(slot_id),
                                    **({"max_file_bytes": throttle_permit.reserved_bytes,
                                        "on_file_size": _reserve_http_size}
                                       if throttle is not None and throttle.byte_limit else {}),
                                    extra_headers=fi.extra_headers or None,
                                    total_timeout=300.0,
                                    transport=download_transport,
                                    browser_origin=browser_origin,
                                    on_observation=source_metrics.observe,
                                ),
                                timeout=7200.0,
                            )

                        try:
                            result = await _download_current_file()
                            result = await _confirm_failed_probe_once(
                                throttle_permit, result,
                                _download_current_file, emit,
                            )
                        except asyncio.TimeoutError:
                            if throttle is not None:
                                await throttle.finish(
                                    throttle_permit, success=False,
                                    throttle_failure=False,
                                )
                                quota_progress.pop(slot_id, None)
                                _emit_quota_status(force=True)
                            emit(WorkerDone(slot_id, fi.filename, "⏸"))
                            emit(Log(f"⏸ {fi.filename[:55]}  [timeout — diferido]", "warn"))
                            await defer_pending_to_next_session(
                                folder, fi.dedup_key,
                            )
                            summ.deferred += 1
                            _counters()
                            continue

                        if throttle is not None:
                            trip = await throttle.finish(
                                throttle_permit,
                                success=result.ok,
                                throttle_failure=(
                                    not result.ok
                                    and getattr(result, "failure_code", None)
                                    in failure_codes
                                ),
                                transferred_bytes=result.file_size,
                            )
                            quota_progress.pop(slot_id, None)
                            _emit_quota_status(force=True)
                            if trip is not None:
                                _report_throttle_trip(template.name, trip.seconds)

                        if not result.ok:
                            failure_detail = _failure_detail(result.error)
                            log.warning(
                                "cherry: descarga fallida | sitio=%s | archivo=%r "
                                "| tipo=%s | detalle=%s",
                                template.name, fi.filename, result.error_kind,
                                failure_detail,
                            )
                            was_deferred = fi.dedup_key in known_deferred
                            if result.error_kind != ErrorKind.NOT_FOUND:
                                await defer_pending_to_next_session(
                                    folder, fi.dedup_key,
                                )
                            is_rejection = result.error_kind in _REJECTIONS
                            raw_failure = result.error or ""
                            hard_stop = (
                                is_rejection
                                and any(
                                    marker in raw_failure.casefold()
                                    for marker in _HARD_STOP_MARKERS
                                )
                            )
                            # Un problema persistente de ESTE archivo no entra
                            # en el guardrail del sitio. Una orden humana de
                            # detenerse sí manda, aunque venga de un diferido.
                            if not was_deferred:
                                reject["recent"].append(is_rejection)
                            if is_rejection:
                                if hard_stop or not was_deferred:
                                    reject["detail"] = raw_failure
                                rejection_count = sum(reject["recent"])
                                if (template.name not in blocked_sites
                                        and (hard_stop
                                             or (not was_deferred and
                                                 rejection_count >= _BLOCK_STREAK))):
                                    reason = (
                                        f"{template.name} pidió detener las peticiones"
                                        if hard_stop else
                                        f"{template.name} rechazó {rejection_count} de "
                                        f"las últimas {len(reject['recent'])} descargas"
                                    )
                                    blocked_sites[template.name] = reason
                                    emit(SiteBlocked(
                                        template.name, rejection_count,
                                        reject["detail"],
                                    ))
                                    emit(Log(
                                        f"⛔ {reason} — se abandona el sitio en esta run. "
                                        f"Los archivos siguen en la cola.", "error",
                                    ))
                                    if reject["detail"]:
                                        emit(Log(f"   {reject['detail']}", "warn"))

                            if was_deferred and not hard_stop:
                                emit(Log(
                                    f"↻ {fi.filename[:50]}  [atascado conocido; "
                                    "no cuenta para el freno del sitio]",
                                    "dim",
                                ))

                            if result.error_kind in ErrorKind.DEFERRABLE:
                                summ.deferred += 1
                                emit(WorkerDone(slot_id, fi.filename, "⏸"))
                                emit(Log(
                                    f"⏸ {fi.filename[:55]}  [{result.error_kind}] "
                                    f"{failure_detail}", "warn",
                                ))
                            elif result.error_kind == ErrorKind.NOT_FOUND:
                                state = await _not_found_action(
                                    template, artist_info, fi,
                                )
                                if state == "awaiting":
                                    await defer_until_archived(folder, fi.dedup_key)
                                    summ.awaiting_archive += 1
                                    emit(WorkerDone(slot_id, fi.filename, "◷"))
                                    emit(Log(
                                        f"◷ {fi.filename[:45]}  [Pawchive aún no "
                                        "archivó el original]", "warn",
                                    ))
                                elif state == "unknown":
                                    await defer_pending_to_next_session(
                                        folder, fi.dedup_key,
                                    )
                                    summ.deferred += 1
                                    emit(WorkerDone(slot_id, fi.filename, "⏸"))
                                    emit(Log(
                                        f"⏸ {fi.filename[:45]}  [404 sin poder "
                                        "comprobar la publicación — se conserva]", "warn",
                                    ))
                                else:
                                    summ.dropped += 1
                                    await remove_pending(folder, fi.dedup_key)
                                    emit(WorkerDone(slot_id, fi.filename, "⊘"))
                                    emit(Log(
                                        f"⊘ {fi.filename[:45]}  "
                                        f"[404 definitivo — fuera de la cola]", "dim",
                                    ))
                            else:
                                summ.errors += 1
                                emit(WorkerDone(slot_id, fi.filename, "✗"))
                                emit(Log(f"✗ {fi.filename[:45]}:  {result.error}", "error"))
                            _counters()
                            continue

                        # Un éxito pesa en la ventana, pero no borra de golpe lo
                        # que acaba de decir el sitio. En la prueba real hubo 6
                        # rechazos de 8 con dos éxitos intercalados.
                        reject["recent"].append(False)

                        # Lo traído se anota aunque el archivo acabe siendo un
                        # duplicado que se borra: el ancho de banda del sitio ya
                        # se gastó, que es lo que el presupuesto cuenta.
                        _record_global_download(result.file_size)
                        source_metrics.downloaded(result.file_size)
                        if day_budget and result.file_size:
                            gastado = budget_add(template.name, result.file_size)
                            if gastado >= day_budget and template.name not in blocked_sites:
                                blocked_sites[template.name] = (
                                    f"{template.name}: presupuesto de hoy agotado "
                                    f"({day_budget / 2**30:.0f} GB)"
                                )
                                emit(SiteBlocked(
                                    template.name, 0, blocked_sites[template.name],
                                ))
                                emit(Log(
                                    f"⛔ {template.name}: {gastado / 2**30:.1f} GB hoy, "
                                    f"tope {day_budget / 2**30:.0f} GB. Sigue mañana; "
                                    f"lo que falta se queda en la cola.", "warn",
                                ))

                        if result.file_hash is None:
                            summ.errors += 1
                            await defer_pending_to_next_session(
                                folder, fi.dedup_key,
                            )
                            emit(Log(f"✗ {fi.filename[:50]}: bug interno — hash nulo", "error"))
                            if result.dest and result.dest.exists():
                                result.dest.unlink()
                            continue

                        # Dedup post-descarga (mismo contenido ya catalogado).
                        if await hash_exists(folder, result.file_hash, include_previews=False):
                            summ.skipped += 1
                            await _remember_content_alias(
                                folder, fi, result.file_hash,
                            )
                            if result.dest and result.dest.exists():
                                result.dest.unlink()
                            emit(WorkerIdle(slot_id))
                            emit(Log(f"≡ {fi.filename[:60]}  [duplicado — hash ya catalogado]", "dim"))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue

                        # Catalogar.
                        if result.file_hash in local_hashes:
                            old_path = local_hashes[result.file_hash]
                            new_path = folder / catalog_name
                            new_path.parent.mkdir(parents=True, exist_ok=True)
                            try:
                                old_path.replace(new_path)
                                local_hashes[result.file_hash] = new_path
                                if result.dest and result.dest.exists():
                                    result.dest.unlink()
                                renamed = True
                            except OSError:
                                try:
                                    old_path.unlink()
                                except OSError:
                                    pass
                                local_hashes[result.file_hash] = result.dest
                                renamed = False
                            await add_file(
                                artist_dir=folder, file_hash=result.file_hash,
                                filename=catalog_name, url_source=fi.dedup_key,
                                file_size=result.file_size, counter=counter,
                                is_preview=fi.is_preview,
                                content_service=fi.content_service,
                                post_id=fi.post_id,
                                content_role=fi.content_role,
                                content_position=fi.content_position,
                            )
                            await _remember_content_alias(
                                folder, fi, result.file_hash, url_recorded=True,
                            )
                            await remove_pending(folder, fi.dedup_key)
                            summ.downloaded += 1
                            icon = "↷" if renamed else "✓"
                            emit(WorkerDone(slot_id, final_name, icon))
                            emit(Log(
                                f"{icon} {fi.filename[:40]}  →  {final_name}"
                                + ("  [renombrado]" if renamed else ""),
                                "ok",
                            ))
                        else:
                            await add_file(
                                artist_dir=folder, file_hash=result.file_hash,
                                filename=catalog_name, url_source=fi.dedup_key,
                                file_size=result.file_size, counter=counter,
                                is_preview=fi.is_preview,
                                content_service=fi.content_service,
                                post_id=fi.post_id,
                                content_role=fi.content_role,
                                content_position=fi.content_position,
                            )
                            await _remember_content_alias(
                                folder, fi, result.file_hash, url_recorded=True,
                            )
                            await remove_pending(folder, fi.dedup_key)
                            local_hashes[result.file_hash] = result.dest
                            summ.downloaded += 1
                            emit(WorkerDone(slot_id, catalog_name, "✓"))
                            emit(Log(f"✓ {fi.filename[:40]}  →  {catalog_name}", "ok"))

                        _counters()

                    except ThrottleDeferred as exc:
                        _defer_throttled_batch(template.name, exc)
                        emit(WorkerDone(slot_id, fi.filename, "⏭"))
                    except asyncio.CancelledError:
                        if throttle is not None:
                            await throttle.abandon(locals().get("throttle_permit"))
                        raise
                    except Exception as exc:
                        import traceback
                        summ.errors += 1
                        await defer_pending_to_next_session(
                            folder, fi.dedup_key,
                        )
                        emit(WorkerDone(slot_id, fi.filename, "✗"))
                        emit(Log(
                            f"✗ {fi.filename[:45]}  [{type(exc).__name__}: {exc}]",
                            "error",
                        ))
                        emit(Log(traceback.format_exc().splitlines()[-1], "dim"))
                        _counters()
                    finally:
                        if throttle is not None:
                            await throttle.abandon(locals().get("throttle_permit"))
                        _worker_state(slot_id, "preparing")
                        if fi.remote_hash:
                            in_progress_hashes.discard(fi.remote_hash)
                        _file_finished()

            all_tasks = [
                asyncio.create_task(producer(), name="producer"),
                *[asyncio.create_task(worker_task(i), name=f"worker-{i}")
                  for i in range(src_workers)],
            ]
            await _gather_transfer_tasks(all_tasks)

            source_dl = summ.downloaded - dl_before
            source_metrics.finish_transfer()
            new_count = (pu["file_count"] or 0) + source_dl
            await update_profile_url_sync(INDEX_DB, pu["id"], file_count=new_count)
            emit(SourceDone(pu["id"], new_count))
            _publish_site_metrics(source_metrics, emit)
            if global_state["reached"]:
                break

    # ── Resumen final ───────────────────────────────────────────────────────
    if finalize_profile:
        await update_profile_last_checked(INDEX_DB, profile["id"])
    if scan_only:
        total_q = await pending_count(folder)
        emit(ScanComplete(total_q))
    return summ
