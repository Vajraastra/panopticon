from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QGridLayout, QFrame, QComboBox, QFileDialog, QLineEdit, QCompleter)
from PySide6.QtCore import Qt, Signal, Slot, QSize, QEvent
from PySide6.QtGui import QPixmap, QMouseEvent, QIcon, QImage
from core.base_module import BaseModule
from modules.librarian.logic.db_manager import DatabaseManager
from modules.librarian.module import ClickableThumbnail  # Reusing the thumb component
from .logic.loader import get_loader
from modules.librarian.logic.tagging_ui import FlowLayout, TagChip
import os

class ClickableRatingLabel(QLabel):
    """A label that cycles ratings when clicked."""
    clicked = Signal()
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

class FolderCard(QFrame):
    clicked = Signal(str)
    
    def __init__(self, path, name, cover_path, count):
        super().__init__()
        self.path = path
        # Ensure path matches DB/Loader format (forward slashes)
        self.cover_path = os.path.normpath(cover_path).replace('\\', '/') if cover_path else None
        self.setFixedSize(150, 200) # Reduced from 200, 240 to fit 5 columns
        self.setStyleSheet("""
            FolderCard {
                background-color: #222;
                border-radius: 10px;
                border: 1px solid #333;
            }
            FolderCard:hover {
                background-color: #333;
                border: 1px solid #00ffcc;
            }
        """)
        self.setCursor(Qt.PointingHandCursor)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Cover Image
        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(130, 120) # Reduced from 180, 160
        self.lbl_cover.setStyleSheet("background-color: black; border-radius: 5px;")
        self.lbl_cover.setAlignment(Qt.AlignCenter)
        
        if cover_path:
            self.lbl_cover.setText("⏳") # Placeholder
        else:
            self.lbl_cover.setText(self.tr("gal.empty", "📁 Empty"))
            
        layout.addWidget(self.lbl_cover)
        
        # Folder Name
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        self.lbl_name.setAlignment(Qt.AlignCenter)
        # Elide text if too long
        # self.lbl_name.setWordWrap(True) 
        layout.addWidget(self.lbl_name)
        
        # Count
        self.lbl_count = QLabel(self.tr("gal.item_count", "{count} items").format(count=count))
        self.lbl_count.setStyleSheet("color: #888; font-size: 12px;")
        self.lbl_count.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_count)

    def setCoverPixmap(self, pixmap):
        if pixmap:
            self.lbl_cover.setPixmap(pixmap.scaled(130, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.lbl_cover.setText("❌")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.path)

class GalleryModule(BaseModule):
    # Signals
    request_open_workshop = Signal(list)
    
    # View Modes
    VIEW_ALBUMS = "albums"
    VIEW_IMAGES = "images"
    VIEW_CUSTOM = "custom"

    def __init__(self):
        super().__init__()
        self.view = None
        self.db = DatabaseManager() 
        
        # State
        self.current_view_mode = self.VIEW_ALBUMS
        self.current_page = 0
        self.page_size = 50 
        self.current_query_tags = [] 
        self.current_folder_filter = None # If set, we are browsing a specific folder
        self.current_paths = []
        self.custom_paths_source = [] # Store custom paths here
        self.total_items = 0
        
        # Selection / Picker Mode
        self.picker_mode = False
        self.picked_paths = set()
        
        # Responsive Grid State
        self.active_widgets = [] 
        self.current_cols = 5

        # Rating Mode / Filter
        self.rating_mode = False
        self.current_rating_filter = 0 # 0 = All

        # Tag Mode / Search
        self.tag_mode = False
        self.current_query_tags = []
        self.current_query_terms = []
        
        # Loader Integration
        self.loader = get_loader()
        self.loader.thumbnail_ready.connect(self.on_thumbnail_ready)

    @property
    def name(self):
        return "The Gallery"

    @property
    def icon(self):
        return "🖼️"

    @property
    def description(self):
        return "Visual Image Browser & Gallery"

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        # Create Components
        self.sidebar = self._create_sidebar()
        self.content = self._create_content() # Contains Grid
        self.bottom = self._create_bottom_bar() # Contains Pagination
        
        # Assemble
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            self.content,
            self.sidebar,
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        # Hook resize event via EventFilter for grid reflow
        self.grid_container.installEventFilter(self)
        
        # Initial Load
        self.refresh_grid()
        
        return self.view

    def _create_sidebar(self) -> QWidget:
        """Sidebar: Navigation & Filters"""
        container = QWidget()
        container.setStyleSheet("""
            QWidget {
                background-color: #1a1a1a;
                border-right: 1px solid #333;
            }
            QLabel {
                color: #e0e0e0;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit {
                background-color: #222;
                color: #eee;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 8px;
                selection-background-color: #00ffcc;
                selection-color: black;
            }
            QLineEdit:focus {
                border: 1px solid #00ffcc;
                background-color: #2a2a2a;
            }
            QPushButton {
                background-color: #252525;
                color: #ccc;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 8px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: #333;
                border-color: #555;
                color: white;
            }
            QComboBox {
                background-color: #222;
                color: #eee;
                border: 1px solid #333;
                border-radius: 6px;
                padding: 6px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #666;
                width: 0;
                height: 0;
                margin-right: 8px;
            }
        """)
        
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 20, 15, 20)
        layout.setSpacing(15)
        
        # Navigation Section
        lbl_nav = QLabel(self.tr("gal.navigation", "NAVIGATION"))
        lbl_nav.setStyleSheet("font-size: 11px; font-weight: bold; color: #666; letter-spacing: 1px;")
        layout.addWidget(lbl_nav)
        
        self.btn_back = QPushButton(f"  {self.tr('gal.back', '🔙 Back to Albums')}")
        self.btn_back.clicked.connect(self.switch_to_albums)
        self.btn_back.setVisible(False)
        self.btn_back.setStyleSheet("background-color: #2a2a2a; color: #00ffcc; font-weight: bold;")
        layout.addWidget(self.btn_back)
        
        self.btn_open_local = QPushButton(f"  {self.tr('gal.open_local', '📂 Open Local Folder')}")
        self.btn_open_local.clicked.connect(self.open_custom_folder)
        layout.addWidget(self.btn_open_local)
        
        layout.addSpacing(10)
        
        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #333;")
        layout.addWidget(line)
        
        layout.addSpacing(10)
        
        # Filter & Search
        lbl_filter = QLabel(self.tr("gal.search_filter_title", "SEARCH & FILTER"))
        lbl_filter.setStyleSheet("font-size: 11px; font-weight: bold; color: #666; letter-spacing: 1px;")
        layout.addWidget(lbl_filter)
        
        # Broad Search
        self.txt_broad_search = QLineEdit()
        self.txt_broad_search.setPlaceholderText(self.tr("gal.search_placeholder", "🔍  Search keywords..."))
        self.txt_broad_search.returnPressed.connect(self.on_search_triggered)
        layout.addWidget(self.txt_broad_search)
        
        # Tag Search
        self.txt_tag_search = QLineEdit()
        self.txt_tag_search.setPlaceholderText(self.tr("gal.tag_filter_placeholder", "🏷️  Filter by tags..."))
        self.txt_tag_search.returnPressed.connect(self.on_search_triggered)
        layout.addWidget(self.txt_tag_search)
        
        # Quality Dropdown (Moved Up)
        self.combo_rating = QComboBox()
        self.combo_rating.addItems([
            self.tr("gal.quality_all", "All Quality Levels"),
            "⭐ 1+ Stars",
            "⭐⭐ 2+ Stars",
            "⭐⭐⭐ 3+ Stars",
            "⭐⭐⭐⭐ 4+ Stars",
            f"⭐⭐⭐⭐⭐ {self.tr('gal.stars_only', '5 Stars Only')}"
        ])
        self.combo_rating.currentIndexChanged.connect(self.on_rating_changed)
        self.combo_rating.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.combo_rating)

        # Clear Button (Moved Up)
        self.btn_clear_all = QPushButton(self.tr("gal.clear_filters", "✕ Clear Filters"))
        self.btn_clear_all.clicked.connect(self.clear_all_filters)
        self.btn_clear_all.setStyleSheet("color: #ff5555; background: transparent; border: 1px solid #331111; text-align: center;")
        layout.addWidget(self.btn_clear_all)

        # Active Chips (Search Tags)
        self.tags_container = QWidget()
        self.tags_container.setStyleSheet("background: transparent;")
        self.tags_layout = FlowLayout(self.tags_container)
        layout.addWidget(self.tags_container)
        
        layout.addSpacing(10)

        # --- AZURE ZONE: IMAGE INFO (Selection Metadata) ---
        self.info_panel = QFrame()
        self.info_panel.setStyleSheet("background-color: #222; border-radius: 8px; border: 1px solid #333;")
        info_layout = QVBoxLayout(self.info_panel)
        info_layout.setContentsMargins(10, 10, 10, 10)
        
        lbl_info_title = QLabel(self.tr("gal.selection_info", "📄 Selection Info"))
        lbl_info_title.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 11px; border: none;")
        info_layout.addWidget(lbl_info_title)
        
        self.lbl_selected_info = QLabel(self.tr("gal.no_selection", "No Selection"))
        self.lbl_selected_info.setStyleSheet("color: #eee; font-size: 11px; border: none;")
        self.lbl_selected_info.setWordWrap(True)
        info_layout.addWidget(self.lbl_selected_info)

        self.lbl_selected_rating = ClickableRatingLabel("")
        self.lbl_selected_rating.setStyleSheet("color: #ffcc00; font-weight: bold; border: none; font-size: 18px;")
        self.lbl_selected_rating.clicked.connect(self.cycle_selected_rating)
        info_layout.addWidget(self.lbl_selected_rating)
        
        self.selected_tags_container = QWidget()
        self.selected_tags_container.setStyleSheet("border: none;")
        self.selected_tags_layout = FlowLayout(self.selected_tags_container)
        info_layout.addWidget(self.selected_tags_container)

        self.btn_view_image = QPushButton(self.tr("gal.fullscreen", "👁️ Fullscreen View"))
        self.btn_view_image.clicked.connect(self.open_current_in_viewer)
        self.btn_view_image.setStyleSheet("margin-top: 10px; font-weight: bold; height: 35px;")
        info_layout.addWidget(self.btn_view_image)
        
        layout.addWidget(self.info_panel)
        
        layout.addStretch()

        # Future Tools Placeholder
        self.combo_tools_placeholder = QComboBox()
        self.combo_tools_placeholder.addItem(f"🛠️  {self.tr('gal.batch_tools', 'Batch Tools (Future)')}")
        self.combo_tools_placeholder.setEnabled(False)
        self.combo_tools_placeholder.setStyleSheet("background-color: #1a1a1a; color: #555; border: 1px dashed #444;")
        layout.addWidget(self.combo_tools_placeholder)

        # Picker Mode at the very bottom
        self.btn_picker_toggle = QPushButton(self.tr("gal.picker_mode", "🎯 Picker Mode"))
        self.btn_picker_toggle.setCheckable(True)
        self.btn_picker_toggle.clicked.connect(self.toggle_picker_mode)
        self.btn_picker_toggle.setFixedSize(170, 40)
        self.btn_picker_toggle.setStyleSheet("""
            QPushButton { background-color: #333; color: #aaa; border-radius: 6px; }
            QPushButton:checked { background-color: #004433; color: #00ffcc; border: 1px solid #00ffcc; }
        """)
        layout.addWidget(self.btn_picker_toggle, 0, Qt.AlignCenter)
        
        return container

    def open_current_in_viewer(self):
        if hasattr(self, 'current_selected_path') and self.current_selected_path:
            full_paths = self._get_full_context_paths()
            try:
                idx = full_paths.index(self.current_selected_path)
            except ValueError:
                idx = 0
                if self.current_selected_path not in full_paths:
                    full_paths.insert(0, self.current_selected_path)
            
            try:
                from .viewer import AdvancedViewer
                viewer = AdvancedViewer(full_paths, start_index=idx, parent=self.view)
                viewer.exec()
            except Exception as e:
                import traceback
                traceback.print_exc()

    def _create_content(self) -> QWidget:
        """Main Content: The Grid"""
        # Outer container to hold Title + Grid
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 1. Header / Title
        self.lbl_title = QLabel(self.tr("gal.title", "🖼️ Gallery Albums"))
        self.lbl_title.setStyleSheet("color: white; font-size: 18px; font-weight: bold; padding: 15px; background-color: #111;")
        layout.addWidget(self.lbl_title)

        # 2. Scroll Area for Grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background-color: #111; border: none;")
        
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(15)
        self.grid_layout.setContentsMargins(15, 15, 15, 15)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        scroll.setWidget(self.grid_container)
        layout.addWidget(scroll)
        
        return container

    def _create_bottom_bar(self) -> QWidget:
        """Bottom: Status, Pagination, Context Actions"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)
        
        # Status Left
        self.lbl_status = QLabel(self.tr("common.status.ready", "Ready"))
        self.lbl_status.setStyleSheet("color: #888;")
        layout.addWidget(self.lbl_status)
        
        layout.addStretch()
        
        # Picker Actions (Hidden by default)
        self.picker_actions = QWidget()
        pa_layout = QHBoxLayout(self.picker_actions)
        pa_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_picked_count = QLabel(self.tr("gal.picked_count", "0 Selected").format(count=0))
        self.lbl_picked_count.setStyleSheet("color: #00ffcc; font-weight: bold; margin-right: 10px;")
        pa_layout.addWidget(self.lbl_picked_count)
        
        self.btn_send_picked = QPushButton(self.tr("gal.send_workshop", "🚀 Send to Workshop"))
        self.btn_send_picked.clicked.connect(self.send_to_workshop)
        self.btn_send_picked.setStyleSheet("background-color: #ffaa00; color: black; font-weight: bold; border-radius: 4px; padding: 5px 10px;")
        pa_layout.addWidget(self.btn_send_picked)
        
        layout.addWidget(self.picker_actions)
        self.picker_actions.setVisible(False)
        
        layout.addStretch()
        
        # Pagination Right
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedSize(30, 30)
        self.btn_prev.clicked.connect(self.prev_page)
        
        self.lbl_page_info = QLabel("Page 1")
        self.lbl_page_info.setStyleSheet("color: white; padding: 0 10px;")
        
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedSize(30, 30)
        self.btn_next.clicked.connect(self.next_page)
        
        for btn in [self.btn_prev, self.btn_next]:
            btn.setStyleSheet("background-color: #333; color: white; border: 1px solid #555; border-radius: 4px;")
        
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.lbl_page_info)
        layout.addWidget(self.btn_next)
        
        return container

    def switch_to_albums(self):
        self.current_view_mode = self.VIEW_ALBUMS
        self.current_folder_filter = None
        self.current_page = 0
        self.page_size = 60 # Increased for responsive grid
        self.btn_back.setVisible(False)
        self.btn_picker_toggle.setVisible(True)
        self.lbl_title.setText("🖼️ Gallery Albums")
        self.refresh_grid()

    def toggle_picker_mode(self, checked):
        self.picker_mode = checked
        
        self.lbl_picked_count.setVisible(checked)
        self.btn_send_picked.setVisible(checked and len(self.picked_paths) > 0)
        self.lbl_status.setText(f"Picker Mode {'ACTIVE' if checked else 'OFF'}")

    def toggle_search_panel(self, checked):
        self.search_panel.setVisible(checked)
        if checked:
            # Setup/Update Auto-completer
            tags = self.db.get_all_tags()
            completer = QCompleter(tags)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setFilterMode(Qt.MatchContains)
            self.txt_tag_search.setCompleter(completer)
            
            self.txt_broad_search.setFocus()
        
        self.lbl_status.setText(f"Search Panel {'OPEN' if checked else 'CLOSED'}")

    def on_search_triggered(self):
        # 1. Broad Terms from txt_broad_search
        broad_str = self.txt_broad_search.text().strip()
        if broad_str:
            new_terms = [t.strip() for t in broad_str.split(",") if t.strip()]
            for nt in new_terms:
                if nt not in self.current_query_terms:
                    self.current_query_terms.append(nt)
            self.txt_broad_search.clear()

        # 2. Tag Terms from txt_tag_search
        tag_str = self.txt_tag_search.text().strip()
        if tag_str:
            new_tags = [t.strip() for t in tag_str.split(",") if t.strip()]
            for nt in new_tags:
                if nt not in self.current_query_tags:
                    self.current_query_tags.append(nt)
            self.txt_tag_search.clear()

        self.current_page = 0
        self.update_tag_chips()
        self.refresh_grid()
        
        self.lbl_status.setText(f"Filter active: {len(self.current_query_tags)} tags, {len(self.current_query_terms)} terms")

    def on_rating_changed(self, index):
        """Index 0 = All, 1 = 1 Star, etc."""
        self.current_rating_filter = index
        self.current_page = 0
        self.refresh_grid()
        
    def clear_all_filters(self):
        """Resets all search and rating filters."""
        self.current_query_tags = []
        self.current_query_terms = []
        self.current_rating_filter = 0
        
        # Reset Stars UI
        self.combo_rating.setCurrentIndex(0)
            
        self.txt_broad_search.clear()
        self.txt_tag_search.clear()
        
        self.update_tag_chips()
        self.current_page = 0
        self.refresh_grid()
        self.lbl_status.setText("All filters cleared.")

    def update_tag_chips(self):
        """Redraws the tag chips based on current_query_tags and current_query_terms."""
        # Clear existing chips
        while self.tags_layout.count():
            item = self.tags_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        if not self.current_query_tags and not self.current_query_terms:
            self.tags_container.setVisible(False)
            return

        self.tags_container.setVisible(True)
        idx = 0
        # Show exact tags
        for tag in self.current_query_tags:
            chip = TagChip(tag, color_Index=idx)
            chip.removed.connect(self.remove_tag_filter)
            self.tags_layout.addWidget(chip)
            idx += 1
            
        # Show broad search terms (with 's:' prefix for visual distinction)
        for term in self.current_query_terms:
            chip = TagChip(f"s:{term}", color_Index=idx)
            chip.removed.connect(self.remove_tag_filter)
            self.tags_layout.addWidget(chip)
            idx += 1

    def remove_tag_filter(self, text):
        """Removes a tag or search term from the active filter and refreshes."""
        if text.startswith("s:"):
            term = text[2:]
            if term in self.current_query_terms:
                self.current_query_terms.remove(term)
        else:
            if text in self.current_query_tags:
                self.current_query_tags.remove(text)
        
        self.update_tag_chips()
        self.current_page = 0
        self.refresh_grid()

    def apply_rating_filter(self, val):
        self.current_rating_filter = val
        # Update button checks
        for i, btn in enumerate(self.rating_btns):
            btn.setChecked(i == val)
        
        self.current_page = 0
        self.refresh_grid()

    def update_rating_on_db(self, path, rating):
        """Signal handler for ClickableThumbnail.rating_changed."""
        self.db.update_file_rating(path, rating)
        self.lbl_status.setText(f"Updated rating for {os.path.basename(path)} to {rating} stars.")

    def load_album(self, path):
        self.current_view_mode = self.VIEW_IMAGES
        self.current_folder_filter = path
        self.current_page = 0
        self.page_size = 60 # Increased to fill large screens (10-12 cols)
        self.btn_back.setVisible(True)
        self.btn_picker_toggle.setVisible(True)
        self.lbl_title.setText(f"📂 {os.path.basename(path)}")
        self.refresh_grid()

    def load_custom_view(self, paths, title="Search Results"):
        """Loads a specific list of paths into the gallery grid."""
        self.current_view_mode = self.VIEW_CUSTOM
        self.custom_paths_source = paths
        self.current_page = 0
        self.page_size = 60
        self.btn_back.setVisible(True) # Allow going back directly to albums
        self.btn_picker_toggle.setVisible(True)
        self.lbl_title.setText(f"🔎 {title}")
        self.refresh_grid()

    def load_image_set(self, paths: list):
        """Standard interface for receiving sets from Librarian."""
        self.load_custom_view(paths, title="Imported Set")

    def open_custom_folder(self):
        """Allows user to select a folder and view its images directly."""
        folder = QFileDialog.getExistingDirectory(None, "Select Folder to View")
        if not folder:
            return
            
        # Scan for images locally
        image_extensions = ('.png', '.jpg', '.jpeg', '.webp')
        local_paths = []
        try:
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(image_extensions):
                        local_paths.append(os.path.join(root, f))
            
            if local_paths:
                self.load_custom_view(sorted(local_paths), title=os.path.basename(folder))
            else:
                self.lbl_status.setText(f"No images found in {folder}")
        except Exception as e:
            print(f"Error scanning local folder: {e}")
            self.lbl_status.setText("Error opening folder.")

    def refresh_grid(self):
        """Loads items for the current page based on View Mode."""
        # Clean current grid
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        self.active_widgets = [] # Clear tracking list
                
        self.lbl_status.setText(f"Loading...")
        offset = self.current_page * self.page_size
        
        if self.current_view_mode == self.VIEW_ALBUMS:
            self._load_albums_grid(offset)
        elif self.current_view_mode == self.VIEW_CUSTOM:
            self._load_custom_grid(offset)
        else:
            self._load_images_grid(offset)

    def _load_albums_grid(self, offset):
        # Albums don't have ratings at the folder level for now
        total, albums = self.db.get_folders_paginated(limit=self.page_size, offset=offset)
        self.total_items = total
        
        cols = 5
        for i, album in enumerate(albums):
            row = i // cols
            col = i % cols
            card = FolderCard(album['path'], album['name'], album['cover'], album['count'])
            
            # Use loader for cover image if available
            if album['cover']:
                img = self.loader.get_thumbnail_image(album['cover'], QSize(130, 120))
                if img:
                    card.setCoverPixmap(QPixmap.fromImage(img))
            
            card.clicked.connect(self.load_album)
            # self.grid_layout.addWidget(card, row, col) -> Defer to reflow
            self.active_widgets.append(card)
            
        self.reflow_grid()
            
        self.update_pagination_controls(total)
        self.lbl_status.setText(f"Loaded {len(albums)} albums.")

    def _load_custom_grid(self, offset):
        # Note: Custom view (local folder) doesn't support DB ratings directly yet
        # unless files are in DB. For now, we'll just show them with 0 stars.
        total_count = len(self.custom_paths_source)
        end = min(offset + self.page_size, total_count)
        paths = self.custom_paths_source[offset:end]
        
        self.total_items = total_count
        self.current_paths = paths # Store for viewer navigation
        
        # cols = 5
        for i, path in enumerate(paths):
            
            # Fetch rating from DB if available
            rating = self.db.get_file_rating(path)
            
            thumb = ClickableThumbnail(path, auto_load=False, rating=rating)
            thumb.setFixedSize(130, 130)
            
            # Check loader for immediate cache hit
            img = self.loader.get_thumbnail_image(path, QSize(130, 130))
            if img:
                thumb.setPixmap(QPixmap.fromImage(img))
            
            # Apply selection state if in Picker Mode
            if path in self.picked_paths:
                thumb.setSelected(True)
            
            thumb.clicked.connect(self.on_thumbnail_clicked)
            thumb.rating_changed.connect(self.update_rating_on_db)
            self.active_widgets.append(thumb)
            
        self.reflow_grid()
            
        self.update_pagination_controls(total_count)
        self.lbl_status.setText(f"Displaying {len(paths)} (of {total_count}) results.")
        self.prefetch_neighbors()

    def _load_images_grid(self, offset):
        # Prepare query arg if filtering by folder or rating
        folder_arg = f"path:{self.current_folder_filter}" if self.current_folder_filter else ""
        rating_arg = f"rating:{self.current_rating_filter}" if self.current_rating_filter > 0 else ""
        
        # Combine filters in query string
        full_query = f"{folder_arg} {rating_arg}".strip()
        
        total_count, results = self.db.search_files_paginated(
            query=full_query,
            tags=self.current_query_tags,
            search_terms=self.current_query_terms,
            limit=self.page_size,
            offset=offset
        )
        self.total_items = total_count
        # Results is list of (path, rating)
        self.current_paths = [r[0] for r in results] 
        
        # cols = 5
        for i, (path, rating) in enumerate(results):
            
            thumb = ClickableThumbnail(path, auto_load=False, rating=rating)
            thumb.setFixedSize(130, 130)
            
            # Check loader
            img = self.loader.get_thumbnail_image(path, QSize(130, 130))
            if img:
                thumb.setPixmap(QPixmap.fromImage(img))
            
            # Apply selection state if in Picker Mode
            if path in self.picked_paths:
                thumb.setSelected(True)
            
            thumb.clicked.connect(self.on_thumbnail_clicked)
            thumb.rating_changed.connect(self.update_rating_on_db)
            self.active_widgets.append(thumb)
            
        self.reflow_grid()
            
        self.update_pagination_controls(total_count)
        self.lbl_status.setText(f"Loaded {len(self.current_paths)} images.")
        self.prefetch_neighbors()

    @Slot(str, QImage)
    def on_thumbnail_ready(self, path, image):
        """Called when any module-wide thumbnail finishes loading."""
        if not image: 
            # print(f"[Gallery] Recv NULL image for {os.path.basename(path)}")
            return
        
        pixmap = QPixmap.fromImage(image)
        matched = False
        
        # Find the widget in the grid that wants this path
        for i in range(self.grid_layout.count()):
            widget = self.grid_layout.itemAt(i).widget()
            if isinstance(widget, ClickableThumbnail) and widget.path == path:
                widget.setPixmap(pixmap)
                matched = True
                break
            elif isinstance(widget, FolderCard) and widget.cover_path == path:
                widget.setCoverPixmap(pixmap)
                matched = True
                break
        
        # if not matched:
        #    print(f"[Gallery] Recv image for {os.path.basename(path)} but NO WIDGET MATCHED")

    def prefetch_neighbors(self):
        """Silently requests thumbnails for next and previous pages."""
        nearby_paths = []
        
        # Pages to prefetch (-2 to +2)
        target_pages = [self.current_page - 1, self.current_page + 1, self.current_page + 2]
        
        for p in target_pages:
            if p < 0: continue
            offset = p * self.page_size
            
            if self.current_view_mode == self.VIEW_CUSTOM:
                if offset < len(self.custom_paths_source):
                    end = min(offset + self.page_size, len(self.custom_paths_source))
                    nearby_paths.extend(self.custom_paths_source[offset:end])
            else:
                # For DB views, extract only the paths from result tuples
                folder_arg = f"path:{self.current_folder_filter}" if self.current_folder_filter else ""
                rating_arg = f"rating:{self.current_rating_filter}" if self.current_rating_filter > 0 else ""
                full_query = f"{folder_arg} {rating_arg}".strip()
                
                _, results = self.db.search_files_paginated(
                    query=full_query,
                    tags=self.current_query_tags,
                    search_terms=self.current_query_terms,
                    limit=self.page_size,
                    offset=offset
                )
                nearby_paths.extend([r[0] for r in results])

        # Trigger loader for all nearby paths (won't emit signal if not needed/cached)
        for path in nearby_paths:
            self.loader.get_thumbnail_image(path)

    def on_thumbnail_clicked(self, path):
        # Update Selection State Logic
        if self.picker_mode:
            # Toggle Selection
            norm_path = os.path.normpath(path)
            if norm_path in self.picked_paths:
                self.picked_paths.remove(norm_path)
            else:
                self.picked_paths.add(norm_path)
            # Update visual state
            for i in range(self.grid_layout.count()):
                widget = self.grid_layout.itemAt(i).widget()
                if isinstance(widget, ClickableThumbnail) and widget.path == norm_path:
                    widget.setSelected(norm_path in self.picked_paths)
                    break
        else:
            # Single Select Logic
            self.current_selected_path = path
            # Update Visual Highlight
            for i in range(self.grid_layout.count()):
                widget = self.grid_layout.itemAt(i).widget()
                if isinstance(widget, ClickableThumbnail):
                    widget.setSelected(widget.path == path)

        # ALWAYS update the Edit Panel (Blue Zone)
        self.update_properties_panel()

    def update_properties_panel(self, path=None): # Path arg deprecated but kept for compat
        """Refreshes the 'Edit Selection' Blue Zone."""
        
        # Case A: Picker Mode with Multiple Items
        if self.picker_mode:
            count = len(self.picked_paths)
            self.lbl_selected_info.setText(f"{count} images selected")
            self.btn_view_image.setVisible(False)
            self.lbl_selected_rating.setText("") # Clear rating in picker mode
            
            # Clear Tags List
            while self.selected_tags_layout.count():
                item = self.selected_tags_layout.takeAt(0)
                if item.widget(): item.widget().deleteLater()

        # Case B: Single Selection
        else:
            if hasattr(self, 'current_selected_path') and self.current_selected_path:
                path = self.current_selected_path
                self.lbl_selected_info.setText(os.path.basename(path))
                self.btn_view_image.setVisible(True)

                # Update Rating Display
                rating = self.db.get_file_rating(path)
                self.lbl_selected_rating.setText("★" * rating if rating > 0 else "☆☆☆☆☆")
                
                # Show Tags for this single file
                tags = self.db.get_tags_for_file(path)
                while self.selected_tags_layout.count():
                    item = self.selected_tags_layout.takeAt(0)
                    if item.widget(): item.widget().deleteLater()
                    
                for tag in tags:
                    chip = TagChip(tag)
                    chip.removed.connect(lambda t=tag, p=path: self.remove_tag_from_current_image(t, p))
                    self.selected_tags_layout.addWidget(chip)
            else:
                self.lbl_selected_info.setText("No image selected")
                self.btn_view_image.setVisible(False)
                self.lbl_selected_rating.setText("")
                
    def cycle_selected_rating(self):
        """Cycles rating for the currently selected image."""
        if not hasattr(self, 'current_selected_path') or not self.current_selected_path:
            return
            
        path = self.current_selected_path
        current = self.db.get_file_rating(path)
        new_rating = (current + 1) % 6
        
        if self.db.update_file_rating(path, new_rating):
            self.lbl_selected_rating.setText("★" * new_rating if new_rating > 0 else "☆☆☆☆☆")
            # Update thumbnail in grid
            for i in range(self.grid_layout.count()):
                w = self.grid_layout.itemAt(i).widget()
                if isinstance(w, ClickableThumbnail) and w.path == path:
                    w.setRating(new_rating)
                    break 


    def remove_tag_from_current_image(self, tag, path):
        # Remove from DB
        success = self.db.remove_tag_from_file(path, tag)
        if success:
            self.update_properties_panel(path) # Refresh UI
            self.lbl_status.setText(f"Removed tag '{tag}'")
        else:
            self.lbl_status.setText("Failed to remove tag.")

    def _get_full_context_paths(self):
        """Returns the complete list of paths for the current view, ignoring pagination."""
        if self.current_view_mode == self.VIEW_CUSTOM:
            return list(self.custom_paths_source)
            
        elif self.current_view_mode == self.VIEW_IMAGES:
            # Reconstruct filters same as _load_images_grid
            folder_arg = f"path:{self.current_folder_filter}" if self.current_folder_filter else ""
            rating_arg = f"rating:{self.current_rating_filter}" if self.current_rating_filter > 0 else ""
            full_query = f"{folder_arg} {rating_arg}".strip()
            
            # Fetch ALL results (limit=100000)
            _, results = self.db.search_files_paginated(
                query=full_query,
                tags=self.current_query_tags,
                search_terms=self.current_query_terms,
                limit=100000, 
                offset=0
            )
            return [r[0] for r in results]
            
        return []

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.refresh_grid()

    def next_page(self):
        # We should calculate total pages ideally but for now simple increment
        self.current_page += 1
        self.refresh_grid()

    def send_to_workshop(self):
        if self.picker_mode:
            if not self.picked_paths:
                return
            paths_to_send = list(self.picked_paths)
            # Reset Picker Mode after sending
            self.picked_paths = set()
            self.picker_mode = False
            self.btn_picker_toggle.setChecked(False)
            self.lbl_picked_count.setVisible(False)
            self.btn_send_picked.setVisible(False)
            self.lbl_status.setText("Sent selection to Workshop.")
            self.refresh_grid() # To clear visual selection
            self.request_open_workshop.emit(paths_to_send)
        else:
            # Legacy/Single Page behavior (fallback if somehow triggered)
            if not self.current_paths:
                return
            self.request_open_workshop.emit(self.current_paths)

    def update_pagination_controls(self, total):
        self.total_items = total
        total_pages = (total + self.page_size - 1) // self.page_size
        if total_pages == 0: total_pages = 1
        
        self.lbl_page_info.setText(f"Page {self.current_page + 1} / {total_pages}")
        
        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled((self.current_page + 1) < total_pages)

    def eventFilter(self, source, event):
        if source == self.grid_container and event.type() == QEvent.Resize:
            self.reflow_grid()
        return super().eventFilter(source, event)

    def reflow_grid(self):
        """calculate optimal columns and position widgets."""
        if not self.active_widgets: return
        
        # Determine item width based on first item (FolderCard or Thumbnail)
        sample = self.active_widgets[0]
        # FolderCard is 150, Thumb is 130. 
        # Add spacing (15) + margins logic
        item_w = sample.width() + 15 # + spacing
        
        aval_w = self.grid_container.width()
        
        # Calculate new cols (min 1)
        new_cols = max(1, aval_w // item_w)
        
        # Always reflow if calls explicitly (active_widgets logic depends on it)
        # But we could check if new_cols != self.current_cols to save performance
        # self.current_cols = new_cols
        
        # Re-layout
        # We need to remove them from layout first? 
        # No, addWidget moves them if they are already in layout.
        
        for i, widget in enumerate(self.active_widgets):
            row = i // new_cols
            col = i % new_cols
            self.grid_layout.addWidget(widget, row, col)
            
        # Debug / Status Update
        self.lbl_status.setText(f"Items: {len(self.active_widgets)} | Width: {aval_w} | Cols: {new_cols}")
