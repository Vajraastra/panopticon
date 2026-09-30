"""Banco reproducible WAL/FULL sobre fixtures; --baseline acepta fuente local anterior."""
import argparse
import importlib.util
import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_repository import LibraryRepository


def measure(repository, scenario, size, rounds):
    times = []
    metrics = None
    for run in range(rounds + 1):
        with tempfile.TemporaryDirectory() as folder:
            conn = sqlite3.connect(Path(folder) / 'fixture.db', isolation_level=None)
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA synchronous=FULL')
                conn.execute('PRAGMA foreign_keys=ON')
                repo = repository(conn)
                repo.create_schema()
                repo.run_transformations()
                batch = [dict(path=f'C:/fixture/{i}.png', filename=f'{i}.png', size=13,
                              created=1.25, hash_original=f'fixture-{i}', artist='fixture artist')
                         for i in range(size)]
                if scenario != 'new':
                    conn.execute('BEGIN IMMEDIATE')
                    assert repo.register_files_minimal(batch) is True
                    conn.commit()
                if scenario == 'size_10_percent':
                    batch = [dict(row, size=26) if i % 10 == 0 else row
                             for i, row in enumerate(batch)]
                trace = []
                syncs = []
                changes = conn.total_changes
                sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone()
                if run == 0:
                    conn.set_trace_callback(trace.append)
                    original = repo._fts_sync_file

                    def sync(file_id, cursor=None):
                        syncs.append(file_id)
                        return original(file_id, cursor)

                    repo._fts_sync_file = sync
                start = time.perf_counter()
                conn.execute('BEGIN IMMEDIATE')
                assert repo.register_files_minimal(batch) is True
                conn.commit()
                elapsed = (time.perf_counter() - start) * 1000
                if run == 0:
                    conn.set_trace_callback(None)
                    final_sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone()
                    metrics = dict(sql_statements=len([sql for sql in trace if not sql.startswith('--')]),
                                   fts_syncs=len(syncs), changes=conn.total_changes - changes,
                                   sequence_delta=final_sequence[0] - (sequence[0] if sequence else 0))
                else:
                    times.append(elapsed)
            finally:
                conn.close()
    return dict(median_ms=round(statistics.median(times), 3), rounds=rounds, **metrics)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--rounds', type=int, default=9)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error('rounds must be 1..100')
    implementations = {'current': LibraryRepository}
    if args.baseline:
        spec = importlib.util.spec_from_file_location('inventory_baseline', args.baseline.resolve())
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        implementations = {'baseline': module.LibraryRepository, **implementations}
    result = {'sqlite': sqlite3.sqlite_version, 'python': sys.version.split()[0],
              'journal': 'WAL', 'synchronous': 'FULL', 'fixture_only': True, 'results': {}}
    for name, repository in implementations.items():
        result['results'][name] = {
            f'{scenario}_{size}': measure(repository, scenario, size, args.rounds)
            for scenario, size in [('unchanged', 100), ('unchanged', 1000),
                                   ('size_10_percent', 1000), ('new', 1000)]
        }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
