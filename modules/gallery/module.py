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
            self.lbl_cover.setText("📁 Empty")
            
        layout.addWidget(self.lbl_cover)
        
        # Folder Name
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        self.lbl_name.setAlignment(Qt.AlignCenter)
        # Elide text if too long
        # self.lbl_name.setWordWrap(True) 
        layout.addWidget(self.lbl_name)
        
        # Count
        self.lbl_count = QLabel(f"{count} items")
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
        
        self.view = QWidget()
        layout = QVBoxLayout(self.view)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # --- Top Bar (Controls) ---
        top_bar = QHBoxLayout()
        
        self.btn_back = QPushButton("🔙 Albums")
        self.btn_back.clicked.connect(self.switch_to_albums)
        self.btn_back.setStyleSheet("background-color: #444; color: white; padding: 5px 10px; border-radius: 5px;")
        self.btn_back.setVisible(False) # Hidden by default
        top_bar.addWidget(self.btn_back)
        
        self.lbl_title = QLabel("🖼️ Gallery Albums")
        self.lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #00ffcc;")
        top_bar.addWidget(self.lbl_title)
        
        self.btn_open_local = QPushButton("📂 Open Folder")
        self.btn_open_local.clicked.connect(self.open_custom_folder)
        self.btn_open_local.setStyleSheet("background-color: #333; color: #00ffcc; padding: 5px 10px; border-radius: 5px; font-weight: bold;")
        top_bar.addWidget(self.btn_open_local)
        
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # --- Middle Area: Grid + Right Sidebar ---
        self.middle_layout = QHBoxLayout()
        
        # 1. Grid Area (Left)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background-color: #111; border: none;")
        
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(15)
        self.grid_layout.setContentsMargins(10, 10, 10, 10)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        # Hook resize event via EventFilter for robustness
        self.grid_container.installEventFilter(self)
        
        scroll.setWidget(self.grid_container)
        self.middle_layout.addWidget(scroll, 1) # Stretch ratio 1

        # 2. Right Sidebar (Panel)
        self.right_panel = QFrame()
        self.right_panel.setFixedWidth(200)
        self.right_panel.setStyleSheet("""
            QFrame { background-color: #1a1a1a; border-left: 1px solid #333; }
        """)
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setAlignment(Qt.AlignTop)
        self.right_layout.setContentsMargins(10, 20, 10, 10)
        self.right_layout.setSpacing(15)
        
        self.btn_picker_toggle = QPushButton("🎯 Picker Mode")
        self.btn_picker_toggle.setCheckable(True)
        self.btn_picker_toggle.clicked.connect(self.toggle_picker_mode)
        self.btn_picker_toggle.setFixedHeight(40)
        self.btn_picker_toggle.setStyleSheet("""
            QPushButton { background-color: #333; color: #888; border-radius: 5px; font-weight: bold; border: 1px solid #444; }
            QPushButton:checked { background-color: #224433; color: #00ffcc; border-color: #00ffcc; }
        """)
        self.right_layout.addWidget(self.btn_picker_toggle)

        self.btn_search_toggle = QPushButton("🕵️ Search & Filter")
        self.btn_search_toggle.setCheckable(True)
        self.btn_search_toggle.clicked.connect(self.toggle_search_panel)
        self.btn_search_toggle.setFixedHeight(40)
        self.btn_search_toggle.setStyleSheet("""
            QPushButton { background-color: #333; color: #eee; border-radius: 5px; font-weight: bold; border: 1px solid #444; }
            QPushButton:checked { background-color: #222244; color: #aaaaff; border-color: #aaaaff; }
        """)
        self.right_layout.addWidget(self.btn_search_toggle)

        # Unified Search Panel
        self.search_panel = QWidget()
        self.search_panel_layout = QVBoxLayout(self.search_panel)
        self.search_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.search_panel_layout.setSpacing(10)
        self.search_panel.setVisible(False)
        self.right_layout.addWidget(self.search_panel)

        # 1. Broad Search
        self.lbl_broad = QLabel("🌐 Broad Search")
        self.lbl_broad.setStyleSheet("color: #888; font-size: 10px; font-weight: bold;")
        self.search_panel_layout.addWidget(self.lbl_broad)
        
        self.txt_broad_search = QLineEdit()
        self.txt_broad_search.setPlaceholderText("Keywords, filename...")
        self.txt_broad_search.setStyleSheet("background-color: #222; color: #fff; border: 1px solid #444; padding: 6px; border-radius: 4px;")
        self.txt_broad_search.returnPressed.connect(self.on_search_triggered)
        self.search_panel_layout.addWidget(self.txt_broad_search)

        # 2. Tag Search
        self.lbl_tag_lbl = QLabel("🏷️ Tag Search")
        self.lbl_tag_lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold;")
        self.search_panel_layout.addWidget(self.lbl_tag_lbl)
        
        self.txt_tag_search = QLineEdit()
        self.txt_tag_search.setPlaceholderText("Exact tags...")
        self.txt_tag_search.setStyleSheet("background-color: #222; color: #fff; border: 1px solid #444; padding: 6px; border-radius: 4px;")
        self.txt_tag_search.returnPressed.connect(self.on_search_triggered)
        self.search_panel_layout.addWidget(self.txt_tag_search)

        # 3. Active Chips (Shared area for both)
        self.tags_container = QWidget()
        self.tags_layout = FlowLayout(self.tags_container)
        self.search_panel_layout.addWidget(self.tags_container)

        # 4. Rating Filter
        self.lbl_rate_lbl = QLabel("⭐ Quality Filter")
        self.lbl_rate_lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold;")
        self.search_panel_layout.addWidget(self.lbl_rate_lbl)

        self.rating_filter_container = QWidget()
        self.rating_filter_layout = QVBoxLayout(self.rating_filter_container)
        self.rating_filter_layout.setContentsMargins(0, 0, 0, 0)
        self.rating_filter_layout.setSpacing(5)
        
        self.rating_btns = []
        for i in range(6): # 0 (All) to 5 Stars
            btn = QPushButton("ALL STARS" if i == 0 else "⭐" * i)
            btn.setCheckable(True)
            if i == 0: btn.setChecked(True)
            btn.setFixedHeight(25)
            btn.setStyleSheet("""
                QPushButton { background-color: #222; color: #ffcc00; border-radius: 3px; font-size: 10px; }
                QPushButton:checked { background-color: #ffcc00; color: black; font-weight: bold; }
            """)
            btn.clicked.connect(lambda checked, val=i: self.apply_rating_filter(val))
            self.rating_btns.append(btn)
            self.rating_filter_layout.addWidget(btn)
        
        self.search_panel_layout.addWidget(self.rating_filter_container)

        # 5. Reset Button
        self.btn_clear_all = QPushButton("🔄 Clear All Filters")
        self.btn_clear_all.clicked.connect(self.clear_all_filters)
        self.btn_clear_all.setStyleSheet("""
            background-color: #442222; color: #ff5555; padding: 5px; border-radius: 4px; font-weight: bold; margin-top: 10px;
        """)
        self.search_panel_layout.addWidget(self.btn_clear_all)

        # Picker specific count
        self.lbl_picked_count = QLabel("0 selected")
        self.lbl_picked_count.setAlignment(Qt.AlignCenter)
        self.lbl_picked_count.setStyleSheet("color: #00ffcc; font-weight: bold; margin-top: 10px;")
        self.lbl_picked_count.setVisible(False)
        self.right_layout.addWidget(self.lbl_picked_count)

        self.btn_send_picked = QPushButton("🚀 SEND TO WORKSHOP")
        self.btn_send_picked.setFixedHeight(50)
        self.btn_send_picked.clicked.connect(self.send_to_workshop)
        self.btn_send_picked.setStyleSheet("""
            background-color: #ffaa00; color: black; border-radius: 8px; font-weight: bold; font-size: 11px;
        """)
        self.btn_send_picked.setVisible(False)
        self.right_layout.addWidget(self.btn_send_picked)
        
        # Spacer before pagination
        self.right_layout.addStretch()

        # Pagination Section
        pagination_group = QFrame()
        pagination_group.setStyleSheet("background-color: #222; border-radius: 5px; border: none;")
        pag_layout = QVBoxLayout(pagination_group)
        
        self.lbl_page_info = QLabel("Page 1 / ?")
        self.lbl_page_info.setAlignment(Qt.AlignCenter)
        self.lbl_page_info.setStyleSheet("color: #eee; font-weight: bold; margin-bottom: 5px; border: none;")
        pag_layout.addWidget(self.lbl_page_info)
        
        pag_btns = QHBoxLayout()
        self.btn_prev = QPushButton("◀")
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_prev.setStyleSheet("padding: 10px; font-weight: bold; border: 1px solid #444;")
        
        self.btn_next = QPushButton("▶")
        self.btn_next.clicked.connect(self.next_page)
        self.btn_next.setStyleSheet("padding: 10px; font-weight: bold; border: 1px solid #444;")
        
        pag_btns.addWidget(self.btn_prev)
        pag_btns.addWidget(self.btn_next)
        pag_layout.addLayout(pag_btns)
        
        self.right_layout.addWidget(pagination_group)
        
        self.middle_layout.addWidget(self.right_panel)
        layout.addLayout(self.middle_layout)
        
        # --- Footer ---
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setStyleSheet("color: #888; padding: 5px;")
        layout.addWidget(self.lbl_status)
        
        # Initial Load
        self.refresh_grid()
        
        return self.view

    def switch_to_albums(self):
        self.current_view_mode = self.VIEW_ALBUMS
        self.current_folder_filter = None
        self.current_page = 0
        self.page_size = 50 # Increased for responsive grid
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

    def clear_all_filters(self):
        """Resets all search and rating filters."""
        self.current_query_tags = []
        self.current_query_terms = []
        self.current_rating_filter = 0
        
        # Reset Stars UI
        for i, btn in enumerate(self.rating_btns):
            btn.setChecked(i == 0)
            
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
        self.page_size = 20 # 5x4 Grid matches albums
        self.btn_back.setVisible(True)
        self.btn_picker_toggle.setVisible(True)
        self.lbl_title.setText(f"📂 {os.path.basename(path)}")
        self.refresh_grid()

    def load_custom_view(self, paths, title="Search Results"):
        """Loads a specific list of paths into the gallery grid."""
        self.current_view_mode = self.VIEW_CUSTOM
        self.custom_paths_source = paths
        self.current_page = 0
        self.page_size = 20
        self.btn_back.setVisible(True) # Allow going back directly to albums
        self.btn_picker_toggle.setVisible(True)
        self.lbl_title.setText(f"🔎 {title}")
        self.refresh_grid()

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
        if self.picker_mode:
            # Toggle Selection
            norm_path = os.path.normpath(path)
            if norm_path in self.picked_paths:
                self.picked_paths.remove(norm_path)
            else:
                self.picked_paths.add(norm_path)
            
            # Update visual state of the clicked widget
            for i in range(self.grid_layout.count()):
                widget = self.grid_layout.itemAt(i).widget()
                if isinstance(widget, ClickableThumbnail) and widget.path == norm_path:
                    widget.setSelected(norm_path in self.picked_paths)
                    break
            
            # Update UI
            count = len(self.picked_paths)
            self.lbl_picked_count.setText(f"{count} selected")
            self.btn_send_picked.setVisible(count > 0)
            return

        # Regular Click: Find index in current page list
        try:
            idx = self.current_paths.index(path)
        except ValueError:
            idx = 0
            
        # Open Advanced Viewer
        from .viewer import AdvancedViewer
        viewer = AdvancedViewer(self.current_paths, start_index=idx, parent=self.view)
        viewer.exec()

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
