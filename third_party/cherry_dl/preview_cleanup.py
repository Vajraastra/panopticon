"""Reversión recuperable de las previews descargadas experimentalmente."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


PREVIEW_TABLE_SQL = """
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
)
"""


@dataclass
class CatalogCleanup:
    catalog: Path
    downloaded: list[tuple[str, str]] = field(default_factory=list)
    queued: int = 0
    awaiting: int = 0
    preview_like_untracked: list[Path] = field(default_factory=list)
    error: str = ""


@dataclass
class CleanupReport:
    root: Path
    catalogs: list[CatalogCleanup]
    applied: bool = False
    quarantine: Path | None = None

    @property
    def downloaded(self) -> int:
        return sum(len(item.downloaded) for item in self.catalogs)

    @property
    def queued(self) -> int:
        return sum(item.queued for item in self.catalogs)

    @property
    def awaiting(self) -> int:
        return sum(item.awaiting for item in self.catalogs)


@dataclass
class IndexReconcileReport:
    adjusted_profiles: int = 0
    removed_from_counts: int = 0
    already_applied: bool = False
    errors: list[str] = field(default_factory=list)


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info('{table}')")}


def inspect_catalog(catalog: Path) -> CatalogCleanup:
    result = CatalogCleanup(catalog=catalog)
    try:
        with closing(sqlite3.connect(catalog)) as db:
            if "is_preview" in _columns(db, "files"):
                result.downloaded = [
                    (str(row[0]), str(row[1]))
                    for row in db.execute(
                        "SELECT hash, filename FROM files WHERE is_preview = 1"
                    )
                ]
            for table, attr in (
                ("pending_queue", "queued"),
                ("awaiting_archive", "awaiting"),
            ):
                if "is_preview" in _columns(db, table):
                    count = db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE is_preview = 1"
                    ).fetchone()[0]
                    setattr(result, attr, int(count))
        marked = {filename.casefold() for _, filename in result.downloaded}
        result.preview_like_untracked = [
            path for path in catalog.parent.glob("preview_*")
            if path.is_file() and path.name.casefold() not in marked
        ]
    except (OSError, sqlite3.Error) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def inspect_tree(root: Path) -> CleanupReport:
    root = root.expanduser().resolve()
    catalogs = [
        inspect_catalog(path)
        for path in sorted(root.rglob("catalog.db"))
        if ".cherry-preview-quarantine" not in path.parts
    ]
    return CleanupReport(root=root, catalogs=catalogs)


def _insert_markers(db: sqlite3.Connection, table: str, now: int) -> None:
    if "is_preview" not in _columns(db, table):
        return
    date_column = "discovered_at" if table == "pending_queue" else "first_seen"
    db.execute(
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


def _apply_catalog(
    root: Path, item: CatalogCleanup, quarantine: Path, now: int,
) -> None:
    if item.error or not (item.downloaded or item.queued or item.awaiting):
        return
    folder = item.catalog.parent.resolve()
    moved: list[tuple[Path, Path]] = []
    backup = quarantine / item.catalog.relative_to(root).with_suffix(
        ".before-preview-cleanup.db"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(item.catalog)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            with closing(sqlite3.connect(backup)) as backup_db:
                db.backup(backup_db)
            for _, filename in item.downloaded:
                source = (folder / filename).resolve()
                if source.parent != folder:
                    raise ValueError(f"nombre inseguro en catálogo: {filename!r}")
                if not source.is_file():
                    continue
                destination = quarantine / source.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"ya existe en cuarentena: {destination}")
                source.replace(destination)
                moved.append((source, destination))

            db.execute("BEGIN IMMEDIATE")
            db.execute(PREVIEW_TABLE_SQL)
            _insert_markers(db, "pending_queue", now)
            _insert_markers(db, "awaiting_archive", now)
            if "is_preview" in _columns(db, "files"):
                db.execute(
                    """
                    INSERT INTO preview_only
                        (url_source, filename_hint, post_id, post_published,
                         observed_hash, content_service, original_filename,
                         content_role, content_position, profile_url_id,
                         first_seen, last_seen)
                    SELECT url_source, filename, post_id, '', hash,
                           content_service, filename, content_role,
                           content_position, NULL, date_added, ?
                    FROM files
                    WHERE is_preview = 1 AND url_source IS NOT NULL
                    ON CONFLICT(url_source) DO UPDATE SET
                        last_seen = excluded.last_seen
                    """,
                    (now,),
                )
                if _columns(db, "content_aliases"):
                    db.execute(
                        "DELETE FROM content_aliases WHERE file_hash IN "
                        "(SELECT hash FROM files WHERE is_preview = 1)"
                    )
                db.execute("DELETE FROM files WHERE is_preview = 1")
            for table in ("pending_queue", "awaiting_archive"):
                if "is_preview" in _columns(db, table):
                    db.execute(f"DELETE FROM {table} WHERE is_preview = 1")
            db.commit()
    except BaseException:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
        raise


def apply_cleanup(
    report: CleanupReport, *, include_untracked: bool = False,
) -> CleanupReport:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    quarantine = report.root / ".cherry-preview-quarantine" / stamp
    now = int(datetime.now().timestamp())
    for item in report.catalogs:
        try:
            _apply_catalog(report.root, item, quarantine, now)
            if include_untracked:
                for source in item.preview_like_untracked:
                    source = source.resolve()
                    if source.parent != item.catalog.parent.resolve():
                        raise ValueError(f"archivo fuera del catálogo: {source}")
                    destination = quarantine / source.relative_to(report.root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        raise FileExistsError(
                            f"ya existe en cuarentena: {destination}"
                        )
                    source.replace(destination)
        except (OSError, sqlite3.Error, ValueError) as exc:
            item.error = f"{type(exc).__name__}: {exc}"
    report.applied = True
    report.quarantine = quarantine if quarantine.exists() else None
    return report


def reconcile_index_from_quarantine(
    index_db: Path, root: Path, quarantine: Path,
) -> IndexReconcileReport:
    """Resta previews de file_count una sola vez por carpeta de cuarentena."""
    result = IndexReconcileReport()
    root = root.expanduser().resolve()
    quarantine = quarantine.expanduser().resolve()
    expected_parent = root / ".cherry-preview-quarantine"
    if quarantine.parent != expected_parent.resolve() or not quarantine.is_dir():
        result.errors.append(f"cuarentena fuera de la biblioteca: {quarantine}")
        return result

    reductions: Counter[str] = Counter()
    for backup in quarantine.rglob("*.before-preview-cleanup.db"):
        try:
            with closing(sqlite3.connect(backup)) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM files WHERE is_preview = 1"
                ).fetchone()[0]
        except sqlite3.Error as exc:
            result.errors.append(f"{backup}: {exc}")
            continue
        reductions[backup.relative_to(quarantine).parts[0]] += int(count)
    if result.errors:
        return result
    operation_key = f"preview-cleanup:{quarantine}"
    try:
        with closing(sqlite3.connect(index_db)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_log ("
                "operation_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            if db.execute(
                "SELECT 1 FROM maintenance_log WHERE operation_key = ?",
                (operation_key,),
            ).fetchone():
                db.rollback()
                result.already_applied = True
                return result

            for folder_name, count in reductions.items():
                folder = str((root / folder_name).resolve())
                rows = db.execute(
                    """
                    SELECT pu.id, pu.file_count
                    FROM profile_urls pu
                    JOIN profiles p ON p.id = pu.profile_id
                    WHERE p.folder_path = ? COLLATE NOCASE
                      AND pu.site = 'pawchive' COLLATE NOCASE
                    """,
                    (folder,),
                ).fetchall()
                if len(rows) != 1:
                    result.errors.append(
                        f"{folder_name}: se esperaban 1 fuente Pawchive, hay {len(rows)}"
                    )
                    continue
                url_id, current = rows[0]
                removed = min(max(0, int(current)), int(count))
                db.execute(
                    "UPDATE profile_urls SET file_count = MAX(0, file_count - ?) "
                    "WHERE id = ?",
                    (count, url_id),
                )
                result.adjusted_profiles += 1
                result.removed_from_counts += removed

            if result.errors:
                db.rollback()
                result.adjusted_profiles = 0
                result.removed_from_counts = 0
                return result
            db.execute(
                "INSERT INTO maintenance_log VALUES (?, datetime('now'))",
                (operation_key,),
            )
            db.commit()
    except (OSError, sqlite3.Error) as exc:
        result.errors.append(f"{type(exc).__name__}: {exc}")
    return result
