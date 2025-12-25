
import sys
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                             QFrame, QSizePolicy)
from PySide6.QtCore import Qt
from core.mod_loader import ModuleLoader

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Panopticon - Modular Image Organizer")
        self.resize(1024, 768)
        
        self.loader = ModuleLoader()
        self.init_ui()
        self.load_available_modules()

    def init_ui(self):
        # Central widget with horizontal layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Sidebar
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(200)
        self.sidebar.setStyleSheet("background-color: #1e1e1e; border-right: 1px solid #333;")
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setAlignment(Qt.AlignTop)
        
        title_label = QLabel("PANOPTICON")
        title_label.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 18px; margin: 20px 0;")
        title_label.setAlignment(Qt.AlignCenter)
        self.sidebar_layout.addWidget(title_label)
        
        self.main_layout.addWidget(self.sidebar)

        # Content area
        self.content_stack = QStackedWidget()
        self.content_stack.setStyleSheet("background-color: #121212;")
        self.main_layout.addWidget(self.content_stack)

        # Dashboard View (ID 0)
        self.dashboard = QWidget()
        dashboard_layout = QVBoxLayout(self.dashboard)
        dashboard_layout.setAlignment(Qt.AlignCenter)
        
        welcome_label = QLabel("Welcome to Panopticon")
        welcome_label.setStyleSheet("color: white; font-size: 24px;")
        dashboard_layout.addWidget(welcome_label)
        
        self.modules_btn_container = QWidget()
        self.modules_btn_layout = QHBoxLayout(self.modules_btn_container)
        dashboard_layout.addWidget(self.modules_btn_container)
        
        self.content_stack.addWidget(self.dashboard)

    def load_available_modules(self):
        self.loaded_modules = {} # Registry
        module_names = self.loader.discover_modules()
        for name in module_names:
            try:
                module = self.loader.load_module(name)
                if module:
                    self.add_module_to_ui(module)
                    self.loaded_modules[module.name] = module
            except Exception as e:
                print(f"Failed to load module {name}: {e}")
        
        # After loading all, setup connections
        self.setup_integrations()

    def setup_integrations(self):
        # Librarian -> Gallery / Metadata
        librarian = self.get_module_by_partial_name("Librarian")
        gallery = self.get_module_by_partial_name("Gallery")
        metadata = self.get_module_by_partial_name("Metadata")
        
        if librarian and gallery:
            librarian.request_open_gallery.connect(
                lambda paths, title: self.switch_to_module(gallery, paths, title)
            )
            
        if librarian and metadata:
            librarian.request_open_metadata.connect(
                lambda paths: self.switch_to_module(metadata, paths)
            )

    def get_module_by_partial_name(self, partial):
        for name, mod in self.loaded_modules.items():
            if partial.lower() in name.lower():
                return mod
        return None

    def add_module_to_ui(self, module):
        # Add to sidebar
        btn = QPushButton(f"🔍 {module.name}")
        btn.setStyleSheet("""
            QPushButton {
                color: white; 
                padding: 12px; 
                text-align: left; 
                border: none;
                border-radius: 5px;
                margin: 2px 5px;
            }
            QPushButton:hover {
                background-color: #333;
            }
        """)
        btn.clicked.connect(lambda: self.switch_to_module(module))
        self.sidebar_layout.addWidget(btn)
        
        # Add to dashboard
        dash_btn = QPushButton(f"✨\n{module.name}")
        dash_btn.setFixedSize(160, 160)
        dash_btn.setStyleSheet("""
            QPushButton {
                background-color: #2a2a2a; 
                color: white; 
                border-radius: 20px; 
                font-size: 16px; 
                font-weight: bold;
                border: 2px solid #333;
            }
            QPushButton:hover {
                background-color: #3a3a3a;
                border: 2px solid #00ffcc;
            }
        """)
        dash_btn.clicked.connect(lambda: self.switch_to_module(module))
        self.modules_btn_layout.addWidget(dash_btn)
        
        # Add view to stack
        view = module.get_view()
        self.content_stack.addWidget(view)
        module.stack_index = self.content_stack.indexOf(view)

    def switch_to_module(self, module, *args):
        self.content_stack.setCurrentIndex(module.stack_index)
        
        # Pass data if needed
        if args:
            if "Gallery" in module.name and hasattr(module, "load_custom_view"):
                if len(args) >= 2:
                    module.load_custom_view(args[0], args[1])
                elif len(args) == 1:
                    module.load_custom_view(args[0])
                    
            elif "Metadata" in module.name and hasattr(module, "load_paths"):
                module.load_paths(args[0])

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
