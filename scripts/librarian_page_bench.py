"""Banco reproducible de páginas, exclusivamente sobre SQLite temporal propio."""
import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_repository import LibraryRepository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--rounds', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.rows <= 100000 or not 1 <= args.rounds <= 30:
        parser.error('rows 1..100000; rounds 1..30')
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / 'fixture.db', isolation_level=None)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            repo = LibraryRepository(conn)
            repo.create_schema()
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT INTO files(path,filename) VALUES (?,?)',
                [(f'C:/images/sub/{i:06}.png', f'{i:06}.png') for i in range(args.rows)])
            conn.commit()
            conn.execute('PRAGMA query_only=ON')
            operations = {
                'legacy_sample': lambda: repo.get_library_selection(folder='C:/images'),
                'first_page': lambda: repo.get_library_page(folder='C:/images'),
                'last_page': lambda: repo.get_library_page(folder='C:/images', offset=(args.rows - 1) // 100 * 100),
            }
            result = dict(rows=args.rows, rounds=args.rounds, sqlite=sqlite3.sqlite_version,
                          python=sys.version.split()[0], journal='WAL', synchronous='FULL', operations={})
            for name, operation in operations.items():
                samples = []
                for _ in range(args.rounds):
                    start = time.perf_counter()
                    conn.execute('BEGIN')
                    value = operation()
                    conn.commit()
                    samples.append((time.perf_counter() - start) * 1000)
                    assert value['count'] == args.rows
                queries = []
                conn.set_trace_callback(queries.append)
                tracemalloc.start()
                conn.execute('BEGIN')
                value = operation()
                conn.commit()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                conn.set_trace_callback(None)
                result['operations'][name] = dict(median_ms=statistics.median(samples),
                    python_peak_bytes=peak, delivered_paths=len(value['paths']), sql=len(queries))
            print(json.dumps(result, indent=2))
        finally:
            conn.close()


if __name__ == '__main__':
    main()
