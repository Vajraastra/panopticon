
import sys
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                             QFrame, QSizePolicy)
from PySide6.QtCore import Qt
from core.theme import Theme
from core.mod_loader import ModuleLoader
from core.theme_manager import ThemeManager
from core.event_bus import EventBus

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Panopticon - Modular Image Organizer")
        self.resize(1024, 768)
        self.resize(1024, 768)
        
        self.theme_manager = ThemeManager()
        self.setStyleSheet(self.theme_manager.get_stylesheet())
        
        self.event_bus = EventBus()
        self.event_bus.subscribe("navigate", self.on_navigate)
        
        # Core Service Context to be injected into modules
        self.context = {
            "theme_manager": self.theme_manager,
            "event_bus": self.event_bus,
            "main_window": self
        }

        self.loader = ModuleLoader()
        self.init_ui()
        self.load_available_modules()

    def init_ui(self):
        # ROOT CONTAINER: Stacked Widget
        self.root_stack = QStackedWidget()
        self.setCentralWidget(self.root_stack)
        
        # --- PAGE 0: DASHBOARD CONTAINER ---
        self.dashboard_page = QWidget()
        self.dashboard_layout = QHBoxLayout(self.dashboard_page)
        self.dashboard_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_layout.setSpacing(0)
        
        # 1. Main Sidebar (Only lives here now)
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(200)
        self.sidebar.setStyleSheet(f"background-color: {Theme.BG_SIDEBAR}; border-right: 1px solid {Theme.BORDER};")
        self.sidebar_layout = QVBoxLayout(self.sidebar)
        self.sidebar_layout.setAlignment(Qt.AlignTop)
        
        title_label = QLabel("PANOPTICON")
        title_label.setStyleSheet(f"color: {Theme.ACCENT_MAIN}; font-weight: bold; font-size: 18px; margin: 20px 0;")
        title_label.setAlignment(Qt.AlignCenter)
        self.sidebar_layout.addWidget(title_label)
        
        self.dashboard_layout.addWidget(self.sidebar)
        
        # 2. Dashboard Content (Welcome + Buttons)
        self.dashboard_content = QWidget()
        self.dashboard_content.setStyleSheet(f"background-color: {Theme.BG_MAIN};")
        db_content_layout = QVBoxLayout(self.dashboard_content)
        db_content_layout.setAlignment(Qt.AlignCenter)
        
        welcome_label = QLabel("Welcome to Panopticon")
        welcome_label.setStyleSheet(f"color: white; font-size: 24px;")
        db_content_layout.addWidget(welcome_label)
        
        self.modules_btn_container = QWidget()
        self.modules_btn_layout = QHBoxLayout(self.modules_btn_container)
        db_content_layout.addWidget(self.modules_btn_container)
        
        self.dashboard_layout.addWidget(self.dashboard_content)
        
        # Add Dashboard Page to Root Stack
        self.root_stack.addWidget(self.dashboard_page) # Index 0

    def load_available_modules(self):
        self.loaded_modules = {} # Registry
        module_names = self.loader.discover_modules()
        for name in module_names:
            try:
                module = self.loader.load_module(name, self.context)
                if module:
                    self.add_module_to_ui(module)
                    self.loaded_modules[module.name] = module
            except Exception as e:
                print(f"Failed to load module {name}: {e}")
        
    # ... (rest of load_available_modules)
        self.setup_integrations()

    def setup_integrations(self):
        # Librarian -> Gallery / Metadata
        librarian = self.get_module_by_partial_name("Librarian")
        gallery = self.get_module_by_partial_name("Gallery")
        workshop = self.get_module_by_partial_name("Workshop")
        
        if librarian and gallery:
            librarian.request_open_gallery.connect(
                lambda paths, title: self.switch_to_module(gallery, paths, title)
            )
            
        if librarian and workshop:
            librarian.request_open_metadata.connect(
                lambda paths: self.switch_to_module(workshop, paths, "reader")
            )
            librarian.request_open_workshop.connect(
                lambda paths: self.switch_to_module(workshop, paths, "modifier")
            )
            
        if gallery and workshop:
            gallery.request_open_workshop.connect(
                lambda paths: self.switch_to_module(workshop, paths)
            )

    def get_module_by_partial_name(self, partial):
        for name, mod in self.loaded_modules.items():
            if partial.lower() in name.lower():
                return mod
        return None

    def add_module_to_ui(self, module):
        # Prevent ghost buttons by ensuring view loads first
        try:
            view = module.get_view()
            if view is None:
                print(f"Module {module.name} returned no view.")
                return
        except Exception as e:
            print(f"Failed to initialize view for {module.name}: {e}")
            raise e

        # Determine module accent color
        accent = getattr(module, "accent_color", Theme.ACCENT_MAIN)
        if "Fashion" in module.name: accent = Theme.ACCENT_FASHION
        
        # Add to sidebar
        icon = getattr(module, "icon", "🧩")
        btn = QPushButton(f"{icon} {module.name}")
        btn.setStyleSheet(f"""
            QPushButton {{
                color: #ddd; 
                padding: 12px; 
                text-align: left; 
                border: none;
                border-radius: 5px;
                margin: 2px 5px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {Theme.BORDER};
                color: {accent};
            }}
        """)
        btn.clicked.connect(lambda: self.switch_to_module(module))
        self.sidebar_layout.addWidget(btn)
        
        # Add to dashboard
        icon = getattr(module, "icon", "✨")
        dash_btn = QPushButton(f"{icon}\n{module.name}")
        dash_btn.setFixedSize(160, 160)
        # Use a slightly modified card style for main dashboard buttons
        dash_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.BG_PANEL}; 
                color: white; 
                border-radius: 20px; 
                font-size: 16px; 
                font-weight: bold;
                border: 2px solid {Theme.BORDER};
            }}
            QPushButton:hover {{
                background-color: #2a2a2a;
                border: 2px solid {accent};
                color: {accent};
            }}
        """)
        dash_btn.clicked.connect(lambda: self.switch_to_module(module))
        self.modules_btn_layout.addWidget(dash_btn)
        
        # Add view to stack
        self.root_stack.addWidget(view)
        module.stack_index = self.root_stack.indexOf(view)

    def switch_to_module(self, module, *args):
        # Simply switch the root stack. The Sidebar is part of Page 0, so it disappears automatically.
        print(f"DEBUG: Switching to {module.name} (Index {module.stack_index})")
        self.root_stack.setCurrentIndex(module.stack_index)
        
        # Pass data if needed
        if args:
            if "Gallery" in module.name and hasattr(module, "load_custom_view"):
                if len(args) >= 2:
                    module.load_custom_view(args[0], args[1])
                elif len(args) == 1:
                    module.load_custom_view(args[0])
                    
            elif "Workshop" in module.name and hasattr(module, "load_images"):
                if len(args) >= 2:
                    module.load_images(args[0], args[1])
                else:
                    module.load_images(args[0])

    def on_navigate(self, target):
        """Handle navigation events from EventBus."""
        if target == "dashboard":
            print("DEBUG: Returning to Dashboard")
            self.root_stack.setCurrentIndex(0) # 0 is dashboard page


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
