
import sys
import os
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                             QFrame, QGridLayout, QComboBox, QMessageBox, QScrollArea)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QFont

# Core Imports
from core.theme import Theme
from core.mod_loader import ModuleLoader
from core.theme_manager import ThemeManager
from core.locale_manager import LocaleManager
from core.event_bus import EventBus

class MainWindow(QMainWindow):
    """
    Ventana principal de Panopticon.
    Actúa como el orquestador central, gestionando la inyección de dependencias,
    la navegación global y el cargador de módulos.
    """
    def __init__(self):
        super().__init__()
        
        # 1. Inicialización de Servicios Centrales (Core Services)
        # Estos servicios se pasan a cada módulo para permitir comunicación y consistencia funcional.
        self.locale_manager = LocaleManager()
        self.tr = self.locale_manager.tr # Atajo para traducciones
        self.theme_manager = ThemeManager()
        self.event_bus = EventBus()
        
        # Suscripción a eventos de navegación global
        self.event_bus.subscribe("navigate", self.on_navigate)
        
        # 2. Configuración de la Ventana
        self.setWindowTitle(f"{self.tr('app.title', 'Panopticon')} - {self.tr('app.subtitle')}")
        self.resize(1280, 800) # Tamaño base razonable
        self.setStyleSheet(self.theme_manager.get_stylesheet())
        
        # 3. Contexto de Inyección de Dependencias
        # Este diccionario se entrega a los módulos cargados para que accedan a los servicios.
        self.context = {
            "theme_manager": self.theme_manager,
            "locale_manager": self.locale_manager,
            "event_bus": self.event_bus,
            "main_window": self
        }

        # 4. UI Initialization
        self.module_grid_count = 0
        self.loaded_modules = {}
        self.root_stack = None
        self.init_ui()
        
        # 5. Load Modules
        self.loader = ModuleLoader()
        self.load_available_modules()

    def init_ui(self):
        """Initialize the main application layout."""
        # Root Container (Stacked Widget for Dashboard vs Settings vs Modules)
        self.root_stack = QStackedWidget()
        self.setCentralWidget(self.root_stack)
        
        # --- PAGE 0: DASHBOARD ---
        self.create_dashboard_page()
        self.root_stack.addWidget(self.dashboard_page) # Index 0
        
        # --- PAGE 1: SETTINGS ---
        self.create_settings_page()
        self.root_stack.addWidget(self.settings_page) # Index 1

    def create_dashboard_page(self):
        self.dashboard_page = QWidget()
        self.dashboard_page.setStyleSheet(f"background-color: {Theme.BG_MAIN};")
        
        # Main Vertical Layout
        layout = QVBoxLayout(self.dashboard_page)
        layout.setAlignment(Qt.AlignTop)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 1. HEADER AREA (Yellow Zone equivalent)
        header = QFrame()
        header.setFixedHeight(140)
        header_layout = QVBoxLayout(header)
        header_layout.setAlignment(Qt.AlignCenter)
        header_layout.setSpacing(5)
        
        lbl_title = QLabel(self.tr('dashboard.welcome', 'PANOPTICON'))
        lbl_title.setStyleSheet("color: white; font-size: 56px; font-weight: bold; letter-spacing: 4px;")
        lbl_title.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(lbl_title)
        
        lbl_subtitle = QLabel(self.tr('app.subtitle', 'Modular Image Organizer'))
        lbl_subtitle.setStyleSheet(f"color: {Theme.ACCENT_MAIN}; font-size: 18px; letter-spacing: 2px;")
        lbl_subtitle.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(lbl_subtitle)
        
        layout.addWidget(header)
        
        # 2. SETTINGS AREA (Purple Zone equivalent)
        settings_bar = QFrame()
        settings_bar.setFixedHeight(80)
        sb_layout = QHBoxLayout(settings_bar)
        sb_layout.setAlignment(Qt.AlignCenter)
        
        btn_settings = QPushButton(f"⚙ {self.tr('settings.title', 'Settings / Configuración')}")
        btn_settings.setFixedSize(280, 45)
        btn_settings.setCursor(Qt.PointingHandCursor)
        btn_settings.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.BG_PANEL};
                color: {Theme.TEXT_DIM};
                border: 1px solid {Theme.BORDER};
                border-radius: 22px;
                font-weight: bold;
                font-size: 14px;
            }}
            QPushButton:hover {{
                background-color: {Theme.BG_SIDEBAR};
                color: white;
                border-color: {Theme.ACCENT_MAIN};
            }}
        """)
        btn_settings.clicked.connect(lambda: self.root_stack.setCurrentIndex(1))
        sb_layout.addWidget(btn_settings)
        
        layout.addWidget(settings_bar)
        
        # 3. TOOLS GRID AREA
        # Label
        lbl_tools_container = QWidget()
        lbl_tools_layout = QVBoxLayout(lbl_tools_container)
        lbl_tools_layout.setContentsMargins(40, 20, 40, 0)
        lbl_tools = QLabel(self.tr('dashboard.tools', 'AVAILABLE TOOLS'))
        lbl_tools.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 12px; font-weight: bold; letter-spacing: 1px;")
        lbl_tools_layout.addWidget(lbl_tools)
        layout.addWidget(lbl_tools_container)

        # Scroll Area for the Grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        
        # The Grid Widget
        self.modules_grid_widget = QWidget()
        self.modules_grid_widget.setStyleSheet("background: transparent;")
        self.modules_grid_layout = QGridLayout(self.modules_grid_widget)
        self.modules_grid_layout.setSpacing(25) # Space between cards
        self.modules_grid_layout.setContentsMargins(40, 20, 40, 20)
        self.modules_grid_layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        
        scroll.setWidget(self.modules_grid_widget)
        layout.addWidget(scroll, 1) # This will take all available space

    def create_settings_page(self):
        self.settings_page = QWidget()
        self.settings_page.setStyleSheet(f"background-color: {Theme.BG_MAIN};")
        
        layout = QVBoxLayout(self.settings_page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(30)
        
        # Title
        lbl = QLabel(self.tr("settings.title", "Settings / Configuración"))
        lbl.setStyleSheet("color: white; font-size: 32px; font-weight: bold;")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)
        
        # Language Selector
        lang_box = QFrame()
        lang_box.setStyleSheet(f"background: {Theme.BG_PANEL}; border-radius: 10px; padding: 20px;")
        lb_layout = QHBoxLayout(lang_box)
        
        lbl_lang = QLabel(self.tr("settings.language", "Language:"))
        lbl_lang.setStyleSheet("color: white; font-size: 18px; border: none;")
        lb_layout.addWidget(lbl_lang)
        
        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["English", "Español"])
        self.combo_lang.setFixedSize(200, 40)
        
        # Determine current index
        current = self.locale_manager.get_locale()
        self.combo_lang.setCurrentIndex(1 if current == "es" else 0)
        
        # Style
        self.combo_lang.setStyleSheet(f"""
            QComboBox {{
                background-color: {Theme.BG_INPUT};
                color: white;
                border: 1px solid {Theme.BORDER};
                border-radius: 5px;
                padding: 5px;
                font-size: 16px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        lb_layout.addWidget(self.combo_lang)
        
        # Set alignment wrapper for box
        box_wrapper = QWidget()
        bw_layout = QHBoxLayout(box_wrapper)
        bw_layout.setAlignment(Qt.AlignCenter)
        bw_layout.addWidget(lang_box)
        layout.addWidget(box_wrapper)
        
        # Save Button
        btn_save = QPushButton(self.tr("settings.save_restart", "Save & Restart"))
        btn_save.setFixedSize(250, 50)
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.ACCENT_MAIN};
                color: black;
                border-radius: 25px;
                font-weight: bold;
                font-size: 16px;
            }}
            QPushButton:hover {{ background-color: white; }}
        """)
        btn_save.clicked.connect(self.apply_settings)
        layout.addWidget(btn_save)
        
        # Back Button
        btn_back = QPushButton(self.tr("settings.back", "Back to Dashboard"))
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setStyleSheet(f"color: {Theme.TEXT_DIM}; background: transparent; text-decoration: underline;")
        btn_back.clicked.connect(lambda: self.root_stack.setCurrentIndex(0))
        layout.addWidget(btn_back)

    def apply_settings(self):
        index = self.combo_lang.currentIndex()
        code = "es" if index == 1 else "en"
        self.locale_manager.set_locale(code)
        QMessageBox.information(self, self.tr("settings.restart_required", "Restart Required"), 
                                self.tr("settings.restart_msg", "Please restart Panopticon to apply language changes."))

    def load_available_modules(self):
        """
        Descubre y carga dinámicamente todos los submódulos presentes en la carpeta /modules.
        Utiliza el ModuleLoader para inicializar cada componente con el contexto inyectado.
        """
        self.loaded_modules = {}
        for name in self.loader.discover_modules():
            try:
                # Se inyecta el contexto (servicios core) durante la carga
                module = self.loader.load_module(name, self.context)
                if module:
                    self.add_module_card(module)
                    self.loaded_modules[module.name] = module
            except Exception as e:
                print(f"Error loading {name}: {e}")
                
        # Configuración de conexiones entre módulos (Inter-module wiring)
        self.setup_integrations()

    def add_module_card(self, module):
        """Create and add a module card to the grid."""
        # Initialize view mostly to ensure it's ready, but we don't show it yet
        try:
            view = module.get_view()
            if not view: 
                print(f"[WARN] Module {module.name} returned empty view.")
                return
        except Exception as e:
            print(f"[ERROR] Failed to get view for {module.name}: {e}")
            import traceback
            traceback.print_exc()
            return

        # 1. Create Card Widget
        card = QFrame()
        card.setFixedSize(220, 220)
        card.setCursor(Qt.PointingHandCursor)
        
        # Accent Color
        accent = getattr(module, "accent_color", Theme.ACCENT_MAIN)
        if "Fashion" in module.name: accent = Theme.ACCENT_FASHION
        
        # Styling
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {Theme.BG_PANEL};
                border: 2px solid {Theme.BORDER};
                border-radius: 16px;
            }}
            QFrame:hover {{
                background-color: {Theme.BG_SIDEBAR};
                border-color: {accent};
            }}
        """)
        
        # Layout
        layout = QVBoxLayout(card)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        # Content
        # Icon
        icon_txt = str(getattr(module, "icon", "📦"))
        lbl_icon = QLabel(icon_txt)
        lbl_icon.setAlignment(Qt.AlignCenter)
        lbl_icon.setFixedHeight(50)
        lbl_icon.setStyleSheet("font-size: 40px; color: white; background: transparent; border: none;")
        layout.addWidget(lbl_icon)
        
        # Title
        title_key = f"tool.{module.name.lower().replace(' ', '_')}.name"
        lbl_title = QLabel(self.tr(title_key, module.name))
        lbl_title.setAlignment(Qt.AlignCenter)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet("font-size: 15px; font-weight: bold; color: white; background: transparent; border: none;")
        layout.addWidget(lbl_title)
        
        # Description
        desc_key = f"tool.{module.name.lower().replace(' ', '_')}.desc"
        default_desc = getattr(module, "description", "No Info")
        desc = self.tr(desc_key, default_desc)
        
        if len(desc) > 60: desc = desc[:57] + "..."
        lbl_desc = QLabel(desc)
        lbl_desc.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"font-size: 11px; color: {Theme.TEXT_DIM}; background: transparent; border: none;")
        layout.addWidget(lbl_desc, 1) # Stretch
        
        # Events
        # We start the module view and switch stack
        self.root_stack.addWidget(view)
        module.stack_index = self.root_stack.indexOf(view)
        
        # Monkey-patch click
        def on_click(e):
            self.switch_to_module(module)
        card.mousePressEvent = on_click
        
        # Place in Grid (5 Columns)
        row = self.module_grid_count // 5
        col = self.module_grid_count % 5
        self.modules_grid_layout.addWidget(card, row, col)
        
        self.module_grid_count += 1

    def switch_to_module(self, module, *args):
        self.root_stack.setCurrentIndex(module.stack_index)
        # Pass args if supported
        if args:
            if hasattr(module, "load_image_set"):
                module.load_image_set(*args)
            elif hasattr(module, "load_images"):
                module.load_images(*args)
            elif hasattr(module, "load_custom_view"):
                module.load_custom_view(*args)

    def setup_integrations(self):
        # Wiring up known module connections
        librarian = self.get_module("Librarian")
        gallery = self.get_module("Gallery")
        optimizer = self.get_module("Image Optimizer")
        cropper = self.get_module("Smart Cropper")
        
        if librarian:
            if gallery:
                librarian.request_open_gallery.connect(
                    lambda paths, title: self.switch_to_module(gallery, paths) # title ignored for standard set load
                )
            if optimizer:
                librarian.request_open_optimizer.connect(
                    lambda paths: self.switch_to_module(optimizer, paths)
                )
            if cropper:
                librarian.request_open_cropper.connect(
                    lambda paths: self.switch_to_module(cropper, paths)
                )

    def get_module(self, partial_name):
        for name, mod in self.loaded_modules.items():
            if partial_name.lower() in name.lower():
                return mod
        return None

    def on_navigate(self, target):
        if target == "dashboard":
            self.root_stack.setCurrentIndex(0)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.showMaximized() # Forzar maximizado al final para evitar re-dimensiones inesperadas
    sys.exit(app.exec())
