from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QPushButton
from core.components.standard_layout import StandardToolLayout
from .sidebar import GallerySidebar
from .grid import GalleryGrid

class GalleryView(QWidget):
    """
    Orchestrator for the Gallery UI.
    """
    def __init__(self, state, query_engine, context):
        super().__init__()
        self.state = state
        self.engine = query_engine
        self.context = context
        
        self.init_ui()
        
    def init_ui(self):
        # 1. Components
        self.sidebar = GallerySidebar(self.state, self.engine)
        self.grid = GalleryGrid(self.state, self.engine)
        self.bottom = self._create_bottom_bar()
        
        # 2. Layout
        self.layout_manager = StandardToolLayout(
            self.grid, 
            self.sidebar, 
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        from PySide6.QtWidgets import QVBoxLayout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.addWidget(self.layout_manager)
        
    def _create_bottom_bar(self):
        bar = QWidget()
        layout = QHBoxLayout(bar)
        
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet("color: #888;")
        
        # Action Buttons (Picker)
        self.btn_send = QPushButton("🚀 Send Selection")
        self.btn_send.setVisible(False)
        # Connect to a placeholder, signal will be connected in Module
        self.btn_send.setStyleSheet("background: #00ffcc; color: black; font-weight: bold; padding: 5px 10px; border-radius: 4px;")
        
        layout.addWidget(self.lbl_status)
        layout.addStretch()
        layout.addWidget(self.btn_send)
        
        # Connect to state
        self.state.selection_changed.connect(self.update_bottom_bar)
        
        return bar
        
    def update_bottom_bar(self):
        count = len(self.state.selected_paths)
        if self.state.picker_active:
            self.lbl_status.setText(f"Picker Active: {count} items selected")
            self.btn_send.setVisible(count > 0)
            self.btn_send.setText(f"🚀 Send {count} items")
        else:
            self.lbl_status.setText("Ready")
            self.btn_send.setVisible(False)
