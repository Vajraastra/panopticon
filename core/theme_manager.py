import json
import os
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from core.locale_manager import LocaleManager

class ThemeManager(QObject):
    """
    Gestor de temas y estilos visuales de la aplicación.
    Maneja una paleta de colores global, permite la persistencia de cambios
    en un archivo JSON y genera hojas de estilo (QSS) dinámicas.
    """
    theme_changed = Signal() # Se emite cuando se actualiza un color para refrescar la UI

    # Colores por defecto (Aestética Cyber/Dark)
    DEFAULTS = {
        "bg_main": "#050505",       # Fondo principal (casi negro)
        "bg_sidebar": "#0a0a0a",    # Fondo lateral (ligeramente más claro)
        "bg_panel": "#111111",      # Fondos de paneles y tarjetas
        "bg_input": "#000000",      # Negro puro para los inputs
        "border": "#333333",        # Bordes base
        "border_highlight": "#00ffcc", # Bordes de acento (Cyan)
        "text_primary": "#ffffff",
        "text_secondary": "#cccccc",
        "text_dim": "#666666",
        "accent_main": "#00ffcc",   # Acento principal Cyber Cyan
        "accent_hover": "#00ccaa",
        "accent_warning": "#ff3333",
        "accent_success": "#00ff66",
    }

    def __init__(self, config_path="theme_config.json"):
        super().__init__()
        self.config_path = config_path
        self.colors = self.DEFAULTS.copy()
        self.load_config()

    def load_config(self):
        """Carga los colores personalizados desde el archivo de configuración si existe."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    saved = json.load(f)
                    self.colors.update(saved)
            except Exception as e:
                print(f"Error loading theme config: {e}")

    def save_config(self):
        """Persiste la paleta de colores actual en disco."""
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.colors, f, indent=4)
        except Exception as e:
            print(f"Error saving theme config: {e}")

    def get_color(self, key):
        """Retorna un código de color Hex por su clave."""
        return self.colors.get(key, "#ff00ff")

    def tr(self, key, default=None):
        """Atajo al LocaleManager para componentes que solo inyectan el ThemeManager."""
        return LocaleManager().tr(key, default)

    def set_color(self, key, value):
        """Actualiza un color, guarda y notifica a la aplicación para refrescar estilos."""
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
