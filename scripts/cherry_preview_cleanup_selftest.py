"""Prueba sin red de la reversión recuperable de previews."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "third_party"))

from cherry_dl.preview_cleanup import (  # noqa: E402
    apply_cleanup,
    inspect_tree,
    reconcile_index_from_quarantine,
)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cherry-preview-cleanup-") as tmp:
        root = Path(tmp)
        artist = root / "Artist"
        artist.mkdir()
        catalog = artist / "catalog.db"
        preview = artist / "preview_Artist_00001.webp"
        untracked = artist / "preview_Artist_00999.webp"
        final = artist / "Artist_00002.png"
        preview.write_bytes(b"degraded")
        untracked.write_bytes(b"orphan")
        final.write_bytes(b"final")
        with closing(sqlite3.connect(catalog)) as db:
            db.executescript(
                """
                CREATE TABLE files (
                    hash TEXT PRIMARY KEY, filename TEXT, url_source TEXT,
                    date_added INTEGER, file_size INTEGER, counter INTEGER,
                    is_preview INTEGER, content_service TEXT, post_id TEXT,
                    content_role TEXT, content_position INTEGER
                );
                CREATE TABLE content_aliases (file_hash TEXT);
                CREATE TABLE pending_queue (
                    url_source TEXT PRIMARY KEY, download_url TEXT,
                    filename_hint TEXT, post_id TEXT, post_published TEXT,
                    remote_hash TEXT, content_service TEXT,
                    original_filename TEXT, content_role TEXT,
                    content_position INTEGER, remote_size INTEGER,
                    is_preview INTEGER, extra_headers TEXT,
                    profile_url_id INTEGER, discovered_at INTEGER,
                    queue_position INTEGER
                );
                CREATE TABLE awaiting_archive (
                    url_source TEXT PRIMARY KEY, download_url TEXT,
                    filename_hint TEXT, post_id TEXT, post_published TEXT,
                    remote_hash TEXT, content_service TEXT,
                    original_filename TEXT, content_role TEXT,
                    content_position INTEGER, remote_size INTEGER,
                    is_preview INTEGER, extra_headers TEXT,
                    profile_url_id INTEGER, first_seen INTEGER,
                    last_checked INTEGER, next_check INTEGER,
                    check_count INTEGER
                );
                """
            )
            db.execute(
                "INSERT INTO files VALUES (?, ?, ?, 1, 8, 1, 1, ?, ?, ?, ?)",
                ("a" * 64, preview.name, "pawchive://post/1", "patreon", "1", "primary", 0),
            )
            db.execute(
                "INSERT INTO files VALUES (?, ?, ?, 1, 5, 2, 0, ?, ?, ?, ?)",
                ("b" * 64, final.name, "pawchive://post/2", "patreon", "2", "primary", 0),
            )
            db.execute(
                "INSERT INTO pending_queue VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, NULL, 1, NULL, 7, 1, 1)",
                ("pawchive://post/3", "https://invalid/3", "three.jpg", "3", "c" * 64,
                 "patreon", "three.jpg", "primary", 0),
            )
            db.commit()

        report = inspect_tree(root)
        assert report.downloaded == 1 and report.queued == 1
        apply_cleanup(report)
        assert not preview.exists()
        assert untracked.exists()
        assert final.exists()
        assert report.quarantine and (report.quarantine / "Artist" / preview.name).exists()
        assert (report.quarantine / "Artist" / "catalog.before-preview-cleanup.db").exists()
        with closing(sqlite3.connect(catalog)) as db:
            assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM pending_queue").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM preview_only").fetchone()[0] == 2
        second = inspect_tree(root)
        apply_cleanup(second, include_untracked=True)
        assert not untracked.exists()
        assert second.quarantine and (
            second.quarantine / "Artist" / untracked.name
        ).exists()
        index_db = root / "index.db"
        with closing(sqlite3.connect(index_db)) as db:
            db.executescript(
                """
                CREATE TABLE profiles (id INTEGER PRIMARY KEY, folder_path TEXT);
                CREATE TABLE profile_urls (
                    id INTEGER PRIMARY KEY, profile_id INTEGER,
                    site TEXT, file_count INTEGER
                );
                """
            )
            db.execute("INSERT INTO profiles VALUES (1, ?)", (str(artist.resolve()),))
            db.execute("INSERT INTO profile_urls VALUES (7, 1, 'pawchive', 10)")
            db.commit()
        reconciled = reconcile_index_from_quarantine(
            index_db, root, second.quarantine,
        )
        assert not reconciled.errors, reconciled.errors
        expected_reduction = 0
        for backup in second.quarantine.rglob("*.before-preview-cleanup.db"):
            with closing(sqlite3.connect(backup)) as db:
                expected_reduction += db.execute(
                    "SELECT COUNT(*) FROM files WHERE is_preview = 1"
                ).fetchone()[0]
        assert reconciled.removed_from_counts == expected_reduction
        again = reconcile_index_from_quarantine(index_db, root, second.quarantine)
        assert again.already_applied
        print("preview cleanup: 12/12")


if __name__ == "__main__":
    main()
