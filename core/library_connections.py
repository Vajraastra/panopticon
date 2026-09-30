"""Conexiones centrales: nunca se transfieren entre hilos."""
import sqlite3
from pathlib import Path


def open_reader(db_path):
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro',
                           uri=True, timeout=0.25)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    except BaseException:
        conn.close()
        raise
