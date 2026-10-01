"""300K etiquetas en SQLite temporal; pico Python de lista completa frente a página."""
import gc
import json
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from contextlib import closing
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.library_service import LibraryService


def measure(action):
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    value = action()
    seconds = time.perf_counter()-started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, dict(seconds=round(seconds, 3), python_peak_bytes=peak)


with tempfile.TemporaryDirectory() as temporary:
    service = LibraryService(Path(temporary)/'private.db')
    try:
        count = 300000
        with closing(sqlite3.connect(service.db_path)) as conn:
            conn.executemany('INSERT INTO tags(name) VALUES (?)', ((f'tag-{n:06d}',) for n in range(count)))
            conn.commit()
        full_count, full = measure(lambda: len(service.read('get_all_tags')))
        value, paged = measure(lambda: service.read('get_tags_page'))
        started = time.perf_counter()
        rare = service.read('get_tags_page', query='tag-299999')
        rare_seconds = time.perf_counter()-started
        assert full_count == count
        assert len(value['names']) == 200 and value['more'] and value['next_name'] == 'tag-000199'
        assert rare['names'] == ['tag-299999'] and not rare['more']
        assert paged['python_peak_bytes'] < full['python_peak_bytes']
        assert service.metrics_snapshot()['reads_active'] == 0
        print(json.dumps(dict(rows=count, full=full, paged=paged, rare_query_seconds=round(rare_seconds, 3)), indent=2))
        print('tag vocabulary benchmark: 5 checks')
    finally:
        service.close()
