from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, 
                               QFrame, QComboBox, QHBoxLayout, QCompleter)
from PySide6.QtCore import Qt, Signal
from core.locale_manager import LocaleManager
from core.library_async import LibraryReader
from core.components.tag_vocabulary import TagVocabulary
from core.components.ui_roles import section, primary, set_role
import logging
from core.components.tag_widgets import FlowLayout, TagChip # widget compartido en core (Regla #3)

class GallerySidebar(QWidget):
    """
    Sidebar Panel for Gallery.
    Handles user input for filters and navigation commands.
    """
    
    def __init__(self, state, query_engine, parent=None):
        super().__init__(parent)
        self.state = state
        self.engine = query_engine
        self.reader = LibraryReader(self.engine.db.service, self)
        
        self.init_ui()
        self.setup_completer()
        
    def init_ui(self):
        tr = LocaleManager().tr
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 10, 15, 15)
        layout.setSpacing(10)

        # 1. Origen (convención: origen → opciones → acciones)
        layout.addWidget(section(tr("gallery.sec.source", "Source")))
        self.btn_open = QPushButton(tr("gallery.open_folder", "Open folder…"))
        self.btn_open.setToolTip(tr(
            "gallery.open_folder_tip", "Opens a folder of images to browse here."))
        self.btn_open.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.btn_open)

        self.btn_back = QPushButton(tr("gallery.back_albums", "← Back to albums"))
        self.btn_back.setToolTip(tr(
            "gallery.back_albums_tip",
            "Back to the album view, leaving this folder's contents."))
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.clicked.connect(lambda: self.state.set_mode(self.state.VIEW_ALBUMS))
        self.btn_back.setVisible(False)
        layout.addWidget(self.btn_back)

        # 2. Filtros
        layout.addWidget(section(tr("gallery.sec.filters", "Search and filters")))
        self.txt_search = QLineEdit()
        self.txt_search.setToolTip(tr(
            "gallery.search_tip",
            "Filters by tags or metadata text. Type several tags to keep only the images "
            "that have them all. Put metadata text in double quotes; separate entries with commas."))
        self.txt_search.setPlaceholderText(tr("gallery.search_placeholder", "Library tags or \"text\"…"))
        self.txt_search.returnPressed.connect(self.on_search_submit)
        layout.addWidget(self.txt_search)

        self.combo_rating = QComboBox()
        self.combo_rating.setToolTip(tr(
            "gallery.rating_tip", "Leaves only the images with that rating or better."))
        self.combo_rating.addItems([tr("gallery.rating_all", "Any rating"),
                                    "⭐ 1+", "⭐⭐ 2+", "⭐⭐⭐ 3+", "⭐⭐⭐⭐ 4+", "⭐⭐⭐⭐⭐ 5"])
        self.combo_rating.currentIndexChanged.connect(lambda idx: self.state.set_rating_filter(idx))
        layout.addWidget(self.combo_rating)

        self.chip_container = QWidget()
        self.chip_layout = FlowLayout(self.chip_container)
        layout.addWidget(self.chip_container)

        self.btn_clear = QPushButton(tr("gallery.clear_filters", "Clear filters"))
        self.btn_clear.setToolTip(tr(
            "gallery.clear_filters_tip",
            "Clears the search and the rating at once, showing everything again."))
        self.btn_clear.clicked.connect(self.clear_all)
        layout.addWidget(self.btn_clear)

        layout.addStretch()

        # 3. Selección múltiple (antes «Picker Mode»; se confundía con Pickler)
        layout.addWidget(section(tr("gallery.sec.selection", "Selection")))
        self.btn_picker = QPushButton(tr("gallery.multi_select", "Multiple selection"))
        self.btn_picker.setToolTip(tr(
            "gallery.picker_tip",
            "Turns on multi-select so you can send several images to another tool. NOTE: "
            "turning it off clears whatever you had selected."))
        self.btn_picker.setCheckable(True)
        self.btn_picker.toggled.connect(lambda chk: self.state.toggle_picker_mode(chk))
        layout.addWidget(self.btn_picker)

        self.state.view_mode_changed.connect(self.on_mode_changed)
        self.state.filter_changed.connect(self.refresh_chips)

    def setup_completer(self):
        self.tag_completions = TagVocabulary(self.engine.db.service, self.txt_search, self, reader=self.reader)
        self.tag_completions.failed.connect(lambda error: self.on_tags_loaded(0, [], error))
        self.tag_completions.refresh()

    def on_tags_loaded(self, ticket, tags, error):
        if error is not None:
            logging.getLogger(__name__).warning('Gallery tags failed: %s', error)
            self.txt_search.setToolTip(LocaleManager().tr(
                'gallery.db.tags_error', 'Tag suggestions could not be loaded; search is still available.'))
            return
    def shutdown(self):
        self.tag_completions.close()
            
    def on_search_submit(self):
        text = self.txt_search.text().strip()
        if not text: return
        
        # Split by comma
        tokens = [t.strip() for t in text.split(',') if t.strip()]
        for t in tokens:
            if len(t) >= 2 and t.startswith('"') and t.endswith('"'):
                if t[1:-1].strip():
                    self.state.add_term(t[1:-1])
            else:
                self.state.add_tag(t)
        self.txt_search.clear()
        
    def refresh_chips(self):
        # Clear existing
        while self.chip_layout.count():
            item = self.chip_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            
        # Add Tags
        for i, tag in enumerate(self.state.tags):
            chip = TagChip(tag, i)
            chip.removed.connect(lambda t=tag: self.state.remove_tag(t))
            self.chip_layout.addWidget(chip)
            
        # Add Terms (differentiated visual?)
        base_idx = len(self.state.tags)
        for i, term in enumerate(self.state.search_terms):
            chip = TagChip(f'"{term}"', base_idx + i)
            chip.removed.connect(lambda t=term: self.state.remove_term(t))
            self.chip_layout.addWidget(chip)
            
    def clear_all(self):
        self.combo_rating.setCurrentIndex(0)
        self.state.clear_filters()
        
    def on_mode_changed(self, mode):
        # Update visibility of back button
        self.btn_back.setVisible(mode != self.state.VIEW_ALBUMS)
        
        # Disable/Enable filters if in Album mode?
        # Generally we might want to filter albums by name, but for now specific filters apply to images
        self.combo_rating.setEnabled(mode != self.state.VIEW_ALBUMS)
