"""
Catálogo por artista — catalog.db.
Cada carpeta de artista contiene su propio catalog.db con hashes y metadatos.
El catálogo viaja junto a los archivos, haciendo la colección auto-contenida.

Esquema:
  files — registro de cada archivo (hash, nombre final, URL, fecha, tamaño, contador)
  meta  — valores únicos por catálogo (ej: contador global de archivos del artista)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import aiosqlite
from .url_receipts import CREATE as _CREATE_URL_RECEIPTS, write_receipt, merge_download_history

CATALOG_NAME = "catalog.db"

# ── Schema ─────────────────────────────────────────────────────────────────────

_CREATE_FILES = """
CREATE TABLE IF NOT EXISTS files (
    hash        TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,       -- nombre final en disco (con prefijo artista)
    url_source  TEXT,
    date_added  INTEGER NOT NULL,    -- unix timestamp
    file_size   INTEGER,             -- bytes
    counter     INTEGER,             -- número secuencial global del artista
    is_preview  INTEGER NOT NULL DEFAULT 0,
    content_service TEXT,
    post_id     TEXT,
    content_role TEXT,
    content_position INTEGER
);
"""

# Tabla meta: almacena el contador global incremental del artista
_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   INTEGER NOT NULL DEFAULT 0
);
"""

# idx_hash es redundante (hash ya es PRIMARY KEY), pero se mantiene por
# compatibilidad con catálogos existentes que lo tengan creado.
_CREATE_IDX = "CREATE INDEX IF NOT EXISTS idx_hash ON files(hash);"

# Índice en url_source: url_exists() hace SELECT por esta columna en cada
# archivo procesado → sin índice es un table scan O(n) por archivo.
_CREATE_IDX_URL = "CREATE INDEX IF NOT EXISTS idx_url_source ON files(url_source);"

# Identidades estables del contenido en el servicio original. Una misma obra
# puede aparecer por el sitio original y por un espejo con URLs distintas.
_CREATE_CONTENT_ALIASES = """
CREATE TABLE IF NOT EXISTS content_aliases (
    service            TEXT NOT NULL,
    post_id            TEXT NOT NULL,
    original_filename  TEXT NOT NULL,
    content_role       TEXT NOT NULL DEFAULT '',
    content_position   INTEGER NOT NULL,
    remote_size        INTEGER,
    file_hash          TEXT NOT NULL,
    date_added         INTEGER NOT NULL,
    PRIMARY KEY (service, post_id, original_filename, content_position),
    FOREIGN KEY (file_hash) REFERENCES files(hash) ON DELETE CASCADE
);
"""

_CREATE_IDX_ALIAS_HASH = (
    "CREATE INDEX IF NOT EXISTS idx_content_alias_hash "
    "ON content_aliases(file_hash);"
)

# Cola de descarga persistente por artista.
# Cada URL descubierta se agrega aquí antes de descargar; se elimina al
# completarse. Si el proceso se interrumpe, la cola sobrevive en disco y
# la próxima sesión retoma sin re-escanear la API.
# profile_url_id identifica qué fuente descubrió el archivo — permite que
# un artista con múltiples fuentes (kemono + patreon) tenga colas separadas.
_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS pending_queue (
    url_source      TEXT PRIMARY KEY,
    download_url    TEXT NOT NULL,
    filename_hint   TEXT NOT NULL,
    post_id         TEXT,
    post_published  TEXT,
    remote_hash     TEXT,
    content_service TEXT,
    original_filename TEXT,
    content_role    TEXT,
    content_position INTEGER,
    remote_size     INTEGER,
    is_preview      INTEGER NOT NULL DEFAULT 0,
    extra_headers   TEXT,
    profile_url_id  INTEGER,
    discovered_at   INTEGER NOT NULL,
    queue_position  INTEGER,
    deferred_attempts INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_IDX_PENDING = (
    "CREATE INDEX IF NOT EXISTS idx_pending_url_id "
    "ON pending_queue(profile_url_id);"
)

_CREATE_IDX_PENDING_ORDER = (
    "CREATE INDEX IF NOT EXISTS idx_pending_order "
    "ON pending_queue(profile_url_id, queue_position);"
)

# Archivos que Pawchive anuncia en sus metadatos pero cuyo original todavía no
# ha sido archivado. No pertenecen a pending_queue: esa cola es trabajo activo
# y reintentarlos en cada Sync solo produce una tormenta de 404. Conservamos el
# payload completo para poder devolver una muestra a la cola sin reescanear.
_CREATE_AWAITING_ARCHIVE = """
CREATE TABLE IF NOT EXISTS awaiting_archive (
    url_source      TEXT PRIMARY KEY,
    download_url    TEXT NOT NULL,
    filename_hint   TEXT NOT NULL,
    post_id         TEXT,
    post_published  TEXT,
    remote_hash     TEXT,
    content_service TEXT,
    original_filename TEXT,
    content_role    TEXT,
    content_position INTEGER,
    remote_size     INTEGER,
    is_preview      INTEGER NOT NULL DEFAULT 0,
    extra_headers   TEXT,
    profile_url_id  INTEGER,
    first_seen      INTEGER NOT NULL,
    last_checked    INTEGER NOT NULL,
    next_check      INTEGER NOT NULL,
    check_count     INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_IDX_AWAITING_ARCHIVE = (
    "CREATE INDEX IF NOT EXISTS idx_awaiting_archive_due "
    "ON awaiting_archive(profile_url_id, next_check);"
)

# Estado observado en Pawchive cuando sólo existe una miniatura. No es una cola:
# no contiene una URL descargable ni reserva contador/nombre de archivo.
_CREATE_PREVIEW_ONLY = """
CREATE TABLE IF NOT EXISTS preview_only (
    url_source        TEXT PRIMARY KEY,
    filename_hint     TEXT NOT NULL,
    post_id           TEXT,
    post_published    TEXT,
    observed_hash     TEXT,
    content_service   TEXT,
    original_filename TEXT,
    content_role      TEXT,
    content_position  INTEGER,
    profile_url_id    INTEGER,
    first_seen        INTEGER NOT NULL,
    last_seen         INTEGER NOT NULL
);
"""

ARCHIVE_RECHECK_SECONDS = 7 * 24 * 60 * 60
ARCHIVE_RECHECK_BATCH = 25

# Metadata del perfil embebida en el catálogo → la carpeta es auto-describible.
# Permite reconstruir index.db (caché por-máquina) desde las carpetas al cambiar
# de OS, sin perder las URLs de scrape ni el estado de sincronización.
# Fila única (id=1) con un blob JSON: display_name, primary_site, urls[], ext_filter.
_CREATE_PROFILE_META = """
CREATE TABLE IF NOT EXISTS profile_meta (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    data  TEXT NOT NULL
);
"""

# Migraciones para catálogos existentes creados antes de agregar estas columnas
_MIGRATE_COUNTER = """
ALTER TABLE files ADD COLUMN counter INTEGER;
"""
_MIGRATE_META = """
INSERT OR IGNORE INTO meta (key, value) VALUES ('counter', 0);
"""


# ── Inicialización ─────────────────────────────────────────────────────────────

def _db(db_path) -> aiosqlite.Connection:
    """Abre catalog.db con timeout=30 s — soporta workers concurrentes."""
    return aiosqlite.connect(db_path, timeout=30)


async def init_catalog(artist_dir: Path) -> None:
    """Crea o migra catalog.db en la carpeta del artista."""
    artist_dir.mkdir(parents=True, exist_ok=True)
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        # WAL: lectores no bloquean escritores ni viceversa
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_CREATE_FILES)
        await db.execute(_CREATE_META)
        await db.execute(_CREATE_IDX)
        await db.execute(_CREATE_IDX_URL)
        await db.execute(_CREATE_URL_RECEIPTS)
        await db.execute(_CREATE_CONTENT_ALIASES)
        await db.execute(_CREATE_IDX_ALIAS_HASH)
        await db.execute(_CREATE_PENDING)
        await db.execute(_CREATE_IDX_PENDING)
        await db.execute(_CREATE_PROFILE_META)
        await db.execute(_CREATE_AWAITING_ARCHIVE)
        await db.execute(_CREATE_IDX_AWAITING_ARCHIVE)
        await db.execute(_CREATE_PREVIEW_ONLY)

        # Migrar columna counter si no existe (catálogos previos)
        async with db.execute(
            "SELECT name FROM pragma_table_info('files') WHERE name='counter'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(_MIGRATE_COUNTER)

        file_columns = {
            "is_preview": "INTEGER NOT NULL DEFAULT 0",
            "content_service": "TEXT",
            "post_id": "TEXT",
            "content_role": "TEXT",
            "content_position": "INTEGER",
        }
        async with db.execute("PRAGMA table_info('files')") as cur:
            existing_files = {row[1] async for row in cur}
        for column, sql_type in file_columns.items():
            if column not in existing_files:
                await db.execute(
                    f"ALTER TABLE files ADD COLUMN {column} {sql_type}"
                )

        # Posición mutable de la cola. Los catálogos anteriores ordenaban sólo
        # por discovered_at, por lo que un archivo fallido reaparecía al frente
        # en cada ejecución. ROWID conserva el FIFO previo durante la migración.
        async with db.execute(
            "SELECT name FROM pragma_table_info('pending_queue') "
            "WHERE name='queue_position'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(
                    "ALTER TABLE pending_queue ADD COLUMN queue_position INTEGER"
                )
        await db.execute(
            "UPDATE pending_queue SET queue_position = rowid "
            "WHERE queue_position IS NULL"
        )
        await db.execute(_CREATE_IDX_PENDING_ORDER)

        # Un fallo que debe esperar a otra sesión queda marcado. Así puede
        # volver a intentarse sin que cinco archivos problemáticos conocidos
        # se confundan con un bloqueo nuevo de todo el sitio.
        async with db.execute(
            "SELECT name FROM pragma_table_info('pending_queue') "
            "WHERE name='deferred_attempts'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(
                    "ALTER TABLE pending_queue ADD COLUMN "
                    "deferred_attempts INTEGER NOT NULL DEFAULT 0"
                )

        # Metadatos semánticos añadidos en O4. También viajan con los
        # originales de Pawchive que esperan ser archivados.
        semantic_columns = {
            "content_service": "TEXT",
            "original_filename": "TEXT",
            "content_role": "TEXT",
            "content_position": "INTEGER",
            "remote_size": "INTEGER",
            "is_preview": "INTEGER NOT NULL DEFAULT 0",
        }
        for table in ("pending_queue", "awaiting_archive"):
            async with db.execute(f"PRAGMA table_info('{table}')") as cur:
                existing = {row[1] async for row in cur}
            for column, sql_type in semantic_columns.items():
                if column not in existing:
                    await db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                    )

        # La versión experimental llegó a encolar previews. Al abrir cualquier
        # catálogo se inmovilizan como marcadores antes de que un worker pueda
        # descargarlas. La limpieza de archivos ya guardados es deliberadamente
        # una herramienta separada y recuperable.
        now = int(time.time())
        for table in ("pending_queue", "awaiting_archive"):
            date_column = "discovered_at" if table == "pending_queue" else "first_seen"
            await db.execute(
                f"""
                INSERT INTO preview_only
                    (url_source, filename_hint, post_id, post_published,
                     observed_hash, content_service, original_filename,
                     content_role, content_position, profile_url_id,
                     first_seen, last_seen)
                SELECT url_source, filename_hint, post_id, post_published,
                       remote_hash, content_service, original_filename,
                       content_role, content_position, profile_url_id,
                       COALESCE({date_column}, ?), ?
                FROM {table}
                WHERE is_preview = 1
                ON CONFLICT(url_source) DO UPDATE SET
                    filename_hint = excluded.filename_hint,
                    post_id = excluded.post_id,
                    post_published = excluded.post_published,
                    observed_hash = excluded.observed_hash,
                    content_service = excluded.content_service,
                    original_filename = excluded.original_filename,
                    content_role = excluded.content_role,
                    content_position = excluded.content_position,
                    profile_url_id = excluded.profile_url_id,
                    last_seen = excluded.last_seen
                """,
                (now, now),
            )
            await db.execute(f"DELETE FROM {table} WHERE is_preview = 1")

        # Asegurar fila del contador en meta
        await db.execute(_MIGRATE_META)
        await db.commit()


# ── Metadata del perfil (auto-descripción para reindex cross-OS) ───────────────

async def write_profile_meta(artist_dir: Path, meta: dict) -> None:
    """Persiste/actualiza la metadata del perfil dentro del catalog.db.

    `meta` debe contener al menos: display_name, primary_site, urls (lista de
    dicts con url/site/artist_id/enabled/last_synced/file_count) y ext_filter.
    """
    db_path = artist_dir / CATALOG_NAME
    payload = json.dumps(meta, ensure_ascii=False)
    async with _db(db_path) as db:
        await db.execute(_CREATE_PROFILE_META)   # idempotente (catálogos previos)
        await db.execute(
            "INSERT INTO profile_meta (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (payload,),
        )
        await db.commit()


async def read_profile_meta(artist_dir: Path) -> dict | None:
    """Lee la metadata del perfil del catalog.db. None si no existe o es inválida."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return None
    async with _db(db_path) as db:
        await db.execute(_CREATE_PROFILE_META)
        async with db.execute(
            "SELECT data FROM profile_meta WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


# ── Contador global del artista ────────────────────────────────────────────────

async def next_counter(artist_dir: Path, namespace: str | None = None) -> int:
    """
    Incrementa atómicamente y retorna el siguiente número secuencial del artista.

    Usa UPDATE … RETURNING value (SQLite 3.35+) para obtener el nuevo valor
    en una sola instrucción atómica — sin gap entre escritura y lectura que
    otro worker pudiera aprovechar para obtener el mismo contador.
    """
    key = "counter" if namespace is None else f"counter:{namespace}"
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, 0)", (key,)
        )
        async with db.execute(
            "UPDATE meta SET value = value + 1 WHERE key = ? RETURNING value",
            (key,),
        ) as cur:
            row = await cur.fetchone()
        await db.commit()

    if row is None:
        raise RuntimeError(
            f"catalog.db corrompido: falta la fila {key!r} en meta ({db_path}). "
            "Borra el archivo para regenerarlo."
        )
    return row[0]


async def reserve_destination(folder, artist_name, filename):
    """El contador no autoriza sobrescribir una ruta ya ocupada.

Comparte nombres entre descarga y fusión. La comprobación no es una reserva
atómica frente a terceros; el límite evita recorrer colisiones indefinidamente.
"""
    from .file_routing import route, routed_name, destination

    file_route = route(filename)
    for _ in range(1000):
        counter = await next_counter(folder, file_route.key)
        final_name = routed_name(artist_name, counter, filename)
        download_folder, catalog_name = destination(folder, final_name)
        if not os.path.lexists(download_folder / final_name):
            return counter, final_name, download_folder, catalog_name
    raise ValueError('download_target_conflict')


async def ensure_layout_version(
    artist_dir: Path,
    namespaces: tuple[str, ...],
    version: int,
) -> bool:
    """Inicializa catálogos vacíos; uno con archivos exige migración explícita."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT value FROM meta WHERE key = 'layout_version'"
        ) as cur:
            row = await cur.fetchone()
        if row and int(row[0]) >= version:
            return True
        async with db.execute("SELECT 1 FROM files LIMIT 1") as cur:
            has_files = await cur.fetchone() is not None
        if has_files:
            return False
        await db.executemany(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, 0)",
            [(f"counter:{name}",) for name in namespaces],
        )
        await db.execute(
            "INSERT INTO meta (key, value) VALUES ('layout_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (version,),
        )
        await db.commit()
    return True


async def apply_layout_version(
    artist_dir: Path,
    updates: list[tuple[str, int, str]],
    counters: dict[str, int],
    version: int,
) -> None:
    """Confirma rutas/contadores de una migración ya aplicada en disco."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.executemany(
            "UPDATE files SET filename = ?, counter = ? WHERE hash = ?",
            updates,
        )
        await db.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(f"counter:{name}", int(value)) for name, value in counters.items()],
        )
        await db.execute(
            "INSERT INTO meta (key, value) VALUES ('layout_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (version,),
        )
        await db.commit()


async def apply_project_maintenance(
    artist_dir: Path,
    files: list[dict],
    counters: dict[str, int],
    audit_version: int,
) -> None:
    """Confirma deduplicación/compactación física en una transacción."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for item in files:
            await db.execute(
                "UPDATE files SET filename = '_purged_' || hash "
                "WHERE filename = ? AND hash != ?",
                (item["new"], item["hash"]),
            )
            await db.execute(
                """
                INSERT INTO files
                    (hash, filename, url_source, date_added, file_size, counter)
                VALUES (?, ?, NULL, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    filename = excluded.filename,
                    file_size = excluded.file_size,
                    counter = excluded.counter
                """,
                (item["hash"], item["new"], int(time.time()),
                 int(item["size"]), int(item["counter"])),
            )
        await db.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [("counter" if name == "legacy" else f"counter:{name}", int(value))
             for name, value in counters.items()],
        )
        if audit_version:
            await db.execute(
                "INSERT INTO meta (key, value) VALUES ('maintenance_hash_audit', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (audit_version,),
            )
        await db.commit()


# ── Consultas ──────────────────────────────────────────────────────────────────

async def url_exists(
    artist_dir: Path, url: str, *, include_previews: bool = True,
) -> bool:
    """
    Retorna True si la URL ya está registrada en el catálogo.
    Permite detectar duplicados ANTES de descargar, sin necesidad de hash.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        preview_clause = "" if include_previews else " AND is_preview = 0"
        async with db.execute(
            "SELECT 1 FROM files WHERE url_source = ? AND content_role IS NOT 'archive_member'" + preview_clause
            + " UNION ALL SELECT 1 FROM downloaded_url_receipts r JOIN files f ON f.hash=r.file_hash "
            "WHERE r.url_source = ?" + ("" if include_previews else " AND f.is_preview=0")
            + " LIMIT 1", (url, url)
        ) as cur:
            return await cur.fetchone() is not None


async def hash_exists(
    artist_dir: Path, file_hash: str, *, include_previews: bool = True,
) -> bool:
    """Retorna True si el hash ya está registrado en el catálogo."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        preview_clause = "" if include_previews else " AND is_preview = 0"
        async with db.execute(
            "SELECT 1 FROM files WHERE hash = ?" + preview_clause + " LIMIT 1",
            (file_hash,),
        ) as cur:
            return await cur.fetchone() is not None


async def get_all_hashes(artist_dir: Path) -> set[str]:
    """Retorna todos los hashes registrados en el catálogo del artista."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return set()
    async with _db(db_path) as db:
        async with db.execute("SELECT hash FROM files") as cur:
            rows = await cur.fetchall()
    return {row[0] for row in rows}


class ScanCatalogBatch:
    """Instantánea y escritor por lotes para un escaneo completo de una fuente.

    Mantiene una sola conexión abierta durante el scan. Las consultas de dedup
    se resuelven en memoria y los pendientes nuevos o con URL firmada renovada
    se confirman en bloques, sin tocar la posición FIFO de filas existentes.
    """

    def __init__(self, artist_dir: Path, db: aiosqlite.Connection,
                 batch_size: int) -> None:
        self.artist_dir = artist_dir
        self._db_conn = db
        self.batch_size = max(1, batch_size)
        self.downloaded_urls: dict[str, str] = {}
        self.downloaded_hashes: set[str] = set()
        self.preview_only_urls: set[str] = set()
        self.content_aliases: dict[
            tuple[str, str, str, int], tuple[str, int | None]
        ] = {}
        self.pending_payloads: dict[str, tuple] = {}
        self.pending_positions: dict[str, int] = {}
        self.awaiting_urls: set[str] = set()
        self._buffer: dict[str, tuple] = {}
        self._preview_buffer: dict[str, tuple] = {}
        self._resolved_previews: set[str] = set()
        self._alias_buffer: dict[tuple[str, str, str, int], tuple] = {}
        self._url_buffer: dict[str, str] = {}
        self._next_queue_position = 0
        self.write_commits = 0
        self._closed = False

    @classmethod
    async def open(cls, artist_dir: Path, *, batch_size: int = 250
                   ) -> "ScanCatalogBatch":
        db = await _db(artist_dir / CATALOG_NAME)
        batch = cls(artist_dir, db, batch_size)
        try:
            async with db.execute(
                "SELECT url_source, hash, is_preview, content_service, post_id, "
                "content_role, content_position FROM files"
            ) as cur:
                async for (url_source, file_hash, is_preview, service, post_id,
                           role, position) in cur:
                    if is_preview:
                        continue
                    if url_source and role != 'archive_member':
                        batch.downloaded_urls[url_source] = file_hash
                    if file_hash:
                        batch.downloaded_hashes.add(file_hash)

            async with db.execute('SELECT r.url_source,r.file_hash FROM downloaded_url_receipts r '
                                  'JOIN files f ON f.hash=r.file_hash WHERE f.is_preview=0') as cur:
                async for url_source, file_hash in cur:
                    batch.downloaded_urls.setdefault(url_source, file_hash)

            async with db.execute(
                "SELECT service, post_id, original_filename, content_position, "
                "file_hash, remote_size FROM content_aliases"
            ) as cur:
                async for service, post_id, name, position, file_hash, size in cur:
                    batch.content_aliases[(service, post_id, name, position)] = (
                        file_hash, size,
                    )

            async with db.execute(
                "SELECT url_source, download_url, filename_hint, post_id, "
                "post_published, remote_hash, content_service, "
                "original_filename, content_role, content_position, "
                "remote_size, extra_headers, is_preview, queue_position "
                "FROM pending_queue"
            ) as cur:
                async for row in cur:
                    url_source, *payload, position = row
                    batch.pending_payloads[url_source] = tuple(payload)
                    batch.pending_positions[url_source] = int(position)

            async with db.execute(
                "SELECT url_source FROM awaiting_archive"
            ) as cur:
                async for row in cur:
                    batch.awaiting_urls.add(row[0])

            async with db.execute("SELECT url_source FROM preview_only") as cur:
                async for row in cur:
                    batch.preview_only_urls.add(row[0])

            async with db.execute(
                "SELECT COALESCE(MAX(queue_position), 0) FROM pending_queue"
            ) as cur:
                row = await cur.fetchone()
                batch._next_queue_position = int(row[0]) if row else 0
        except BaseException:
            await db.close()
            raise
        return batch

    def is_downloaded(self, url_source: str, remote_hash: str = "") -> bool:
        return self.find_downloaded_hash(url_source, remote_hash) is not None

    def find_downloaded_hash(
        self,
        url_source: str,
        remote_hash: str = "",
        *,
        service: str = "",
        post_id: str = "",
        original_filename: str = "",
        position: int | None = None,
        remote_size: int | None = None,
    ) -> str | None:
        """Resuelve hash remoto → alias lógico → URL estable, sin tocar red."""
        if remote_hash and remote_hash in self.downloaded_hashes:
            return remote_hash
        key = content_identity_key(
            service, post_id, original_filename, position,
        )
        if key is not None and key in self.content_aliases:
            file_hash, known_size = self.content_aliases[key]
            candidate_size = _remote_size_value(remote_size)
            if (candidate_size is None or known_size is None
                    or candidate_size == int(known_size)):
                return file_hash
        return self.downloaded_urls.get(url_source)

    async def stage_content_alias(
        self,
        file_hash: str,
        service: str,
        post_id: str,
        original_filename: str,
        role: str,
        position: int | None,
        remote_size: int | None = None,
    ) -> bool:
        """Persiste al cerrar el lote una identidad observada durante el scan."""
        key = content_identity_key(service, post_id, original_filename, position)
        if key is None or not file_hash:
            return False
        size = _remote_size_value(remote_size)
        self.content_aliases[key] = (file_hash, size)
        self._alias_buffer[key] = (
            *key[:3], role or "", key[3], size, file_hash, int(time.time()),
            file_hash,
        )
        if len(self._alias_buffer) >= self.batch_size:
            await self.flush()
        return True

    async def stage_downloaded_url(self, url_source: str, file_hash: str) -> bool:
        if self._closed:
            raise RuntimeError("el lote de catálogo ya está cerrado")
        if not url_source or file_hash not in self.downloaded_hashes:
            return False
        previous = self.downloaded_urls.get(url_source)
        if previous is not None:
            if previous != file_hash:
                raise ValueError('download_url_history_conflict')
            return True
        self.downloaded_urls[url_source] = file_hash
        self._url_buffer[url_source] = file_hash
        if len(self._url_buffer) >= self.batch_size:
            await self.flush()
        return True

    def resolve_preview_only(self, url_source: str) -> None:
        """Retira un marcador cuando esa identidad ya tiene archivo final."""
        if url_source not in self.preview_only_urls:
            return
        self.preview_only_urls.discard(url_source)
        self._preview_buffer.pop(url_source, None)
        self._resolved_previews.add(url_source)

    async def stage_pending(
        self,
        url_source: str,
        download_url: str,
        filename_hint: str,
        post_id: str = "",
        post_published: str = "",
        remote_hash: str = "",
        content_service: str = "",
        original_filename: str = "",
        content_role: str = "",
        content_position: int | None = None,
        remote_size: int | None = None,
        extra_headers: str | None = None,
        is_preview: bool = False,
        profile_url_id: int | None = None,
    ) -> bool:
        """Prepara un UPSERT y retorna True sólo cuando la fila es nueva.

        Una fila en ``awaiting_archive`` se ignora: verla otra vez durante un
        scan no debe devolverla a la cola activa. Para un pendiente activo se
        refrescan URL y headers sin cambiar su posición.
        """
        if self._closed:
            raise RuntimeError("el lote de catálogo ya está cerrado")
        if url_source in self.awaiting_urls:
            return False

        # La misma identidad ya dejó de ser preview: el original reemplaza el
        # marcador y entra a la cola normal en esta misma confirmación.
        self.resolve_preview_only(url_source)

        is_new = url_source not in self.pending_payloads
        previous = self.pending_payloads.get(url_source)
        payload = (
            download_url, filename_hint, post_id or "", post_published or "",
            remote_hash or "", content_service or "",
            original_filename or filename_hint, content_role or "",
            content_position, _remote_size_value(remote_size), extra_headers,
            int(bool(is_preview)),
        )
        if previous == payload:
            return False

        if is_new:
            self._next_queue_position += 1
            queue_position = self._next_queue_position
            self.pending_positions[url_source] = queue_position
        else:
            # En conflicto SQLite conserva la posición existente.
            queue_position = self.pending_positions[url_source]

        self.pending_payloads[url_source] = payload
        self._buffer[url_source] = (
            url_source, download_url, filename_hint,
            post_id or "", post_published or "", remote_hash or "",
            content_service or "", original_filename or filename_hint,
            content_role or "", content_position,
            _remote_size_value(remote_size),
            extra_headers, int(bool(is_preview)), profile_url_id,
            int(time.time()), queue_position,
        )
        if len(self._buffer) >= self.batch_size:
            await self.flush()
        return is_new

    async def stage_preview_only(
        self,
        url_source: str,
        filename_hint: str,
        post_id: str = "",
        post_published: str = "",
        observed_hash: str = "",
        content_service: str = "",
        original_filename: str = "",
        content_role: str = "",
        content_position: int | None = None,
        profile_url_id: int | None = None,
    ) -> bool:
        """Registra disponibilidad parcial sin crear trabajo de descarga."""
        if self._closed:
            raise RuntimeError("el lote de catálogo ya está cerrado")
        is_new = url_source not in self.preview_only_urls
        now = int(time.time())
        self.preview_only_urls.add(url_source)
        self._resolved_previews.discard(url_source)
        self._buffer.pop(url_source, None)
        self.pending_payloads.pop(url_source, None)
        self.pending_positions.pop(url_source, None)
        self._preview_buffer[url_source] = (
            url_source, filename_hint, post_id or "", post_published or "",
            observed_hash or "", content_service or "",
            original_filename or filename_hint, content_role or "",
            content_position, profile_url_id, now, now,
        )
        if len(self._preview_buffer) >= self.batch_size:
            await self.flush()
        return is_new

    async def flush(self) -> None:
        if (not self._buffer and not self._alias_buffer
                and not self._preview_buffer and not self._resolved_previews and not self._url_buffer):
            return
        await self._db_conn.execute('BEGIN IMMEDIATE')
        try:
            await self._flush_buffers()
            await self._db_conn.commit()
        except BaseException:
            await self._db_conn.rollback()
            raise
        self.write_commits += 1
        self._buffer.clear()
        self._alias_buffer.clear()
        self._url_buffer.clear()
        self._preview_buffer.clear()
        self._resolved_previews.clear()

    async def _flush_buffers(self) -> None:
        if self._preview_buffer:
            preview_urls = tuple(self._preview_buffer)
            placeholders = ",".join("?" for _ in preview_urls)
            await self._db_conn.execute(
                f"DELETE FROM pending_queue WHERE url_source IN ({placeholders})",
                preview_urls,
            )
            await self._db_conn.executemany(
                """
                INSERT INTO preview_only
                    (url_source, filename_hint, post_id, post_published,
                     observed_hash, content_service, original_filename,
                     content_role, content_position, profile_url_id,
                     first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_source) DO UPDATE SET
                    filename_hint = excluded.filename_hint,
                    post_id = excluded.post_id,
                    post_published = excluded.post_published,
                    observed_hash = excluded.observed_hash,
                    content_service = excluded.content_service,
                    original_filename = excluded.original_filename,
                    content_role = excluded.content_role,
                    content_position = excluded.content_position,
                    profile_url_id = excluded.profile_url_id,
                    last_seen = excluded.last_seen
                """,
                list(self._preview_buffer.values()),
            )
        if self._resolved_previews:
            await self._db_conn.executemany(
                "DELETE FROM preview_only WHERE url_source = ?",
                [(url,) for url in self._resolved_previews],
            )
        if self._buffer:
            await self._db_conn.executemany(
                """
                INSERT INTO pending_queue
                    (url_source, download_url, filename_hint, post_id,
                     post_published, remote_hash, content_service,
                     original_filename, content_role, content_position,
                     remote_size, extra_headers, is_preview, profile_url_id,
                     discovered_at, queue_position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_source) DO UPDATE SET
                    download_url  = excluded.download_url,
                    filename_hint = excluded.filename_hint,
                    post_id = excluded.post_id,
                    post_published = excluded.post_published,
                    remote_hash = excluded.remote_hash,
                    content_service = excluded.content_service,
                    original_filename = excluded.original_filename,
                    content_role = excluded.content_role,
                    content_position = excluded.content_position,
                    remote_size = excluded.remote_size,
                    extra_headers = excluded.extra_headers,
                    is_preview = excluded.is_preview,
                    deferred_attempts = CASE
                        WHEN pending_queue.download_url IS NOT excluded.download_url
                          OR pending_queue.remote_hash IS NOT excluded.remote_hash
                        THEN 0
                        ELSE pending_queue.deferred_attempts
                    END
                """,
                list(self._buffer.values()),
            )
        if self._alias_buffer:
            await self._db_conn.executemany(
                """
                INSERT INTO content_aliases
                    (service, post_id, original_filename, content_role,
                     content_position, remote_size, file_hash, date_added)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM files WHERE hash = ?)
                ON CONFLICT(service, post_id, original_filename, content_position)
                DO UPDATE SET
                    content_role = excluded.content_role,
                    remote_size = COALESCE(excluded.remote_size,
                                           content_aliases.remote_size),
                    file_hash = excluded.file_hash
                """,
                list(self._alias_buffer.values()),
            )
        for url_source, file_hash in self._url_buffer.items():
            if not await write_receipt(self._db_conn, url_source, file_hash):
                raise ValueError('download_url_history_unproven')

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self.flush()
        finally:
            self._closed = True
            await self._db_conn.close()


async def add_file(
    artist_dir: Path,
    file_hash: str,
    filename: str,
    url_source: str | None = None,
    file_size: int | None = None,
    counter: int | None = None,
    is_preview: bool = False,
    content_service: str = "",
    post_id: str = "",
    content_role: str = "",
    content_position: int | None = None,
) -> None:
    """Registra un archivo nuevo en el catálogo."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            """
            INSERT INTO files
                (hash, filename, url_source, date_added, file_size, counter,
                 is_preview, content_service, post_id, content_role,
                 content_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
                filename = excluded.filename,
                url_source = excluded.url_source,
                file_size = excluded.file_size,
                counter = excluded.counter,
                is_preview = 0,
                content_service = excluded.content_service,
                post_id = excluded.post_id,
                content_role = excluded.content_role,
                content_position = excluded.content_position
            WHERE files.is_preview = 1 AND excluded.is_preview = 0
            """,
            (
                file_hash, filename, url_source, int(time.time()), file_size,
                counter, int(bool(is_preview)), content_service or "",
                post_id or "", content_role or "", content_position,
            ),
        )
        if not is_preview and url_source and content_role != 'archive_member':
            if not await write_receipt(db, url_source, file_hash):
                raise ValueError('download_url_history_unproven')
        await db.commit()


async def relocate_file(artist_dir: Path, file_hash: str, filename: str) -> None:
    """Actualiza sólo la ruta después de organizar un archivo; conserva su identidad."""
    async with _db(artist_dir / CATALOG_NAME) as db:
        await db.execute("UPDATE files SET filename = ? WHERE hash = ?", (filename, file_hash))
        await db.commit()


async def copy_content_aliases(source: Path, destination: Path, file_hash: str) -> None:
    """Preserva las identidades de una descarga al migrar su recibo."""
    async with _db(source / CATALOG_NAME) as db:
        async with db.execute(
            "SELECT service, post_id, original_filename, content_role, "
            "content_position, remote_size FROM content_aliases WHERE file_hash = ?",
            (file_hash,),
        ) as cursor:
            aliases = await cursor.fetchall()
    for service, post_id, name, role, position, size in aliases:
        await add_content_alias(destination, file_hash, service, post_id, name, role, position, size)


async def get_all_files(artist_dir: Path) -> list[dict]:
    """
    Retorna todos los registros del catálogo como lista de dicts.
    Útil para repair: comparar archivos físicos contra el catálogo.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with _db(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hash, filename, url_source, date_added, file_size, counter, is_preview, "
            "content_service, post_id, content_role, content_position FROM files"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def remove_file(artist_dir: Path, file_hash: str) -> None:
    """Elimina un registro del catálogo por hash (usado en repair al re-indexar)."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "DELETE FROM content_aliases WHERE file_hash = ?", (file_hash,)
        )
        await db.execute("DELETE FROM files WHERE hash = ?", (file_hash,))
        await db.commit()


async def get_numbered_files(
    artist_dir: Path,
) -> list[tuple[int, str, str]]:
    """
    Retorna archivos numerados que existen en disco.

    Resultado: lista de (counter, filename, hash) ordenada por el
    contador extraído del nombre del archivo (no del campo counter en DB,
    que puede estar desactualizado).

    Solo incluye archivos presentes físicamente en la carpeta —
    los archivos purgados quedan en el catálogo pero se omiten aquí.
    """
    import re as _re
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT filename, hash FROM files WHERE counter IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

    result = []
    for filename, file_hash in rows:
        if not (artist_dir / filename).exists():
            continue
        m = _re.search(r'_(\d{5})\.[^.]*$', filename)
        if m:
            result.append((int(m.group(1)), filename, file_hash))

    result.sort(key=lambda x: x[0])
    return result


def plan_compaction(
    files: list[tuple[int, str, str]],
) -> list[tuple[str, str, str, int]]:
    """
    Calcula los renombres necesarios para eliminar huecos.

    Entrada: lista de (counter, filename, hash) ordenada por counter.
    Salida:  lista de (old_name, new_name, hash, new_counter).
             Solo incluye archivos cuyo nombre cambia.

    El nuevo counter se asigna secuencialmente desde 1.
    Reemplaza el patrón _NNNNN. en el nombre de archivo.
    """
    import re
    plan = []
    for new_counter, (_, filename, file_hash) in enumerate(files, start=1):
        new_name = re.sub(
            r'_(\d{5})(\.[^.]*$)',
            f'_{new_counter:05d}\\2',
            filename,
        )
        if new_name == filename:
            continue
        plan.append((filename, new_name, file_hash, new_counter))
    return plan


async def apply_compaction(
    artist_dir: Path,
    plan: list[tuple[str, str, str, int]],
    new_total: int,
) -> None:
    """
    Ejecuta el plan de compactación en dos fases (anti-colisión):

    Fase 1: old_name → old_name.tmp  (todos)
    Fase 2: old_name.tmp → new_name  (todos)

    Después actualiza catalog.db en una transacción atómica:
    - NULL-ifica registros "fantasma" que ocupan el nuevo nombre
      (archivos purgados cuyo slot se reutiliza — se preserva su hash
      para evitar re-descargas pero se limpia el filename)
    - SET filename, counter WHERE hash = ? (update por clave primaria,
      evita el problema de UPDATE encadenado por filename)
    - SET counter (meta) = new_total
    """
    if not plan:
        return

    db_path = artist_dir / CATALOG_NAME

    # replace() (no rename()): en Windows rename lanza FileExistsError si el
    # destino existe (slots reutilizados tras purgas); replace sobrescribe en
    # ambos OS de forma atómica.
    # Fase 1: → .tmp
    for old_name, _, _, _ in plan:
        (artist_dir / old_name).replace(
            artist_dir / (old_name + ".tmp")
        )

    # Fase 2: .tmp → new_name
    for old_name, new_name, _, _ in plan:
        (artist_dir / (old_name + ".tmp")).replace(
            artist_dir / new_name
        )

    # Actualizar DB en transacción atómica
    async with _db(db_path) as db:
        # Paso 1: "apartar" cualquier registro que ya tenga
        # filename = new_name pero que NO sea el archivo que movemos.
        # Esto evita la "reactivación" de registros de archivos
        # purgados cuyo slot es reutilizado.
        # Se usa el prefijo '_purged_' (no coincide con _\d{5}\.ext)
        # para respetar la restricción NOT NULL de la columna.
        for _, new_name, file_hash, _ in plan:
            await db.execute(
                "UPDATE files SET filename = '_purged_' || hash"
                " WHERE filename = ? AND hash != ?",
                (new_name, file_hash),
            )
        # Paso 2: actualizar por hash (clave primaria) — sin riesgo
        # de UPDATE encadenado por filename.
        for _, new_name, file_hash, new_ctr in plan:
            await db.execute(
                "UPDATE files SET filename = ?, counter = ?"
                " WHERE hash = ?",
                (new_name, new_ctr, file_hash),
            )
        await db.execute(
            "UPDATE meta SET value = ? WHERE key = 'counter'",
            (new_total,),
        )
        await db.commit()


# ── Meta genérica (enteros) ────────────────────────────────────────────────────

async def clean_pending_catalog_overlap(
    artist_dir: Path,
    profile_url_id: int | None = None,
) -> int:
    """
    Elimina de pending_queue los archivos que ya están en el catálogo
    (descargados en sesiones anteriores pero no removidos de la cola).
    Retorna el número de entradas eliminadas.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0

    pu_clause = "AND profile_url_id = ?" if profile_url_id is not None else ""
    params    = [profile_url_id] if profile_url_id is not None else []

    async with _db(db_path) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM pending_queue "
            f"WHERE url_source IN (SELECT url_source FROM files "
            f"WHERE is_preview = 0) {pu_clause}",
            params,
        ) as cur:
            row = await cur.fetchone()
        removed = row[0] if row else 0
        if removed:
            await db.execute(
                f"DELETE FROM pending_queue "
                f"WHERE url_source IN (SELECT url_source FROM files "
                f"WHERE is_preview = 0) {pu_clause}",
                params,
            )
            await db.commit()
    return removed


async def set_meta_int(artist_dir: Path, key: str, value: int) -> None:
    """
    Guarda (o actualiza) un entero en la tabla meta con clave arbitraria.
    Útil para guardar totales de batch, fronteras de scan, etc.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def get_meta_int(artist_dir: Path, key: str) -> int | None:
    """
    Lee un entero de la tabla meta por clave. Retorna None si no existe.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return None
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── Cola de pendientes ─────────────────────────────────────────────────────────

async def add_pending(
    artist_dir: Path,
    url_source: str,
    download_url: str,
    filename_hint: str,
    post_id: str = "",
    post_published: str = "",
    remote_hash: str = "",
    content_service: str = "",
    original_filename: str = "",
    content_role: str = "",
    content_position: int | None = None,
    remote_size: int | None = None,
    extra_headers: str | None = None,
    is_preview: bool = False,
    profile_url_id: int | None = None,
) -> None:
    """
    Agrega un archivo a la cola de pendientes. Seguro llamar múltiples veces:
    la url_source es la clave, así que un re-scan no duplica la fila.

    Si la fila ya existe se refrescan la URL firmada, headers y metadatos
    semánticos. La posición FIFO y `discovered_at` conservan cuándo se vio el
    archivo por primera vez.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            """
            INSERT INTO pending_queue
                (url_source, download_url, filename_hint, post_id,
                 post_published, remote_hash, content_service,
                 original_filename, content_role, content_position,
                 remote_size, extra_headers, is_preview, profile_url_id,
                 discovered_at, queue_position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    (SELECT COALESCE(MAX(queue_position), 0) + 1
                     FROM pending_queue))
            ON CONFLICT(url_source) DO UPDATE SET
                download_url  = excluded.download_url,
                filename_hint = excluded.filename_hint,
                post_id = excluded.post_id,
                post_published = excluded.post_published,
                remote_hash = excluded.remote_hash,
                content_service = excluded.content_service,
                original_filename = excluded.original_filename,
                content_role = excluded.content_role,
                content_position = excluded.content_position,
                remote_size = excluded.remote_size,
                extra_headers = excluded.extra_headers,
                is_preview = excluded.is_preview,
                deferred_attempts = CASE
                    WHEN pending_queue.download_url IS NOT excluded.download_url
                      OR pending_queue.remote_hash IS NOT excluded.remote_hash
                    THEN 0
                    ELSE pending_queue.deferred_attempts
                END
            """,
            (
                url_source, download_url, filename_hint,
                post_id or "", post_published or "", remote_hash or "",
                content_service or "", original_filename or filename_hint,
                content_role or "", content_position,
                _remote_size_value(remote_size),
                extra_headers, int(bool(is_preview)), profile_url_id,
                int(time.time()),
            ),
        )
        await db.commit()


async def pending_url_exists(artist_dir: Path, url_source: str) -> bool:
    """True si la URL está activa o esperando que Pawchive la archive."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return False
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM pending_queue WHERE url_source = ? "
            "UNION ALL "
            "SELECT 1 FROM awaiting_archive WHERE url_source = ? LIMIT 1",
            (url_source, url_source),
        ) as cur:
            return await cur.fetchone() is not None


def content_identity_key(
    service: str,
    post_id: str,
    original_filename: str,
    position: int | None,
) -> tuple[str, str, str, int] | None:
    """Normaliza una identidad lógica; None significa que no es segura."""
    service = (service or "").strip().casefold()
    post_id = (post_id or "").strip()
    original_filename = (original_filename or "").strip()
    if not service or not post_id or not original_filename:
        return None
    try:
        position = int(position) if position is not None else -1
    except (TypeError, ValueError):
        return None
    if position < 0:
        return None
    return service, post_id, original_filename, position


def _remote_size_value(value: int | None) -> int | None:
    try:
        size = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return size if size is None or size >= 0 else None


async def content_alias_hash(
    artist_dir: Path,
    service: str,
    post_id: str,
    original_filename: str,
    position: int | None,
    remote_size: int | None = None,
) -> str | None:
    """Busca un hash por identidad lógica, validando tamaño si ambos lo saben."""
    key = content_identity_key(service, post_id, original_filename, position)
    if key is None:
        return None
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return None
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT file_hash, remote_size FROM content_aliases "
            "WHERE service = ? AND post_id = ? AND original_filename = ? "
            "AND content_position = ?",
            key,
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    remote_size = _remote_size_value(remote_size)
    if remote_size is not None and row[1] is not None:
        if remote_size != int(row[1]):
            return None
    return str(row[0])


async def add_content_alias(
    artist_dir: Path,
    file_hash: str,
    service: str,
    post_id: str,
    original_filename: str,
    role: str,
    position: int | None,
    remote_size: int | None = None,
) -> bool:
    """Liga una identidad semántica a un hash ya presente en ``files``."""
    key = content_identity_key(service, post_id, original_filename, position)
    if key is None or not file_hash:
        return False
    size = _remote_size_value(remote_size)
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        async with db.execute(
            """
            INSERT INTO content_aliases
                (service, post_id, original_filename, content_role,
                 content_position, remote_size, file_hash, date_added)
            SELECT ?, ?, ?, ?, ?, ?, ?, ?
            WHERE EXISTS (SELECT 1 FROM files WHERE hash = ?)
            ON CONFLICT(service, post_id, original_filename, content_position)
            DO UPDATE SET
                content_role = excluded.content_role,
                remote_size = COALESCE(excluded.remote_size,
                                       content_aliases.remote_size),
                file_hash = excluded.file_hash
            """,
            (*key[:3], role or "", key[3], size, file_hash,
             int(time.time()), file_hash),
        ) as cur:
            changed = cur.rowcount > 0
        await db.commit()
    return changed


async def defer_until_archived(
    artist_dir: Path,
    url_source: str,
    *,
    recheck_seconds: int = ARCHIVE_RECHECK_SECONDS,
) -> bool:
    """Mueve atómicamente un 404 temporal fuera de la cola activa.

    Solo se llama después de que el template de Pawchive haya confirmado en la
    página del post "haven't archived this post yet" o "Previews only".
    """
    db_path = artist_dir / CATALOG_NAME
    now = int(time.time())
    async with _db(db_path) as db:
        await db.execute(_CREATE_AWAITING_ARCHIVE)
        await db.execute(_CREATE_IDX_AWAITING_ARCHIVE)
        async with db.execute(
            "SELECT download_url, filename_hint, post_id, post_published, "
            "remote_hash, content_service, original_filename, content_role, "
            "content_position, remote_size, extra_headers, is_preview, "
            "profile_url_id "
            "FROM pending_queue WHERE url_source = ?",
            (url_source,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        await db.execute(
            """
            INSERT INTO awaiting_archive
                (url_source, download_url, filename_hint, post_id,
                 post_published, remote_hash, content_service,
                  original_filename, content_role, content_position, remote_size,
                  extra_headers, is_preview, profile_url_id,
                  first_seen, last_checked, next_check, check_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(url_source) DO UPDATE SET
                download_url = excluded.download_url,
                filename_hint = excluded.filename_hint,
                post_id = excluded.post_id,
                post_published = excluded.post_published,
                remote_hash = excluded.remote_hash,
                content_service = excluded.content_service,
                original_filename = excluded.original_filename,
                content_role = excluded.content_role,
                content_position = excluded.content_position,
                remote_size = excluded.remote_size,
                extra_headers = excluded.extra_headers,
                is_preview = excluded.is_preview,
                profile_url_id = excluded.profile_url_id,
                last_checked = excluded.last_checked,
                next_check = excluded.next_check,
                check_count = awaiting_archive.check_count + 1
            """,
            (url_source, *row, now, now, now + max(1, recheck_seconds)),
        )
        await db.execute(
            "DELETE FROM pending_queue WHERE url_source = ?", (url_source,)
        )
        await db.commit()
        return True


async def awaiting_archive_count(
    artist_dir: Path, profile_url_id: int | None = None,
) -> int:
    """Cuenta originales anunciados por Pawchive pero aún no publicados."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0
    condition = " WHERE profile_url_id = ?" if profile_url_id is not None else ""
    params = (profile_url_id,) if profile_url_id is not None else ()
    async with _db(db_path) as db:
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM awaiting_archive" + condition, params,
            ) as cur:
                row = await cur.fetchone()
        except aiosqlite.OperationalError:
            return 0  # catálogo anterior: la migración ocurrirá al sincronizar
    return int(row[0]) if row else 0


async def reactivate_awaiting_archive(
    artist_dir: Path,
    profile_url_id: int | None = None,
    *,
    limit: int = ARCHIVE_RECHECK_BATCH,
    due_only: bool = False,
) -> int:
    """Devuelve una muestra a pending_queue para comprobarla de nuevo.

    ``due_only`` se usa en Sync normal (una vez por semana). El botón de la UI
    lo ignora para permitir una comprobación manual inmediata. Nunca reactiva
    más de una tanda conservadora por ejecución.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists() or limit <= 0:
        return 0
    now = int(time.time())
    conditions: list[str] = ["is_preview = 0"]
    params: list[int] = []
    if profile_url_id is not None:
        conditions.append("profile_url_id = ?")
        params.append(profile_url_id)
    if due_only:
        conditions.append("next_check <= ?")
        params.append(now)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    async with _db(db_path) as db:
        await db.execute(_CREATE_PENDING)
        await db.execute(_CREATE_AWAITING_ARCHIVE)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM awaiting_archive" + where
            + " ORDER BY next_check, first_seen, rowid LIMIT ?",
            (*params, int(limit)),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return 0
        moved: list[str] = []
        for row in rows:
            async with db.execute(
                """
                INSERT OR IGNORE INTO pending_queue
                    (url_source, download_url, filename_hint, post_id,
                     post_published, remote_hash, content_service,
                     original_filename, content_role, content_position,
                     remote_size, extra_headers, is_preview, profile_url_id,
                     discovered_at, queue_position)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        (SELECT COALESCE(MAX(queue_position), 0) + 1
                         FROM pending_queue))
                """,
                (
                    row["url_source"], row["download_url"], row["filename_hint"],
                    row["post_id"], row["post_published"], row["remote_hash"],
                    row["content_service"], row["original_filename"],
                    row["content_role"], row["content_position"],
                    row["remote_size"],
                    row["extra_headers"], row["is_preview"],
                    row["profile_url_id"], now,
                ),
            ) as cur:
                if cur.rowcount > 0:
                    moved.append(row["url_source"])
        if moved:
            placeholders = ",".join("?" for _ in moved)
            await db.execute(
                f"DELETE FROM awaiting_archive WHERE url_source IN ({placeholders})",
                moved,
            )
        await db.commit()
        return len(moved)


async def pending_count(
    artist_dir: Path,
    profile_url_id: int | None = None,
    ext_filter: set[str] | None = None,
) -> int:
    """
    Retorna la cantidad de archivos en la cola de pendientes.
    - profile_url_id: filtra por fuente (opcional).
    - ext_filter: set de extensiones con punto, p.ej. {'.jpg','.zip'}.
      Si está vacío o es None, cuenta todo.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0

    conditions: list[str] = []
    params: list = []

    if profile_url_id is not None:
        conditions.append("profile_url_id = ?")
        params.append(profile_url_id)

    if ext_filter:
        # filename_hint termina en una de las extensiones del filtro
        like_clauses = " OR ".join(
            "LOWER(filename_hint) LIKE ?" for _ in ext_filter
        )
        conditions.append(f"({like_clauses})")
        params.extend(f"%{ext.lower()}" for ext in ext_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql   = f"SELECT COUNT(*) FROM pending_queue {where}"

    async with _db(db_path) as db:
        async with db.execute(sql, params) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_pending_files(
    artist_dir: Path,
    profile_url_id: int | None = None,
) -> list[dict]:
    """
    Retorna los archivos pendientes de descarga como lista de dicts.
    Si profile_url_id está definido, filtra por esa fuente.
    Ordenados por la posición persistente de la cola. Cada fallo mueve su fila
    al fondo, incluso si Cherry se cierra antes de la siguiente ejecución.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with _db(db_path) as db:
        db.row_factory = aiosqlite.Row
        if profile_url_id is not None:
            async with db.execute(
                """
                SELECT url_source, download_url, filename_hint,
                       post_id, post_published, remote_hash, content_service,
                       original_filename, content_role, content_position,
                       remote_size, extra_headers, is_preview,
                       deferred_attempts
                FROM pending_queue
                WHERE profile_url_id = ? AND is_preview = 0
                ORDER BY queue_position, discovered_at, rowid
                """,
                (profile_url_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """
                SELECT url_source, download_url, filename_hint,
                       post_id, post_published, remote_hash, content_service,
                       original_filename, content_role, content_position,
                       remote_size, extra_headers, is_preview,
                       deferred_attempts
                FROM pending_queue
                WHERE is_preview = 0
                ORDER BY queue_position, discovered_at, rowid
                """
            ) as cur:
                rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def move_pending_to_back(artist_dir: Path, url_source: str) -> bool:
    """Mueve un pendiente fallido al fondo de su cola de forma persistente.

    Retorna ``True`` si la fila seguía en la cola. La actualización es una sola
    sentencia SQL, de modo que los workers concurrentes quedan serializados por
    SQLite y no pueden devolver accidentalmente el archivo al frente.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return False
    async with _db(db_path) as db:
        async with db.execute(
            """
            UPDATE pending_queue
            SET queue_position = (
                SELECT COALESCE(MAX(queue_position), 0) + 1
                FROM pending_queue
            )
            WHERE url_source = ?
            """,
            (url_source,),
        ) as cur:
            moved = cur.rowcount > 0
        await db.commit()
        return moved


async def defer_pending_to_next_session(
    artist_dir: Path, url_source: str,
) -> bool:
    """Rota un fallo y lo marca como problema conocido para futuras sesiones."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return False
    async with _db(db_path) as db:
        async with db.execute(
            """
            UPDATE pending_queue
            SET queue_position = (
                    SELECT COALESCE(MAX(queue_position), 0) + 1
                    FROM pending_queue
                ),
                deferred_attempts = deferred_attempts + 1
            WHERE url_source = ?
            """,
            (url_source,),
        ) as cur:
            moved = cur.rowcount > 0
        await db.commit()
        return moved


async def remove_pending(artist_dir: Path, url_source: str) -> None:
    """
    Elimina un archivo de la cola de pendientes.
    Llamar tras descarga exitosa o skip por dedup.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "DELETE FROM pending_queue WHERE url_source = ?", (url_source,)
        )
        await db.commit()


async def remove_pending_for_profile_url(
    artist_dir: Path, profile_url_id: int,
) -> int:
    """Retira toda cola activa o en espera de una fuente eliminada.

    Los archivos ya catalogados no se tocan. Retorna cuántas filas pendientes
    salieron para que la UI pueda explicar exactamente lo que hizo.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0
    async with _db(db_path) as db:
        async with db.execute(
            "DELETE FROM pending_queue WHERE profile_url_id = ?",
            (profile_url_id,),
        ) as cur:
            removed = max(0, cur.rowcount)
        try:
            async with db.execute(
                "DELETE FROM awaiting_archive WHERE profile_url_id = ?",
                (profile_url_id,),
            ) as cur:
                removed += max(0, cur.rowcount)
        except aiosqlite.OperationalError:
            pass
        await db.commit()
    return removed


async def compare_catalogs(folder_a: Path, folder_b: Path) -> dict:
    """
    Compara dos catálogos de artista por hashes SHA-256.

    Diseñado para detectar perfiles duplicados: si una fracción alta de los
    hashes de B ya existe en A, ambas carpetas probablemente son el mismo artista.

    Retorna:
        total_a     — archivos en catálogo A (el más grande / más antiguo)
        total_b     — archivos en catálogo B (candidato a fusión)
        matches     — hashes presentes en ambos catálogos
        coverage    — fracción de B que ya existe en A  (0.0 – 1.0)
        unique_to_b — hashes en B que NO están en A (necesitarían moverse)
    """
    hashes_a = await get_all_hashes(folder_a)
    hashes_b = await get_all_hashes(folder_b)

    if not hashes_b:
        return {
            "total_a": len(hashes_a),
            "total_b": 0,
            "matches": 0,
            "coverage": 0.0,
            "unique_to_b": [],
        }

    common = hashes_a & hashes_b
    unique = hashes_b - hashes_a
    coverage = len(common) / len(hashes_b)

    return {
        "total_a":     len(hashes_a),
        "total_b":     len(hashes_b),
        "matches":     len(common),
        "coverage":    coverage,
        "unique_to_b": list(unique),
    }


async def compare_by_hash_join(folder_a: Path, folder_b: Path) -> dict:
    """
    Compara dos catálogos por hashes usando SQL ATTACH + INNER JOIN.
    No lee archivos del disco — solo consulta las catalog.db existentes.

    Retorna:
        total_a  — archivos registrados en A
        total_b  — archivos registrados en B
        matches  — hashes presentes en ambos catálogos
        coverage — matches / min(total_a, total_b)  (métrica simétrica)
    """
    db_a = folder_a / CATALOG_NAME
    db_b = folder_b / CATALOG_NAME

    if not db_a.exists() or not db_b.exists():
        return {"total_a": 0, "total_b": 0, "matches": 0, "coverage": 0.0}

    async with aiosqlite.connect(db_a, timeout=30) as db:
        await db.execute("ATTACH DATABASE ? AS cat_b", (str(db_b),))
        try:
            # Un catalog.db de un perfil sin descargar sólo tiene `profile_meta`
            # (lo crea write_profile_meta), no `files`. Si falta la tabla en
            # cualquiera de los dos no puede haber solape de hashes → 0.
            # Además, SIEMPRE cualificar con main./cat_b.: un `FROM files` sin
            # prefijo, si `files` no existe en main, cae a la base adjunta y
            # cuenta los archivos del OTRO catálogo → falso 100%.
            async with db.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM main.sqlite_master "
                " WHERE type='table' AND name='files'), "
                "(SELECT COUNT(*) FROM cat_b.sqlite_master "
                " WHERE type='table' AND name='files')"
            ) as cur:
                has_a, has_b = await cur.fetchone()
            if not has_a or not has_b:
                return {"total_a": 0, "total_b": 0, "matches": 0, "coverage": 0.0}

            async with db.execute("SELECT COUNT(*) FROM main.files") as cur:
                total_a = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM cat_b.files") as cur:
                total_b = (await cur.fetchone())[0]

            if total_a == 0 or total_b == 0:
                return {
                    "total_a": total_a, "total_b": total_b,
                    "matches": 0, "coverage": 0.0,
                }

            async with db.execute(
                "SELECT COUNT(*) FROM main.files a "
                "INNER JOIN cat_b.files b ON a.hash = b.hash"
            ) as cur:
                matches = (await cur.fetchone())[0]
        finally:
            try:
                await db.execute("DETACH DATABASE cat_b")
            except Exception:
                pass

    coverage = matches / min(total_a, total_b)
    return {
        "total_a":  total_a,
        "total_b":  total_b,
        "matches":  matches,
        "coverage": coverage,
    }


async def migrate_unique_files(
    folder_src: Path,
    folder_dst: Path,
    on_progress: Callable[[int, int], None] | None = None,
    *, move_file=None,
) -> dict:
    """
    Mueve archivos únicos (por hash) de folder_src a folder_dst.

    - Hash NO existe en dst + archivo existe en disco  → mover, registrar en dst
    - Hash SÍ existe en dst                            → dejar en src (huérfano)
    - En catalog de src pero no en disco               → no mover bytes (purgado)
    - Sin errores físicos                             → conservar todo el historial en dst

    Retorna:
        moved          — archivos migrados exitosamente
        orphaned       — archivos que ya existen en dst (quedan en src)
        purged         — archivos en catalog sin presencia física (omitidos)
        errors         — lista de strings describiendo fallos
        orphaned_paths — rutas físicas de los archivos huérfanos en src
    """
    from .file_routing import (
        COUNTER_NAMESPACES, LAYOUT_VERSION,
    )

    if not await ensure_layout_version(
        folder_dst, COUNTER_NAMESPACES, LAYOUT_VERSION,
    ):
        return {
            "moved": 0, "orphaned": 0, "purged": 0,
            "errors": ["El destino necesita la migración de organización v2."],
            "orphaned_paths": [],
        }
    profile_meta = await read_profile_meta(folder_dst) or {}
    artist_name = profile_meta.get("display_name") or folder_dst.name

    src_files = await get_all_files(folder_src)
    total = len(src_files)

    moved    = 0
    orphaned = 0
    purged   = 0
    errors: list[str] = []
    orphaned_paths: list[Path] = []
    changed_paths: list[str] = []

    for i, f in enumerate(src_files):
        src_path = folder_src / f["filename"]

        if not src_path.exists():
            purged += 1
            if on_progress:
                on_progress(i + 1, total)
            continue

        if await hash_exists(folder_dst, f["hash"]):
            orphaned += 1
            orphaned_paths.append(src_path)
            if on_progress:
                on_progress(i + 1, total)
            continue

        new_path = None
        try:
            ctr, basename, new_dir, new_name = await reserve_destination(
                folder_dst, artist_name, f["filename"])
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = folder_dst / new_name

            # The host can preserve its own identity/curation before catalog acknowledgement.
            if new_path.exists():
                raise FileExistsError(str(new_path))
            handled = await move_file(src_path, new_path, f) if move_file is not None else False
            if not handled:
                await asyncio.to_thread(shutil.move, str(src_path), str(new_path))
            changed_paths.append(str(new_path))
            await add_file(
                folder_dst, f["hash"], new_name,
                f.get("url_source"), f.get("file_size"), ctr,
                is_preview=bool(f.get("is_preview")),
                content_service=f.get("content_service") or "",
                post_id=f.get("post_id") or "",
                content_role=f.get("content_role") or "",
                content_position=f.get("content_position"),
            )
            moved += 1
        except Exception as exc:
            errors.append(f"{f['filename']}: {exc}" + (f"; destination: {new_path}" if new_path is not None else ""))

        if on_progress:
            on_progress(i + 1, total)

    if not errors:
        from .archives import migrate_history
        try:
            await migrate_history(folder_src, folder_dst)
            await merge_download_history(folder_src, folder_dst)
        except Exception as exc:
            errors.append(f"Historial de descargas y comprimidos: {exc}")

    return {
        "moved":          moved,
        "orphaned":       orphaned,
        "purged":         purged,
        "errors":         errors,
        "orphaned_paths": orphaned_paths,
        "changed_paths": changed_paths,
    }


async def get_stats(artist_dir: Path) -> dict:
    """Retorna estadísticas básicas del catálogo."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return {"total": 0, "total_size": 0}
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files"
        ) as cur:
            row = await cur.fetchone()
    return {"total": row[0], "total_size": row[1]}
