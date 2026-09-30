"""Preparación explícita por dueño; las lecturas nunca crean índices."""
import re
import sqlite3
import time

from core.duplicate_candidates import DIGEST, VALID

NAME = 'idx_panopticon_duplicate_hash_v1'
DDL = f'CREATE INDEX {NAME} ON files({DIGEST}) WHERE {VALID}'
MAX_BUILD_SECONDS = 30


def _canonical(sql):
    # No casefold sobre literales SQL: 'text' y 'TEXT' no son equivalentes.
    return re.sub(r'\s+', ' ', sql).strip().rstrip(';')


def status(conn):
    row = conn.execute('SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?', (NAME,)).fetchone()
    if row is None:
        return dict(prepared=False)
    if row[0] != 'index' or row[1] != 'files' or not isinstance(row[2], str) or _canonical(row[2]) != _canonical(DDL):
        raise ValueError('duplicate_index_name_conflict')
    return dict(prepared=True)


def prepare(conn):
    if not conn.in_transaction:
        raise RuntimeError('duplicate_index_requires_transaction')
    if status(conn)['prepared']:
        return True
    deadline = time.monotonic()+MAX_BUILD_SECONDS
    timed_out = False
    def interrupt():
        nonlocal timed_out
        timed_out = time.monotonic() >= deadline
        return int(timed_out)
    conn.set_progress_handler(interrupt, 1000)
    try:
        conn.execute(DDL)
    except sqlite3.Error as exc:
        if timed_out:
            raise ValueError('duplicate_index_build_budget_exceeded') from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
    if not status(conn)['prepared']:
        raise ValueError('duplicate_index_prepare_unconfirmed')
    return True  # La fachada sólo entrega este resultado tras COMMIT confirmado.
