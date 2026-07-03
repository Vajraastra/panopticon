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
from PySide6.QtCore import QThread, Signal

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

    def __init__(self, db_manager, batch_size=200):
        super().__init__()
        self.db         = db_manager
        self.batch_size = batch_size
        self.is_running = True

    def run(self):
        total = self.db.count_files_missing_hash()
        if not total:
            self.progress_signal.emit("✅ Nada que hashear.")
            self.finished_signal.emit(0, 0)
            return

        self.progress_signal.emit(f"🧬 Calculando SHA-256 de {total} archivos...")
        done = failed = 0
        last_id = 0  # paginación por keyset: los fallidos no re-entran en esta corrida

        while self.is_running:
            batch = self.db.get_files_missing_hash(after_id=last_id, limit=self.batch_size)
            if not batch:
                break

            pairs = []
            for file_id, path in batch:
                if not self.is_running:
                    break
                last_id = file_id
                try:
                    pairs.append((sha256_file(path), file_id))
                    done += 1
                except OSError as e:
                    failed += 1
                    log.warning("[Backfill] Ilegible, se salta: %s (%s)", path, e)

            if pairs:
                self.db.set_hash_original_bulk(pairs)
            self.count_signal.emit(done + failed, total)

        self.progress_signal.emit(
            f"✅ Backfill: {done} hasheados, {failed} saltados."
        )
        self.finished_signal.emit(done, failed)

    def stop(self):
        self.is_running = False
