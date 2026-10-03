from core.native_threads import start_worker, native_done, owner_threads_ready
from shiboken6 import isValid
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QListWidget, QFileDialog, QMessageBox, QProgressBar, QFrame,
                               QLineEdit, QDialog, QCompleter, QScrollArea, QGridLayout, QComboBox)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, QSize, Signal, QTimer, QStringListModel
from core.base_module import BaseModule
from core.components.thumbnail import ClickableThumbnail
from core.thumbnail_loader import ThumbnailLoader
from core.components.worker_preview import WorkerPreviewDialog
from core.components.tag_widgets import FlowLayout, TagChip
from core.database_manager import DatabaseManager
from core.library_revision_async import LibraryRevisionWatcher
from core.library_async import LibraryReader, LibraryWriter
from core.components.tag_vocabulary import TagVocabulary, TagPageBar
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
    El Bibliotecario (Librarian).
    Módulo core encargado de la gestión de la base de datos de imágenes,
    escaneo de carpetas, etiquetado y búsqueda avanzada.
    Sirve como fuente de datos para casi todos los demás módulos.
    """
    # Señales para comunicación con otros módulos vía EventBus
    request_open_gallery = Signal(list, str)   # (lista_rutas, contexto_titulo)
    request_open_optimizer = Signal(list)     # (lista_rutas)
    request_open_cropper = Signal(list)       # (lista_rutas)
    request_open_pickler = Signal(list)       # (lista_rutas)

    def __init__(self):
        super().__init__()
        self._name = "Librarian"
        self._description = "Indexador central y gestor de etiquetas de la librería."
        self._icon = "📚"
        
        self.view = None
        self._thumb_loader = None
        self._thumb_widgets = {}
        self._image_previews = []
        # Gestor de base de datos SQLite persistente
        self.db = None
        self.indexer_thread = None
        self.backfill_thread = None

        # Re-index dirigido tras cambios externos (ej. Pickler mueve archivos).
        # Carpetas marcadas como "sucias" + temporizador para coalescer eventos.
        self._dirty_folders: set[str] = set()
        self._reindex_thread = None
        self._dirty_timer = QTimer()
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(1500)
        self._dirty_timer.timeout.connect(self._flush_dirty_reindex)
        
        # Estado de Paginación del Explorador de Tags
        self.current_page = 0
        self.page_size = 100
        self.total_paths = []  # Sólo la página confirmada.
        self._selection_count = 0
        self._external_paths = None
        self._handoff = None
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
        self.handoff_reader = LibraryReader(self.db.service, self)
        self.handoff_reader.result.connect(self._on_handoff)
        self.export_reader = LibraryReader(CurationExportTasks(self.db.db_path), self)
        self.export_reader.result.connect(self._on_export_finished)
        self.overview_reader.result.connect(self._on_overview)
        self.browse_reader.result.connect(self._on_selection)
        self.scan_reader.result.connect(self._on_scan_folders)
        self.folder_writer = LibraryWriter(self.db.service, self)
        self.folder_writer.result.connect(self._on_folder_written)
        self.folder_archive = FolderArchiveController(self)

    def get_view(self) -> QWidget:
        """Inicializa y retorna la vista principal del bibliotecario."""
        if self.view: return self.view
        
        # 1. Crear sub-componentes UI
        self.sidebar = self._create_sidebar()
        self.content = self._create_content()
        self.bottom = self._create_bottom_bar()
        
        # 2. Ensamblar usando el Layout Estándar de 3 paneles
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            self.content,
            self.sidebar, 
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        self.view.destroyed.connect(self.folder_archive.close)
        
        initial_revision = self.db.service.revision_snapshot()
        self.setup_completer()
        self.refresh_ui()
        self.revisions = LibraryRevisionWatcher(self.db.service, self.view, busy=self._revision_busy, initial=initial_revision)
        self.revisions.changed.connect(self._on_native_revision)

        # Escuchar cambios externos en disco (ej. Pickler movió archivos)
        eb = self.context.get('event_bus')
        if eb:
            eb.subscribe("library_changed", self._on_library_changed)

        return self.view

    def _create_sidebar(self) -> QWidget:
        """Sidebar: Watched Folders Management"""
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else Theme._c

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Header
        lbl = QLabel(self.tr("lib.title", "📚 Library Sources"))
        lbl.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {c('text_primary')};")
        layout.addWidget(lbl)

        # List
        self.folder_list = QListWidget()
        self.folder_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {c('bg_panel')};
                border: 1px solid {c('border')};
                border-radius: 8px;
                color: {c('text_secondary')};
                font-size: 13px;
                outline: none;
            }}
            QListWidget::item {{ padding: 8px; }}
            QListWidget::item:selected {{
                background-color: {c('border')};
                color: {c('accent_main')};
                border: 1px solid {c('accent_main')};
            }}
        """)
        self.folder_list.itemClicked.connect(self.on_folder_selected)
        layout.addWidget(self.folder_list)

        # Buttons
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton(self.tr("lib.add", "➕ Add"))
        self.btn_add.setToolTip(self.tr(
            "lib.add_tip",
            "Registers a folder to watch. It copies nothing: it only notes where to look "
            "when indexing."))
        self.btn_add.clicked.connect(self.add_folder)
        self.btn_add.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('accent_main')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px;")

        self.btn_remove = QPushButton(self.tr("lib.remove", "Archive folder"))
        self.btn_remove.setToolTip(self.tr(
            "lib.remove_tip",
            "Previews indexed copies, then asks to stop watching and keep them offline. "
            "Files and curation are retained."))
        self.btn_remove.clicked.connect(self.remove_folder)
        self.btn_remove.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('accent_warning')}; border: 1px solid {c('border_strong')}; border-radius: 4px; padding: 6px;")

        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        layout.addLayout(btn_layout)
        self.lbl_folder_write = QLabel()
        self.lbl_folder_write.setWordWrap(True)
        self.lbl_folder_write.setTextFormat(Qt.PlainText)
        self.lbl_folder_write.setStyleSheet(f"color: {c('text_secondary')};")
        layout.addWidget(self.lbl_folder_write)

        self.btn_export_curation = QPushButton(self.tr('lib.export', 'Export folder curation'))
        self.btn_export_curation.setToolTip(self.tr('lib.export_tip',
            'Select a folder and clear tag filters. Exports every indexed copy in that folder, including subfolders, '
            'to panopticon.curation.db. Replaces its previous export; preserves images and Cherry catalogs.'))
        self.btn_export_curation.setEnabled(False)
        self.btn_export_curation.setStyleSheet(
            f"background-color: {c('bg_input')}; color: {c('accent_info')}; border: 1px solid {c('border_strong')}; padding: 6px;")
        self.btn_export_curation.clicked.connect(self.export_folder_curation)
        layout.addWidget(self.btn_export_curation)
        self.lbl_export = QLabel()
        self.lbl_export.setWordWrap(True)
        self.lbl_export.setTextFormat(Qt.PlainText)
        self.lbl_export.setStyleSheet(f"color: {c('text_secondary')};")
        layout.addWidget(self.lbl_export)
        self.btn_review_curation = QPushButton(self.tr('lib.review', 'Review folder curation'))
        self.btn_review_curation.setToolTip(self.tr('lib.review_tip',
            'Select a folder and clear tag filters. Compare its exported curation with the central database '
            'in pages; conflicts retain both values. No changes are applied.'))
        self.btn_review_curation.setEnabled(False)
        self.btn_review_curation.setStyleSheet(
            f"background-color: {c('bg_input')}; color: {c('accent_info')}; border: 1px solid {c('border_strong')}; padding: 6px;")
        self.btn_review_curation.clicked.connect(self.review_folder_curation)
        layout.addWidget(self.btn_review_curation)
        self.btn_recover_curation = QPushButton(self.tr('lib.recovery.open', 'Recover interrupted export…'))
        self.btn_recover_curation.setToolTip(self.tr('lib.recovery.open_tip',
            'Choose any folder, including one not indexed. Inspect interrupted export resources, '
            'then explicitly quarantine verified resources without touching its images or catalogs.'))
        self.btn_recover_curation.setStyleSheet(
            f"background-color: {c('bg_input')}; color: {c('text_primary')}; border: 1px solid {c('border_strong')}; padding: 6px;")
        self.btn_recover_curation.clicked.connect(self.recover_folder_curation)
        layout.addWidget(self.btn_recover_curation)
        self.btn_path_identity = QPushButton(self.tr('lib.identity.open', 'Review path identities…'))
        self.btn_path_identity.setToolTip(self.tr('lib.identity.open_tip',
            'Prepare Windows naming evidence and review conflicting copy IDs. Preserves names, files, curation and portable folders.'))
        self.btn_path_identity.clicked.connect(self.review_path_identities)
        layout.addWidget(self.btn_path_identity)
        self.btn_copy_versions = QPushButton(self.tr('lib.versions.open', 'Copy versions and offline catalog…'))
        self.btn_copy_versions.setToolTip(self.tr('lib.versions.open_tip',
            'Review stored copies by ID, preserve an active copy offline or verify a file before making it available.'))
        self.btn_copy_versions.setStyleSheet(self.btn_recover_curation.styleSheet())
        self.btn_copy_versions.clicked.connect(self.review_copy_versions)
        layout.addWidget(self.btn_copy_versions)

        # --- Section 2: Tag Selection ---
        layout.addSpacing(20)
        lbl_tags = QLabel(self.tr("lib.tags_title", "🏷️ Available Tags"))
        lbl_tags.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c('text_dim')};")
        layout.addWidget(lbl_tags)

        self.tag_list = QListWidget()
        self.tag_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {c('bg_main')};
                border: 1px solid {c('border')};
                border-radius: 6px;
                color: {c('text_secondary')};
                font-size: 12px;
                outline: none;
            }}
            QListWidget::item {{ padding: 5px; }}
            QListWidget::item:selected {{
                background-color: {c('accent_main')};
                color: {c('bg_main')};
                border-radius: 4px;
            }}
        """)
        self.tag_list.itemClicked.connect(self._on_tag_item_clicked)
        layout.addWidget(self.tag_list)
        self.tag_pager = TagPageBar()
        layout.addWidget(self.tag_pager)

        # Stats at bottom of sidebar
        layout.addStretch()
        self.lbl_stats = QLabel(self.tr("common.status.loading", "Loading..."))
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet(f"color: {c('text_dim')}; font-size: 12px; border-top: 1px solid {c('border')}; padding-top: 10px;")
        layout.addWidget(self.lbl_stats)

        return container

    def _create_content(self) -> QWidget:
        """Main Content: Tag Explorer and Preview"""
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else lambda k: '#000000'

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)  # StandardLayout handles padding
        layout.setSpacing(15)

        # --- Section 1: Tag Explorer ---
        search_frame = QFrame()
        search_frame.setStyleSheet("background-color: transparent;")
        sf_layout = QVBoxLayout(search_frame)
        sf_layout.setContentsMargins(0, 0, 0, 0)

        lbl_search = QLabel(self.tr("lib.explorer_title", "🔭 Tag Explorer"))
        lbl_search.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {c('text_primary')};")
        sf_layout.addWidget(lbl_search)

        # Input
        input_layout = QHBoxLayout()
        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText(self.tr("lib.search_placeholder", "Type a tag and hit Enter..."))
        self.input_search.returnPressed.connect(self.add_tag_from_input)
        self.input_search.setStyleSheet(f"""
            QLineEdit {{
                background-color: {c('bg_input')};
                border: 1px solid {c('border')};
                border-radius: 6px;
                padding: 10px;
                color: {c('text_primary')};
                font-size: 14px;
            }}
            QLineEdit:focus {{ border: 1px solid {c('accent_main')}; }}
        """)

        self.btn_search = QPushButton("🔍")
        self.btn_search.setToolTip(self.tr(
            "lib.search_tip",
            "Searches by name, tags and metadata text across the whole indexed library, "
            "not just the open folder."))
        self.input_search.setToolTip(self.tr(
            "lib.search_tip",
            "Searches by name, tags and metadata text across the whole indexed library, "
            "not just the open folder."))
        self.btn_search.setFixedWidth(50)
        self.btn_search.clicked.connect(self.perform_search)
        self.btn_search.setStyleSheet(f"background-color: {c('bg_panel')}; color: {c('text_primary')}; border-radius: 6px; border: 1px solid {c('border')};")

        input_layout.addWidget(self.input_search)
        input_layout.addWidget(self.btn_search)
        sf_layout.addLayout(input_layout)

        # Active Tags Area
        self.tag_container = QWidget()
        self.tag_flow_layout = FlowLayout(self.tag_container)
        sf_layout.addWidget(self.tag_container)
        self.active_tags = []

        layout.addWidget(search_frame)

        # --- Section 2: Preview (GRID) ---
        self.lbl_folder_stats = QLabel("")
        self.lbl_folder_stats.setAlignment(Qt.AlignCenter)
        self.lbl_folder_stats.setStyleSheet(f"color: {c('accent_main')}; font-size: 14px; font-weight: bold;")
        layout.addWidget(self.lbl_folder_stats)

        # Pagination Header
        self.pagination_layout = QHBoxLayout()
        self.btn_prev = QPushButton(self.tr("common.prev", "◀ Prev"))
        self.btn_prev.setToolTip(self.tr("common.prev_tip", "Previous page of results."))
        self.btn_next = QPushButton(self.tr("common.next", "Next ▶"))
        self.btn_next.setToolTip(self.tr("common.next_tip", "Next page of results."))
        self.lbl_page_info = QLabel(self.tr("common.pagination", "Page 1 of 1").format(curr=1, total=1))
        self.lbl_page_info.setStyleSheet(f"color: {c('text_dim')}; font-weight: bold;")

        for b in [self.btn_prev, self.btn_next]:
            b.setFixedWidth(80)
            b.setEnabled(False)
            b.setCursor(Qt.PointingHandCursor)

        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next.clicked.connect(self.next_page)

        self.pagination_layout.addStretch()
        self.pagination_layout.addWidget(self.btn_prev)
        self.pagination_layout.addWidget(self.lbl_page_info)
        self.pagination_layout.addWidget(self.btn_next)
        self.pagination_layout.addStretch()
        layout.addLayout(self.pagination_layout)

        # Scrollable Grid Area
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(f"background-color: {c('bg_panel')}; border: 1px solid {c('border')}; border-radius: 8px;")

        self.grid_widget = QWidget()
        self.grid_layout_images = QGridLayout(self.grid_widget)
        self.grid_layout_images.setSpacing(10)
        self.grid_layout_images.setAlignment(Qt.AlignTop)

        self.scroll_area.setWidget(self.grid_widget)
        layout.addWidget(self.scroll_area)

        # --- Section 3: Actions (DROPDOWN) ---
        action_bar = QFrame()
        action_bar.setStyleSheet(f"background-color: {c('bg_panel')}; border-radius: 10px; padding: 10px;")
        ab_layout = QHBoxLayout(action_bar)

        lbl_act = QLabel(self.tr("lib.selection_action", "🎯 Selection Action:"))
        lbl_act.setStyleSheet(f"color: {c('text_dim')}; font-weight: bold;")
        ab_layout.addWidget(lbl_act)

        self.combo_actions = QComboBox()
        self.combo_actions.addItems([
            f"--- {self.tr('lib.selection_action', 'Send Selection to...')} ---",
            self.tr("lib.actions.gallery", "🖼️ Open in Gallery"),
            self.tr("lib.actions.optimizer", "🚀 Send to Optimizer"),
            self.tr("lib.actions.cropper", "✂️ Send to Cropper"),
            self.tr("lib.actions.pickler", "🥒 Send to Pickler")
        ])
        self.combo_actions.setStyleSheet(f"""
            QComboBox {{
                background-color: {c('bg_input')};
                color: {c('text_primary')};
                padding: 8px;
                border-radius: 6px;
                min-width: 250px;
                border: 1px solid {c('border')};
                font-weight: bold;
            }}
            QComboBox::drop-down {{ border: none; width: 30px; }}
            QComboBox QAbstractItemView {{
                background-color: {c('bg_input')};
                color: {c('text_primary')};
                selection-background-color: {c('accent_main')};
                selection-color: {c('bg_main')};
            }}
        """)
        self.combo_actions.setToolTip(self.tr(
            "lib.selection_action_tip",
            "Which tool to send the selected images to. Pick here and press Execute."))
        self.combo_actions.currentIndexChanged.connect(self._on_action_selection_changed)
        ab_layout.addWidget(self.combo_actions)

        self.btn_execute = QPushButton(self.tr("lib.execute", "⚡ Execute"))
        self.btn_execute.setCursor(Qt.PointingHandCursor)
        self.btn_execute.setEnabled(False)
        self.btn_execute.setToolTip(self.tr(
            "lib.execute_tip",
            "Sends every match (up to 1,000); larger sets require choosing the current page. No partial set is sent as complete."))
        self.btn_execute.clicked.connect(self.execute_selected_action)
        self.btn_execute.setStyleSheet(f"""
            QPushButton {{ background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; padding: 8px 20px; border-radius: 4px; }}
            QPushButton:hover {{ background-color: {c('text_primary')}; color: {c('bg_main')}; }}
            QPushButton:disabled {{ background-color: {c('border')}; color: {c('text_dim')}; }}
        """)
        self.combo_scope = QComboBox()
        self.combo_scope.addItems([
            self.tr('lib.scope.all', 'All matches (up to 1,000)'),
            self.tr('lib.scope.page', 'Current page')])
        self.combo_scope.setToolTip(self.tr('lib.scope_tip',
            'Choose the complete matching set or only the displayed page. Large complete sets require streaming support.'))
        ab_layout.addWidget(self.combo_scope)
        ab_layout.addWidget(self.btn_execute)

        layout.addWidget(action_bar)

        return container

    def _on_action_selection_changed(self, index):
        """Enables execute button only if a valid tool is selected and we have images."""
        self.btn_execute.setEnabled(self._browse_ready and self._handoff is None and index > 0 and len(self.total_paths) > 0)

    def _create_bottom_bar(self) -> QWidget:
        """Bottom: Indexing Controls"""
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else lambda k: '#000000'

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(15)

        # Scan Status / Progress
        self.lbl_status = QLabel(self.tr("lib.status.ready", "Ready"))
        self.lbl_status.setStyleSheet(f"color: {c('text_secondary')};")
        layout.addWidget(self.lbl_status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{ border: 1px solid {c('border')}; border-radius: 4px; background: {c('bg_panel')}; color: transparent; }}
            QProgressBar::chunk {{ background: {c('accent_main')}; border-radius: 3px; }}
        """)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        layout.addStretch()

        # Controls
        self.btn_backfill = QPushButton(self.tr("lib.backfill", "🧬 Compute Hashes"))
        self.btn_backfill.clicked.connect(self.toggle_backfill)
        self.btn_backfill.setMinimumWidth(120)
        self.btn_backfill.setCursor(Qt.PointingHandCursor)
        self.btn_backfill.setToolTip(self.tr(
            "lib.backfill_tip",
            "Compute the SHA-256 content identity of files that lack it "
            "(cherry-dl folders get theirs from catalog.db). Resumable."
        ))
        self.btn_backfill.setStyleSheet(f"background-color: transparent; color: {c('text_secondary')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px 12px;")

        self.btn_sync = QPushButton(self.tr("lib.purge", "🧹 Sweep Ghost Files"))
        self.btn_sync.setToolTip(self.tr(
            "lib.purge_tip",
            "Goes through the index and drops entries whose file is gone from disk. "
            "Useful after deleting or moving things outside the app. It deletes no "
            "files."))
        self.btn_sync.clicked.connect(lambda: self.toggle_scan(sync_only=True))
        self.btn_sync.setMinimumWidth(120)
        self.btn_sync.setCursor(Qt.PointingHandCursor)
        self.btn_sync.setStyleSheet(f"background-color: transparent; color: {c('text_secondary')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px 12px;")

        self.btn_scan = QPushButton(self.tr("lib.run_indexer", "🚀 Run Indexer"))
        self.btn_scan.setToolTip(self.tr(
            "lib.run_indexer_tip",
            "Walks the watched folders and adds whatever is new to the library. On a "
            "large library it takes a while; the same button stops it."))
        self.btn_scan.clicked.connect(self.toggle_scan)
        self.btn_scan.setMinimumWidth(120)
        self.btn_scan.setCursor(Qt.PointingHandCursor)
        self.btn_scan.setStyleSheet(f"background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; border-radius: 4px; padding: 6px 12px;")

        layout.addWidget(self.btn_backfill)
        layout.addWidget(self.btn_sync)
        layout.addWidget(self.btn_scan)

        return container

    def setup_completer(self):
        self.tag_completions = TagVocabulary(self.db.service, self.input_search, self)
        self.completer_model = self.tag_completions.model
        self.completer = self.tag_completions.completer
        self.tag_pager.bind(self.tag_completions)
        self.tag_completions.busy_changed.connect(lambda busy: self.tag_list.clear() if busy else None)
        self.tag_completions.changed.connect(lambda value: self.tag_list.addItems(value['names']))
        self.tag_completions.refresh()

    def add_tag_from_input(self):
        text = self.input_search.text().strip()
        if not text: return
        
        # Handle commas
        new_tags = [t.strip() for t in text.split(',') if t.strip()]
        
        tags_added = False
        for tag in new_tags:
            if tag not in self.active_tags:
                self.active_tags.append(tag)
                self.add_chip_to_ui(tag)
                tags_added = True
        
        self.input_search.clear()
        
        if tags_added:
            self.perform_search()

    def add_chip_to_ui(self, tag_text):
        # Index determines color cycling
        idx = len(self.active_tags)
        chip = TagChip(tag_text, idx)
        chip.removed.connect(self.remove_tag)
        self.tag_flow_layout.addWidget(chip)
        
    def remove_tag(self, tag_text):
        if tag_text in self.active_tags:
            self.active_tags.remove(tag_text)
            self.perform_search() # Real-time update

    def perform_search(self):
        """Captura la búsqueda; sólo se publica la solicitud vigente."""
        self._request_selection()

    def execute_selected_action(self):
        """Dispatches the current selection to the chosen tool (index-based, locale-safe)."""
        idx = self.combo_actions.currentIndex()
        if idx == 1:
            self.send_to_gallery()
        elif idx == 2:
            self.send_to_optimizer()
        elif idx == 3:
            self.send_to_cropper()
        elif idx == 4:
            self.send_to_pickler()

    def prev_page(self):
        if self._browse_ready and self.current_page > 0:
            self._load_page(self.current_page - 1)

    def next_page(self):
        if self._browse_ready and (self.current_page + 1) * self.page_size < self._selection_count:
            self._load_page(self.current_page + 1)

    def _load_page(self, page):
        if self._external_paths is not None:
            self.current_page = page
            start = page * self.page_size
            self.total_paths = list(self._external_paths[start:start + self.page_size])
            self.refresh_thumbnails_grid()
        else:
            self._request_selection(page)

    def refresh_thumbnails_grid(self):
        """Redraws the thumbnails for the current page (non-blocking)."""
        # Cancel any in-progress lazy load before clearing the widget list
        if hasattr(self, '_lazy_load_timer') and self._lazy_load_timer.isActive():
            self._lazy_load_timer.stop()
        self._lazy_load_pending = []
        self._thumb_widgets = {}
        if self._thumb_loader is None:
            self._thumb_loader = ThumbnailLoader()
            self._thumb_loader.thumbnail_ready.connect(self._thumbnail_ready)
        self._thumb_loader.invalidate()

        # Clear previous thumbnails
        while self.grid_layout_images.count():
            item = self.grid_layout_images.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        page_items = self.total_paths

        # Dynamic columns based on current viewport width
        vp_width = self.scroll_area.viewport().width()
        thumb_total = 110  # 100px thumb + 10px spacing
        cols = max(3, vp_width // thumb_total) if vp_width > 110 else 5

        tm = self.context.get('theme_manager')
        accent = tm.get_color('accent_main') if tm else '#00ffcc'
        border = tm.get_color('border') if tm else '#555555'
        bg = tm.get_color('bg_main') if tm else '#000000'

        for i, path in enumerate(page_items):
            thumb = ClickableThumbnail(path, auto_load=False,
                                       border_accent=accent, border_default=border, bg=bg)
            thumb.clicked.connect(self.on_thumbnail_clicked)
            self.grid_layout_images.addWidget(thumb, i // cols, i % cols)
            self._lazy_load_pending.append(thumb)
            key = os.path.normpath(path).replace("\\", "/")
            self._thumb_widgets.setdefault(key, []).append(thumb)

        # Start non-blocking lazy load (batch per idle cycle)
        if not hasattr(self, '_lazy_load_timer'):
            self._lazy_load_timer = QTimer()
            self._lazy_load_timer.setInterval(0)
            self._lazy_load_timer.timeout.connect(self._load_next_batch)
        if self._lazy_load_pending:
            self._lazy_load_timer.start()

        # Update Pagination UI
        total = self._selection_count
        num_pages = (total - 1) // self.page_size + 1 if total > 0 else 1
        self.lbl_page_info.setText(self.tr("common.pagination", "Page {curr} of {total}").format(curr=self.current_page + 1, total=num_pages))

        self.btn_prev.setEnabled(self._browse_ready and self.current_page > 0)
        self.btn_next.setEnabled(self._browse_ready and self.current_page < num_pages - 1)

        # Enable Execute if we have results
        self.btn_execute.setEnabled(self._browse_ready and bool(self.total_paths) and self._handoff is None and self.combo_actions.currentIndex() > 0)
        self._update_export_controls()

    def _can_export_curation(self):
        return (not self._closed and self.view is not None and self._browse_ready and
                bool(self.current_folder_path) and not self.active_tags and
                self._external_paths is None and
                self._exporting is None and self._folder_write is None)

    def _update_export_controls(self):
        if hasattr(self, 'btn_export_curation'):
            self.btn_export_curation.setEnabled(self._can_export_curation())
        if hasattr(self, 'btn_review_curation'):
            self.btn_review_curation.setEnabled(not self._closed and self._browse_ready and
                bool(self.current_folder_path) and not self.active_tags and self._external_paths is None and
                self._exporting is None and self._folder_write is None)

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
        if self._external_paths is None:
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

    def _load_next_batch(self):
        """Admit requests only; source stat/decode/cache verification stays in workers."""
        if self._closed:
            return
        for _ in range(min(10,len(self._lazy_load_pending))):
            thumb = self._lazy_load_pending.pop(0)
            if isValid(thumb):
                self._thumb_loader.get_thumbnail_image(thumb.path,QSize(100,100))
        if not self._lazy_load_pending:
            self._lazy_load_timer.stop()

    def _thumbnail_ready(self,path,image):
        if self._closed:
            return
        for thumb in self._thumb_widgets.get(path,[]):
            if isValid(thumb):
                if image.isNull():thumb.setText("❌")
                else:thumb.setPixmap(QPixmap.fromImage(image))

    def _on_tag_item_clicked(self, item):
        """Adds a tag from the sidebar list to the search input."""
        tag = item.text()
        if tag not in self.active_tags:
            self.active_tags.append(tag)
            self.add_chip_to_ui(tag)
            self.perform_search()

    def populate_tag_sidebar(self):
        self.update_global_stats()

    def on_thumbnail_clicked(self,path):
        if self._closed:
            return
        dialog = WorkerPreviewDialog(path,self.tr,self.view)
        self._image_previews.append(dialog)
        try:
            dialog.exec()
        finally:
            # The dialog defers done/close until its decoder has actually exited.
            if dialog.shutdown_ready():
                self._image_previews.remove(dialog)
                dialog.deleteLater()

    def on_folder_selected(self, item):
        """Displays preview for the selected folder."""
        folder_path = item.text()
        self.current_folder_path = folder_path
        self.load_folder_paths(folder_path)

    def load_folder_paths(self, folder_path):
        self.current_folder_path = folder_path
        self._request_selection()

    def _revision_busy(self):
        return (self._path_identity_busy or self.overview_reader._active or self.browse_reader._active or
                self.tag_completions.busy or self._handoff is not None or
                self._exporting is not None or self._folder_write is not None)

    def _on_native_revision(self, revision):
        if self._closed or not self.view:
            return
        self.overview_reader.request('get_library_overview', include_tags=False)
        self.tag_completions.reload_page()
        if self._external_paths is None:
            self._request_selection(self.current_page)

    def update_global_stats(self):
        if not self._closed and self.view:
            self.overview_reader.request('get_library_overview', include_tags=False)
            self.tag_completions.refresh()

    def refresh_ui(self):
        if self._closed or not self.view:
            return
        self.update_global_stats()
        self.refresh_current_view()

    def refresh_current_view(self):
        self._request_selection()

    def _request_selection(self, page=0):
        if self._closed or not self.view:
            return
        self.handoff_reader.invalidate()
        self._handoff = None
        self._external_paths = None
        self._browse_ready = False
        self.total_paths = []
        self._selection_count = 0
        self.current_page = page
        self.refresh_thumbnails_grid()
        terms = tuple(self.active_tags)
        folder = self.current_folder_path
        self._selection_context = (terms, folder)
        if not terms and not folder:
            self.browse_reader.invalidate()
            self._browse_ready = True
            self.lbl_folder_stats.setText(self.tr('common.no_selection', 'No Selection'))
            return
        self.lbl_folder_stats.setText(self.tr('lib.db.loading', 'Loading library...'))
        self.browse_reader.request('get_library_page', terms=terms, folder=folder,
                                   limit=self.page_size, offset=page * self.page_size)

    def _read_error(self, label, error):
        log.warning('Librarian read: %s', error)
        label.setText(self.tr('lib.db.read_error', 'Could not read library: {reason}')
                      .format(reason=getattr(error, 'reason', str(error))))

    def _on_selection(self, ticket, value, error):
        if self._closed:
            return
        if error is not None:
            self._read_error(self.lbl_folder_stats, error)
            return
        terms, folder = self._selection_context
        self.total_paths = value['paths']
        self._browse_ready = True
        self.current_page = value['offset'] // self.page_size
        count = self._selection_count = value['count']
        last_page = max(0, (count - 1) // self.page_size)
        if self.current_page > last_page:
            self._request_selection(last_page)
            return
        if terms:
            text = self.tr('lib.msg.found_tags', 'Found {count} items matching tags').format(count=count)
        else:
            text = self.tr('lib.stats.folder', '{name} | {count} Images Found').format(
                name=os.path.basename(folder), count=count)
        if count > len(self.total_paths):
            text += self.tr('lib.db.page', ' | Page contains {loaded} of {count}').format(
                loaded=len(self.total_paths), count=count)
        self.lbl_folder_stats.setText(text)
        self.refresh_thumbnails_grid()

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
        self.lbl_stats.setText(self.tr('lib.stats.global', 'Indexed Files: {count} | Watched Folders: {total}')
                               .format(count=value['count'], total=len(value['folders'])))
        if self._auto_scan_pending:
            self._auto_scan_pending = False
            if value['folders']:
                if self._folder_write is not None:
                    self._auto_scan_after_write = True
                else:
                    self._auto_scan_timer.start(500)

    def on_unload(self):
        if self._thumb_loader is not None:self._thumb_loader.shutdown()
        for dialog in tuple(self._image_previews):dialog.close()
        self._closed = True
        if hasattr(self, 'revisions'):
            self.revisions.close()
        if hasattr(self, 'tag_completions'):
            self.tag_completions.close()
        if hasattr(self, 'folder_archive'):
            self.folder_archive.close()
        for worker in (self.indexer_thread, self.backfill_thread, self._reindex_thread):
            if worker and worker.isRunning():
                worker.stop()
        if hasattr(self, 'folder_writer'):
            self.folder_writer.close()
        self._handoff = None
        self._exporting = None
        if self._review_dialog is not None:
            self._review_dialog.close()
        if self._recovery_dialog is not None:
            self._recovery_dialog.close()
        if self._versions_dialog is not None:
            self._versions_dialog.close()
        if self._identity_dialog is not None:
            self._identity_dialog.close()
        for name in ('overview_reader', 'browse_reader', 'scan_reader', 'handoff_reader', 'export_reader'):
            reader = getattr(self, name, None)
            if reader:
                reader.close()
        self._dirty_timer.stop()
        self._auto_scan_timer.stop()
        if hasattr(self, '_lazy_load_timer'):
            self._lazy_load_timer.stop()
        eb = (self.context or {}).get('event_bus')
        if eb:
            eb.unsubscribe('library_changed', self._on_library_changed)

    def shutdown_ready(self):
        readers = self.findChildren(LibraryReader)
        if self.view is not None and isValid(self.view):
            readers += self.view.findChildren(LibraryReader)
        return (self._thumb_loader is None or self._thumb_loader.shutdown_ready()) and all(dialog.shutdown_ready() for dialog in self._image_previews) and owner_threads_ready(self) and ((not hasattr(self, 'folder_archive') or self.folder_archive.shutdown_ready())
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
        c = tm.get_color if tm else lambda k: '#000000'
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
            c = tm.get_color if tm else lambda k: '#000000'
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
        c = tm.get_color if tm else lambda k: '#000000'
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

    def _get_active_paths(self):
        """La página confirmada completa, nunca un prefijo oculto."""
        return list(self.total_paths) if self._browse_ready and not self._closed else []

    def _send_selection(self, destination):
        if self._closed or not self._browse_ready or self._handoff is not None:
            return
        title = self.tr('lib.handoff.title', 'Librarian: {context}').format(
            context=', '.join(self.active_tags) if self.active_tags else
            os.path.basename(self.current_folder_path or ''))
        if self.combo_scope.currentIndex() == 1:
            title += self.tr('lib.handoff.page', ' | Page {page}').format(page=self.current_page + 1)
            self._emit_selection(destination, self._get_active_paths(), title)
        elif self._external_paths is not None:
            if len(self._external_paths) > 1000:
                self._handoff_error(ValueError('selection_requires_streaming'))
            else:
                self._emit_selection(destination, list(self._external_paths), title)
        else:
            self._handoff = (destination, title)
            self.btn_execute.setEnabled(False)
            self.handoff_reader.request('resolve_library_selection',
                terms=tuple(self.active_tags), folder=self.current_folder_path)

    def _on_handoff(self, ticket, value, error):
        if self._closed or self._handoff is None:
            return
        destination, title = self._handoff
        self._handoff = None
        self._on_action_selection_changed(self.combo_actions.currentIndex())
        if error is not None:
            self._handoff_error(error)
            return
        self._emit_selection(destination, value['paths'], title)

    def _handoff_error(self, error):
        reason = getattr(error, 'cause', error)
        if 'selection_requires_streaming' in str(reason):
            message = self.tr('lib.handoff.too_large',
                'The complete set exceeds 1,000 images. Choose Current page to send a page explicitly. No images were sent.')
        else:
            message = self.tr('lib.db.read_error', 'Could not read library: {reason}').format(reason=str(error))
        QMessageBox.warning(self.view, self.tr('common.error', 'Error'), message)

    def _emit_selection(self, destination, paths, title):
        if not paths:
            QMessageBox.warning(self.view, self.tr('common.error', 'No Files'), self.tr('lib.msg.no_files', 'No files found to send.'))
            return
        signal = getattr(self, 'request_open_' + destination)
        if destination == 'gallery':
            signal.emit(paths, title)
        else:
            signal.emit(paths)

    def send_to_gallery(self):
        self._send_selection('gallery')

    def send_to_optimizer(self):
        self._send_selection('optimizer')

    def send_to_cropper(self):
        self._send_selection('cropper')

    def send_to_pickler(self):
        self._send_selection('pickler')

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
        """Permite cargar carpetas o rutas en modo programático (sin interacción del usuario)."""
        if not paths:
            return
        if self._closed:
            return
        if hasattr(self, 'browse_reader'):
            self.browse_reader.invalidate()
        self._browse_ready = True
        self.handoff_reader.invalidate()
        self._handoff = None
        self._external_paths = tuple(paths)
        self._selection_count = len(paths)
        self.total_paths = list(paths[:self.page_size])
        self.current_page = 0
        if self.view:
            self.refresh_thumbnails_grid()
