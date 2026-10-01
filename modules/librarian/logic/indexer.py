import os
import logging
import threading
from PySide6.QtCore import QThread, Signal
from core.catalog_reader import is_cherry_catalog, read_image_files, get_artist_info
from core.inventory import InventoryResult, InventoryStatus, directory_identity, directory_chain_identity

from core.inventory_candidates import InventoryCandidates, IdentityCache, pages, InventoryCancelled
from core.library_service import LibraryError
from core.library_batch import BatchCancelled, MAX_BATCH_ROWS

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
        self._inventory_cancel = threading.Event()
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
            if type(self.batch_size) is not int or self.batch_size < 1:
                raise ValueError('invalid_indexer_batch_size')
            batch_size = min(self.batch_size, MAX_BATCH_ROWS)
            extensions = ('.png', '.jpg', '.jpeg', '.webp', '.avif')
            for folder in self.folders:
                if not self.is_running:
                    if self.outcome == InventoryStatus.COMPLETE:
                        self.outcome = InventoryStatus.CANCELLED
                    break
                self._progress("lib.scan.folder", "Scanning: {folder}", folder=folder)
                result = self._discover_tree(folder, extensions)
                try:
                    for i in range(0, len(result.files), batch_size):
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            break
                        batch = result.files[i:i + batch_size]
                        if not self.db.register_files_minimal(batch, cancel_event=self._inventory_cancel):
                            result.fail(InventoryStatus.ERROR, "No se pudo confirmar el lote")
                            break
                        total_found += len(batch)
                        self.count_signal.emit(i + len(batch), len(result.files))
                    if not self.is_running and result.complete:
                        result.fail(InventoryStatus.CANCELLED)
                    if self.deep_clean and result.complete:
                        self._reconcile_missing(folder, result)
                except BatchCancelled:
                    result.fail(InventoryStatus.CANCELLED)
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
        self._inventory_cancel.set()

    def _reconcile_missing(self, folder, result):
        # Una omisión de catálogo no demuestra ausencia. Leer todas las páginas
        # antes de archivar; retener el ID inicial en un temporal, no otro set DB.
        try:
            identity = directory_identity(folder)
            if identity != result.root_identity:
                result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió antes de limpiar")
                return
            discovered = result.paths
            parents = IdentityCache(lambda parent: directory_chain_identity(parent))
            with InventoryCandidates() as missing, self.db.inventory_reader(cancel_event=self._inventory_cancel) as reader:
                for batch in pages(reader, folder, lambda: not self.is_running, self._inventory_cancel):
                    for copy_id, path in batch:
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            return
                        if path in discovered:
                            continue
                        try:
                            os.lstat(path)
                        except FileNotFoundError:
                            parent = os.path.dirname(path)
                            before = parents.read(parent)
                            try:
                                os.lstat(path)
                            except FileNotFoundError:
                                pass
                            else:
                                continue
                            missing.add(copy_id, path, before)
                        except OSError as exc:
                            result.fail(InventoryStatus.ERROR, exc)
                            return
                if directory_identity(folder) != identity:
                    result.fail(InventoryStatus.UNAVAILABLE, "La raíz cambió antes de limpiar")
                    return
                # Validar cada padre y la integridad/completitud del temporal
                # antes del primer UPDATE. El cache tiene como máximo256 entradas.
                parents = IdentityCache(lambda parent: directory_chain_identity(parent))
                for block in missing.blocks():
                    for _, path, dev, ino in block:
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            return
                        if parents.read(os.path.dirname(path)) != (dev, ino):
                            result.fail(InventoryStatus.UNAVAILABLE, 'La carpeta padre cambió antes de limpiar')
                            return
                if not self.is_running:
                    result.fail(InventoryStatus.CANCELLED)
                    return
                if missing.count:
                    self._progress("lib.scan.cleanup", "Reviewing {count} missing candidates for archival by copy ID...", count=missing.count)
                for records in missing.blocks():
                    if not self.is_running:
                        result.fail(InventoryStatus.CANCELLED)
                        return
                    block = [record[1] for record in records]
                    current = self.db.get_duplicate_snapshot(block)
                    if (not isinstance(current, list) or len(current) != len(block) or
                            [copy.get('path') for copy in current] != block or
                            any(copy['state'] is None or copy['state']['file']['id'] != record[0]
                                for copy, record in zip(current, records))):
                        result.fail(InventoryStatus.ERROR, 'Cambió la identidad del inventario')
                        return
                    approved, touched = [], {}
                    for copy, record in zip(current, records):
                        if not self.is_running:
                            result.fail(InventoryStatus.CANCELLED)
                            return
                        try:
                            os.lstat(copy['path'])
                        except FileNotFoundError:
                            parent = os.path.dirname(copy['path'])
                            before = tuple(record[2:])
                            if parent in touched and touched[parent] != before:
                                result.fail(InventoryStatus.UNAVAILABLE, 'La carpeta padre cambió antes de archivar')
                                return
                            touched[parent] = before
                            approved.append(dict(path=copy['path'], copy_id=record[0], fingerprint=copy['fingerprint']))
                        except OSError as exc:
                            result.fail(InventoryStatus.UNAVAILABLE, exc)
                            return
                    if directory_identity(folder) != identity or any(
                            directory_chain_identity(parent) != before for parent, before in touched.items()):
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
        except InventoryCancelled:
            result.fail(InventoryStatus.CANCELLED)
        except LibraryError as exc:
            result.fail(InventoryStatus.CANCELLED if exc.reason == 'cancelled' and not self.is_running
                        else InventoryStatus.ERROR, exc)
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
