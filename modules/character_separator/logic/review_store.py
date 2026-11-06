"""Durable CCIP review decisions and verified exports for each source folder."""

from contextlib import contextmanager, closing
import json
import os
from pathlib import Path
import sqlite3

import numpy as np

from core.ai.ccip import DIMENSIONS, SPACE
from core.output_paths import safe_path
from core.path_identity import local_path
from core.paths import ProjectPaths
from .export import IMAGE_EXTENSIONS, file_signature, validate_name
from .recognition import Sample, ScanCancelled
from .review import Decision, FullRun, ReviewSession


_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_scopes (
    scope TEXT PRIMARY KEY,
    mother TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recent_review (
    space TEXT PRIMARY KEY,
    scope TEXT NOT NULL REFERENCES review_scopes(scope)
);
CREATE TABLE IF NOT EXISTS review_decisions (
    scope TEXT NOT NULL REFERENCES review_scopes(scope),
    filename TEXT NOT NULL,
    name TEXT,
    signature TEXT NOT NULL,
    embedding BLOB NOT NULL,
    reference INTEGER NOT NULL CHECK(reference IN (0, 1)),
    PRIMARY KEY(scope, filename)
);
CREATE TABLE IF NOT EXISTS review_exports (
    scope TEXT NOT NULL REFERENCES review_scopes(scope),
    filename TEXT NOT NULL,
    name TEXT NOT NULL,
    destination TEXT NOT NULL,
    source_signature TEXT NOT NULL,
    destination_signature TEXT NOT NULL,
    PRIMARY KEY(scope, filename)
);
"""


def _directory_stamp(path):
    info = path.stat()
    if not path.is_dir():
        raise ValueError("invalid_scope")
    return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", 0)


def _scope(mother, source):
    mother = safe_path(local_path(str(mother)))
    source = safe_path(local_path(str(source)))
    if source != mother and source.parent != mother:
        raise ValueError("invalid_scope")
    key = json.dumps((SPACE, os.path.normcase(mother), _directory_stamp(mother),
                      os.path.normcase(source), _directory_stamp(source)))
    return key, mother, source


def _filename(path, source):
    path = Path(path)
    if path.parent != source or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("invalid_review_path")
    local_path(str(path))
    return path.name


def _destination(path, mother, source, name):
    path = Path(path)
    if (path.parent.parent != mother or path.parent == source
            or path.parent.name.casefold() != name.casefold()
            or path.suffix.lower() not in IMAGE_EXTENSIONS):
        raise ValueError("invalid_review_destination")
    return Path(local_path(str(path)))


def _signature(path):
    try:
        return file_signature(path)
    except (OSError, ValueError):
        return None


def _decode_signature(raw):
    signature = json.loads(raw)
    if not isinstance(signature, list) or len(signature) != 5 or any(type(item) is not int for item in signature):
        raise ValueError("invalid_review_signature")
    return tuple(signature)


def _check_vector(vector):
    if vector.shape != (DIMENSIONS,) or not np.isfinite(vector).all() or not np.any(vector):
        raise ValueError("invalid_review_embedding")
    return vector


class ReviewStore:
    """Short-lived SQLite connections. Call operations from a worker."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else ProjectPaths.root() / "data" / "character_separator.db"

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(_SCHEMA)
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db

    @staticmethod
    def _ensure_scope(db, key, mother, source):
        db.execute(
            "INSERT OR IGNORE INTO review_scopes(scope, mother, source) VALUES (?, ?, ?)",
            (key, str(mother), str(source)),
        )

    def last_scope(self) -> tuple[Path, Path] | None:
        if not self.path.exists():
            return None
        with self._transaction() as db:
            row = db.execute(
                """SELECT mother, source FROM recent_review
                   JOIN review_scopes USING (scope) WHERE space = ?""",
                (SPACE,),
            ).fetchone()
        return (Path(row[0]), Path(row[1])) if row is not None else None

    def load(self, mother: Path, source: Path, *, cancelled=None) -> ReviewSession:
        key, mother, source = _scope(mother, source)
        with self._transaction() as db:
            self._ensure_scope(db, key, mother, source)
            decisions = db.execute(
                """SELECT filename, name, signature, embedding, reference
                   FROM review_decisions WHERE scope = ? ORDER BY filename""", (key,),
            ).fetchall()
            exports = db.execute(
                """SELECT filename, name, destination, source_signature, destination_signature
                   FROM review_exports WHERE scope = ?""", (key,),
            ).fetchall()

        session = ReviewSession()
        for filename, name, signature, embedding, reference in decisions:
            if cancelled is not None and cancelled():
                raise ScanCancelled()
            path = source / filename
            if filename != _filename(path, source):
                raise ValueError("invalid_review_path")
            stamp = _decode_signature(signature)
            if _signature(path) != stamp:
                continue
            if name is not None:
                name = validate_name(name)
            vector = _check_vector(np.frombuffer(embedding, dtype="<f4"))
            session.decisions[path] = Decision(name, Sample(stamp, vector), bool(reference))

        for filename, name, destination, source_sig, destination_sig in exports:
            if cancelled is not None and cancelled():
                raise ScanCancelled()
            path = source / filename
            if filename != _filename(path, source):
                raise ValueError("invalid_review_path")
            destination = _destination(destination, mother, source, validate_name(name))
            try:
                actual = _signature(safe_path(destination))
            except (OSError, ValueError):
                continue
            if _signature(path) != _decode_signature(source_sig) or actual != _decode_signature(destination_sig):
                continue
            session.exported.add(path)
            session.destinations.setdefault(name, set()).add(destination)
            session.receipts[path] = name, destination

        if cancelled is not None and cancelled():
            raise ScanCancelled()
        with self._transaction() as db:
            db.execute(
                """INSERT INTO recent_review(space, scope) VALUES (?, ?)
                   ON CONFLICT(space) DO UPDATE SET scope = excluded.scope""",
                (SPACE, key),
            )
        return session

    def save_decisions(self, mother: Path, source: Path, decisions: dict[Path, Decision]) -> None:
        key, mother, source = _scope(mother, source)
        rows = []
        for path, decision in decisions.items():
            filename = _filename(path, source)
            name = validate_name(decision.name) if decision.name is not None else None
            sample = decision.sample
            if _signature(path) != sample.signature:
                raise ValueError("source_changed")
            embedding = _check_vector(np.asarray(sample.embedding, dtype="<f4")).tobytes()
            rows.append((key, filename, name, json.dumps(sample.signature), embedding, int(decision.reference)))
        with self._transaction() as db:
            self._ensure_scope(db, key, mother, source)
            db.executemany(
                """INSERT INTO review_decisions(scope, filename, name, signature, embedding, reference)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope, filename) DO UPDATE SET
                   name = excluded.name, signature = excluded.signature,
                   embedding = excluded.embedding, reference = excluded.reference""",
                rows,
            )

    def delete_character(self, mother: Path, source: Path, name: str) -> None:
        key, _, _ = _scope(mother, source)
        name = validate_name(name)
        with self._transaction() as db:
            db.execute("DELETE FROM review_decisions WHERE scope = ? AND name = ?", (key, name))
            db.execute("DELETE FROM review_exports WHERE scope = ? AND name = ?", (key, name))

    def save_export(self, mother: Path, source: Path, run: FullRun) -> None:
        key, mother, source = _scope(mother, source)
        rows = []
        for path, (name, destination) in zip(run.output.completed, run.output.destinations, strict=True):
            filename = _filename(path, source)
            name = validate_name(name)
            destination = _destination(destination, mother, source, name)
            source_sig = _signature(path)
            if source_sig is None or source_sig != run.signatures.get(path):
                continue
            try:
                destination_sig = _signature(safe_path(destination))
            except (OSError, ValueError):
                continue
            if destination_sig is not None:
                rows.append((key, filename, name, str(destination),
                             json.dumps(source_sig), json.dumps(destination_sig)))

        with self._transaction() as db:
            self._ensure_scope(db, key, mother, source)
            db.executemany(
                """INSERT INTO review_exports(
                       scope, filename, name, destination, source_signature, destination_signature)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope, filename) DO UPDATE SET
                   name = excluded.name, destination = excluded.destination,
                   source_signature = excluded.source_signature,
                   destination_signature = excluded.destination_signature""",
                rows,
            )
            if run.error and run.error.code == "source_changed" and run.error.path is not None:
                filename = _filename(run.error.path, source)
                db.execute("DELETE FROM review_decisions WHERE scope = ? AND filename = ?", (key, filename))
                db.execute("DELETE FROM review_exports WHERE scope = ? AND filename = ?", (key, filename))
