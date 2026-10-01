"""Synthetic SQL-only scaling fixture. No images, real DB or filesystem keys."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService
from core.library_connections import open_reader


def benchmark(rows=10000, warm=30):
    if type(rows) is not int or not 1<=rows<=500000 or type(warm) is not int or not 1<=warm<=100:
        raise ValueError('invalid_private_benchmark_size')
    with tempfile.TemporaryDirectory() as private:
        service=LibraryService(Path(private)/'synthetic.db')
        try:
            service.submit('prepare_path_identity').result(5)
            started=time.perf_counter()
            with closing(sqlite3.connect(service.db_path)) as writer:
                # Fabricated bindings benchmark SQL only, never physical proof.
                writer.executemany('INSERT INTO files(id,path,filename) VALUES(?,?,?)',
                    ((i,f'C:/PrivateFixture/Image{i}.png',f'Image{i}.png') for i in range(1,rows+1)))
                writer.executemany('INSERT INTO path_identity_records VALUES(?,?,0,\'resolved\',?,?,NULL)',
                    ((i,f'C:/PrivateFixture/Image{i}.png',f'C:/PrivateFixture/Image{i}.png','[1,2,3,4,5,6,7]') for i in range(1,rows+1)))
                writer.commit()
            setup=time.perf_counter()-started
            counts=dict(coverage_scans=0, conflict_scans=0)
            def trace(sql):
                if 'SUM(CASE WHEN r.file_id IS NULL' in sql:
                    counts['coverage_scans']+=1
                if 'GROUP BY r.canonical HAVING' in sql:
                    counts['conflict_scans']+=1
            def reader(path):
                conn=open_reader(path)
                conn.set_trace_callback(trace)
                return conn
            def lookup():
                started=time.perf_counter()
                value=service.read('get_path_identity_resolution','C:/PrivateFixture/Image1.png')
                elapsed=time.perf_counter()-started
                assert value['status']=='unique' and value['file_id']==1 and value['coverage']['total']==rows
                return elapsed
            with patch('core.library_service.open_reader',side_effect=reader):
                cold=lookup()
                samples=[lookup() for _ in range(warm)]
            assert counts==dict(coverage_scans=1,conflict_scans=1),counts
            return dict(fixture='synthetic SQL only; no filesystem identity proof',fixture_rows=rows,
                setup_seconds=round(setup,6),cold_seconds=round(cold,6),
                warm_lookups=warm,warm_median_seconds=round(statistics.median(samples),6),
                warm_max_seconds=round(max(samples),6),**counts)
        finally:
            service.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=10000)
    parser.add_argument('--warm',type=int,default=30)
    args=parser.parse_args()
    print(json.dumps(benchmark(args.rows,args.warm),ensure_ascii=False,indent=2))
