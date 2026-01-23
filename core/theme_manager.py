import json
import os
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

class ThemeManager(QObject):
    """
    Manages application-wide themes and colors.
    Supports hot-reloading of styles.
    """
    theme_changed = Signal()

    DEFAULTS = {
        "bg_main": "#050505",       # Very dark, almost black
        "bg_sidebar": "#0a0a0a",    # Slightly lighter
        "bg_panel": "#111111",      # Panels
        "bg_input": "#000000",      # Deep black for inputs
        "border": "#333333",        # Base border
        "border_highlight": "#00ffcc", # Accent border
        "text_primary": "#ffffff",
        "text_secondary": "#cccccc",
        "text_dim": "#666666",
        "accent_main": "#00ffcc",   # Cyber Cyan
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
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    saved = json.load(f)
                    self.colors.update(saved)
            except Exception as e:
                print(f"Error loading theme config: {e}")

    def save_config(self):
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.colors, f, indent=4)
        except Exception as e:
            print(f"Error saving theme config: {e}")

    def get_color(self, key):
        return self.colors.get(key, "#ff00ff")

    def set_color(self, key, value):
        self.colors[key] = value
        self.save_config()
        self.theme_changed.emit()

    def get_stylesheet(self):
        c = self.colors
        return f"""
            /* Global Reset */
            QMainWindow, QWidget {{
                background-color: {c['bg_main']};
                color: {c['text_primary']};
                font-family: 'Segoe UI', 'Roboto', sans-serif;
                font-size: 14px;
            }}
            
            /* Frames & Containers */
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

            /* Inputs */
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

            /* ComboBox Specifics */
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left-width: 1px;
                border-left-color: {c['border']};
                border-left-style: solid;
            }}
            QComboBox QAbstractItemView {{
                border: 2px solid {c['accent_main']};
                background-color: #050505; /* Deep black to match main bg, ensuring opacity */
                selection-background-color: {c['accent_main']};
                selection-color: black;
                outline: none;
            }}
            QComboBox QAbstractItemView::item {{
                height: 25px; /* Ensure good touch target */
                color: {c['text_primary']};
            }}
            QComboBox QAbstractItemView::item:selected {{
                background-color: {c['accent_main']};
                color: black;
            }}

            /* Checkboxes - High Contrast */
            QCheckBox {{
                spacing: 8px;
                color: {c['text_primary']};
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border: 1px solid {c['text_dim']};
                background: {c['bg_input']};
                border-radius: 2px;
            }}
            QCheckBox::indicator:hover {{
                border: 1px solid {c['accent_main']};
            }}
            QCheckBox::indicator:checked {{
                background: {c['accent_main']};
                border: 1px solid {c['accent_main']};
                image: url(none); /* We use background color for now unless we have an icon */
            }}
            /* Add a pseudo-check symbol logic usually requires an image, 
               but we can simulate 'filled' as checked for now logic */

            /* Buttons */
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
            /* Primary Action Button overrides are handled in module code usually, 
               but we can target object names if standardized */

            /* Scrollbars */
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
