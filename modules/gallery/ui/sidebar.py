from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, 
                               QFrame, QComboBox, QHBoxLayout, QCompleter)
from PySide6.QtCore import Qt, Signal
from modules.librarian.logic.tagging_ui import FlowLayout, TagChip # Recycle from Librarian

class GallerySidebar(QWidget):
    """
    Sidebar Panel for Gallery.
    Handles user input for filters and navigation commands.
    """
    
    def __init__(self, state, query_engine, parent=None):
        super().__init__(parent)
        self.state = state
        self.engine = query_engine
        
        self.init_ui()
        self.setup_completer()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 20, 15, 20)
        layout.setSpacing(15)
        
        # Styles
        # Styles
        self.setStyleSheet("""
            QLabel { color: #ddd; font-family: 'Segoe UI'; }
            QLineEdit { background: #222; border: 1px solid #333; color: white; padding: 6px; border-radius: 4px; }
            QLineEdit:focus { border-color: #00ffcc; }
            QPushButton { background: #333; color: white; border: none; padding: 8px; border-radius: 4px; }
            QPushButton:hover { background: #444; }
            QComboBox { background: #222; color: white; border: 1px solid #333; padding: 5px; border-radius: 4px; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView {
                background-color: #222;
                color: white;
                selection-background-color: #00ffcc;
                selection-color: black;
                border: 1px solid #333;
            }
        """)
        
        # 1. Navigation
        lbl_nav = QLabel("NAVIGATION")
        lbl_nav.setStyleSheet("color: #666; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_nav)
        
        # New Open Folder Button
        self.btn_open = QPushButton("📂 Open Folder")
        self.btn_open.setCursor(Qt.PointingHandCursor)
        self.btn_open.setStyleSheet("background-color: #222; color: #00ffcc; border: 1px solid #444; font-weight: bold;")
        layout.addWidget(self.btn_open)
        
        self.btn_back = QPushButton("🔙 Back to Albums")
        self.btn_back.setCursor(Qt.PointingHandCursor)
        self.btn_back.clicked.connect(lambda: self.state.set_mode(self.state.VIEW_ALBUMS))
        self.btn_back.setVisible(False) # Default hidden
        self.btn_back.setStyleSheet("color: #00ffcc; background: #222;")
        layout.addWidget(self.btn_back)
        
        # 2. Filters
        layout.addSpacing(10)
        lbl_filter = QLabel("FILTERS & SEARCH")
        lbl_filter.setStyleSheet("color: #666; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_filter)
        
        # Search Input
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search tags or text...")
        self.txt_search.returnPressed.connect(self.on_search_submit)
        layout.addWidget(self.txt_search)
        
        # Rating
        self.combo_rating = QComboBox()
        self.combo_rating.addItems(["All Ratings", "⭐ 1+", "⭐⭐ 2+", "⭐⭐⭐ 3+", "⭐⭐⭐⭐ 4+", "⭐⭐⭐⭐⭐ 5"])
        self.combo_rating.currentIndexChanged.connect(lambda idx: self.state.set_rating_filter(idx))
        layout.addWidget(self.combo_rating)
        
        # Active Chips
        self.chip_container = QWidget()
        self.chip_layout = FlowLayout(self.chip_container)
        layout.addWidget(self.chip_container)
        
        # Clear
        self.btn_clear = QPushButton("✕ Clear Filters")
        self.btn_clear.clicked.connect(self.clear_all)
        self.btn_clear.setStyleSheet("color: #ff5555; background: transparent; border: 1px solid #331111;")
        layout.addWidget(self.btn_clear)
        
        layout.addStretch()
        
        # 3. Picker Toggle
        self.btn_picker = QPushButton("🎯 Picker Mode")
        self.btn_picker.setCheckable(True)
        self.btn_picker.toggled.connect(lambda chk: self.state.toggle_picker_mode(chk))
        self.btn_picker.setStyleSheet("""
            QPushButton:checked { border: 1px solid #00ffcc; color: #00ffcc; background: #112211; }
        """)
        layout.addWidget(self.btn_picker)
        
        # Connect state changes to UI updates
        self.state.view_mode_changed.connect(self.on_mode_changed)
        self.state.filter_changed.connect(self.refresh_chips)
        
    def setup_completer(self):
        tags = self.engine.get_all_tags()
        if tags:
            comp = QCompleter(tags)
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            self.txt_search.setCompleter(comp)
            
    def on_search_submit(self):
        text = self.txt_search.text().strip()
        if not text: return
        
        # Split by comma
        tokens = [t.strip() for t in text.split(',') if t.strip()]
        for t in tokens:
            self.state.add_tag(t) # Assuming tag search for now
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
