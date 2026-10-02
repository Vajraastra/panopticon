"""Private metadata-only memory fixture; disk spool, no image tree or real DB."""
import argparse
import gc
import json
import os
from pathlib import Path
import sys
import time
import tracemalloc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.inventory import InventoryResult
from core.inventory_spool import InventorySpool


def record(i):
    return dict(path=f'C:/PrivateFixture/Árbol/Image{i:06d}.png', filename=f'Image{i:06d}.png',
                size=7, created=1, hash_original=f'{i:064x}',
                origin_url=f'https://example.invalid/private/{i}', origin_site='private', artist='private')


def benchmark(rows=10000):
    if type(rows) is not int or not 1 <= rows <= 300000:
        raise ValueError('invalid_private_benchmark_size')
    tracemalloc.start()
    started = time.perf_counter()
    inventory = InventoryResult(files=[record(i) for i in range(rows)])
    paths = inventory.paths
    assert record(rows-1)['path'] in paths
    list_seconds = time.perf_counter()-started
    _, list_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del inventory, paths
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with InventorySpool() as spool:
            for i in range(rows):
                spool.append(record(i))
            spool.seal()
            assert len(spool) == rows
            assert spool[rows-1]['hash_original'] == f'{rows-1:064x}'
            assert record(rows-1)['path'] in spool.path_membership()
            stream_bytes = os.fstat(spool._file.fileno()).st_size
            index_bytes = (Path(spool._dir.name)/'paths.db').stat().st_size
            spool_seconds = time.perf_counter()-started
            _, spool_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return dict(fixture='metadata only, Python tracemalloc; no image tree, RSS, real DB or live Cherry',
        rows=rows, legacy_list_and_set_peak_bytes=list_peak, spool_peak_bytes=spool_peak,
        legacy_instrumented_seconds=round(list_seconds, 6), spool_instrumented_seconds=round(spool_seconds, 6),
        stream_bytes=stream_bytes, index_bytes=index_bytes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=10000)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.rows), indent=2))
