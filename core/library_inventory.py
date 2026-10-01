"""Páginas del inventario operativo: IDs, sin conexión compartida ni snapshot largo."""
from core.library_results import collect_rows, ReadResultError, validate_page
import threading


def _id(value):
    return type(value) is int and -2**63 <= value <= 2**63-1


def page(conn, folder, *, folder_like, after_id=None, upper_id=None, limit=256):
    validate_page(limit)
    if (limit > 256 or not isinstance(folder, str) or not folder or len(folder) > 4096 or '\x00' in folder or
            (after_id is not None and not _id(after_id)) or
            (upper_id is not None and not _id(upper_id)) or
            (after_id is not None and (upper_id is None or after_id > upper_id))):
        raise ReadResultError('invalid_inventory_page')
    pattern = folder_like(folder)
    if upper_id is None:
        upper_id = conn.execute(
            "SELECT max(id) FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\'",
            (pattern,)).fetchone()[0]
    if upper_id is None:
        return dict(rows=[], upper_id=None, after_id=None, done=True)
    where = "is_offline=0 AND path LIKE ? ESCAPE '\\' AND id <= ?"
    params = [pattern, upper_id]
    if after_id is not None:
        where += ' AND id > ?'
        params.append(after_id)
    rows = collect_rows(conn.execute(
        f'SELECT id, path FROM files WHERE {where} ORDER BY id LIMIT ?', [*params, limit]))
    if any(not _id(row[0]) or not isinstance(row[1], str) or not row[1] or
           len(row[1]) > 4096 or '\x00' in row[1] for row in rows):
        raise ReadResultError('invalid_inventory_path')
    last = rows[-1][0] if rows else after_id
    return dict(rows=rows, upper_id=upper_id, after_id=last,
                done=len(rows) < limit or last == upper_id)


class _PageLease:
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        try:
            self._conn.set_progress_handler(None, 0)
        finally:
            self._conn.rollback()


class InventoryReader:
    """Objeto privado de páginas, ligado al hilo/scope; no expone una conexión."""
    def __init__(self, service, conn, cancel_event):
        self._service, self._conn, self._cancel = service, conn, cancel_event
        self._thread = threading.get_ident()
        self._closed = False

    def get_inventory_page(self, folder, *, cancel_event=None, **kwargs):
        from core.library_service import LibraryError
        operation = 'get_inventory_page'
        if self._closed:
            raise LibraryError(operation, 'inventory_reader_closed')
        if threading.get_ident() != self._thread:
            raise LibraryError(operation, 'inventory_reader_wrong_thread')
        return self._service._read(operation, (folder,), kwargs,
            cancel_event if cancel_event is not None else self._cancel,
            lambda: _PageLease(self._conn))

    def close(self):
        self._closed = True
