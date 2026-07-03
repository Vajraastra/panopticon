"""
Servicio de descarga compartido, agnóstico de UI.

Encapsula el flujo canónico de descarga en dos fases (extraído de la TUI, que
queda como referencia hasta deprecarse):

    scan API (iter_files, since=last_synced)  →  pending_queue
      → cooldown si el scan fue agresivo (politeness)
      → descarga productor/workers (dedup atómico, remove_pending por éxito)
      → cola diferida (errores temporales)
      → update last_synced + file_count

La UI no conoce asyncio: pasa un callback `emit(evento)` y recibe eventos
tipados (ver más abajo). Todo corre en un único event loop (qasync/textual),
por lo que `emit` es síncrono y seguro para tocar widgets directamente.

Cancelación: cancelar la task que await-ea `run_profile_download` — el flujo
propaga CancelledError limpiamente (cancela productor + workers y re-lanza).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from .config import INDEX_DB, load_config
from .engine import DownloadEngine, ErrorKind
from .downloads import _build_local_hash_map, _passes_ext_filter, build_filename
from .catalog import (
    add_file,
    add_pending,
    clean_pending_catalog_overlap,
    get_meta_int,
    get_pending_files,
    hash_exists,
    init_catalog,
    next_counter,
    pending_count,
    pending_url_exists,
    remove_pending,
    set_meta_int,
    url_exists,
)
from .index import (
    get_or_create_artist,
    get_or_create_site,
    init_index,
    update_profile_last_checked,
    update_profile_url_sync,
)
from .templates._registry import find_template, get_template
from .templates.base import FileInfo, parse_date_utc
from .auth.patreon import NeedsManualAuth
from .auth.pixiv import NeedsPixivAuth


# ── Eventos ─────────────────────────────────────────────────────────────────────
# La UI hace match/isinstance sobre estos y actualiza sus widgets.

@dataclass(frozen=True)
class Log:
    """Línea de log. level ∈ {info, dim, ok, warn, error}."""
    msg: str
    level: str = "info"


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


DownloadEvent = Union[
    Log, WorkersResolved, SourceStarted, Cooldown, BatchInfo,
    WorkerStart, WorkerProgress, WorkerDone, WorkerIdle,
    Counters, SourceDone, ScanComplete, ScanProgress,
]

EmitFn = Callable[[DownloadEvent], None]
# resolve_auth(site) -> True si el usuario se autenticó y se puede reintentar.
AuthResolver = Callable[[str], Awaitable[bool]]


@dataclass
class DownloadSummary:
    downloaded: int = 0
    skipped: int = 0
    errors: int = 0
    deferred: int = 0


# ── Servicio ────────────────────────────────────────────────────────────────────

async def run_profile_download(
    profile: dict,
    *,
    workers: int,
    ext_filter: set[str],
    exclude_mode: bool = False,
    force_full: bool = False,
    scan_only: bool = False,
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
      emit          — callback síncrono que recibe DownloadEvent.
      resolve_auth  — callback async opcional para resolver auth interactiva.
    """
    summ = DownloadSummary()

    def _counters() -> None:
        emit(Counters(summ.downloaded, summ.skipped, summ.errors, summ.deferred))

    config = config or load_config()
    folder = Path(profile["folder_path"])

    # Cap de workers por el template más restrictivo del perfil.
    for pu in profile.get("urls", []):
        if pu.get("enabled") and pu.get("url"):
            cls = find_template(pu["url"])
            if cls and getattr(cls, "max_workers", None):
                workers = min(workers, cls.max_workers)
    emit(WorkersResolved(workers))

    deferred: list[tuple] = []

    async with DownloadEngine(config, workers=workers) as engine:
        for pu in profile["urls"]:
            if not pu["enabled"] or not pu["url"]:
                continue

            template = get_template(pu["url"], engine)
            if not template:
                emit(Log(f"Sin template para: {pu['url']}", "error"))
                continue

            # ── Resolver artista (puede disparar lazy auth / modal) ─────────
            try:
                artist_info = await template.get_artist_info(pu["url"])
            except (NeedsManualAuth, NeedsPixivAuth) as exc:
                site = "pixiv" if isinstance(exc, NeedsPixivAuth) else "patreon"
                emit(Log(f"⚠ {site.capitalize()} requiere autenticación", "warn"))
                if resolve_auth is None or not await resolve_auth(site):
                    emit(Log("✗ Autenticación cancelada", "error"))
                    continue
                try:
                    artist_info = await template.get_artist_info(pu["url"])
                except Exception as exc2:
                    emit(Log(f"✗ Error tras auth: {exc2}", "error"))
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

            existing_pending = await pending_count(folder, pu_id)

            new_this_scan = 0

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
            # Migración legacy: un catálogo ya sincronizado antes (tiene
            # last_synced) es anterior a este flag → se asume con cobertura
            # histórica completa, para no re-escanear todo una vez más.
            if not full_done and url_since is not None:
                await set_meta_int(folder, "full_scan_done", 1)
                full_done = 1
            need_full = force_full or not full_done
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
                    if await url_exists(folder, key):
                        continue
                    if fi.remote_hash and await hash_exists(folder, fi.remote_hash):
                        continue
                    if not await pending_url_exists(folder, key):
                        await add_pending(
                            folder,
                            url_source=key,
                            download_url=fi.url,
                            filename_hint=fi.filename,
                            post_id=fi.post_id,
                            post_published=fi.date_published,
                            remote_hash=fi.remote_hash,
                            extra_headers=(
                                json.dumps(fi.extra_headers)
                                if fi.extra_headers else None
                            ),
                            profile_url_id=pu_id,
                        )
                        new_this_scan += 1
            except asyncio.CancelledError:
                raise
            except Exception as scan_exc:
                scan_ok = False
                emit(Log(
                    f"✗ Error en escaneo ({type(scan_exc).__name__}): {scan_exc}",
                    "error",
                ))
                emit(Log("⚠ Scan parcial — usá Sync para continuar.", "warn"))

            # Marcar cobertura histórica completa SOLO si un scan completo
            # terminó entero (sin error). last_synced se fija al inicio de la
            # run, así que sin este flag un completo interrumpido se tomaría por
            # incremental en la próxima run y perdería el tramo no paginado.
            if need_full and scan_ok and scan_files_seen > 0:
                await set_meta_int(folder, "full_scan_done", 1)
            emit(Log(f"Scan: {new_this_scan} nuevo(s) en cola", "dim"))

            threshold = getattr(template, "cooldown_threshold", 200)
            cooldown = getattr(template, "cooldown_seconds", 10.0)
            if new_this_scan >= threshold:
                emit(Cooldown(cooldown))
                emit(Log(f"⏸ Enfriamiento {cooldown:.0f}s antes de descargar…", "warn"))
                await asyncio.sleep(cooldown)

            if scan_only:
                count = await pending_count(folder, pu_id)
                emit(Log(
                    f"⏳ {count} archivo(s) en cola" if count else "✓ Ya está al día",
                    "info" if count else "ok",
                ))
                continue

            # ── Fase 2: descargar desde la cola ─────────────────────────────
            pending_list = await get_pending_files(folder, pu_id)
            if not pending_list:
                emit(Log("✓ Sin archivos nuevos", "ok"))
                await update_profile_url_sync(
                    INDEX_DB, pu["id"], file_count=pu.get("file_count") or 0,
                )
                continue

            # Progreso "X / N" con resume.
            batch_key = f"pending_batch_{pu_id}"
            if existing_pending == 0:
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

            resume_note = f" — retomando desde {batch_offset} / {batch_total}" if batch_offset else ""
            emit(Log(f"Descargando {len(pending_list)} archivo(s){resume_note}", "info"))

            file_queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 3)

            async def producer() -> None:
                """Alimenta la cola de workers; ext_filter se aplica aquí para
                dejar lo no-coincidente en pending_queue para futuras sesiones."""
                try:
                    for pf in pending_list:
                        if ext_filter and not _passes_ext_filter(
                            pf.get("filename_hint") or "", ext_filter, exclude_mode
                        ):
                            continue
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
                            extra_headers=extra,
                        )
                        await asyncio.wait_for(file_queue.put(fi), timeout=120.0)
                    # Terminación normal: un centinela None por worker para que
                    # cada bucle corte. Solo aquí — en cancelación NO se encolan:
                    # los workers ya fueron cancelados por el gather padre y no
                    # consumen, así que un put() en cola llena se colgaría sin
                    # consumidor (deadlock). El except deja propagar el cancel.
                    for _ in range(workers):
                        await file_queue.put(None)
                except asyncio.CancelledError:
                    raise

            in_progress_hashes: set[str] = set()

            async def worker_task(slot_id: int) -> None:
                while True:
                    fi = await file_queue.get()
                    if fi is None:
                        emit(WorkerIdle(slot_id))
                        break

                    if fi.remote_hash and fi.remote_hash in in_progress_hashes:
                        summ.skipped += 1
                        emit(Log(f"— {fi.filename[:60]}  [hash en progreso]", "dim"))
                        _counters()
                        continue
                    if fi.remote_hash:
                        in_progress_hashes.add(fi.remote_hash)

                    try:
                        if await url_exists(folder, fi.dedup_key):
                            summ.skipped += 1
                            emit(Log(f"— {fi.filename[:60]}  [URL en catálogo]", "dim"))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue
                        if fi.remote_hash and await hash_exists(folder, fi.remote_hash):
                            summ.skipped += 1
                            emit(Log(f"— {fi.filename[:60]}  [hash en catálogo]", "dim"))
                            await remove_pending(folder, fi.dedup_key)
                            _counters()
                            continue

                        counter = await next_counter(folder)
                        final_name = build_filename(artist_info.name, counter, fi.filename)
                        emit(WorkerStart(slot_id, fi.filename))

                        def make_cb(s: int) -> Callable[[int, int], None]:
                            def cb(done: int, total: int) -> None:
                                emit(WorkerProgress(s, done, total))
                            return cb

                        try:
                            result = await asyncio.wait_for(
                                engine.download(
                                    url=fi.url,
                                    dest_dir=folder,
                                    filename=final_name,
                                    on_progress=make_cb(slot_id),
                                    extra_headers=fi.extra_headers or None,
                                    total_timeout=300.0,
                                ),
                                timeout=7200.0,
                            )
                        except asyncio.TimeoutError:
                            emit(WorkerDone(slot_id, fi.filename, "⏸"))
                            emit(Log(f"⏸ {fi.filename[:55]}  [timeout — diferido]", "warn"))
                            deferred.append((fi, artist_info, folder))
                            summ.deferred += 1
                            _counters()
                            continue

                        if not result.ok:
                            if result.error_kind in ErrorKind.DEFERRABLE:
                                deferred.append((fi, artist_info, folder))
                                emit(WorkerDone(slot_id, fi.filename, "⏸"))
                                emit(Log(f"⏸ {fi.filename[:55]}  [{result.error_kind}]", "warn"))
                            else:
                                summ.errors += 1
                                emit(WorkerDone(slot_id, fi.filename, "✗"))
                                emit(Log(f"✗ {fi.filename[:45]}:  {result.error}", "error"))
                            _counters()
                            continue

                        if result.file_hash is None:
                            summ.errors += 1
                            emit(Log(f"✗ {fi.filename[:50]}: bug interno — hash nulo", "error"))
                            if result.dest and result.dest.exists():
                                result.dest.unlink()
                            continue

                        # Dedup post-descarga (mismo contenido ya catalogado).
                        if await hash_exists(folder, result.file_hash):
                            summ.skipped += 1
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
                            new_path = folder / final_name
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
                                filename=final_name, url_source=fi.dedup_key,
                                file_size=result.file_size, counter=counter,
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
                                filename=final_name, url_source=fi.dedup_key,
                                file_size=result.file_size, counter=counter,
                            )
                            await remove_pending(folder, fi.dedup_key)
                            local_hashes[result.file_hash] = result.dest
                            summ.downloaded += 1
                            emit(WorkerDone(slot_id, final_name, "✓"))
                            emit(Log(f"✓ {fi.filename[:40]}  →  {final_name}", "ok"))

                        _counters()

                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        import traceback
                        summ.errors += 1
                        emit(WorkerDone(slot_id, fi.filename, "✗"))
                        emit(Log(
                            f"✗ {fi.filename[:45]}  [{type(exc).__name__}: {exc}]",
                            "error",
                        ))
                        emit(Log(traceback.format_exc().splitlines()[-1], "dim"))
                        _counters()
                    finally:
                        if fi.remote_hash:
                            in_progress_hashes.discard(fi.remote_hash)

            all_tasks = [
                asyncio.create_task(producer(), name="producer"),
                *[asyncio.create_task(worker_task(i), name=f"worker-{i}")
                  for i in range(workers)],
            ]
            try:
                results = await asyncio.gather(*all_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for t in all_tasks:
                    t.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
                raise

            # Si el productor murió, la paginación quedó incompleta.
            producer_exc = results[0]
            if isinstance(producer_exc, Exception):
                emit(Log(
                    f"✗ Error en paginación ({type(producer_exc).__name__}): {producer_exc}",
                    "error",
                ))
                emit(Log("⚠ Descarga incompleta. Usá Sync para continuar.", "warn"))

            source_dl = summ.downloaded - dl_before
            new_count = (pu["file_count"] or 0) + source_dl
            await update_profile_url_sync(INDEX_DB, pu["id"], file_count=new_count)
            emit(SourceDone(pu["id"], new_count))

        # ── Cola diferida ───────────────────────────────────────────────────
        if deferred:
            emit(Log(f"⏭ Cola diferida: {len(deferred)} archivo(s)…", "warn"))
            for file_info, a_info, dest_folder in deferred:
                if await url_exists(dest_folder, file_info.dedup_key):
                    summ.skipped += 1
                    continue
                emit(WorkerStart(0, file_info.filename))
                ext = Path(file_info.filename).suffix
                tmp_name = "_dl_" + hashlib.md5(file_info.url.encode()).hexdigest()[:12] + ext
                try:
                    result = await asyncio.wait_for(
                        engine.download(
                            url=file_info.url, dest_dir=dest_folder, filename=tmp_name,
                            extra_headers=file_info.extra_headers or None,
                            total_timeout=300.0,
                        ),
                        timeout=7200.0,
                    )
                except asyncio.TimeoutError:
                    emit(WorkerDone(0, file_info.filename, "⏭"))
                    summ.deferred += 1
                    emit(Log(f"⏭ {file_info.filename[:55]}  [timeout — próx. sync]", "warn"))
                    _counters()
                    continue
                if result.ok and result.file_hash:
                    counter = await next_counter(dest_folder)
                    final_name = build_filename(a_info.name, counter, file_info.filename)
                    if result.dest and result.dest.exists():
                        try:
                            result.dest.replace(dest_folder / final_name)
                        except OSError:
                            final_name = tmp_name
                    await add_file(
                        artist_dir=dest_folder, file_hash=result.file_hash,
                        filename=final_name, url_source=file_info.dedup_key,
                        file_size=result.file_size, counter=counter,
                    )
                    await remove_pending(dest_folder, file_info.dedup_key)
                    summ.downloaded += 1
                    emit(WorkerDone(0, final_name, "✓"))
                    emit(Log(f"✓ {final_name} (reintento)", "ok"))
                else:
                    if result.dest and result.dest.exists():
                        result.dest.unlink(missing_ok=True)
                    emit(WorkerDone(0, file_info.filename, "⏭"))
                    summ.deferred += 1
                    emit(Log(f"⏭ {file_info.filename[:55]}  — pendiente próx. sync", "warn"))
                _counters()
            emit(WorkerIdle(0))

    # ── Resumen final ───────────────────────────────────────────────────────
    await update_profile_last_checked(INDEX_DB, profile["id"])
    if scan_only:
        total_q = await pending_count(folder)
        emit(ScanComplete(total_q))
    return summ
