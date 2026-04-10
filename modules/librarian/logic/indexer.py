import os
import logging
from PySide6.QtCore import QThread, Signal
from core.catalog_reader import is_cherry_catalog, get_image_files

log = logging.getLogger(__name__)


class IndexerWorker(QThread):
    """
    Hilo de trabajo para el indexado robusto de archivos.

    Fase 1: Descubrimiento — los archivos aparecen en la DB lo antes posible.
    Fase 2: Limpieza de huérfanos — elimina registros de archivos borrados.

    Modo cherry-dl aware:
      Si la carpeta contiene un catalog.db de cherry-dl, se usa CatalogReader
      para obtener la lista de archivos. Esto garantiza que:
        - Solo se indexan imágenes (se ignoran .psd, .zip, .mp4, etc.)
        - Solo se indexan archivos que realmente existen en disco
          (las entradas de archivos borrados en catalog.db se saltan)
      Si no hay catalog.db, el comportamiento es el habitual (os.walk).
    """

    progress_signal = Signal(str)    # Mensaje de estado para la UI
    count_signal    = Signal(int, int)  # (actual, total)
    finished_signal = Signal()

    def __init__(self, db_manager, folders, deep_clean=False):
        super().__init__()
        self.db         = db_manager
        self.folders    = folders
        self.deep_clean = deep_clean
        self.is_running = True
        self.batch_size = 100

    # ------------------------------------------------------------------
    # Punto de entrada del hilo
    # ------------------------------------------------------------------

    def run(self):
        self.progress_signal.emit("🚀 Iniciando el indexador...")

        extensions  = ('.png', '.jpg', '.jpeg', '.webp', '.avif')
        total_found = 0

        for folder in self.folders:
            if not self.is_running:
                break

            folder_name = os.path.basename(folder)
            self.progress_signal.emit(f"🔍 Scanning: {folder_name}")

            # --- FASE 1: DESCUBRIMIENTO ---
            if is_cherry_catalog(folder):
                disk_files, disk_paths = self._discover_cherry(folder)
            else:
                disk_files, disk_paths = self._discover_walk(folder, extensions)

            total_found += len(disk_files)

            if disk_files:
                self.progress_signal.emit(
                    f"💾 Registrando {len(disk_files)} archivos en {folder_name}..."
                )
                for i in range(0, len(disk_files), self.batch_size):
                    self.db.register_files_minimal(disk_files[i : i + self.batch_size])

            # --- FASE 2: LIMPIEZA DE HUÉRFANOS ---
            if self.deep_clean:
                known_paths = self.db.get_known_files_in_folder(folder)
                orphans = list(known_paths - disk_paths)
                if orphans:
                    self.progress_signal.emit(
                        f"🧹 Eliminando {len(orphans)} archivos inexistentes..."
                    )
                    self.db.remove_files(orphans)

        self.progress_signal.emit(f"✅ Indexado completo. {total_found} archivos activos.")
        self.finished_signal.emit()

    def stop(self):
        self.is_running = False

    # ------------------------------------------------------------------
    # Estrategias de descubrimiento
    # ------------------------------------------------------------------

    def _discover_cherry(self, folder: str) -> tuple[list[dict], set[str]]:
        """
        Modo cherry-dl: usa CatalogReader para obtener la lista de imágenes.
        Devuelve (disk_files, disk_paths).
        """
        log.info("[Indexer] Modo cherry-dl detectado en: %s", folder)
        self.progress_signal.emit(f"🍒 Modo cherry-dl: leyendo catalog.db...")

        entries = get_image_files(folder)

        disk_files = []
        disk_paths = set()

        for entry in entries:
            if not self.is_running:
                break
            norm = os.path.normpath(entry['path']).replace('\\', '/')
            disk_files.append({
                'path':     norm,
                'filename': entry['filename'],
                'size':     entry['size'],
                'created':  entry['created'],
            })
            disk_paths.add(norm)

        return disk_files, disk_paths

    def _discover_walk(self, folder: str, extensions: tuple) -> tuple[list[dict], set[str]]:
        """
        Modo estándar: escaneo recursivo con os.walk.
        Devuelve (disk_files, disk_paths).
        """
        disk_files = []
        disk_paths = set()

        for root, _, files in os.walk(folder):
            for f in files:
                if not self.is_running:
                    return disk_files, disk_paths
                if f.lower().endswith(extensions):
                    full_path = os.path.join(root, f)
                    try:
                        stat = os.stat(full_path)
                        norm = os.path.normpath(full_path).replace('\\', '/')
                        disk_files.append({
                            'path':     norm,
                            'filename': f,
                            'size':     stat.st_size,
                            'created':  stat.st_ctime,
                        })
                        disk_paths.add(norm)
                    except OSError:
                        pass

        return disk_files, disk_paths
