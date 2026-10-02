"""Bounded synthetic SQL ingestion benchmark; no real DB or physical identity proof."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService
from core.library_repository import LibraryRepository
from core.path_identity import PathIdentity


def benchmark(rows=10000, batches=30, size=100):
    if (type(rows) is not int or not 1 <= rows <= 500000 or
            type(batches) is not int or not 1 <= batches <= 100 or
            type(size) is not int or not 1 <= size <= 100):
        raise ValueError('invalid_private_benchmark_size')
    counts = dict(coverage_scans=0, conflict_scans=0)
    original = LibraryRepository.__init__
    def repository(repo, conn, **kwargs):
        original(repo, conn, **kwargs)
        def trace(sql):
            if 'SUM(CASE WHEN r.file_id IS NULL' in sql:
                counts['coverage_scans'] += 1
            if 'GROUP BY r.canonical HAVING' in sql:
                counts['conflict_scans'] += 1
        conn.set_trace_callback(trace)
    def observation(path, **kwargs):
        return PathIdentity(path=path, status='resolved', canonical=path, kind='file',
                            physical=(1, 2, 3, 0, 7, 0, 0), reason=None)
    with tempfile.TemporaryDirectory() as private, patch.object(LibraryRepository, '__init__', repository):
        service = LibraryService(Path(private)/'synthetic.db')
        try:
            service.submit('prepare_path_identity').result(5)
            started = time.perf_counter()
            with closing(sqlite3.connect(service.db_path)) as writer:
                # Fabricated SQL bindings, never authority for a real library.
                writer.executemany('INSERT INTO files(id,path,filename) VALUES(?,?,?)',
                    ((i, f'C:/PrivateFixture/Image{i}.png', f'Image{i}.png') for i in range(1, rows+1)))
                writer.executemany("INSERT INTO path_identity_records VALUES(?,?,0,'resolved',?,?,NULL)",
                    ((i, f'C:/PrivateFixture/Image{i}.png', f'C:/PrivateFixture/Image{i}.png',
                      '[1,2,3,0,7,0,0]') for i in range(1, rows+1)))
                writer.commit()
            setup = time.perf_counter()-started
            started = time.perf_counter()
            coverage = service.read('get_path_identity_status')
            cold = time.perf_counter()-started
            assert coverage['total'] == rows and coverage['pending'] == 0
            samples = []
            with patch('core.library_path_ingest.observe_path', side_effect=observation):
                for batch in range(batches):
                    records = [dict(path=f'C:/PrivateFixture/New{batch}-{i}.png',
                                    filename=f'New{batch}-{i}.png', size=7, created=1) for i in range(size)]
                    started = time.perf_counter()
                    assert service.register_files_observed(records, cancel_event=threading.Event()) is True
                    samples.append(time.perf_counter()-started)
            coverage = service.read('get_path_identity_status')
            assert coverage['total'] == rows+batches*size and coverage['pending'] == 0
            return dict(fixture='synthetic SQL only; mocked probes; no physical identity or real DB',
                fixture_rows=rows, inserted_rows=batches*size, batch_rows=size, batches=batches,
                setup_seconds=round(setup, 6), cold_coverage_seconds=round(cold, 6),
                batch_median_seconds=round(statistics.median(samples), 6),
                batch_max_seconds=round(max(samples), 6), **counts)
        finally:
            service.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--batches', type=int, default=30)
    parser.add_argument('--size', type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.rows, args.batches, args.size), indent=2))
