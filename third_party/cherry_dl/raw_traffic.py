"""Durable rolling download-body traffic, independent of successful files.

Pending reservations survive an unclean exit conservatively: on the next
download they count as received at restart. Normal cancellation releases only
the unused reservation. A one-second bucket expires at its upper bound.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3
from uuid import uuid4


class RawQuotaExceeded(ValueError):
    """An HTTP attempt cannot be admitted within its raw-body reservation."""


class RawTrafficWindow:
    def __init__(self, path: Path, window: float, limit: int):
        self.path, self.window, self.limit = path, window, limit
        self.ready = False
        self.failed = False

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            if write:
                if self.failed:
                    raise OSError("La cuota de tráfico requiere recuperación antes de continuar")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=2)
            else:
                connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        except Exception:
            if write:
                self.failed = True
            raise
        finally:
            if connection is not None:
                connection.close()

    def initialize(self, now: float, legacy: list[dict]):
        if self.failed:
            raise OSError("La cuota de tráfico requiere recuperación antes de continuar")
        if self.ready:
            return
        with self._connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS traffic (at REAL PRIMARY KEY, bytes INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS pending (token TEXT PRIMARY KEY, bytes INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY)")
            if not db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
                for item in legacy:
                    self._add(db, float(item['at']), int(item['bytes']))
                db.execute("INSERT INTO metadata VALUES ('seeded')")
            # A previous producer did not acknowledge releasing these bytes.
            orphaned = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM pending").fetchone()[0]
            if orphaned:
                self._add(db, math.ceil(now), orphaned)
            db.execute("DELETE FROM pending")
            db.execute("DELETE FROM traffic WHERE at<=?", (now - self.window,))
        self.ready = True

    @staticmethod
    def _add(db, at: float, amount: int):
        if amount > 0:
            db.execute("INSERT INTO traffic VALUES (?, ?) ON CONFLICT(at) DO UPDATE SET bytes=bytes+excluded.bytes",
                       (at, amount))

    def snapshot(self, now: float, legacy: list[dict]):
        if not self.path.exists():
            return [(v['at'], v['bytes']) for v in legacy if v['at'] > now - self.window], 0, 0
        with self._connection() as db:
            traffic = db.execute("SELECT at, bytes FROM traffic WHERE at>? ORDER BY at", (now - self.window,)).fetchall()
            reserved, count = db.execute("SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM pending").fetchone()
        return traffic, reserved, count

    def reserve(self, amount: int, now: float) -> str:
        token = uuid4().hex
        with self._connection(write=True) as db:
            used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM traffic WHERE at>?", (now - self.window,)).fetchone()[0]
            reserved = db.execute("SELECT COALESCE(SUM(bytes),0) FROM pending").fetchone()[0]
            if used + reserved + amount > self.limit:
                raise ValueError("La reserva supera la cuota de tráfico crudo")
            db.execute("INSERT INTO pending VALUES (?,?)", (token, amount))
        return token

    def resize(self, token: str, amount: int, now: float) -> bool:
        with self._connection(write=True) as db:
            used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM traffic WHERE at>?", (now - self.window,)).fetchone()[0]
            other = db.execute("SELECT COALESCE(SUM(bytes),0) FROM pending WHERE token!=?", (token,)).fetchone()[0]
            if used + other + amount > self.limit:
                return False
            return db.execute("UPDATE pending SET bytes=? WHERE token=?", (amount, token)).rowcount == 1

    def receive(self, token: str, amount: int, now: float) -> bool:
        with self._connection(write=True) as db:
            row = db.execute("SELECT bytes FROM pending WHERE token=?", (token,)).fetchone()
            if row is None:
                raise ValueError("La transferencia no tiene una reserva vigente")
            self._add(db, math.ceil(now), amount)
            db.execute("UPDATE pending SET bytes=? WHERE token=?", (max(0, row[0] - amount), token))
            db.execute("DELETE FROM traffic WHERE at<=?", (now - self.window,))
        return amount <= row[0]

    def release(self, token: str):
        if self.failed:
            # Do not erase evidence after receipt persistence failed.
            return
        with self._connection(write=True) as db:
            db.execute("DELETE FROM pending WHERE token=?", (token,))
