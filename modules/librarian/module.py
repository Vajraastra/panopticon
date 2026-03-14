from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
                               QListWidget, QFileDialog, QMessageBox, QProgressBar, QFrame,
                               QLineEdit, QDialog, QCompleter, QScrollArea, QGridLayout, QComboBox)
from PySide6.QtGui import QPixmap, QIcon, QMouseEvent
from PySide6.QtCore import Qt, QSize, Signal, QTimer, QStringListModel
from core.base_module import BaseModule
from .logic.db_manager import DatabaseManager
from .logic.indexer import IndexerWorker
from .logic.tagging_ui import FlowLayout, TagChip
import os
import logging

log = logging.getLogger(__name__)

class ClickableThumbnail(QLabel):
    clicked = Signal(str)
    selection_changed = Signal(bool)
    rating_changed = Signal(str, int)

    def __init__(self, path, parent=None, auto_load=True, rating=0,
                 border_accent='#00ffcc', border_default='#555', bg='#000000'):
        super().__init__(parent)
        # Ensure path matches DB/Loader format (forward slashes)
        self.path = os.path.normpath(path).replace('\\', '/')
        self.selected = False
        self.rating = rating
        self.border_accent = border_accent
        self.border_default = border_default
        self.bg = bg
        self.setFixedSize(100, 100)
        self.setStyleSheet(f"border: 1px solid {border_default}; border-radius: 5px; background-color: {bg};")
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        
        # Rating Label (Overlay)
        self.lbl_rating = QLabel(self)
        self.lbl_rating.setFixedSize(30, 20)
        self.lbl_rating.setAlignment(Qt.AlignCenter)
        self.lbl_rating.setStyleSheet("""
            background-color: rgba(0, 0, 0, 150); 
            color: #ffcc00; 
            font-weight: bold; 
            font-size: 10px; 
            border-bottom-left-radius: 5px;
        """)
        # Position in top-right
        self.lbl_rating.move(70, 0)
        self.update_rating_display()
        
        if auto_load:
            pix = QPixmap(path)
            if not pix.isNull():
                self.setPixmap(pix.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.setText("❌")
        else:
            self.setText("⏳") # Placeholder

    def update_rating_display(self):
        if self.rating > 0:
            self.lbl_rating.setText(f"⭐{self.rating}")
            self.lbl_rating.show()
        else:
            self.lbl_rating.hide()

    def setRating(self, rating):
        self.rating = rating
        self.update_rating_display()

    def cycleRating(self):
        self.rating = (self.rating + 1) % 6 # 0,1,2,3,4,5 then back to 0
        self.update_rating_display()
        self.rating_changed.emit(self.path, self.rating)

    def setSelected(self, selected):
        self.selected = selected
        if self.selected:
            self.setStyleSheet(f"border: 3px solid {self.border_accent}; border-radius: 5px; background-color: {self.bg};")
        else:
            self.setStyleSheet(f"border: 1px solid {self.border_default}; border-radius: 5px; background-color: {self.bg};")
        self.selection_changed.emit(self.selected)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ControlModifier:
                self.cycleRating()
            else:
                self.clicked.emit(self.path)

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

    def __init__(self):
        super().__init__()
        self._name = "Librarian"
        self._description = "Indexador central y gestor de etiquetas de la librería."
        self._icon = "📚"
        
        self.view = None
        # Gestor de base de datos SQLite persistente
        self.db = DatabaseManager()
        self.indexer_thread = None
        
        # Estado de Paginación del Explorador de Tags
        self.current_page = 0
        self.page_size = 100
        self.total_paths = []
        self.current_folder_path = None

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
        
        # 3. Cargar datos iniciales
        self.refresh_ui()
        self.update_global_stats()
        
        # 4. Configurar autocompletado de tags
        self.setup_completer()
        self.populate_tag_sidebar()

        # 5. Disparar escaneo inicial inteligente si hay carpetas registradas
        if self.db.get_watched_folders():
            QTimer.singleShot(500, lambda: self.toggle_scan(auto=True))
            
        return self.view

    def _create_sidebar(self) -> QWidget:
        """Sidebar: Watched Folders Management"""
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else lambda k: '#000000'

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
        self.btn_add.clicked.connect(self.add_folder)
        self.btn_add.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('accent_main')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px;")

        self.btn_remove = QPushButton(self.tr("lib.remove", "🗑️ Remove"))
        self.btn_remove.clicked.connect(self.remove_folder)
        self.btn_remove.setStyleSheet(f"background-color: {c('bg_input')}; color: {c('accent_warning')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px;")

        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        layout.addLayout(btn_layout)

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
        self.btn_next = QPushButton(self.tr("common.next", "Next ▶"))
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
            self.tr("lib.actions.cropper", "✂️ Send to Cropper")
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
        self.combo_actions.currentIndexChanged.connect(self._on_action_selection_changed)
        ab_layout.addWidget(self.combo_actions)

        self.btn_execute = QPushButton(self.tr("lib.execute", "⚡ Execute"))
        self.btn_execute.setCursor(Qt.PointingHandCursor)
        self.btn_execute.setEnabled(False)
        self.btn_execute.clicked.connect(self.execute_selected_action)
        self.btn_execute.setStyleSheet(f"""
            QPushButton {{ background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; padding: 8px 20px; border-radius: 4px; }}
            QPushButton:hover {{ background-color: {c('text_primary')}; color: {c('bg_main')}; }}
            QPushButton:disabled {{ background-color: {c('border')}; color: {c('text_dim')}; }}
        """)
        ab_layout.addWidget(self.btn_execute)

        layout.addWidget(action_bar)

        return container

    def _on_action_selection_changed(self, index):
        """Enables execute button only if a valid tool is selected and we have images."""
        self.btn_execute.setEnabled(index > 0 and len(self.total_paths) > 0)

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
        self.btn_sync = QPushButton(self.tr("lib.purge", "🧹 Sweep Ghost Files"))
        self.btn_sync.clicked.connect(lambda: self.toggle_scan(sync_only=True))
        self.btn_sync.setMinimumWidth(120)
        self.btn_sync.setCursor(Qt.PointingHandCursor)
        self.btn_sync.setStyleSheet(f"background-color: transparent; color: {c('text_secondary')}; border: 1px solid {c('border')}; border-radius: 4px; padding: 6px 12px;")

        self.btn_scan = QPushButton(self.tr("lib.run_indexer", "🚀 Run Indexer"))
        self.btn_scan.clicked.connect(self.toggle_scan)
        self.btn_scan.setMinimumWidth(120)
        self.btn_scan.setCursor(Qt.PointingHandCursor)
        self.btn_scan.setStyleSheet(f"background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; border-radius: 4px; padding: 6px 12px;")

        layout.addWidget(self.btn_sync)
        layout.addWidget(self.btn_scan)

        return container

    def setup_completer(self):
        """Initializes the QCompleter for the tag input."""
        all_tags = self.db.get_all_tags()
        self.completer_model = QStringListModel(all_tags)
        self.completer = QCompleter(self.completer_model)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains) # Allow matching 'lee' in 'Zoe Lee'
        self.input_search.setCompleter(self.completer)

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
        """Executes search based on active_tags and updates UI."""
        if not self.active_tags:
            # If search cleared, fallback to folder or show nothing
            if hasattr(self, 'current_folder_path') and self.current_folder_path:
                self.load_folder_paths(self.current_folder_path)
            else:
                self.total_paths = []
                self.current_page = 0
                self.refresh_thumbnails_grid()
                self.lbl_folder_stats.setText(self.tr("lib.explorer_cleared", "Tag Explorer Cleared."))
            return

        # Fetch results
        count, paths = self.db.search_by_terms(self.active_tags, limit=10000)
        self.total_paths = paths
        self.current_page = 0
        
        if count == 0:
            self.lbl_folder_stats.setText(self.tr("lib.msg.no_results", "⚠️ No images found with these tags."))
        else:
            self.lbl_folder_stats.setText(self.tr("lib.msg.found_tags", "🔍 Found {count} items matching tags").format(count=count))
            
        self.refresh_thumbnails_grid()
        
        # Update Footer Stats
        folders = self.db.get_watched_folders()
        self.lbl_stats.setText(self.tr("lib.stats.search", "📊 SEARCH | {tags} tags | Found: {count} | Folders: {total}")
                               .format(tags=len(self.active_tags), count=count, total=len(folders)))

    def execute_selected_action(self):
        """Dispatches the current selection to the chosen tool (index-based, locale-safe)."""
        idx = self.combo_actions.currentIndex()
        if idx == 1:
            self.send_to_gallery()
        elif idx == 2:
            self.send_to_optimizer()
        elif idx == 3:
            self.send_to_cropper()

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.refresh_thumbnails_grid()

    def next_page(self):
        max_pages = (len(self.total_paths) - 1) // self.page_size
        if self.current_page < max_pages:
            self.current_page += 1
            self.refresh_thumbnails_grid()

    def refresh_thumbnails_grid(self):
        """Redraws the thumbnails for the current page (non-blocking)."""
        # Cancel any in-progress lazy load before clearing the widget list
        if hasattr(self, '_lazy_load_timer') and self._lazy_load_timer.isActive():
            self._lazy_load_timer.stop()
        self._lazy_load_pending = []

        # Clear previous thumbnails
        while self.grid_layout_images.count():
            item = self.grid_layout_images.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        start = self.current_page * self.page_size
        end = start + self.page_size
        page_items = self.total_paths[start:end]

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

        # Start non-blocking lazy load (batch per idle cycle)
        if not hasattr(self, '_lazy_load_timer'):
            self._lazy_load_timer = QTimer()
            self._lazy_load_timer.setInterval(0)
            self._lazy_load_timer.timeout.connect(self._load_next_batch)
        if self._lazy_load_pending:
            self._lazy_load_timer.start()

        # Update Pagination UI
        total = len(self.total_paths)
        num_pages = (total - 1) // self.page_size + 1 if total > 0 else 1
        self.lbl_page_info.setText(self.tr("common.pagination", "Page {curr} of {total}").format(curr=self.current_page + 1, total=num_pages))

        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled(self.current_page < num_pages - 1)

        # Enable Execute if we have results
        self.btn_execute.setEnabled(total > 0 and self.combo_actions.currentIndex() > 0)

    def _load_next_batch(self):
        """Loads a small batch of thumbnail pixmaps per event-loop cycle to keep UI responsive."""
        batch_size = 10
        loaded = 0
        while self._lazy_load_pending and loaded < batch_size:
            thumb = self._lazy_load_pending.pop(0)
            try:
                pix = QPixmap(thumb.path)
                if not pix.isNull():
                    thumb.setPixmap(pix.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                else:
                    thumb.setText("❌")
            except RuntimeError:
                pass  # Widget was deleted between batches (page changed)
            loaded += 1
        if not self._lazy_load_pending:
            self._lazy_load_timer.stop()

    def _on_tag_item_clicked(self, item):
        """Adds a tag from the sidebar list to the search input."""
        tag = item.text()
        if tag not in self.active_tags:
            self.active_tags.append(tag)
            self.add_chip_to_ui(tag)
            self.perform_search()

    def populate_tag_sidebar(self):
        """Fetches all tags from the DB and populates the sidebar list."""
        self.tag_list.clear()
        all_tags = self.db.get_all_tags()
        if all_tags:
            self.tag_list.addItems(all_tags)

    def on_thumbnail_clicked(self, path):
        """Opens the clicked thumbnail in a larger dialog."""
        dlg = QDialog(self.view)
        dlg.setWindowTitle(self.tr("lib.preview.title", "Image Preview"))
        dlg.resize(800, 600)
        
        vbox = QVBoxLayout(dlg)
        lbl_img = QLabel()
        lbl_img.setAlignment(Qt.AlignCenter)
        
        pix = QPixmap(path)
        if not pix.isNull():
            lbl_img.setPixmap(pix.scaled(750, 550, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            lbl_img.setText(self.tr("gal.viewer.load_error", "Error loading image"))
            
        vbox.addWidget(lbl_img)
        dlg.exec()

    def on_folder_selected(self, item):
        """Displays preview for the selected folder."""
        folder_path = item.text()
        self.current_folder_path = folder_path
        self.load_folder_paths(folder_path)

    def load_folder_paths(self, folder_path):
        """Helper to fetch and display paths for a specific folder."""
        paths = self.db.get_files_recursive(folder_path, limit=10000)
        self.total_paths = paths
        self.current_page = 0
        
        self.lbl_folder_stats.setText(self.tr("lib.stats.folder", "📂 {name} | {count} Images Found").format(name=os.path.basename(folder_path), count=len(paths)))
        self.refresh_thumbnails_grid()


    def update_global_stats(self):
        if self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT count(*) FROM files")
            count = cursor.fetchone()[0]
            folders = self.db.get_watched_folders()
            self.lbl_stats.setText(self.tr("lib.stats.global", "📊 Global Status | Indexed Files: {count} | Watched Folders: {total}")
                                   .format(count=count, total=len(folders)))

    def refresh_ui(self):
        # specific reload logic
        self.folder_list.clear()
        folders = self.db.get_watched_folders()
        self.folder_list.addItems(folders)
        self.refresh_current_view()
        self.update_global_stats()

    def refresh_current_view(self):
        """Forces a re-fetch of data for the current search or folder."""
        # Check if current folder is still valid
        if hasattr(self, 'current_folder_path') and self.current_folder_path:
             folders = self.db.get_watched_folders()
             if self.current_folder_path not in folders:
                 self.current_folder_path = None

        if self.active_tags:
            self.perform_search()
        elif hasattr(self, 'current_folder_path') and self.current_folder_path:
            self.load_folder_paths(self.current_folder_path)
        else:
            self.total_paths = []
            self.current_page = 0
            self.refresh_thumbnails_grid()
            self.lbl_folder_stats.setText(self.tr("common.no_selection", "No Selection"))

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self.view, self.tr("common.select_folder", "Select Folder to Index"))
        if folder:
            if self.db.add_watched_folder(folder):
                self.refresh_ui()
                self.update_global_stats()
            else:
                QMessageBox.warning(self.view, self.tr("common.error", "Error"), self.tr("lib.msg.add_error", "Could not add folder"))

    def remove_folder(self):
        selected = self.folder_list.currentItem()
        if not selected: return
        
        path = selected.text()
        confirm = QMessageBox.question(self.view, self.tr("common.confirm", "Remove Folder"), 
                                       self.tr("lib.msg.remove_confirm", "Stop watching '{path}'?").format(path=path),
                                       QMessageBox.Yes | QMessageBox.No)
        
        if confirm == QMessageBox.Yes:
            if self.db.remove_watched_folder(path):
                # Trigger VACUUM to reclaim disk space immediately
                self.db.vacuum_database()
                if hasattr(self, 'current_folder_path') and self.current_folder_path == path:
                    self.current_folder_path = None
                self.refresh_ui()
                self.update_global_stats()

    def toggle_scan(self, sync_only=False, auto=False):
        if self.indexer_thread and self.indexer_thread.isRunning():
            # Stop logic
            self.indexer_thread.stop()
            self.btn_scan.setText(self.tr("lib.status.stopping", "Stopping..."))
            if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
            self.btn_scan.setEnabled(False)
        else:
            # Start logic
            folders = self.db.get_watched_folders()
            if not folders:
                if not auto:
                    QMessageBox.warning(self.view, self.tr("common.error", "No Folders"), self.tr("lib.msg.no_folders", "Please add at least one folder to index."))
                return
            
            # Update UI state
            if not auto:
                tm = self.context.get('theme_manager')
                c = tm.get_color if tm else lambda k: '#000000'
                self.btn_scan.setText(self.tr("lib.stop_indexer", "🛑 Stop Scanning"))
                self.btn_scan.setStyleSheet(f"background-color: {c('accent_warning')}; color: {c('text_primary')}; border-radius: 4px; padding: 6px; font-weight: bold;")
                if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
                self.progress_bar.setVisible(True)
                self.progress_bar.setValue(0)
                self.lbl_status.setText(self.tr("lib.indexing", "🔍 Running Background Sync..."))
            
            # Pass sync_only as deep_clean to trigger Full Audit + Vacuum
            self.indexer_thread = IndexerWorker(self.db, folders, deep_clean=sync_only)
            self.indexer_thread.progress_signal.connect(self.update_progress_text)
            self.indexer_thread.count_signal.connect(self.update_progress_bar)
            self.indexer_thread.finished_signal.connect(self.scan_finished)
            self.indexer_thread.start()

    def update_progress_text(self, text):
        self.lbl_status.setText(text)

    def update_progress_bar(self, current, total):
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def scan_finished(self):
        tm = self.context.get('theme_manager')
        c = tm.get_color if tm else lambda k: '#000000'
        self.btn_scan.setText(self.tr("lib.run_indexer", "🚀 Run Indexer"))
        self.btn_scan.setStyleSheet(f"background-color: {c('accent_main')}; color: {c('bg_main')}; font-weight: bold; border-radius: 4px; padding: 6px;")
        if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.refresh_ui()
        self.populate_tag_sidebar() # Ensure new tags appear
        QMessageBox.information(self.view, self.tr("common.success", "Done"), self.tr("lib.msg.done", "Library Scan Complete!"))

    def _get_active_paths(self, limit=1000):
        """Helper to get currently filtered or selected paths."""
        if self.active_tags:
            _, paths = self.db.search_by_terms(self.active_tags, limit=limit)
            return paths
        elif self.folder_list.currentItem():
            folder_path = self.folder_list.currentItem().text()
            return self.db.get_files_recursive(folder_path, limit=limit)
        return []

    def send_to_gallery(self):
        paths = self._get_active_paths()
        if paths:
            title = f"Librarian: {', '.join(self.active_tags)}" if self.active_tags else f"Librarian: {os.path.basename(self.folder_list.currentItem().text())}"
            self.request_open_gallery.emit(paths, title)
             
    def send_to_optimizer(self):
        paths = self._get_active_paths()
        if paths:
            self.request_open_optimizer.emit(paths)
        else:
            QMessageBox.warning(self.view, self.tr("common.error", "No Files"), self.tr("lib.msg.no_files", "No files found to send."))

    def send_to_cropper(self):
        paths = self._get_active_paths()
        if paths:
            self.request_open_cropper.emit(paths)
        else:
            QMessageBox.warning(self.view, self.tr("common.error", "No Files"), self.tr("lib.msg.no_files", "No files found to send."))

    def run_headless(self, paths: list = None):
        """Permite cargar carpetas o rutas en modo programático (sin interacción del usuario)."""
        if not paths:
            return
        self.total_paths = paths
        self.current_page = 0
        if self.view:
            self.refresh_thumbnails_grid()
