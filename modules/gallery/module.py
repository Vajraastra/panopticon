from PySide6.QtWidgets import QWidget, QMenu
from PySide6.QtGui import QCursor
from PySide6.QtCore import Signal
from core.base_module import BaseModule
from .logic.state import GalleryState
from .logic.query_engine import QueryEngine
from .ui.view import GalleryView

class GalleryModule(BaseModule):
    """
    Gallery Module (Modular Refactor).
    Explores albums, browses images, and manages ratings.
    """
    # Signals for integrations
    request_open_workshop = Signal(list)
    request_open_optimizer = Signal(list)
    
    def __init__(self):
        super().__init__()
        self._name = "Gallery"
        self._description = "Browse and organize your image library."
        self._icon = "🖼️"
        self.accent_color = "#00ffcc"
        
        self.view_widget = None
        self.state = None
        self.engine = None
        
    def get_view(self) -> QWidget:
        if self.view_widget: return self.view_widget
        
        # Initialize Logic
        self.state = GalleryState()
        self.engine = QueryEngine()
        
        # Initialize UI
        self.view_widget = GalleryView(self.state, self.engine, self.context)
        
        # Connect Send Actions
        self.view_widget.btn_send.clicked.connect(self.show_send_menu)
        
        # Connect Sidebar Actions
        self.view_widget.sidebar.btn_open.clicked.connect(self.open_folder_dialog)
        
        return self.view_widget
        
    def open_folder_dialog(self):
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self.view_widget, "Open Folder to View")
        if folder:
            import os
            # We want to support viewing folders even if not indexed.
            # We'll just scan it temporarily for the UI.
            files = []
            valid_ext = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
            for root, _, filenames in os.walk(folder):
                for f in filenames:
                    if os.path.splitext(f)[1].lower() in valid_ext:
                        files.append(os.path.join(root, f).replace('\\', '/'))
            
            if files:
                # Use load_image_set which handles VIEW_CUSTOM mode
                self.load_image_set(files)

        
    def load_image_set(self, paths: list):
        """Called by Librarian or other modules."""
        # Ensure UI is built
        self.get_view() 
        self.state.set_mode(self.state.VIEW_CUSTOM, custom_paths=paths, title="Imported Set")
        
    def show_send_menu(self):
        """Shows context menu for 'Send To...'"""
        menu = QMenu(self.view_widget)
        # Fix: Force opacity and add border
        menu.setStyleSheet("""
            QMenu { 
                background-color: #222222; 
                color: white; 
                border: 1px solid #555;
                border-radius: 4px;
                padding: 5px;
            }
            QMenu::item {
                padding: 5px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected { 
                background-color: #00ffcc; 
                color: black; 
            }
        """)
        
        act_opt = menu.addAction("🚀 Send to Optimizer")
        act_work = menu.addAction("🛠️ Send to Workshop")
        
        # Get cursor position
        pos = QCursor.pos()
        selected = menu.exec(pos)
        
        paths = list(self.state.selected_paths)
        if not paths: return
        
        if selected == act_opt:
            self.request_open_optimizer.emit(paths)
        elif selected == act_work:
            self.request_open_workshop.emit(paths)
