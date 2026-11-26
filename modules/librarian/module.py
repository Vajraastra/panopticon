from core.native_threads import start_worker, native_done, owner_threads_ready
from shiboken6 import isValid
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QListWidget, QFileDialog, QMessageBox, QProgressBar, QGroupBox)
from PySide6.QtCore import Qt, QTimer
from core.base_module import BaseModule
from core.components.ui_roles import primary, section, hint
from core.database_manager import DatabaseManager
from core.library_revision_async import LibraryRevisionWatcher
from core.library_async import LibraryReader, LibraryWriter
from core.curation_tasks import CurationExportTasks
from core.components.curation_review import CurationReviewDialog
from core.components.curation_recovery import CurationRecoveryDialog
from core.components.copy_versions import CopyVersionsDialog
from core.components.path_identity_review import PathIdentityReviewDialog
from .logic.indexer import IndexerWorker
from .logic.hash_backfill import HashBackfillWorker
from .ui.folder_archive import FolderArchiveController, retain_worker
from core.inventory import InventoryStatus
from core.theme import Theme
import os
import logging

log = logging.getLogger(__name__)


class LibrarianModule(BaseModule):
    """
    El Bibliotecario (Librarian): administración de la biblioteca.
    Carpetas vigiladas, indexador, hashes, entradas fantasma, curación por carpeta,
    identidades de ruta y versiones de copia. Explorar y buscar (etiquetas,
    calificación, texto, envío a otras herramientas) es trabajo de Gallery (UI_DESIGN U1).
    """

    def __init__(self):
        super().__init__()
        self._name = "Librarian"
        self._description = "Library administration: watched folders, indexing and maintenance."
        self._icon = "📚"
        
        self.view = None
        # Gestor de base de datos SQLite persistente
        self.db = None
        self.indexer_thread = None
        self.backfill_thread = None

        # Re-index dirigido tras cambios externos (ej. Pickler mueve archivos).
        # Carpetas marcadas como "sucias" + temporizador para coalescer eventos.
        self._dirty_folders: set[str] = set()
        self._cherry_ingestor = None
        self._reindex_thread = None
        self._dirty_timer = QTimer()
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(1500)
        self._dirty_timer.timeout.connect(self._flush_dirty_reindex)
        
        self._folder_count = None  # imágenes indexadas de la carpeta seleccionada
        self._exporting = None
        self._review_dialog = None
        self._recovery_dialog = None
        self._versions_dialog = None
        self._identity_dialog = None
        self._path_identity_busy = False
        self._path_identity_controls = []
        self.current_folder_path = None
        self._closed = False
        self._browse_ready = False
        self._overview_ready = False
        self._scan_request = None
        self._folder_write = None
        self._auto_scan_after_write = False
        self._auto_scan_pending = True
        self._auto_scan_timer = QTimer(self)
        self._auto_scan_timer.setSingleShot(True)
        self._auto_scan_timer.timeout.connect(lambda: self.toggle_scan(auto=True))

    def on_load(self, context=None):
        super().on_load(context)
        self.db = DatabaseManager(service=(context or {}).get('data_service'))
        self.overview_reader = LibraryReader(self.db.service, self)
        self.browse_reader = LibraryReader(self.db.service, self)
        self.scan_reader = LibraryReader(self.db.service, self)
        self.export_reader = LibraryReader(CurationExportTasks(self.db.db_path), self)
        self.export_reader.result.connect(self._on_export_finished)
        self.overview_reader.result.connect(self._on_overview)
        self.browse_reader.result.connect(self._on_folder_count)
        self.scan_reader.result.connect(self._on_scan_folders)
        self.folder_writer = LibraryWriter(self.db.service, self)
        self.folder_writer.result.connect(self._on_folder_written)
        self.folder_archive = FolderArchiveController(self)

    def get_view(self) -> QWidget:
        """Inicializa y retorna la vista principal del bibliotecario."""
        if self.view: return self.view

        self.sidebar = self._create_sidebar()
        self.content = self._create_content()
        self.bottom = self._create_bottom_bar()

        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            self.content,
            self.sidebar,
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus'),
            tool_header=(self._icon, self.tr("tool.librarian.name", "Librarian"),
                         self.tr("tool.librarian.desc", self._description)),
        )
        self.view.destroyed.connect(self.folder_archive.close)

        initial_revision = self.db.service.revision_snapshot()
        self.refresh_ui()
        self.revisions = LibraryRevisionWatcher(self.db.service, self.view, busy=self._revision_busy, initial=initial_revision)
        self.revisions.changed.connect(self._on_native_revision)

        # Escuchar cambios externos en disco (ej. Pickler movió archivos)
        eb = self.context.get('event_bus')
        if eb:
            eb.subscribe("library_changed", self._on_library_changed)
        self._cherry_ingestor = self.context.get('cherry_ingestor')
        if self._cherry_ingestor is not None:
            self._cherry_ingestor.batch_deferred.connect(self._on_cherry_batch_deferred, Qt.QueuedConnection)

        return self.view

    def _create_sidebar(self) -> QWidget:
        """Barra lateral: carpetas vigiladas (origen de todo lo demás)."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 10, 15, 15)
        layout.setSpacing(10)

        layout.addWidget(section(self.tr("lib.sec.sources", "Watched folders")))
        self.folder_list = QListWidget()
        self.folder_list.setToolTip(self.tr(
            "lib.folder_list_tip",
            "Folders the indexer watches. Select one to see its count and to export or "
            "review its curation."))
        self.folder_list.itemClicked.connect(self.on_folder_selected)
        layout.addWidget(self.folder_list, 1)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton(self.tr("lib.add", "Add folder…"))
        self.btn_add.setToolTip(self.tr(
            "lib.add_tip",
            "Registers a folder to watch. It copies nothing: it only notes where to look "
            "when indexing."))
        self.btn_add.clicked.connect(self.add_folder)
        self.btn_remove = QPushButton(self.tr("lib.remove", "Archive folder"))
        self.btn_remove.setToolTip(self.tr(
            "lib.remove_tip",
            "Previews indexed copies, then asks to stop watching and keep them offline. "
            "Files and curation are retained."))
        self.btn_remove.clicked.connect(self.remove_folder)
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        layout.addLayout(btn_layout)
        self.lbl_folder_write = hint("")
        self.lbl_folder_write.setTextFormat(Qt.PlainText)
        layout.addWidget(self.lbl_folder_write)
        return container

    def _group(self, title, text):
        box = QGroupBox(title)
        lay = QVBoxLayout(box)
        lay.setSpacing(8)
        if text:
            lay.addWidget(hint(text))
        return box, lay

    def _create_content(self) -> QWidget:
        """Lienzo: estado de la biblioteca y tareas de administración agrupadas."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)

        self.lbl_stats = QLabel(self.tr("common.status.loading", "Loading..."))
        self.lbl_stats.setStyleSheet("font-size: 16px; font-weight: bold; background: transparent;")
        self.lbl_stats.setWordWrap(True)
        layout.addWidget(self.lbl_stats)
        self.lbl_folder_stats = hint(self.tr("lib.folder.none", "Select a watched folder on the left."))
        layout.addWidget(self.lbl_folder_stats)

        # Curación de la carpeta seleccionada
        box, lay = self._group(self.tr("lib.group.curation", "Curation of the selected folder"), self.tr(
            "lib.group.curation_hint",
            "Exports or reviews the tags and ratings of every indexed copy in the selected folder "
            "(subfolders included). Images are never modified."))
        row = QHBoxLayout()
        self.btn_export_curation = QPushButton(self.tr('lib.export', 'Export folder curation'))
        self.btn_export_curation.setToolTip(self.tr('lib.export_tip',
            'Exports every indexed copy in the selected folder, including subfolders, '
            'to panopticon.curation.db. Replaces its previous export; preserves images and Cherry catalogs.'))
        self.btn_export_curation.setEnabled(False)
        self.btn_export_curation.clicked.connect(self.export_folder_curation)
        self.btn_review_curation = QPushButton(self.tr('lib.review', 'Review folder curation'))
        self.btn_review_curation.setToolTip(self.tr('lib.review_tip',
            'Compare the selected folder\'s exported curation with the central database '
            'in pages; conflicts retain both values. No changes are applied.'))
        self.btn_review_curation.setEnabled(False)
        self.btn_review_curation.clicked.connect(self.review_folder_curation)
        row.addWidget(self.btn_export_curation)
        row.addWidget(self.btn_review_curation)
        row.addStretch()
        lay.addLayout(row)
        self.lbl_export = hint("")
        self.lbl_export.setTextFormat(Qt.PlainText)
        lay.addWidget(self.lbl_export)
        layout.addWidget(box)

        # Identidades y versiones
        box, lay = self._group(self.tr("lib.group.identity", "Identities and copy versions"), self.tr(
            "lib.group.identity_hint",
            "Resolve naming conflicts between copies and manage offline or replaced versions."))
        row = QHBoxLayout()
        self.btn_path_identity = QPushButton(self.tr('lib.identity.open', 'Review path identities…'))
        self.btn_path_identity.setToolTip(self.tr('lib.identity.open_tip',
            'Prepare Windows naming evidence and review conflicting copy IDs. Preserves names, files, curation and portable folders.'))
        self.btn_path_identity.clicked.connect(self.review_path_identities)
        self.btn_copy_versions = QPushButton(self.tr('lib.versions.open', 'Copy versions and offline catalog…'))
        self.btn_copy_versions.setToolTip(self.tr('lib.versions.open_tip',
            'Review stored copies by ID, preserve an active copy offline or verify a file before making it available.'))
        self.btn_copy_versions.clicked.connect(self.review_copy_versions)
        row.addWidget(self.btn_path_identity)
        row.addWidget(self.btn_copy_versions)
        row.addStretch()
        lay.addLayout(row)
        layout.addWidget(box)

        layout.addWidget(hint(self.tr(
            "lib.explore_in_gallery",
            "To search by tags, rating or text and send images to other tools, use Gallery.")))
        layout.addStretch()
        return container

    def _create_bottom_bar(self) -> QWidget:
        """Barra inferior: estado del indexador; acción principal a la derecha."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.lbl_status = hint(self.tr("lib.status.ready", "Ready"))
        layout.addWidget(self.lbl_status, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.btn_backfill = QPushButton(self.tr("lib.backfill", "Compute hashes"))
        self.btn_backfill.clicked.connect(self.toggle_backfill)
        self.btn_backfill.setToolTip(self.tr(
            "lib.backfill_tip",
            "Compute the SHA-256 content identity of files that lack it "
            "(cherry-dl folders get theirs from catalog.db). Resumable."
        ))
        self.btn_sync = QPushButton(self.tr("lib.purge", "Sweep ghost entries"))
        self.btn_sync.setToolTip(self.tr(
            "lib.purge_tip",
            "Goes through the index and drops entries whose file is gone from disk. "
            "Useful after deleting or moving things outside the app. It deletes no "
            "files."))
        self.btn_sync.clicked.connect(lambda: self.toggle_scan(sync_only=True))
        self.btn_scan = primary(QPushButton(self.tr("lib.run_indexer", "Run indexer")))
        self.btn_scan.setToolTip(self.tr(
            "lib.run_indexer_tip",
            "Walks the watched folders and adds whatever is new to the library. On a "
            "large library it takes a while; the same button stops it."))
        self.btn_scan.clicked.connect(self.toggle_scan)
        for button in (self.btn_backfill, self.btn_sync, self.btn_scan):
            button.setCursor(Qt.PointingHandCursor)
            layout.addWidget(button)
        return container

    def _can_export_curation(self):
        return (not self._closed and self.view is not None and self._browse_ready and
                bool(self.current_folder_path) and
                self._exporting is None and self._folder_write is None)

    def _update_export_controls(self):
        if hasattr(self, 'btn_export_curation'):
            self.btn_export_curation.setEnabled(self._can_export_curation())
        if hasattr(self, 'btn_review_curation'):
            self.btn_review_curation.setEnabled(self._can_export_curation())

    def review_folder_curation(self):
        if not self.btn_review_curation.isEnabled():
            return
        if self._review_dialog is None:
            self._review_dialog = CurationReviewDialog(self.db.db_path, self.current_folder_path,
                self.tr, self.context.get('theme_manager'), self.view, service=self.db.service)
            self._review_dialog.finished.connect(lambda _: self._review_closed())
            self._review_dialog.curation_restored.connect(self._on_curation_restored)
        self._review_dialog.show()
        self._review_dialog.raise_()
        self._review_dialog.activateWindow()

    def _on_curation_restored(self, path):
        self.update_global_stats()
        self.refresh_current_view()

    def _review_closed(self):
        self._review_dialog = None

    def review_path_identities(self):
        if self._closed:
            return
        if self._identity_dialog is None:
            self._identity_dialog = PathIdentityReviewDialog(self.db.service, self.tr,
                self.context.get('theme_manager'), self.view, can_prepare=self._can_write_folder)
            self._identity_dialog.finished.connect(lambda _: self._identity_closed())
            self._identity_dialog.busy_changed.connect(self._identity_busy_changed)
            self._identity_dialog.review_copies_requested.connect(self.review_copy_versions)
        self._identity_dialog.show()
        self._identity_dialog.raise_()
        self._identity_dialog.activateWindow()

    def _identity_busy_changed(self, busy):
        self._path_identity_busy = busy
        if self._closed:
            return
        if busy:
            self._path_identity_controls = []
            for name in ('btn_add', 'btn_remove', 'btn_scan', 'btn_sync', 'btn_backfill'):
                button = getattr(self, name, None)
                if button is not None:
                    self._path_identity_controls.append((button, button.isEnabled()))
                    button.setEnabled(False)
        else:
            for button, enabled in self._path_identity_controls:
                if isValid(button):
                    button.setEnabled(enabled)
            self._path_identity_controls = []
            if self._auto_scan_pending:
                self._auto_scan_timer.start(500)

    def _identity_closed(self):
        self._identity_dialog = None
        self._identity_busy_changed(False)

    def review_copy_versions(self):
        if self._closed:
            return
        if self._versions_dialog is None:
            self._versions_dialog = CopyVersionsDialog(self.db.service, self.tr,
                self.context.get('theme_manager'), self.view)
            self._versions_dialog.finished.connect(lambda _: self._versions_closed())
            self._versions_dialog.copies_changed.connect(lambda: self._on_curation_restored(''))
        self._versions_dialog.show()
        self._versions_dialog.raise_()
        self._versions_dialog.activateWindow()

    def _versions_closed(self):
        self._versions_dialog = None

    def recovery_entries(self):
        return [("curation_export",
                 self.tr('lib.recovery.open', 'Recover interrupted export…').rstrip('…'),
                 self.tr('lib.recovery.open_tip',
                    'Choose any folder, including one not indexed. Inspect interrupted export resources, '
                    'then explicitly quarantine verified resources without touching its images or catalogs.'),
                 self.recover_folder_curation)]

    def recover_folder_curation(self):
        if self._closed:
            return
        if self._recovery_dialog is None:
            folder = QFileDialog.getExistingDirectory(self.view,
                self.tr('lib.recovery.choose', 'Choose folder to inspect'), self.current_folder_path or '')
            if not folder:
                return
            self._recovery_dialog = CurationRecoveryDialog(folder, self.tr,
                self.context.get('theme_manager'), self.view)
            self._recovery_dialog.finished.connect(lambda _: self._recovery_closed())
        self._recovery_dialog.show()
        self._recovery_dialog.raise_()
        self._recovery_dialog.activateWindow()

    def _recovery_closed(self):
        self._recovery_dialog = None

    def export_folder_curation(self):
        if not self._can_export_curation():
            return
        folder = str(self.current_folder_path)
        self._exporting = folder
        self._update_export_controls()
        self.lbl_export.setText(self.tr('lib.exporting', 'Exporting curation: {folder}').format(folder=folder))
        self.export_reader.request('export_curation', folder=folder)

    def _on_export_finished(self, ticket, value, error):
        if self._closed or self._exporting is None:
            return
        folder, self._exporting = self._exporting, None
        self._update_export_controls()
        if error is not None:
            reason = str(error)
            if reason in ('sidecar_export_busy', 'invalid_export_guard'):
                reason = self.tr('lib.export_reason.' + reason,
                    'Another export is active; try again later.' if reason == 'sidecar_export_busy' else
                    'The export guard requires manual review; it was preserved.')
            self.lbl_export.setText(self.tr('lib.export_error', 'Could not export {folder}: {reason}')
                                    .format(folder=folder, reason=reason))
        else:
            self.lbl_export.setText(self.tr('lib.exported',
                'Exported {count} copies to {path}. Missing hash: {missing}.').format(
                    count=value['copies'], path=value['path'], missing=value['missing_hash']))

    def on_folder_selected(self, item):
        self.load_folder_paths(item.text())

    def load_folder_paths(self, folder_path):
        self.current_folder_path = folder_path
        self._request_folder_count()

    def _revision_busy(self):
        return (self._path_identity_busy or self.overview_reader._active or self.browse_reader._active or
                self._exporting is not None or self._folder_write is not None)

    def _on_native_revision(self, revision):
        if self._closed or not self.view:
            return
        self.overview_reader.request('get_library_overview', include_tags=False)
        self._request_folder_count()

    def update_global_stats(self):
        if not self._closed and self.view:
            self.overview_reader.request('get_library_overview', include_tags=False)

    def refresh_ui(self):
        if self._closed or not self.view:
            return
        self.update_global_stats()
        self.refresh_current_view()

    def refresh_current_view(self):
        self._request_folder_count()

    def _request_folder_count(self):
        """Solo el recuento de la carpeta seleccionada (una fila); explorar es de Gallery."""
        if self._closed or not self.view:
            return
        self._browse_ready = False
        self._folder_count = None
        folder = self.current_folder_path
        if not folder:
            self.browse_reader.invalidate()
            self._browse_ready = True
            self.lbl_folder_stats.setText(self.tr("lib.folder.none", "Select a watched folder on the left."))
            self._update_export_controls()
            return
        self.lbl_folder_stats.setText(self.tr('lib.db.loading', 'Loading library...'))
        self._update_export_controls()
        self.browse_reader.request('get_library_page', folder=folder, limit=1, offset=0)

    def _read_error(self, label, error):
        log.warning('Librarian read: %s', error)
        label.setText(self.tr('lib.db.read_error', 'Could not read library: {reason}')
                      .format(reason=getattr(error, 'reason', str(error))))

    def _on_folder_count(self, ticket, value, error):
        if self._closed:
            return
        if error is not None:
            self._read_error(self.lbl_folder_stats, error)
            return
        self._browse_ready = True
        self._folder_count = value['count']
        self.lbl_folder_stats.setText(self.tr('lib.stats.folder', '📂 {name}: {count} indexed images').format(
            name=os.path.basename(self.current_folder_path or ''), count=value['count']))
        self._update_export_controls()

    def _on_overview(self, ticket, value, error):
        if self._closed:
            return
        if error is not None:
            self._overview_ready = False
            self._read_error(self.lbl_stats, error)
            return
        self._overview_ready = True
        self.folder_list.clear()
        self.folder_list.addItems(value['folders'])
        selected = self.folder_list.findItems(self.current_folder_path or '', Qt.MatchExactly)
        if selected:
            self.folder_list.setCurrentItem(selected[0])
        elif self.current_folder_path:
            self.current_folder_path = None
            self.refresh_current_view()
        self.lbl_stats.setText(self.tr('lib.stats.global', '{count} indexed files in {total} watched folders')
                               .format(count=value['count'], total=len(value['folders'])))
        if self._auto_scan_pending:
            self._auto_scan_pending = False
            if value['folders']:
                if self._folder_write is not None:
                    self._auto_scan_after_write = True
                else:
                    self._auto_scan_timer.start(500)

    def on_unload(self):
        self._closed = True
        if hasattr(self, 'revisions'):
            self.revisions.close()
        if hasattr(self, 'folder_archive'):
            self.folder_archive.close()
        for worker in (self.indexer_thread, self.backfill_thread, self._reindex_thread):
            if worker and worker.isRunning():
                worker.stop()
        if hasattr(self, 'folder_writer'):
            self.folder_writer.close()
        self._exporting = None
        if self._review_dialog is not None:
            self._review_dialog.close()
        if self._recovery_dialog is not None:
            self._recovery_dialog.close()
        if self._versions_dialog is not None:
            self._versions_dialog.close()
        if self._identity_dialog is not None:
            self._identity_dialog.close()
        for name in ('overview_reader', 'browse_reader', 'scan_reader', 'export_reader'):
            reader = getattr(self, name, None)
            if reader:
                reader.close()
        self._dirty_timer.stop()
        self._auto_scan_timer.stop()
        eb = (self.context or {}).get('event_bus')
        if eb:
            eb.unsubscribe('library_changed', self._on_library_changed)
        if self._cherry_ingestor is not None:
            self._cherry_ingestor.batch_deferred.disconnect(self._on_cherry_batch_deferred)
            self._cherry_ingestor = None

    def shutdown_ready(self):
        readers = self.findChildren(LibraryReader)
        if self.view is not None and isValid(self.view):
            readers += self.view.findChildren(LibraryReader)
        return owner_threads_ready(self) and ((not hasattr(self, 'folder_archive') or self.folder_archive.shutdown_ready())
                and all(reader.shutdown_ready() for reader in readers)
                and all(worker is None or native_done(worker) for worker in
                        (self.indexer_thread, self.backfill_thread, self._reindex_thread)))

    def _can_write_folder(self):
        if self._closed or self._folder_write is not None or self._path_identity_busy:
            return False
        # Un inventario en curso podría volver a registrar lo que se acaba de quitar.
        busy = self._scan_request is not None or any(
            worker and worker.isRunning() for worker in
            (self.indexer_thread, self.backfill_thread, self._reindex_thread))
        if busy:
            self.lbl_folder_write.setText(self.tr(
                'lib.folder.busy', 'Wait for indexing or hashing to finish before changing watched folders.'))
            return False
        return True

    def add_folder(self):
        if not self._can_write_folder():
            return
        folder = QFileDialog.getExistingDirectory(self.view, self.tr("common.select_folder", "Select Folder to Index"))
        if folder:
            self._submit_folder_write('add_watched_folder', folder)

    def remove_folder(self):
        if not self._can_write_folder():
            return
        selected = self.folder_list.currentItem()
        if not selected:
            return
        self.folder_archive.start(selected.text())

    def _submit_folder_write(self, operation, path):
        # Los diálogos ejecutan un event loop anidado: revalidar al salir.
        if not self._can_write_folder():
            return
        self._folder_write = (None, operation, path)
        self.btn_add.setEnabled(False)
        self.btn_remove.setEnabled(False)
        self._auto_scan_after_write = self._auto_scan_timer.isActive()
        self._auto_scan_timer.stop()
        self.lbl_folder_write.setText(self.tr(
            'lib.folder.saving', "Saving watched folder: {path}").format(path=path))
        try:
            ticket = self.folder_writer.submit(operation, path)
            self._folder_write = (ticket, operation, path)
            self._update_export_controls()
        except Exception as error:
            self._on_folder_written(None, None, error)

    def _on_folder_written(self, ticket, value, error):
        if self._closed or self._folder_write is None or ticket != self._folder_write[0]:
            return
        _, operation, path = self._folder_write
        self._folder_write = None
        if self.view is None or not isValid(self.view):
            return
        self._update_export_controls()
        self.btn_add.setEnabled(True)
        self.btn_remove.setEnabled(True)
        if error == 'folder_archive_cancelled':
            self.lbl_folder_write.setText(self.tr('lib.folder.archive_cancelled',
                'Folder archival cancelled. No records changed.'))
        elif error is not None or value is not True:
            reason = str(error) if error is not None else 'operation_failed'
            log.warning('Watched folder write failed: %s %s: %s', operation, path, reason)
            self.lbl_folder_write.setText(self.tr(
                'lib.folder.write_error', "Could not save watched folder '{path}': {reason}")
                .format(path=path, reason=reason))
        else:
            if operation == 'remove_watched_folder' and self.current_folder_path == path:
                self.current_folder_path = None
            self.lbl_folder_write.setText(self.tr(
                'lib.folder.saved', "Watched folder change saved: {path}").format(path=path))
            self.refresh_ui()
        if self._auto_scan_after_write:
            self._auto_scan_after_write = False
            self._auto_scan_timer.start(500)

    def toggle_scan(self, sync_only=False, auto=False):
        if self._path_identity_busy:
            return
        if self._closed:
            return
        if self.indexer_thread and self.indexer_thread.isRunning():
            # Stop logic
            self.indexer_thread.stop()
            self.btn_scan.setText(self.tr("lib.status.stopping", "Stopping..."))
            if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
            self.btn_scan.setEnabled(False)
        else:
            # Start logic
            if self._folder_write is not None:
                return
            if self.backfill_thread and self.backfill_thread.isRunning():
                return  # excluyente con el backfill de hashes
            if self._scan_request is not None:
                return
            self._scan_request = (sync_only, auto)
            self.scan_reader.request('get_watched_folders')

    def _on_scan_folders(self, ticket, folders, error):
        if self._closed or self._scan_request is None:
            return
        sync_only, auto = self._scan_request
        self._scan_request = None
        if error is not None:
            self._read_error(self.lbl_status, error)
            return
        if (self._path_identity_busy or self._folder_write is not None or
                (self.indexer_thread and self.indexer_thread.isRunning()) or
                (self.backfill_thread and self.backfill_thread.isRunning())):
            return
        if not folders:
            if not auto:
                QMessageBox.warning(self.view, self.tr('common.error', 'No Folders'),
                                    self.tr('lib.msg.no_folders', 'Please add a folder to index.'))
            return
        self._start_scan(folders, sync_only, auto)

    def _start_scan(self, folders, sync_only, auto):
        # Automatic scans expose the same cancellation controls as manual scans.
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else Theme._c
        self.btn_scan.setText(self.tr("lib.stop_indexer", "🛑 Stop Scanning"))
        self.btn_scan.setStyleSheet(f"background-color: {c('accent_warning')}; color: {c('text_primary')}; border-radius: 4px; padding: 6px; font-weight: bold;")
        if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
        if hasattr(self, 'btn_backfill'): self.btn_backfill.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.lbl_status.setText(self.tr("lib.indexing", "🔍 Running Background Sync..."))

        # Una auditoría profunda reconcilia sólo inventarios completos.
        self.indexer_thread = IndexerWorker(self.db, folders, deep_clean=sync_only, translate=self.tr)
        retain_worker(self.indexer_thread)
        self.indexer_thread.progress_signal.connect(self.update_progress_text)
        self.indexer_thread.count_signal.connect(self.update_progress_bar)
        self.indexer_thread.finished_signal.connect(self.scan_finished)
        start_worker(self.indexer_thread, self)

    def toggle_backfill(self):
        if self._path_identity_busy:
            return
        """Backfill de hash_original (M3). Excluyente con el indexer: ambos
        escriben en files y compartir la barra de progreso sería ambiguo."""
        if self._closed or self._folder_write is not None:
            return
        if self.backfill_thread and self.backfill_thread.isRunning():
            self.backfill_thread.stop()
            self.btn_backfill.setText(self.tr("lib.status.stopping", "Stopping..."))
            self.btn_backfill.setEnabled(False)
        else:
            if self.indexer_thread and self.indexer_thread.isRunning():
                return
            tm = self.context.get('theme_manager')
            c = tm.get_color if tm else Theme._c
            self.btn_backfill.setText(self.tr("lib.stop_backfill", "🛑 Stop Hashing"))
            self.btn_backfill.setStyleSheet(f"background-color: {c('accent_warning')}; color: {c('text_primary')}; border-radius: 4px; padding: 6px 12px; font-weight: bold;")
            self.btn_scan.setEnabled(False)
            if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)

            self.backfill_thread = HashBackfillWorker(self.db, translate=self.tr)
            retain_worker(self.backfill_thread)
            self.backfill_thread.progress_signal.connect(self.update_progress_text)
            self.backfill_thread.count_signal.connect(self.update_progress_bar)
            self.backfill_thread.finished_signal.connect(self.backfill_finished)
            start_worker(self.backfill_thread, self)

    def backfill_finished(self, hashed, failed):
        if self._closed:
            return
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else Theme._c
        self.btn_backfill.setText(self.tr("lib.backfill", "🧬 Compute Hashes"))
        self.btn_backfill.setStyleSheet(f"background-color: transparent; color: {c('text_secondary')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px 12px;")
        self.btn_backfill.setEnabled(True)
        self.btn_scan.setEnabled(True)
        if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(True)
        self.progress_bar.setVisible(False)
        if self.backfill_thread.outcome == InventoryStatus.COMPLETE:
            QMessageBox.information(
                self.view, self.tr("common.success", "Done"),
                self.tr("lib.msg.backfill_done", "Hashing complete: {hashed} hashed, {failed} skipped.")
                    .format(hashed=hashed, failed=failed)
            )
        else:
            QMessageBox.warning(self.view, self.tr("lib.scan.warning", "Incomplete scan"),
                self.tr("lib.hash.incomplete", "Hashing incomplete: {hashed} confirmed, {failed} skipped.")
                    .format(hashed=hashed, failed=failed))

    def update_progress_text(self, text):
        if self._closed:
            return
        self.lbl_status.setText(text)

    def update_progress_bar(self, current, total):
        if self._closed:
            return
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def scan_finished(self):
        if self._closed:
            return
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else Theme._c
        self.btn_scan.setText(self.tr("lib.run_indexer", "🚀 Run Indexer"))
        self.btn_scan.setStyleSheet(f"background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; border-radius: 4px; padding: 6px;")
        if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(True)
        if hasattr(self, 'btn_backfill'): self.btn_backfill.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.refresh_ui()
        outcome = self.indexer_thread.outcome
        archived = self._archived_summary(self.indexer_thread)
        if outcome == InventoryStatus.COMPLETE:
            QMessageBox.information(self.view, self.tr("common.success", "Done"), self.tr("lib.msg.done", "Library Scan Complete!")+'\n\n'+archived)
        else:
            key = "lib.scan.cancelled" if outcome == InventoryStatus.CANCELLED else "lib.scan.incomplete"
            QMessageBox.warning(self.view, self.tr("lib.scan.warning", "Incomplete scan"), self.tr(key, "Indexing incomplete; unverified batches were not archived. Confirmed batches remain.")+'\n\n'+archived)

    def _archived_summary(self, worker):
        return self.tr('lib.scan.archived_count', 'Identities confirmed offline: {count}. Curation and history are retained.').format(count=getattr(worker, 'archived_count', 0))

    # ── Re-index dirigido tras cambios externos en disco ──────────────
    def _on_library_changed(self, folder):
        """
        Suscrito a EventBus 'library_changed' (ej. Pickler movió/copió archivos).
        Coalesce las carpetas afectadas y dispara un re-index dirigido y silencioso
        tras un breve debounce, en vez de reescanear toda la librería.
        """
        if self._closed or not folder:
            return
        self._dirty_folders.add(str(folder))
        self._dirty_timer.start()  # reinicia el debounce

    def _flush_dirty_reindex(self):
        """Re-indexa (silenciosamente) solo las carpetas marcadas como sucias."""
        if self._closed:
            return
        if self._cherry_ingestor is not None and self._cherry_ingestor.batch_active:
            return
        # No interferir con un escaneo manual ni con otro re-index en curso.
        busy = self._path_identity_busy or self._folder_write is not None or \
               (self.indexer_thread and self.indexer_thread.isRunning()) or \
               (self._reindex_thread and self._reindex_thread.isRunning())
        if busy:
            self._dirty_timer.start()  # reintentar más tarde
            return
        if not self._dirty_folders:
            return
        folders = [f for f in self._dirty_folders if os.path.isdir(f)]
        self._dirty_folders.clear()
        if not folders:
            return
        # deep_clean=True para reconciliar huérfanos (originales movidos) en esas carpetas.
        self._reindex_thread = IndexerWorker(self.db, folders, deep_clean=True, translate=self.tr)
        retain_worker(self._reindex_thread)
        self._reindex_thread.finished_signal.connect(self._quiet_reindex_finished)
        start_worker(self._reindex_thread, self)

    def _on_cherry_batch_deferred(self, deferred):
        if not deferred and not self._closed and self._dirty_folders:
            self._dirty_timer.start()

    def _quiet_reindex_finished(self):
        """Refresca la vista tras el re-index dirigido, sin diálogos."""
        if self._closed:
            return
        try:
            if self._reindex_thread.outcome != InventoryStatus.COMPLETE:
                self.lbl_status.setText(self.tr("lib.scan.incomplete", "Indexing incomplete; unverified batches were not archived. Confirmed batches remain.")+' '+self._archived_summary(self._reindex_thread))
            else:
                self.lbl_status.setText(self._archived_summary(self._reindex_thread))
            self.refresh_ui()
        except Exception as e:
            log.warning(f"Refresh tras re-index dirigido falló: {e}")

    def run_headless(self, paths: list = None):
        """Sin vista de exploración: los conjuntos de imágenes se abren en Gallery."""
        return None
