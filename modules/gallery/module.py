from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QGridLayout, QFrame, QComboBox, QFileDialog)
from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QPixmap, QMouseEvent, QIcon
from core.base_module import BaseModule
from modules.librarian.logic.db_manager import DatabaseManager
from modules.librarian.module import ClickableThumbnail  # Reusing the thumb component
import os

class FolderCard(QFrame):
    clicked = Signal(str)
    
    def __init__(self, path, name, cover_path, count):
        super().__init__()
        self.path = path
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
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self.lbl_cover.setPixmap(pix.scaled(130, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation))
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

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.path)

class GalleryModule(BaseModule):
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

    @property
    def name(self):
        return "The Gallery"

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
        
        self.lbl_page_info = QLabel("Page 1 / ?")
        self.lbl_page_info.setStyleSheet("color: #eee; font-weight: bold;")
        top_bar.addWidget(self.lbl_page_info)
        
        self.btn_prev = QPushButton("◀ Prev")
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_prev.setStyleSheet("padding: 5px 15px; font-weight: bold;")
        
        self.btn_next = QPushButton("Next ▶")
        self.btn_next.clicked.connect(self.next_page)
        self.btn_next.setStyleSheet("padding: 5px 15px; font-weight: bold;")
        
        top_bar.addWidget(self.btn_prev)
        top_bar.addWidget(self.btn_next)
        
        layout.addLayout(top_bar)
        
        # --- Grid Area ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background-color: #111; border: none;")
        
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(15)
        self.grid_layout.setContentsMargins(20, 20, 20, 20)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        scroll.setWidget(self.grid_container)
        layout.addWidget(scroll)
        
        # --- Footer ---
        self.lbl_status = QLabel("Ready.")
        self.lbl_status.setStyleSheet("color: #888;")
        layout.addWidget(self.lbl_status)
        
        # Initial Load
        self.refresh_grid()
        
        return self.view

    def switch_to_albums(self):
        self.current_view_mode = self.VIEW_ALBUMS
        self.current_folder_filter = None
        self.current_page = 0
        self.page_size = 20 # 5x4 Grid
        self.btn_back.setVisible(False)
        self.lbl_title.setText("🖼️ Gallery Albums")
        self.refresh_grid()

    def load_album(self, path):
        self.current_view_mode = self.VIEW_IMAGES
        self.current_folder_filter = path
        self.current_page = 0
        self.page_size = 20 # 5x4 Grid matches albums
        self.btn_back.setVisible(True)
        self.lbl_title.setText(f"📂 {os.path.basename(path)}")
        self.refresh_grid()

    def load_custom_view(self, paths, title="Search Results"):
        """Loads a specific list of paths into the gallery grid."""
        self.current_view_mode = self.VIEW_CUSTOM
        self.custom_paths_source = paths
        self.current_page = 0
        self.page_size = 20
        self.btn_back.setVisible(True) # Allow going back directly to albums
        self.lbl_title.setText(f"🔍 {title}")
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
                
        self.lbl_status.setText(f"Loading...")
        offset = self.current_page * self.page_size
        
        if self.current_view_mode == self.VIEW_ALBUMS:
            self._load_albums_grid(offset)
        elif self.current_view_mode == self.VIEW_CUSTOM:
            self._load_custom_grid(offset)
        else:
            self._load_images_grid(offset)

    def _load_albums_grid(self, offset):
        total, albums = self.db.get_folders_paginated(limit=self.page_size, offset=offset)
        self.total_items = total
        
        cols = 5
        for i, album in enumerate(albums):
            row = i // cols
            col = i % cols
            card = FolderCard(album['path'], album['name'], album['cover'], album['count'])
            card.clicked.connect(self.load_album)
            self.grid_layout.addWidget(card, row, col)
            
        self.update_pagination_controls(total)
        self.lbl_status.setText(f"Loaded {len(albums)} albums.")

    def _load_custom_grid(self, offset):
        # Slice the custom source list
        total_count = len(self.custom_paths_source)
        end = min(offset + self.page_size, total_count)
        paths = self.custom_paths_source[offset:end]
        
        self.total_items = total_count
        self.current_paths = paths # Store for viewer navigation
        
        cols = 5
        for i, path in enumerate(paths):
            row = i // cols
            col = i % cols
            
            thumb = ClickableThumbnail(path)
            thumb.setFixedSize(130, 130)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(130, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            thumb.clicked.connect(self.on_thumbnail_clicked)
            self.grid_layout.addWidget(thumb, row, col)
            
        self.update_pagination_controls(total_count)
        self.lbl_status.setText(f"Displaying {len(paths)} (of {total_count}) results.")

    def _load_images_grid(self, offset):
        # Prepare query arg if filtering by folder
        query_arg = f"path:{self.current_folder_filter}" if self.current_folder_filter else None
        
        total_count, paths = self.db.search_files_paginated(
            query=query_arg,
            tags=self.current_query_tags,
            limit=self.page_size,
            offset=offset
        )
        self.total_items = total_count
        self.current_paths = paths # Store for viewer navigation
        
        cols = 5
        for i, path in enumerate(paths):
            row = i // cols
            col = i % cols
            
            thumb = ClickableThumbnail(path)
            thumb.setFixedSize(130, 130)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(130, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            thumb.clicked.connect(self.on_thumbnail_clicked)
            self.grid_layout.addWidget(thumb, row, col)
            
        self.update_pagination_controls(total_count)
        self.lbl_status.setText(f"Loaded {len(paths)} images.")

    def on_thumbnail_clicked(self, path):
        # Find index in current page list
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
        # Check if we have more pages
        # if (self.current_page + 1) * self.page_size < self.total_items:
        self.current_page += 1
        self.refresh_grid()

    def update_pagination_controls(self, total):
        self.total_items = total
        total_pages = (total + self.page_size - 1) // self.page_size
        if total_pages == 0: total_pages = 1
        
        self.lbl_page_info.setText(f"Page {self.current_page + 1} / {total_pages}")
        
        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled((self.current_page + 1) < total_pages)
