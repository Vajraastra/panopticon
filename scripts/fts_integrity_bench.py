"""Banco FTS reproducible, exclusivamente con bases temporales propias."""
import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_repository import LibraryRepository


def measure(rows, scenario, operation, trace):
    with tempfile.TemporaryDirectory() as folder:
        with closing(sqlite3.connect(Path(folder) / 'fixture.db', isolation_level=None)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            repo = LibraryRepository(conn)
            repo.create_schema()
            conn.execute('BEGIN IMMEDIATE')
            conn.executemany('INSERT INTO files(path,filename,meta_positive,meta_model) VALUES (?,?,?,?)',
                             ((f'/fixture/{i}.png', f'picture_{i}.png',
                               'flowers trees clouds sunshine detailed illustration ' * 3,
                               'fixture model') for i in range(rows)))
            repo.repair_fts()
            conn.commit()
            if scenario == 'missing_10_percent':
                conn.execute('DELETE FROM files_fts WHERE rowid % 10=0')
            elif scenario == 'stale_10_percent':
                conn.execute("UPDATE files_fts SET meta_positive='stale' WHERE rowid % 10=0")
            elif scenario == 'equal_count_wrong_ids':
                conn.execute('UPDATE files_fts SET rowid=rowid+? WHERE rowid % 10=0', (rows,))
            statements = []
            if trace:
                conn.set_trace_callback(lambda s: statements.append(s) if not s.startswith('--') else None)
            before = conn.total_changes
            start = time.perf_counter()
            conn.execute('BEGIN' if operation == 'check_fts' else 'BEGIN IMMEDIATE')
            result = getattr(repo, operation)()
            conn.commit()
            elapsed = (time.perf_counter() - start) * 1000
            conn.set_trace_callback(None)
            changes = conn.total_changes - before
            if operation != 'check_fts' and not repo.check_fts()['consistent']:
                raise AssertionError('repair verification failed')
            return dict(ms=elapsed, sql_statements=len(statements), changes=changes,
                        fts_deletes=sum(s.startswith('DELETE FROM files_fts') for s in statements),
                        fts_inserts=sum(s.startswith('INSERT INTO files_fts') for s in statements),
                        result=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--rounds', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.rows <= 100000 or not 1 <= args.rounds <= 30:
        parser.error('rows:1..100000; rounds:1..30')
    results = {}
    for scenario in ('healthy', 'missing_10_percent', 'stale_10_percent', 'equal_count_wrong_ids'):
        results[scenario] = {}
        for operation in ('check_fts', 'repair_fts', 'rebuild_fts'):
            counted = measure(args.rows, scenario, operation, True)
            counted.pop('ms')  # El trace no contamina las medianas.
            counted['median_ms'] = statistics.median(
                measure(args.rows, scenario, operation, False)['ms'] for _ in range(args.rounds))
            results[scenario][operation] = counted
    print(json.dumps(dict(fixture_only=True, sqlite=sqlite3.sqlite_version,
                         python=sys.version, rows=args.rows, rounds=args.rounds,
                         journal='WAL', synchronous='FULL', results=results), indent=2))


if __name__ == '__main__':
    main()
