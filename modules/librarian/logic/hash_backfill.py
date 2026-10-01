"""
Backfill de hash_original para archivos sin identidad de contenido (M3 de
CHERRY_FUSION_DESIGN). Calcula SHA-256 en streaming para cada fila con
hash_original IS NULL — las carpetas cherry no pasan por aquí: sus hashes
llegan gratis del catalog.db vía el indexer (M2).

Reanudable por diseño: el progreso ES la columna poblada; si se detiene o
se corta, la próxima corrida continúa donde quedó. Los archivos ilegibles
(borrados/movidos a media corrida) se saltan y quedan NULL para el próximo
intento o para la limpieza de huérfanos del indexer.
"""

import hashlib
import logging
import threading
from PySide6.QtCore import QThread, Signal
from core.inventory import InventoryStatus
from core.library_batch import BatchCancelled, MAX_BATCH_ROWS

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 1 MiB — I/O-bound; sin decodificar imagen


def sha256_file(path: str) -> str:
    """SHA-256 hex de un archivo, en streaming (no carga el archivo entero)."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class HashBackfillWorker(QThread):
    progress_signal = Signal(str)       # mensaje de estado para la UI
    count_signal    = Signal(int, int)  # (procesados, total)
    finished_signal = Signal(int, int)  # (hasheados, fallidos)

    def __init__(self, db_manager, batch_size=200, translate=None):
        super().__init__()
        self.db         = db_manager
        self.batch_size = batch_size
        self.is_running = True
        self._cancel = threading.Event()
        self.tr = translate or (lambda key, default: default)
        self.outcome = InventoryStatus.COMPLETE

    def run(self):
        done = failed = 0
        pending = []
        try:
            total = self.db.count_files_missing_hash()
            self.progress_signal.emit(self.tr(
                "lib.hash.start", "Computing SHA-256 for {count} files...").format(count=total))
            last_id = 0
            while self.is_running:
                batch = self.db.get_files_missing_hash(after_id=last_id, limit=self.batch_size)
                if not batch:
                    break
                pending = []
                for file_id, path in batch:
                    if not self.is_running:
                        break
                    last_id = file_id
                    try:
                        pending.append((sha256_file(path), file_id))
                    except OSError as exc:
                        failed += 1
                        log.warning("[Backfill] Ilegible: %s (%s)", path, exc)
                if not pending:
                    self.count_signal.emit(done + failed, total)
                while pending and self.is_running:
                    block = pending[:MAX_BATCH_ROWS]
                    if not self.db.set_hash_original_bulk(block, cancel_event=self._cancel):
                        raise RuntimeError('Hash batch was not committed')
                    done += len(block)
                    del pending[:len(block)]
                    self.count_signal.emit(done + failed, total)
        except BatchCancelled:
            self.outcome = InventoryStatus.CANCELLED
        except Exception:
            failed += len(pending)
            self.outcome = InventoryStatus.ERROR
            log.exception('[Backfill] Fallo de base de datos')
        finally:
            if not self.is_running and self.outcome != InventoryStatus.ERROR:
                self.outcome = InventoryStatus.CANCELLED
            try:
                key = 'lib.hash.complete' if self.outcome == InventoryStatus.COMPLETE else 'lib.hash.incomplete'
                self.progress_signal.emit(self.tr(key,
                    "Hashing: {hashed} confirmed, {failed} skipped.").format(hashed=done, failed=failed))
            finally:
                self.finished_signal.emit(done, failed)

    def stop(self):
        self.is_running = False
        self._cancel.set()
