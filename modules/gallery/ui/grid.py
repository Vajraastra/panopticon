from PySide6.QtWidgets import (QWidget, QGridLayout, QScrollArea, QVBoxLayout, QLabel, 
                               QPushButton, QHBoxLayout)
from PySide6.QtCore import Qt, Slot, QSize
from core.locale_manager import LocaleManager
from core.library_async import LibraryReader, LibraryWriter
import logging
from PySide6.QtGui import QImage, QPixmap

from modules.gallery.logic.loader import ThumbnailLoader
from .components import FolderCard, ClickableThumbnail

class GalleryGrid(QWidget):
    """
    Main display area. Swaps between Album Grid and Image Grid based on state.
    """
    def __init__(self, state, query_engine, parent=None):
        super().__init__(parent)
        self.state = state
        self.engine = query_engine
        self.loader = ThumbnailLoader()
        self._closed = False
        self._loading = False
        self._image_bindings = {}
        self._pending_image_page = None
        self.reader = LibraryReader(self.engine.db.service, self)
        self.writer = LibraryWriter(self.engine.db.service, self)
        self.reader.result.connect(self.on_query_result)
        self.writer.result.connect(self.on_rating_result)
        
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
        self.btn_prev.setToolTip(LocaleManager().tr(
            "gallery.page_prev_tip", "Previous page of images."))
        self.btn_prev.setFixedSize(40, 30)
        self.btn_prev.clicked.connect(self.prev_page)
        
        self.lbl_page = QLabel("Page 1")
        self.lbl_page.setStyleSheet("color: #888;")
        
        self.btn_next = QPushButton("▶")
        self.btn_next.setToolTip(LocaleManager().tr(
            "gallery.page_next_tip", "Next page of images."))
        self.btn_next.setFixedSize(40, 30)
        self.btn_next.clicked.connect(self.next_page)
        
        f_layout.addStretch()
        f_layout.addWidget(self.btn_prev)
        f_layout.addWidget(self.lbl_page)
        f_layout.addWidget(self.btn_next)
        f_layout.addStretch()
        
        layout.addWidget(footer)
        
    def refresh(self):
        if self._closed:
            return
        # 1. Clear Grid
        self.clear_grid()
        self._image_bindings = {}
        self._pending_image_page = None
        
        # 2. Fetch Data
        self._loading = True
        self.btn_prev.setEnabled(False)
        self.btn_next.setEnabled(False)
        self.lbl_page.setText(LocaleManager().tr('gallery.db.loading', 'Loading…'))
        if self.state.mode == self.state.VIEW_ALBUMS:
            self.lbl_title.setText("📂 Albums")
            self.reader.request('get_folders_paginated', limit=self.state._page_size,
                                offset=self.state._page * self.state._page_size)
        else:
            name = self.state.current_folder if self.state.current_folder else "All Images"
            self.lbl_title.setText(f"🖼️ {name}")
            if self.state.mode == self.state.VIEW_CUSTOM:
                self._custom_filtered = self.engine.has_custom_filters(self.state)
                if self._custom_filtered:
                    self.reader.request('get_external_files_page',
                                        **self.engine.custom_query(self.state, self.state._page_size))
                    return
                self._custom_total = len(self.state._custom_paths)
                start = self.state._page * self.state._page_size
                self._custom_page = tuple(self.state._custom_paths[start:start + self.state._page_size])
                if self._custom_page:
                    self.reader.request('get_curation_snapshot', self._custom_page, include_revision=True)
                else:
                    self.reader.invalidate()
                    self.on_query_result(0, [], None)
            else:
                self.reader.request('search_files_paginated',
                                    **self.engine.image_query(self.state, self.state._page_size))

    def on_query_result(self, ticket, value, error):
        if self._closed:
            return
        self._loading = False
        if error is not None:
            logging.getLogger(__name__).warning('Gallery query failed: %s', error)
            reason = str(error)
            if 'external_selection_requires_staging' in reason or 'read_payload_too_large' in reason:
                self.lbl_page.setText(LocaleManager().tr('gallery.external_scope_limit',
                    'This filtered external set exceeds the supported request size. Use a smaller set.'))
            else:
                self.lbl_page.setText(LocaleManager().tr('gallery.db.read_error', 'Could not load library.'))
            self.lbl_page.setToolTip(reason)
            self.btn_prev.setEnabled(self.state._page > 0)
            self.btn_next.setEnabled(False)
            return
        if self._pending_image_page is not None:
            total, paths = self._pending_image_page
            self._pending_image_page = None
            self._image_bindings = dict(zip(paths, value))
            rows = [(path, record['rating'] or 0) for path, record in zip(paths, value)]
        elif self.state.mode == self.state.VIEW_CUSTOM and not self._custom_filtered:
            total = self._custom_total
            self._image_bindings = dict(zip(self._custom_page, value))
            rows = [(path, record['rating'] or 0)
                    for path, record in zip(self._custom_page, value)]
        else:
            total, rows = value
            if self.state.mode != self.state.VIEW_ALBUMS and rows:
                paths = tuple(path for path, _ in rows)
                self._pending_image_page = (total, paths)
                self._loading = True
                self.reader.request('get_curation_snapshot', paths, include_revision=True)
                return
        self.lbl_page.setToolTip('')
        self.state.set_total_items(total)
        if self.state._page >= self.state.total_pages:
            self.state.set_page(self.state.total_pages - 1)
            return
        self.clear_grid()
        self.update_pagination_ui()
        if self.state.mode == self.state.VIEW_ALBUMS:
            self.load_albums(rows)
        else:
            self.load_images(rows)
            
    def clear_grid(self):
        self.loader.invalidate()
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            widget = item.widget()
            if widget: widget.deleteLater()
            
    def load_albums(self, albums):
        cols = 5
        for i, album in enumerate(albums):
            card = FolderCard(album['path'], album['name'], album['cover'], album['count'])
            card.clicked.connect(lambda p=album['path']: self.state.set_mode(self.state.VIEW_IMAGES, folder=p))
            
            # Request cover
            if album['cover']:
                img = self._request_thumbnail(album['cover'], card.lbl_cover)
                if img: card.set_cover_image(QPixmap.fromImage(img))
                
            self.grid_layout.addWidget(card, i // cols, i % cols)
            
    def load_images(self, results):
        cols = 6 # Slightly denser for images
        for i, (path, rating) in enumerate(results):
            thumb = ClickableThumbnail(path, rating=rating, auto_load=False)
            
            # Connect signals
            thumb.clicked.connect(self.on_image_clicked)
            binding = self._image_bindings.get(path, {})
            thumb.rating_changed.connect(
                lambda p, r, row=binding: self.update_rating(p, r, binding=row))
            
            # Selection state
            if path in self.state.selected_paths:
                thumb.setSelected(True)
            
            # Request Image
            img = self._request_thumbnail(path, thumb)
            if img: thumb.setPixmap(QPixmap.fromImage(img))
            
            self.grid_layout.addWidget(thumb, i // cols, i % cols)
            
    def _request_thumbnail(self, path, label):
        try:
            return self.loader.get_thumbnail_image(path)
        except ValueError:
            label.setText(LocaleManager().tr('gallery.thumbnail_unavailable', 'Unavailable'))
            return None

    def update_pagination_ui(self):
        current = self.state._page + 1
        total_pages = self.state.total_pages
        
        self.lbl_page.setText(f"Page {current} / {total_pages}")
        self.btn_prev.setEnabled(current > 1)
        self.btn_next.setEnabled(current < total_pages)
        
    def prev_page(self):
        if self._loading:
            return
        if self.state._page > 0:
            self.state.set_page(self.state._page - 1)
            
    def next_page(self):
        if self._loading:
            return
        total_pages = self.state.total_pages
        if self.state._page < total_pages - 1:
            self.state.set_page(self.state._page + 1)

    def update_rating(self, path, rating, *, binding=None):
        if self._closed:
            return
        row = binding if binding is not None else self._image_bindings.get(path)
        if not row or row['id'] is None or not row.get('fingerprint'):
            self.on_rating_result(0, False, ValueError('gallery_unconfirmed_copy'))
            return
        self.writer.submit('edit_curation', path, rating=rating,
                           expected_copy_id=row['id'], expected_fingerprint=row['fingerprint'])

    def on_rating_result(self, ticket, value, error):
        if self._closed:
            return
        if error is not None or value is not True:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, LocaleManager().tr('gallery.db.title', 'Library'),
                                LocaleManager().tr('gallery.db.write_error', 'Could not save rating.'))
        self.refresh()

    def shutdown(self):
        self._closed = True
        self.reader.close()
        self.writer.close()
        self.loader.shutdown()
            
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
                viewer = AdvancedViewer(current_paths, start_index=idx, parent=self,
                                        service=self.engine.db.service)
                viewer.curation_saved.connect(self.refresh)
                viewer.exec()
                self.refresh()
            
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
        if self._closed: return
        pix = None if image.isNull() else QPixmap.fromImage(image)
        
        # Iterate and find
        for i in range(self.grid_layout.count()):
            w = self.grid_layout.itemAt(i).widget()
            if isinstance(w, FolderCard) and w.cover_path == path:
                if pix is None:
                    w.lbl_cover.setText(LocaleManager().tr('gallery.thumbnail_unavailable', 'Unavailable'))
                else:
                    w.set_cover_image(pix)
            elif isinstance(w, ClickableThumbnail) and w.path == path:
                if pix is None:
                    w.setText(LocaleManager().tr('gallery.thumbnail_unavailable', 'Unavailable'))
                else:
                    w.setPixmap(pix)
