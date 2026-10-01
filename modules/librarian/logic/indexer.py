import os
import logging
from PySide6.QtCore import QThread, Signal
from core.catalog_reader import is_cherry_catalog, read_image_files, get_artist_info
from core.inventory import InventoryResult, InventoryStatus, directory_identity, directory_chain_identity

log = logging.getLogger(__name__)


class IndexerWorker(QThread):
    """Ingesta observaciones válidas; sólo reconcilia inventarios completos."""

    progress_signal = Signal(str)
    count_signal = Signal(int, int)
    finished_signal = Signal()

    def __init__(self, db_manager, folders, deep_clean=False, translate=None):
        super().__init__()
        self.db = db_manager
        self.folders = folders
        self.deep_clean = deep_clean
        self.is_running = True
        self.batch_size = 100
        self.tr = translate or (lambda key, default: default)
        self.outcome = InventoryStatus.COMPLETE
        self.folder_statuses = {}
        self.archived_count = 0

    def _progress(self, key, default, **values):
        self.progress_signal.emit(self.tr(key, default).format(**values))

    def run(self):
        total_found = 0
        try:
            self._progress("lib.scan.start", "Starting indexer...")
            extensions = ('.png', '.jpg', '.jpeg', '.webp', '.avif')
            for folder in self.folders:
                if not self.is_running:
                    if self.outcome == InventoryStatus.COMPLETE:
                        self.outcome = InventoryStatus.CANCELLED
                    break
                self._progress("lib.scan.folder", "Scanning: {folder}", folder=folder)
                result = self._discover_tree(folder, extensions)
                try:
                    for i in range(0, len(result.files), self.batch_size):
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            break
                        batch = result.files[i:i + self.batch_size]
                        if not self.db.register_files_minimal(batch):
                            result.fail(InventoryStatus.ERROR, "No se pudo confirmar el lote")
                            break
                        total_found += len(batch)
                        self.count_signal.emit(i + len(batch), len(result.files))
                    if not self.is_running and result.complete:
                        result.fail(InventoryStatus.CANCELLED)
                    if self.deep_clean and result.complete:
                        self._reconcile_missing(folder, result)
                except Exception as exc:
                    log.exception("[Indexer] No se pudo indexar %s", folder)
                    result.fail(InventoryStatus.ERROR, exc)
                self.folder_statuses[str(folder)] = result.status
                if not result.complete:
                    if self.outcome == InventoryStatus.COMPLETE:
                        self.outcome = result.status
                    log.warning("[Indexer] Inventario %s: %s; %s", folder, result.status.value, result.issues)
                    self._progress("lib.scan.incomplete_folder",
                                   "Incomplete inventory; further offline archival stopped: {folder}", folder=folder)
        except Exception:
            self.outcome = InventoryStatus.ERROR
            log.exception("[Indexer] Fallo inesperado")
        finally:
            if not self.is_running and self.outcome == InventoryStatus.COMPLETE:
                self.outcome = InventoryStatus.CANCELLED
            try:
                if self.outcome == InventoryStatus.COMPLETE:
                    self._progress("lib.scan.complete", "Indexing complete: {count} files registered.", count=total_found)
                elif self.outcome == InventoryStatus.CANCELLED:
                    self._progress("lib.scan.cancelled", "Indexing cancelled; confirmed batches remain. Pending inventory was not archived.")
                else:
                    self._progress("lib.scan.incomplete", "Indexing incomplete; unverified batches were not archived. Confirmed batches remain.")
            finally:
                self.finished_signal.emit()

    def stop(self):
        self.is_running = False

    def _reconcile_missing(self, folder, result):
        # Un catálogo puede ir detrás del disco: una omisión no demuestra un borrado.
        try:
            identity = directory_identity(folder)
            if identity != result.root_identity:
                result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió antes de limpiar")
                return
            candidates = set(self.db.get_known_files_in_folder(folder)) - result.paths
            missing = []
            parents = {}
            for path in candidates:
                if not self.is_running:
                    result.fail(InventoryStatus.CANCELLED)
                    return
                try:
                    os.lstat(path)
                except FileNotFoundError:
                    # ENOENT también ocurre por padre inaccesible/desconectado;
                    # no convertirlo en ausencia confirmada por una raíz sana.
                    parent = os.path.dirname(path)
                    if parent not in parents:
                        parents[parent] = directory_chain_identity(parent)
                    # La carpeta pudo cambiar mientras verificábamos acceso.
                    try:
                        os.lstat(path)
                    except FileNotFoundError:
                        pass
                    else:
                        continue
                    missing.append(path)
                except OSError as exc:
                    result.fail(InventoryStatus.ERROR, exc)
                    return
            if directory_identity(folder) != identity:
                result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió antes de limpiar")
                return
            for parent, before in parents.items():
                if not self.is_running:
                    result.fail(InventoryStatus.CANCELLED)
                    return
                if directory_chain_identity(parent) != before:
                    result.fail(InventoryStatus.UNAVAILABLE, 'La carpeta padre cambió antes de limpiar')
                    return
            if not self.is_running:
                result.fail(InventoryStatus.CANCELLED)
                return
            if missing:
                self._progress("lib.scan.cleanup", "Reviewing {count} missing candidates for archival by copy ID...", count=len(missing))
                if not self.is_running:
                    result.fail(InventoryStatus.CANCELLED)
                    return
                # Leer la versión antes de verificar ausencia de nuevo. El owner
                # compara IDs/estado completo: nunca archivar por la ruta sola.
                missing.sort()
                for start in range(0, len(missing), 256):
                    if not self.is_running:
                        result.fail(InventoryStatus.CANCELLED)
                        return
                    block = missing[start:start+256]
                    current = self.db.get_duplicate_snapshot(block)
                    if (not isinstance(current, list) or len(current) != len(block) or
                            [copy.get('path') for copy in current] != block or
                            any(copy['state'] is None for copy in current)):
                        result.fail(InventoryStatus.ERROR, 'Cambió la identidad del inventario')
                        return
                    approved, touched = [], set()
                    for copy in current:
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            return
                        try:
                            os.lstat(copy['path'])
                        except FileNotFoundError:
                            touched.add(os.path.dirname(copy['path']))
                            approved.append(dict(path=copy['path'], copy_id=copy['state']['file']['id'],
                                fingerprint=copy['fingerprint']))
                        except OSError as exc:
                            result.fail(InventoryStatus.UNAVAILABLE, exc)
                            return
                    if directory_identity(folder) != identity or any(
                            directory_chain_identity(parent) != parents[parent] for parent in touched):
                        result.fail(InventoryStatus.UNAVAILABLE, 'La carpeta cambió antes de archivar el lote')
                        return
                    if not self.is_running:
                        result.fail(InventoryStatus.CANCELLED)
                        return
                    if approved:
                        normalized = os.path.normpath(folder).replace('\\', '/')
                        if self.db.archive_missing_copies(approved, folder=normalized) is not True:
                            result.fail(InventoryStatus.ERROR, 'No se pudo confirmar el archivado por ID')
                            return
                        self.archived_count += len(approved)
        except OSError as exc:
            result.fail(InventoryStatus.UNAVAILABLE, exc)

    def _discover_tree(self, folder, extensions):
        result = InventoryResult()
        if not self.is_running:
            result.fail(InventoryStatus.CANCELLED)
            return result
        try:
            result.root_identity = directory_identity(folder)
        except OSError as exc:
            result.fail(InventoryStatus.UNAVAILABLE, exc)
            return result
        try:
            if is_cherry_catalog(folder):
                return self._discover_cherry(folder)

            def walk_error(exc):
                result.fail(InventoryStatus.ERROR, exc)

            for root, dirs, files in os.walk(folder, onerror=walk_error):
                if not self.is_running:
                    result.fail(InventoryStatus.CANCELLED)
                    break
                for d in list(dirs):
                    if not self.is_running:
                        result.fail(InventoryStatus.CANCELLED)
                        break
                    if is_cherry_catalog(os.path.join(root, d)):
                        result.merge(self._discover_cherry(os.path.join(root, d)))
                        dirs.remove(d)
                for filename in files:
                    if not self.is_running:
                        result.fail(InventoryStatus.CANCELLED)
                        break
                    if filename.lower().endswith(extensions):
                        path = os.path.join(root, filename)
                        try:
                            info = os.stat(path)
                            result.files.append({
                                'path': os.path.normpath(path).replace('\\', '/'),
                                'filename': filename, 'size': info.st_size,
                                'created': info.st_ctime,
                            })
                        except OSError as exc:
                            result.fail(InventoryStatus.ERROR, exc)
            if not self.is_running:
                result.fail(InventoryStatus.CANCELLED)
            elif result.complete and directory_identity(folder) != result.root_identity:
                result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió durante el recorrido")
        except OSError as exc:
            result.fail(InventoryStatus.ERROR, exc)
        return result

    def _discover_cherry(self, folder):
        self._progress("lib.scan.catalog", "Reading Cherry catalog...")
        inventory = read_image_files(folder, cancelled=lambda: not self.is_running)
        info = (get_artist_info(folder) or {}) if self.is_running else {}
        result = InventoryResult(status=inventory.status, issues=list(inventory.issues),
                                 root_identity=inventory.root_identity)
        for entry in inventory.files:
            if not self.is_running:
                result.fail(InventoryStatus.CANCELLED)
                break
            result.files.append({
                'path': os.path.normpath(entry['path']).replace('\\', '/'),
                'filename': entry['filename'], 'size': entry['size'],
                'created': entry['created'], 'hash_original': entry['cherry_hash'],
                'origin_url': entry['cherry_url'], 'origin_site': info.get('primary_site'),
                'artist': info.get('display_name'),
            })
        return result
