from PySide6.QtWidgets import (QWidget, QGridLayout, QScrollArea, QVBoxLayout, QLabel, 
                               QPushButton, QHBoxLayout)
from PySide6.QtCore import Qt, Slot, QSize
from PySide6.QtGui import QImage, QPixmap

from modules.gallery.logic.loader import get_loader
from .components import FolderCard, ClickableThumbnail

class GalleryGrid(QWidget):
    """
    Main display area. Swaps between Album Grid and Image Grid based on state.
    """
    def __init__(self, state, query_engine, parent=None):
        super().__init__(parent)
        self.state = state
        self.engine = query_engine
        self.loader = get_loader()
        
        self.init_ui()
        
        # Connections
        self.state.view_mode_changed.connect(lambda _: self.refresh())
        self.state.filter_changed.connect(lambda: self.refresh())
        self.state.page_changed.connect(lambda _: self.refresh())
        self.state.selection_changed.connect(self.update_selection_visuals)
        
        self.loader.thumbnail_ready.connect(self.on_thumbnail_loaded)
        
        # Load initial
        self.refresh()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Header (Title)
        self.lbl_title = QLabel("Gallery")
        self.lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: white; padding: 15px;")
        layout.addWidget(self.lbl_title)
        
        # Scroll Area
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: #111; border: none;")
        
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid_layout.setSpacing(15)
        self.grid_layout.setContentsMargins(15, 15, 15, 15)
        
        self.scroll.setWidget(self.grid_container)
        layout.addWidget(self.scroll)
        
        # Footer (Pagination)
        footer = QWidget()
        f_layout = QHBoxLayout(footer)
        
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedSize(40, 30)
        self.btn_prev.clicked.connect(self.prev_page)
        
        self.lbl_page = QLabel("Page 1")
        self.lbl_page.setStyleSheet("color: #888;")
        
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedSize(40, 30)
        self.btn_next.clicked.connect(self.next_page)
        
        f_layout.addStretch()
        f_layout.addWidget(self.btn_prev)
        f_layout.addWidget(self.lbl_page)
        f_layout.addWidget(self.btn_next)
        f_layout.addStretch()
        
        layout.addWidget(footer)
        
    def refresh(self):
        # 1. Clear Grid
        self.clear_grid()
        
        # 2. Fetch Data
        if self.state.mode == self.state.VIEW_ALBUMS:
            self.lbl_title.setText("📂 Albums")
            self.load_albums()
        else:
            name = self.state.current_folder if self.state.current_folder else "All Images"
            self.lbl_title.setText(f"🖼️ {name}")
            self.load_images()
            
    def clear_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget: widget.deleteLater()
            
    def load_albums(self):
        total, albums = self.engine.fetch_albums(page=self.state._page, page_size=self.state._page_size)
        self.state.set_total_items(total)
        self.update_pagination_ui()
        
        cols = 5
        for i, album in enumerate(albums):
            card = FolderCard(album['path'], album['name'], album['cover'], album['count'])
            card.clicked.connect(lambda p=album['path']: self.state.set_mode(self.state.VIEW_IMAGES, folder=p))
            
            # Request cover
            if album['cover']:
                img = self.loader.get_thumbnail_image(album['cover'])
                if img: card.set_cover_image(QPixmap.fromImage(img))
                
            self.grid_layout.addWidget(card, i // cols, i % cols)
            
    def load_images(self):
        total, results = self.engine.fetch_images(self.state, page_size=self.state._page_size)
        self.state.set_total_items(total)
        self.update_pagination_ui()
        
        cols = 6 # Slightly denser for images
        for i, (path, rating) in enumerate(results):
            thumb = ClickableThumbnail(path, rating=rating, auto_load=False)
            
            # Connect signals
            thumb.clicked.connect(self.on_image_clicked)
            thumb.rating_changed.connect(lambda p, r: self.engine.update_rating(p, r))
            
            # Selection state
            if path in self.state.selected_paths:
                thumb.setSelected(True)
            
            # Request Image
            img = self.loader.get_thumbnail_image(path)
            if img: thumb.setPixmap(QPixmap.fromImage(img))
            
            self.grid_layout.addWidget(thumb, i // cols, i % cols)
            
    def update_pagination_ui(self):
        current = self.state._page + 1
        total_pages = (self.state._total_items // self.state._page_size) + 1
        if self.state._total_items == 0: total_pages = 1
        
        self.lbl_page.setText(f"Page {current} / {total_pages}")
        self.btn_prev.setEnabled(current > 1)
        self.btn_next.setEnabled(current < total_pages)
        
    def prev_page(self):
        if self.state._page > 0:
            self.state.set_page(self.state._page - 1)
            
    def next_page(self):
        total_pages = (self.state._total_items // self.state._page_size) + 1
        if self.state._page < total_pages - 1:
            self.state.set_page(self.state._page + 1)
            
    def on_image_clicked(self, path):
        if self.state.picker_active:
            self.state.toggle_selection(path)
            # Visual update handled by signal/refresh or explicit update
            # We can optimize by finding the specific widget, but refresh is safer for sync
            self.update_single_widget_selection(path)
        else:
            # Open AdvancedViewer
            from .viewer_window import AdvancedViewer
            
            # Fetch context paths (current displayed images)
            current_paths = []
            for i in range(self.grid_layout.count()):
                w = self.grid_layout.itemAt(i).widget()
                if isinstance(w, ClickableThumbnail):
                    current_paths.append(w.path)
            
            if path in current_paths:
                idx = current_paths.index(path)
                viewer = AdvancedViewer(current_paths, start_index=idx, parent=self)
                viewer.exec()
            
    def update_single_widget_selection(self, path):
        # Find widget
        for i in range(self.grid_layout.count()):
            w = self.grid_layout.itemAt(i).widget()
            if isinstance(w, ClickableThumbnail) and w.path == path:
                w.setSelected(path in self.state.selected_paths)
                break
                
    def update_selection_visuals(self):
        if not self.state.picker_active:
            # Clear all selections
            for i in range(self.grid_layout.count()):
                w = self.grid_layout.itemAt(i).widget()
                if isinstance(w, ClickableThumbnail):
                    w.setSelected(False)
        else:
            # Sync all
            for i in range(self.grid_layout.count()):
                w = self.grid_layout.itemAt(i).widget()
                if isinstance(w, ClickableThumbnail):
                    w.setSelected(w.path in self.state.selected_paths)

    @Slot(str, QImage)
    def on_thumbnail_loaded(self, path, image):
        if not image: return
        pix = QPixmap.fromImage(image)
        
        # Iterate and find
        for i in range(self.grid_layout.count()):
            w = self.grid_layout.itemAt(i).widget()
            if isinstance(w, FolderCard) and w.cover_path == path:
                w.set_cover_image(pix)
            elif isinstance(w, ClickableThumbnail) and w.path == path:
                w.setPixmap(pix)
