class Theme:
    # Colors
    BG_MAIN = "#121212"
    BG_SIDEBAR = "#1e1e1e"
    BG_PANEL = "#1a1a1a"
    BG_INPUT = "#222222"
    
    BORDER = "#333333"
    BORDER_HOVER = "#555555"
    
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#eeeeee"
    TEXT_DIM = "#888888"
    
    # Module Accents
    ACCENT_MAIN = "#00ffcc"    # Cyan (Panopticon/Workshop)
    ACCENT_FASHION = "#ff79c6" # Pink (Fashion Studio)
    ACCENT_WARNING = "#ff5555" # Red
    ACCENT_INFO = "#bd93f9"    # Purple
    ACCENT_ACTION = "#f1fa8c"  # Yellow
    ACCENT_SUCCESS = "#50fa7b" # Green

    @staticmethod
    def get_button_style(accent_color=ACCENT_MAIN):
        return f"""
            QPushButton {{
                background-color: #333; 
                color: {accent_color}; 
                border-radius: 6px; 
                font-weight: bold; 
                font-size: 11px;
                padding: 5px 10px;
                border: 1px solid transparent;
            }}
            QPushButton:hover {{ 
                background-color: #444; 
                border: 1px solid {accent_color};
            }}
            QPushButton:pressed {{
                background-color: #222;
            }}
        """

    @staticmethod
    def get_action_button_style(bg_color=ACCENT_MAIN, text_color="#000000"):
        return f"""
            QPushButton {{
                background-color: {bg_color};
                color: {text_color};
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                padding: 10px;
            }}
            QPushButton:hover {{
                background-color: {bg_color}dd; /* Slight transparency for hover */
            }}
            QPushButton:disabled {{
                background-color: #333;
                color: #555;
            }}
        """

    @staticmethod
    def get_input_style(accent_color=ACCENT_MAIN):
        return f"""
            QLineEdit, QSpinBox, QComboBox {{
                background: {Theme.BG_INPUT};
                color: {accent_color};
                border: 1px solid #444;
                padding: 5px;
                border-radius: 4px;
                selection-background-color: {accent_color};
                selection-color: #000;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: transparent;
            }}
        """

    @staticmethod
    def get_card_style(accent_color=ACCENT_MAIN):
        return f"""
            QFrame {{
                background-color: {Theme.BG_PANEL};
                border: 2px solid {Theme.BORDER};
                border-radius: 15px;
            }}
            QFrame:hover {{
                border: 2px solid {accent_color};
                background-color: #222;
            }}
        """
