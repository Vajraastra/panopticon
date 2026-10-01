"""Fachada compatible de datos; no expone conexiones SQLite a los módulos."""
import logging
import os

from core.library_service import READS, WRITES, LibraryError, get_library_service

log = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_path=None, *, service=None):
        self.service = service or get_library_service(db_path)
        self.db_path = self.service.db_path

    def inventory_reader(self, *, cancel_event=None):
        return self.service.inventory_reader(cancel_event=cancel_event)

    def __getattr__(self, name):
        if name in READS:
            return lambda *args, **kwargs: self.service.read(name, *args, **kwargs)
        if name in WRITES:
            return lambda *args, **kwargs: self._write(name, *args, **kwargs)
        raise AttributeError(name)

    def _write(self, name, *args, **kwargs):
        try:
            return self.service.submit(name, *args, **kwargs).result()
        except LibraryError as exc:
            log.warning('[DB] %s', exc)
            return False

    def add_tag_to_file(self, path, tag_name):
        # Stat fuera de la transacción. Registro perezoso + tag + FTS son atómicos.
        file_data = None
        if not self.get_file_id(path):
            try:
                info = os.stat(path)
                file_data = dict(path=path, filename=os.path.basename(path),
                                 size=info.st_size, created=info.st_ctime)
            except OSError:
                return False
        return self._write('add_tag_to_file', path, tag_name, file_data=file_data)
