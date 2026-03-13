
import sys
import os
import logging
import traceback
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                             QFrame, QGridLayout, QComboBox, QMessageBox, QScrollArea,
                             QSizePolicy)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QFont

# Core Imports
from core.mod_loader import ModuleLoader
from core.theme_manager import ThemeManager
from core.locale_manager import LocaleManager
from core.event_bus import EventBus

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

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
        # Live theme preview: refresh global QSS whenever the theme changes
        self.theme_manager.theme_changed.connect(
            lambda: QApplication.instance().setStyleSheet(self.theme_manager.get_stylesheet())
        )
        
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
        self.dashboard_page.setStyleSheet(f"background-color: {self.theme_manager.get_color('bg_main')};")
        
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
        lbl_subtitle.setStyleSheet(f"color: {self.theme_manager.get_color('accent_main')}; font-size: 18px; letter-spacing: 2px;")
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
                background-color: {self.theme_manager.get_color('bg_panel')};
                color: {self.theme_manager.get_color('text_dim')};
                border: 1px solid {self.theme_manager.get_color('border')};
                border-radius: 22px;
                font-weight: bold;
                font-size: 14px;
            }}
            QPushButton:hover {{
                background-color: {self.theme_manager.get_color('bg_sidebar')};
                color: white;
                border-color: {self.theme_manager.get_color('accent_main')};
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
        lbl_tools.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; font-size: 12px; font-weight: bold; letter-spacing: 1px;")
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
        self.settings_page.setStyleSheet(f"background-color: {self.theme_manager.get_color('bg_main')};")

        outer_layout = QVBoxLayout(self.settings_page)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        layout.setSpacing(24)
        layout.setContentsMargins(60, 40, 60, 40)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

        # Title
        lbl = QLabel(self.tr("settings.title", "Settings"))
        lbl.setStyleSheet(f"color: {self.theme_manager.get_color('text_primary')}; font-size: 32px; font-weight: bold;")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        # ── LANGUAGE ────────────────────────────────────────────────────────────
        lbl_lang_sec = QLabel(self.tr("settings.language_section", "LANGUAGE"))
        lbl_lang_sec.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(lbl_lang_sec)

        lang_box = QFrame()
        lang_box.setStyleSheet(f"background: {self.theme_manager.get_color('bg_panel')}; border-radius: 10px;")
        lb_layout = QHBoxLayout(lang_box)
        lb_layout.setContentsMargins(20, 14, 20, 14)

        lbl_lang = QLabel(self.tr("settings.language", "Language:"))
        lbl_lang.setStyleSheet(f"color: {self.theme_manager.get_color('text_primary')}; font-size: 16px; border: none;")
        lb_layout.addWidget(lbl_lang)
        lb_layout.addStretch()

        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["English", "Español"])
        self.combo_lang.setFixedSize(200, 40)
        self.combo_lang.setCurrentIndex(1 if self.locale_manager.get_locale() == "es" else 0)
        self.combo_lang.setStyleSheet(f"""
            QComboBox {{
                background-color: {self.theme_manager.get_color('bg_input')};
                color: {self.theme_manager.get_color('text_primary')};
                border: 1px solid {self.theme_manager.get_color('border')};
                border-radius: 5px; padding: 5px; font-size: 16px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        lb_layout.addWidget(self.combo_lang)
        layout.addWidget(lang_box)

        # ── THEME ────────────────────────────────────────────────────────────────
        lbl_theme_sec = QLabel(self.tr("settings.theme_section", "THEME"))
        lbl_theme_sec.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(lbl_theme_sec)

        themes_frame = QFrame()
        themes_frame.setStyleSheet(f"background: {self.theme_manager.get_color('bg_panel')}; border-radius: 10px;")
        tf_layout = QVBoxLayout(themes_frame)
        tf_layout.setContentsMargins(20, 18, 20, 18)
        tf_layout.setSpacing(12)

        self._theme_cards = {}
        self._theme_card_dots = {}
        self._pending_theme = self.theme_manager.current_theme

        all_themes = ["cyberpunk", "midnight", "forest", "slate", "light",
                      "sepia", "cosmic", "grape", "ocean", "aurora"]

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, key in enumerate(all_themes):
            card = self._make_theme_card(key)
            grid.addWidget(card, i // 5, i % 5)

        tf_layout.addLayout(grid)
        layout.addWidget(themes_frame)

        # ── SAVE & BACK ──────────────────────────────────────────────────────────
        btn_save = QPushButton(self.tr("settings.save_restart", "Save & Restart"))
        btn_save.setFixedSize(250, 50)
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme_manager.get_color('accent_main')};
                color: black; border-radius: 25px; font-weight: bold; font-size: 16px;
            }}
            QPushButton:hover {{ background-color: white; }}
        """)
        btn_save.clicked.connect(self.apply_settings)
        layout.addWidget(btn_save, alignment=Qt.AlignHCenter)

        btn_back = QPushButton(self.tr("settings.back", "Back to Dashboard"))
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; background: transparent; text-decoration: underline; border: none;")
        btn_back.clicked.connect(lambda: self.root_stack.setCurrentIndex(0))
        layout.addWidget(btn_back, alignment=Qt.AlignHCenter)

    def _make_theme_card(self, key):
        """Build a clickable theme preview card."""
        from core.theme_manager import ThemeManager as TM
        colors = TM.THEMES[key]
        name = TM.THEME_NAMES.get(key, key.capitalize())
        is_selected = (key == self._pending_theme)

        card = QFrame()
        card.setFixedSize(150, 98)
        card.setCursor(Qt.PointingHandCursor)
        self._theme_cards[key] = card
        self._apply_theme_card_style(card, key, is_selected)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        card_layout.setSpacing(0)

        # ── Preview area (top) ──
        preview = QWidget()
        preview.setFixedHeight(60)
        preview.setStyleSheet(f"background: {colors['bg_main']}; border-radius: 6px 6px 0 0; border: none;")
        prev_layout = QHBoxLayout(preview)
        prev_layout.setContentsMargins(10, 0, 10, 0)
        prev_layout.setAlignment(Qt.AlignCenter)
        prev_layout.setSpacing(8)

        # Three color swatches: bg_panel · accent_main · accent_warning
        for swatch_key in ('bg_panel', 'accent_main', 'accent_warning'):
            swatch = QFrame()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(f"background: {colors[swatch_key]}; border-radius: 9px; border: none;")
            prev_layout.addWidget(swatch)
        card_layout.addWidget(preview, 1)

        # ── Name bar (bottom) ──
        name_bar = QWidget()
        name_bar.setFixedHeight(30)
        name_bar.setStyleSheet(f"background: {colors['bg_panel']}; border-radius: 0 0 6px 6px; border: none;")
        nb_layout = QHBoxLayout(name_bar)
        nb_layout.setContentsMargins(8, 0, 8, 0)

        lbl = QLabel(name)
        lbl.setStyleSheet(f"color: {colors['text_primary']}; font-size: 11px; font-weight: bold; background: transparent; border: none;")
        nb_layout.addWidget(lbl)
        nb_layout.addStretch()

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {colors['accent_main']}; font-size: 8px; background: transparent; border: none;")
        dot.setVisible(is_selected)
        nb_layout.addWidget(dot)
        self._theme_card_dots[key] = dot

        card_layout.addWidget(name_bar)

        def on_click(e, k=key):
            self._select_theme(k)
        card.mousePressEvent = on_click
        return card

    def _apply_theme_card_style(self, card, key, is_selected):
        from core.theme_manager import ThemeManager as TM
        colors = TM.THEMES[key]
        border_color = colors['accent_main'] if is_selected else colors['border']
        border_width = 3 if is_selected else 1
        card.setStyleSheet(f"""
            QFrame {{
                background: {colors['bg_main']};
                border: {border_width}px solid {border_color};
                border-radius: 8px;
            }}
        """)

    def _select_theme(self, key):
        # Apply and save theme, then rebuild the settings page in-place so
        # all inline styles (backgrounds, borders, labels) reflect the new colors.
        self.theme_manager.set_theme(key)
        self._rebuild_settings_page()

    def _rebuild_settings_page(self):
        """Replace the settings page widget in the stack without touching other pages."""
        old = self.root_stack.widget(1)
        self.root_stack.removeWidget(old)
        old.deleteLater()
        self.create_settings_page()
        self.root_stack.insertWidget(1, self.settings_page)
        self.root_stack.setCurrentIndex(1)

    def apply_settings(self):
        index = self.combo_lang.currentIndex()
        code = "es" if index == 1 else "en"
        self.locale_manager.set_locale(code)
        # Theme is already applied and saved via live preview (_select_theme)
        QMessageBox.information(self, self.tr("settings.restart_required", "Restart Required"),
                                self.tr("settings.restart_msg", "Please restart Panopticon to apply all changes."))

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
                log.error("Error loading module %s: %s\n%s", name, e, traceback.format_exc())
                
        # Configuración de conexiones entre módulos (Inter-module wiring)
        self.setup_integrations()

    def add_module_card(self, module):
        """Create and add a module card to the grid."""
        # Initialize view mostly to ensure it's ready, but we don't show it yet
        try:
            view = module.get_view()
            if not view:
                log.warning("[WARN] Module %s returned empty view.", module.name)
                return
        except Exception as e:
            log.error("[ERROR] Failed to get view for %s: %s\n%s", module.name, e, traceback.format_exc())
            return

        # 1. Create Card Widget
        card = QFrame()
        card.setFixedSize(220, 220)
        card.setCursor(Qt.PointingHandCursor)
        
        # Accent Color
        accent = getattr(module, "accent_color", self.theme_manager.get_color('accent_main'))

        # Styling
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme_manager.get_color('bg_panel')};
                border: 2px solid {self.theme_manager.get_color('border')};
                border-radius: 16px;
            }}
            QFrame:hover {{
                background-color: {self.theme_manager.get_color('bg_sidebar')};
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
        lbl_desc.setStyleSheet(f"font-size: 11px; color: {self.theme_manager.get_color('text_dim')}; background: transparent; border: none;")
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
