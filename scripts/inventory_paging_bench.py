"""Banco SQL privado de 300K rutas; mide memoria Python, no archivos ni RAM de SQLite."""
import gc
import json
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database_manager import DatabaseManager
from core.inventory_candidates import pages
from core.library_service import close_library_services


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
    db = DatabaseManager(Path(temporary)/'private.db')
    try:
        count, folder = 300000, 'C:/private-collection'
        with closing(sqlite3.connect(db.db_path)) as conn:
            conn.executemany('INSERT INTO files(path,filename) VALUES (?,?)',
                ((folder+'/image-%06d.png' % n, 'image.png') for n in range(count)))
            conn.commit()
        def legacy():
            known = db.get_known_files_in_folder(folder)
            return len(known)
        old_count, old = measure(legacy)
        state = dict(count=0, maximum_page=0, page_count=0, last_id=None)
        def paginated():
            with db.inventory_reader() as reader:
                for rows in pages(reader, folder, lambda: False, threading.Event()):
                    state['page_count'] += 1
                    state['maximum_page'] = max(state['maximum_page'], len(rows))
                    for copy_id, path in rows:
                        if state['last_id'] is not None and copy_id <= state['last_id']:
                            raise AssertionError('keyset repeated an ID')
                        if path != folder+'/image-%06d.png' % (copy_id-1):
                            raise AssertionError('path/ID mismatch')
                        state['last_id'] = copy_id
                        state['count'] += 1
            return state['count']
        new_count, new = measure(paginated)
        assert old_count == new_count == count
        assert state['maximum_page'] == 256
        assert state['page_count'] == 1172 and state['last_id'] == count
        assert new['python_peak_bytes'] < old['python_peak_bytes']
        assert db.service.metrics_snapshot()['reads_active'] == 0
        started = time.perf_counter()
        assert legacy() == count
        old_seconds = time.perf_counter()-started
        state = dict(count=0, maximum_page=0, page_count=0, last_id=None)
        started = time.perf_counter()
        assert paginated() == count
        new_seconds = time.perf_counter()-started
        print(json.dumps(dict(rows=count, legacy=old, paginated=new,
            untraced_seconds=dict(legacy=round(old_seconds, 3), paginated=round(new_seconds, 3)),
            **state), indent=2))
        print('inventory paging benchmark: 7 checks')
    finally:
        close_library_services()
