import json
import os
import logging
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from core.locale_manager import LocaleManager

log = logging.getLogger(__name__)

class ThemeManager(QObject):
    """
    Gestor de temas y estilos visuales de la aplicación.
    Maneja una paleta de colores global, permite la persistencia de cambios
    en un archivo JSON y genera hojas de estilo (QSS) dinámicas.
    """
    theme_changed = Signal() # Se emite cuando se actualiza un color para refrescar la UI

    # Colores por defecto (Aestética Cyber/Dark)
    DEFAULTS = {
        "bg_main": "#050505",
        "bg_sidebar": "#0a0a0a",
        "bg_panel": "#111111",
        "bg_input": "#000000",
        "border": "#333333",
        "border_highlight": "#00ffcc",
        "text_primary": "#ffffff",
        "text_secondary": "#cccccc",
        "text_dim": "#666666",
        "accent_main": "#00ffcc",
        "accent_hover": "#00ccaa",
        "accent_warning": "#ff3333",
        "accent_success": "#00ff66",
    }

    THEME_NAMES = {
        "cyberpunk": "Cyberpunk",
        "midnight": "Midnight",
        "forest": "Forest",
        "slate": "Slate",
        "light": "Light",
        "sepia": "Sepia",
        "cosmic": "Cosmic",
        "grape": "Grape",
        "ocean": "Ocean",
        "aurora": "Aurora",
    }

    THEMES = {
        "cyberpunk": {
            "bg_main": "#050505", "bg_sidebar": "#0a0a0a", "bg_panel": "#111111", "bg_input": "#000000",
            "border": "#333333", "border_highlight": "#00ffcc",
            "text_primary": "#ffffff", "text_secondary": "#cccccc", "text_dim": "#666666",
            "accent_main": "#00ffcc", "accent_hover": "#00ccaa", "accent_warning": "#ff3333", "accent_success": "#00ff66",
        },
        "midnight": {
            "bg_main": "#0a0e1a", "bg_sidebar": "#0f1525", "bg_panel": "#141d35", "bg_input": "#080c18",
            "border": "#1e2d4a", "border_highlight": "#4d9fff",
            "text_primary": "#e8f0ff", "text_secondary": "#a0b4d6", "text_dim": "#4a6080",
            "accent_main": "#4d9fff", "accent_hover": "#3080dd", "accent_warning": "#ff5555", "accent_success": "#44ff88",
        },
        "forest": {
            "bg_main": "#080d08", "bg_sidebar": "#0e140e", "bg_panel": "#131f13", "bg_input": "#060a06",
            "border": "#1a2e1a", "border_highlight": "#44ff88",
            "text_primary": "#dfffdf", "text_secondary": "#a0c8a0", "text_dim": "#4a6a4a",
            "accent_main": "#44ff88", "accent_hover": "#33cc66", "accent_warning": "#ff5533", "accent_success": "#00ff44",
        },
        "slate": {
            "bg_main": "#0f0f0f", "bg_sidebar": "#141414", "bg_panel": "#1c1c1c", "bg_input": "#0a0a0a",
            "border": "#2e2e2e", "border_highlight": "#999999",
            "text_primary": "#ffffff", "text_secondary": "#b0b0b0", "text_dim": "#555555",
            "accent_main": "#cccccc", "accent_hover": "#aaaaaa", "accent_warning": "#ff4444", "accent_success": "#44cc44",
        },
        "light": {
            "bg_main": "#f0f0f0", "bg_sidebar": "#e8e8e8", "bg_panel": "#ffffff", "bg_input": "#fafafa",
            "border": "#cccccc", "border_highlight": "#0066cc",
            "text_primary": "#111111", "text_secondary": "#444444", "text_dim": "#888888",
            "accent_main": "#0066cc", "accent_hover": "#0055aa", "accent_warning": "#cc2200", "accent_success": "#008833",
        },
        "sepia": {
            "bg_main": "#1a1208", "bg_sidebar": "#221a0e", "bg_panel": "#2a200f", "bg_input": "#120e05",
            "border": "#3a2a15", "border_highlight": "#c8a04a",
            "text_primary": "#f5e6c8", "text_secondary": "#c8a878", "text_dim": "#7a6040",
            "accent_main": "#c8a04a", "accent_hover": "#aa8835", "accent_warning": "#cc3300", "accent_success": "#44aa66",
        },
        "cosmic": {
            "bg_main": "#1f004b", "bg_sidebar": "#2a005f", "bg_panel": "#350078", "bg_input": "#160035",
            "border": "#4a0090", "border_highlight": "#54efea",
            "text_primary": "#ffffff", "text_secondary": "#d0b0ff", "text_dim": "#7050a0",
            "accent_main": "#54efea", "accent_hover": "#3dccc4", "accent_warning": "#ec00f0", "accent_success": "#44ff88",
        },
        "grape": {
            "bg_main": "#1a0030", "bg_sidebar": "#22003e", "bg_panel": "#2e0055", "bg_input": "#110020",
            "border": "#440090", "border_highlight": "#ec00f0",
            "text_primary": "#ffffff", "text_secondary": "#e0b0ff", "text_dim": "#8844cc",
            "accent_main": "#ec00f0", "accent_hover": "#c400cc", "accent_warning": "#ff5555", "accent_success": "#54efea",
        },
        "ocean": {
            "bg_main": "#060810", "bg_sidebar": "#0a1020", "bg_panel": "#0d1a30", "bg_input": "#040610",
            "border": "#1a3050", "border_highlight": "#00d4ff",
            "text_primary": "#e0f0ff", "text_secondary": "#88b8d8", "text_dim": "#3a5878",
            "accent_main": "#00d4ff", "accent_hover": "#00aacc", "accent_warning": "#ff5533", "accent_success": "#41abae",
        },
        "aurora": {
            "bg_main": "#060810", "bg_sidebar": "#0a1210", "bg_panel": "#0d1e18", "bg_input": "#040810",
            "border": "#1a3028", "border_highlight": "#74f9fe",
            "text_primary": "#e8fff0", "text_secondary": "#88c8a8", "text_dim": "#3a6050",
            "accent_main": "#74f9fe", "accent_hover": "#50d8e0", "accent_warning": "#ff5959", "accent_success": "#a7fff3",
        },
    }

    def __init__(self, config_path="theme_config.json"):
        super().__init__()
        self.config_path = config_path
        self.current_theme = "cyberpunk"
        self.colors = self.DEFAULTS.copy()
        self.load_config()

    def load_config(self):
        """Carga el tema activo y colores personalizados desde el archivo de configuración."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    saved = json.load(f)
                    theme_key = saved.get("current_theme", "cyberpunk")
                    if theme_key in self.THEMES:
                        self.current_theme = theme_key
                        self.colors = self.THEMES[theme_key].copy()
                    # Apply any individual color overrides on top
                    for k, v in saved.items():
                        if k != "current_theme" and k in self.DEFAULTS:
                            self.colors[k] = v
            except Exception as e:
                log.warning("Error loading theme config: %s", e)

    def save_config(self):
        """Persiste el tema activo y la paleta de colores en disco."""
        try:
            data = {"current_theme": self.current_theme}
            data.update(self.colors)
            with open(self.config_path, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            log.warning("Error saving theme config: %s", e)

    def set_theme(self, theme_key):
        """Aplica un tema predefinido, guarda la configuración y emite theme_changed."""
        if theme_key in self.THEMES:
            self.current_theme = theme_key
            self.colors = self.THEMES[theme_key].copy()
            self.save_config()
            self.theme_changed.emit()

    def get_color(self, key):
        """Retorna un código de color Hex por su clave."""
        return self.colors.get(key, "#ff00ff")

    def tr(self, key, default=None):
        """Atajo al LocaleManager para componentes que solo inyectan el ThemeManager."""
        return LocaleManager().tr(key, default)

    def set_color(self, key, value):
        """Actualiza un color individual, guarda y notifica."""
        self.colors[key] = value
        self.save_config()
        self.theme_changed.emit()

    def get_stylesheet(self):
        """
        Genera el bloque masivo de CSS (QSS) inyectando los colores de la paleta actual.
        Define la estética global de botones, inputs, barras de desplazamiento, etc.
        """
        c = self.colors
        return f"""
            /* Reset Global y Tipografía */
            QMainWindow, QWidget {{
                background-color: {c['bg_main']};
                color: {c['text_primary']};
                font-family: 'Segoe UI', 'Roboto', sans-serif;
                font-size: 14px;
            }}
            
            /* Contenedores y Marcos */
            QFrame {{
                border: none;
            }}
            QFrame#sidebar_container {{
                background-color: {c['bg_sidebar']};
                border-right: 1px solid {c['border']};
            }}
            QFrame#drop_zone {{
                border: 2px dashed {c['border']};
                border-radius: 8px;
            }}
            QFrame#drop_zone:hover {{
                border: 2px dashed {c['accent_main']};
                background-color: {c['bg_panel']};
            }}

            /* Campos de Entrada (Inputs) */
            QLineEdit, QSpinBox, QComboBox {{
                background-color: {c['bg_input']};
                color: {c['text_primary']};
                border: 1px solid {c['border']};
                padding: 6px;
                border-radius: 2px;
            }}
            QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
                border: 1px solid {c['text_dim']};
            }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
                border: 1px solid {c['accent_main']};
                background-color: {c['bg_panel']};
            }}

            /* Menús Desplegables (ComboBox) */
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left-width: 1px;
                border-left-color: {c['border']};
                border-left-style: solid;
            }}
            
            /* El menú que se despliega (Popup) */
            QComboBox QAbstractItemView {{
                background-color: {c['bg_sidebar']} !important;
                background: {c['bg_sidebar']} !important;
                color: {c['text_primary']};
                border: 1px solid {c['accent_main']};
                selection-background-color: {c['accent_main']};
                selection-color: black;
                outline: none;
            }}

            QComboBox QAbstractItemView::item {{
                background-color: {c['bg_sidebar']};
                padding: 8px;
            }}

            /* Forzar el fondo del widget de lista interno y su zona de visualización */
            QComboBox QListView, QComboBox QListView::viewport {{
                background-color: {c['bg_sidebar']} !important;
                background: {c['bg_sidebar']} !important;
                border: none;
            }}

            /* Botones Estándar */
            QPushButton {{
                background-color: {c['bg_panel']}; 
                color: {c['text_primary']}; 
                border: 1px solid {c['border']};
                padding: 8px 16px;
                border-radius: 3px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border: 1px solid {c['accent_main']};
                color: {c['accent_main']};
            }}
            QPushButton:pressed {{
                background-color: {c['accent_main']};
                color: black;
            }}

            /* Barras de Desplazamiento (ScrollBars) */
            QScrollBar:vertical {{
                background: {c['bg_main']};
                width: 8px;
            }}
            QScrollBar::handle:vertical {{
                background: {c['border']};
                min-height: 20px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {c['accent_main']};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """
