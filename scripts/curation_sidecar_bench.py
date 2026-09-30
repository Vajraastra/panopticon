"""Banco reproducible de exportación, exclusivamente DB/carpeta temporales."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import curation_sidecar
from core.library_repository import LibraryRepository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--rounds', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.rows <= 100000 or not 1 <= args.rounds <= 30:
        parser.error('rows1..100000;rounds1..30')
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory) / 'collection'
        folder.mkdir()
        db = Path(directory) / 'fixture.db'
        with closing(sqlite3.connect(db, isolation_level=None)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            LibraryRepository(conn).create_schema()
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT INTO files(path,filename,hash_original,file_size,created_date,rating) '
                'VALUES(?,?,?,1,1,3)', [((folder / f'{i:06}.png').as_posix(), f'{i:06}.png', 'a'*64) for i in range(args.rows)])
            conn.executemany("INSERT INTO tags(name,category) VALUES(?,'manual')", [('alpha',), ('beta',), ('gamma',)])
            conn.execute("INSERT INTO file_tags(file_id,tag_id,source) SELECT f.id,t.id,'manual' FROM files f CROSS JOIN tags t")
            conn.commit()
            timings = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                result = curation_sidecar.export_curation(db, folder)
                timings.append((time.perf_counter() - start) * 1000)
                assert result['copies'] == args.rows
            real_open = curation_sidecar.open_reader
            queries = []
            def reader(path):
                connection = real_open(path)
                if Path(path).resolve() == db.resolve():
                    connection.set_trace_callback(queries.append)
                return connection
            from unittest.mock import patch
            with patch.object(curation_sidecar, 'open_reader', reader):
                tracemalloc.start()
                curation_sidecar.export_curation(db, folder)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            source_selects = sum(sql.startswith('SELECT') for sql in queries)
            queries.clear()
            with patch.object(curation_sidecar, 'open_reader', reader):
                page = curation_sidecar.compare_curation_page(db, folder)
                assert all(row['status'] == 'same' for row in page['reports'])
            print(json.dumps(dict(fixture_only=True, rows=args.rows, tags_per_copy=3, rounds=args.rounds,
                sqlite=sqlite3.sqlite_version, python=sys.version.split()[0], median_ms=statistics.median(timings),
                python_peak_bytes=peak, source_selects=source_selects, compare_page_source_selects=sum(
                    sql.startswith('SELECT') for sql in queries), output_bytes=(folder / curation_sidecar.NAME).stat().st_size), indent=2))


if __name__ == '__main__':
    main()
