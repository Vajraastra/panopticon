"""Existing Cherry read contract, paged RO snapshots closed before file I/O.

One connection, initial rowid frontier, <=128 rows per snapshot. No SQL writes,
schema changes or shared snapshot across the pass. Physical presence remains
the archival guard; omissions or an interrupted pass never prove deletion.
"""
import time
from core.inventory_candidates import InventoryCancelled

PAGE_ROWS = 128
READ_SECONDS = 10
TEXT_LIMITS = {'hash': 128, 'filename': 4096, 'url_source': 16384}
CONDITIONS = {name: f"(typeof({name})='null' OR (typeof({name})='text' AND length({name})<={limit}))"
              for name, limit in TEXT_LIMITS.items()}
CONDITIONS['date_added'] = "(typeof(date_added) IN ('null','integer','real') OR (typeof(date_added)='text' AND length(date_added)<=64))"
CONDITIONS['file_size'] = "typeof(file_size) IN ('null','integer')"
PROJECTION = ','.join(f'CASE WHEN {condition} THEN {name} ELSE NULL END AS {name}'
                      for name, condition in CONDITIONS.items())
VALID = ' AND '.join(CONDITIONS.values())


def pages(conn, cancelled):
    after = upper = None
    first = True
    while True:
        if cancelled():
            raise InventoryCancelled()
        started = time.monotonic()
        conn.set_progress_handler(lambda: int(cancelled() or time.monotonic()-started > READ_SECONDS), 1000)
        try:
            conn.execute('BEGIN')
            if first:
                upper = conn.execute('SELECT MAX(rowid) FROM files').fetchone()[0]
            if upper is None:
                block = []
            else:
                condition, args = 'rowid<=?', [upper]
                if after is not None:
                    condition += ' AND rowid>?'
                    args.append(after)
                # Oversized/invalid fields are flagged, never delivered as a
                # shortened accepted value. Python page memory remains bounded.
                cursor = conn.execute(f'SELECT rowid AS _inventory_id,({VALID}) AS _inventory_valid,{PROJECTION} '
                    'FROM files WHERE '+condition+' ORDER BY rowid LIMIT ?', (*args, PAGE_ROWS))
                try:
                    block = cursor.fetchall()
                finally:
                    cursor.close()
        finally:
            conn.set_progress_handler(None, 0)
            conn.rollback()
        if cancelled():
            raise InventoryCancelled()
        if block:
            yield block
        if len(block) < PAGE_ROWS or block[-1]['_inventory_id'] == upper:
            return
        after, first = block[-1]['_inventory_id'], False
