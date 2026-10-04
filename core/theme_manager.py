import colorsys
import json
import os
import logging
import tempfile
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from core.locale_manager import LocaleManager

log = logging.getLogger(__name__)


def _to_hex(rgb):
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


def _luminance(hex_color):
    """Luminancia relativa WCAG."""
    h = hex_color.lstrip("#")
    chan = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        chan.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return .2126 * chan[0] + .7152 * chan[1] + .0722 * chan[2]


def _contrast(a, b):
    """Razón de contraste WCAG entre dos colores."""
    l1, l2 = _luminance(a), _luminance(b)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + .05) / (l2 + .05)

class ThemeManager(QObject):
    """
    Gestor de temas y estilos visuales de la aplicación.
    Maneja una paleta de colores global, permite la persistencia de cambios
    en un archivo JSON y genera hojas de estilo (QSS) dinámicas.
    """
    theme_changed = Signal() # Se emite cuando se actualiza un color para refrescar la UI

    # Las paletas se generan, no se eligen a ojo: para cada tema se fija un tono de
    # identidad y se derivan las superficies por LUMINANCIA objetivo (input hundido <
    # sidebar < main < panel elevado), los textos y los dos niveles de borde por su
    # contraste contra la superficie más clara, y los cinco acentos por tono, separados
    # >=38 grados entre sí para que ninguno se confunda con otro. Todas las combinaciones
    # que la UI puede producir superan WCAG AA. El generador vive en `scripts/`.
    #
    # Subir esto invalida los colores guardados en theme_config.json: la paleta anterior
    # se persistía entera como "overrides" y taparía la nueva al arrancar.
    PALETTE_VERSION = 2

    # Paleta por defecto (= tema Cyberpunk).
    DEFAULTS = {
        "bg_main": "#171f1d", "bg_sidebar": "#0f1513", "bg_panel": "#23302d", "bg_input": "#090d0c",
        "border": "#314843", "border_strong": "#4c6f68", "border_highlight": "#73dec9",
        "text_primary": "#d7dfdd", "text_secondary": "#afbdba", "text_dim": "#839a95",
        "accent_main": "#73dec9", "accent_hover": "#a4eadc", "accent_info": "#73a5de",
        "accent_success": "#73de84", "accent_warning": "#dec073", "accent_danger": "#de7c73",
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

    THEME_ICONS = {
        "cyberpunk": "⚡",
        "midnight": "🌙",
        "forest": "🌿",
        "slate": "🪨",
        "light": "☀️",
        "sepia": "📜",
        "cosmic": "🔮",
        "grape": "🍇",
        "ocean": "🌊",
        "aurora": "🌌",
    }

    THEMES = {
        "cyberpunk": {
            "bg_main": "#171f1d", "bg_sidebar": "#0f1513", "bg_panel": "#23302d", "bg_input": "#090d0c",
            "border": "#314843", "border_strong": "#4c6f68", "border_highlight": "#73dec9",
            "text_primary": "#d7dfdd", "text_secondary": "#afbdba", "text_dim": "#839a95",
            "accent_main": "#73dec9", "accent_hover": "#a4eadc", "accent_info": "#73a5de",
            "accent_success": "#73de84", "accent_warning": "#dec073", "accent_danger": "#de7c73",
        },
        "midnight": {
            "bg_main": "#171d26", "bg_sidebar": "#0f1419", "bg_panel": "#242e3a", "bg_input": "#090c0f",
            "border": "#32445d", "border_strong": "#4e6b90", "border_highlight": "#6fa1e2",
            "text_primary": "#d8dee5", "text_secondary": "#b0bbca", "text_dim": "#8596ae",
            "accent_main": "#6fa1e2", "accent_hover": "#a2c2ec", "accent_info": "#9d8be4",
            "accent_success": "#73deac", "accent_warning": "#dec073", "accent_danger": "#de7c73",
        },
        "forest": {
            "bg_main": "#151f18", "bg_sidebar": "#0e1511", "bg_panel": "#203026", "bg_input": "#080d0a",
            "border": "#2c4936", "border_strong": "#457354", "border_highlight": "#76db98",
            "text_primary": "#d4dfd8", "text_secondary": "#a9bfb1", "text_dim": "#7b9d86",
            "accent_main": "#76db98", "accent_hover": "#a6e7bc", "accent_info": "#769edb",
            "accent_success": "#76dbd7", "accent_warning": "#dbbe76", "accent_danger": "#db7e76",
        },
        "slate": {
            "bg_main": "#1c1d20", "bg_sidebar": "#131315", "bg_panel": "#2b2d31", "bg_input": "#0b0b0d",
            "border": "#3f434c", "border_strong": "#626975", "border_highlight": "#8b9ec6",
            "text_primary": "#dbdcdf", "text_secondary": "#b8babe", "text_dim": "#91959c",
            "accent_main": "#8b9ec6", "accent_hover": "#b4c1da", "accent_info": "#a38bda",
            "accent_success": "#7bd5ab", "accent_warning": "#d5bc7b", "accent_danger": "#d5837b",
        },
        "light": {
            "bg_main": "#e2e5e8", "bg_sidebar": "#eef0f2", "bg_panel": "#feffff", "bg_input": "#fafafb",
            "border": "#afb7c1", "border_strong": "#778496", "border_highlight": "#1f64bd",
            "text_primary": "#2e363f", "text_secondary": "#485363", "text_dim": "#586679",
            "accent_main": "#1f64bd", "accent_hover": "#1f64bd", "accent_info": "#684dd5",
            "accent_success": "#1b744a", "accent_warning": "#7d621d", "accent_danger": "#b7372b",
        },
        "sepia": {
            "bg_main": "#211c15", "bg_sidebar": "#17130e", "bg_panel": "#332b20", "bg_input": "#0d0b09",
            "border": "#4f412d", "border_strong": "#7b6546", "border_highlight": "#dbb276",
            "text_primary": "#e3dcd1", "text_secondary": "#c7b8a3", "text_dim": "#a89270",
            "accent_main": "#dbb276", "accent_hover": "#e7cda6", "accent_info": "#76a5db",
            "accent_success": "#76dbac", "accent_warning": "#c3db76", "accent_danger": "#dc7a7d",
        },
        "cosmic": {
            "bg_main": "#231a2b", "bg_sidebar": "#17111d", "bg_panel": "#352642", "bg_input": "#0d0a11",
            "border": "#52376b", "border_strong": "#8155a8", "border_highlight": "#b37de3",
            "text_primary": "#e2dae8", "text_secondary": "#c4b4d1", "text_dim": "#a38cb8",
            "accent_main": "#b37de3", "accent_hover": "#c9a3eb", "accent_info": "#73a5de",
            "accent_success": "#73deac", "accent_warning": "#dec073", "accent_danger": "#de7c73",
        },
        "grape": {
            "bg_main": "#251929", "bg_sidebar": "#18111b", "bg_panel": "#39263f", "bg_input": "#0e0a10",
            "border": "#583663", "border_strong": "#89539b", "border_highlight": "#c878e3",
            "text_primary": "#e4dae7", "text_secondary": "#c8b3cf", "text_dim": "#a98ab4",
            "accent_main": "#c878e3", "accent_hover": "#d9a2ec", "accent_info": "#73a5de",
            "accent_success": "#73deac", "accent_warning": "#dec073", "accent_danger": "#de7c73",
        },
        "ocean": {
            "bg_main": "#141f22", "bg_sidebar": "#0e1517", "bg_panel": "#203035", "bg_input": "#080c0e",
            "border": "#2b4751", "border_strong": "#426e7f", "border_highlight": "#6fc3e2",
            "text_primary": "#d3dee3", "text_secondary": "#a6bdc6", "text_dim": "#769ba8",
            "accent_main": "#6fc3e2", "accent_hover": "#a2d8ec", "accent_info": "#8790e3",
            "accent_success": "#73deac", "accent_warning": "#dec073", "accent_danger": "#de7c73",
        },
        "aurora": {
            "bg_main": "#14201f", "bg_sidebar": "#0e1515", "bg_panel": "#1f302f", "bg_input": "#080c0c",
            "border": "#294846", "border_strong": "#40716d", "border_highlight": "#74dcd5",
            "text_primary": "#d2dfdf", "text_secondary": "#a4bfbe", "text_dim": "#729d9a",
            "accent_main": "#74dcd5", "accent_hover": "#a5e9e4", "accent_info": "#74a1dc",
            "accent_success": "#74dc93", "accent_warning": "#dcbf74", "accent_danger": "#dc7d74",
        },
    }

    # Instancia en uso. `main.py` construye una sola y la inyecta en los módulos, pero
    # la fachada `core.theme.Theme` no recibe inyección: la necesita para resolver los
    # nombres históricos (Theme.BG_PANEL y compañía) contra la paleta viva.
    _active = None

    @classmethod
    def active(cls):
        """El ThemeManager en uso, o uno con la paleta por defecto si aún no hay."""
        if cls._active is None:
            cls._active = cls()
        return cls._active

    def __init__(self, config_path="theme_config.json"):
        super().__init__()
        self.config_path = config_path
        self.current_theme = "cyberpunk"
        self.colors = self.DEFAULTS.copy()
        self.load_config()
        ThemeManager._active = self

    def load_config(self):
        """Carga el tema activo y colores personalizados desde el archivo de configuración."""
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path, 'r') as f:
                saved = json.load(f)
            if not isinstance(saved, dict):
                raise ValueError("Theme configuration must be an object")
        except Exception as e:
            log.warning("Error loading theme config: %s", e)
            return

        theme_key = saved.get("current_theme", "cyberpunk")
        if theme_key in self.THEMES:
            self.current_theme = theme_key
            self.colors = self.THEMES[theme_key].copy()

        # Los overrides de una paleta anterior son la paleta vieja completa, no una
        # personalización: aplicarlos dejaría el tema nuevo sin efecto.
        if saved.get("palette_version") != self.PALETTE_VERSION:
            log.info("theme: paleta v%s descartada, se adopta v%s",
                     saved.get("palette_version"), self.PALETTE_VERSION)
            self.save_config()
            return

        for k, v in saved.items():
            if k not in ("current_theme", "palette_version") and k in self.DEFAULTS:
                self.colors[k] = v

    def save_config(self):
        """Persiste el tema activo y los colores personalizados en disco.

        Solo se guarda lo que se APARTA del tema: volcar la paleta entera la convertía
        en overrides que tapaban cualquier retoque posterior de `THEMES` (es lo que
        obligó a la migración de PALETTE_VERSION).
        """
        temporary = None
        try:
            base = self.THEMES.get(self.current_theme, self.DEFAULTS)
            data = {"current_theme": self.current_theme,
                    "palette_version": self.PALETTE_VERSION}
            data.update({k: v for k, v in self.colors.items() if base.get(k) != v})
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8',
                dir=os.path.dirname(os.path.abspath(self.config_path)),
                prefix='.theme-', suffix='.tmp', delete=False,
            ) as f:
                temporary = f.name
                json.dump(data, f, indent=4)
            os.replace(temporary, self.config_path)
        except Exception as e:
            log.warning("Error saving theme config: %s", e)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError as e:
                    log.warning("Error removing temporary theme config: %s", e)

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

    def themed_accent(self, hex_color):
        """Reencaja el acento de un módulo en la paleta activa.

        Cada módulo declara un `accent_color` que es su identidad visual. Como eran
        hex fijos, los once fallaban AA en algún tema: sobre el tema claro llegaban a
        1.12:1 (invisibles) y sobre los oscuros a 12:1, que deslumbra. Aquí se
        conserva el TONO —lo que distingue una herramienta de otra— y se le dan el
        croma y la claridad del acento del tema, de modo que el módulo se siga
        reconociendo y el contraste lo garantice la paleta.
        """
        try:
            h = hex_color.lstrip("#")
            rgb = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        except (AttributeError, ValueError, IndexError):
            return self.get_color("accent_main")

        hue = colorsys.rgb_to_hls(*rgb)[0]
        ref = self.get_color("accent_main").lstrip("#")
        ref_rgb = tuple(int(ref[i:i + 2], 16) / 255 for i in (0, 2, 4))
        _, light, sat = colorsys.rgb_to_hls(*ref_rgb)

        panel = self.get_color("bg_panel")
        # El tono cambia la luminancia aunque claridad y croma no se muevan (un azul
        # es más oscuro que un amarillo con el mismo HSL), así que se corrige hasta
        # recuperar AA, en la dirección que marque la polaridad del tema.
        step = -0.02 if _luminance(panel) > 0.18 else 0.02
        out = _to_hex(colorsys.hls_to_rgb(hue, light, sat))
        for _ in range(40):
            if _contrast(out, panel) >= 4.5:
                break
            light = min(0.97, max(0.03, light + step))
            out = _to_hex(colorsys.hls_to_rgb(hue, light, sat))
        return out

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

        Hay dos niveles de borde y no da igual cuál se use: `border` separa superficies
        (marcos, cabeceras, líneas divisorias) y `border_strong` dibuja lo que el usuario
        puede accionar o escribir (casillas, campos, botones). Un control pintado con
        `border` se pierde contra el fondo — que es justo lo que le pasaba a las casillas.
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
            QWidget:disabled {{ color: {c['text_dim']}; }}

            /* Contenedores y Marcos */
            QFrame {{
                border: none;
            }}
            QFrame#sidebar_container {{
                background-color: {c['bg_sidebar']};
                border-right: 1px solid {c['border']};
            }}
            QFrame#drop_zone {{
                border: 2px dashed {c['border_strong']};
                border-radius: 8px;
            }}
            QFrame#drop_zone:hover {{
                border: 2px dashed {c['accent_main']};
                background-color: {c['bg_panel']};
            }}
            QGroupBox {{
                border: 1px solid {c['border']};
                border-radius: 4px;
                margin-top: 10px;
                padding-top: 8px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: {c['accent_main']};
            }}
            QSplitter::handle {{ background-color: {c['border']}; }}
            QSplitter::handle:horizontal {{ width: 1px; }}
            QSplitter::handle:vertical {{ height: 1px; }}
            QSplitter::handle:hover {{ background-color: {c['accent_main']}; }}

            /* Campos de Entrada — borde fuerte: son controles, no superficies */
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
                background-color: {c['bg_input']};
                color: {c['text_primary']};
                border: 1px solid {c['border_strong']};
                padding: 6px;
                border-radius: 3px;
                selection-background-color: {c['accent_main']};
                selection-color: {c['bg_main']};
            }}
            QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
            QPlainTextEdit:hover, QTextEdit:hover {{
                border: 1px solid {c['accent_hover']};
            }}
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
            QPlainTextEdit:focus, QTextEdit:focus {{
                border: 1px solid {c['accent_main']};
            }}
            QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
                background-color: {c['bg_panel']};
                border: 1px solid {c['border']};
                color: {c['text_dim']};
            }}

            /* Menús Desplegables (ComboBox) */
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left-width: 1px;
                border-left-color: {c['border_strong']};
                border-left-style: solid;
            }}

            /* El menú que se despliega (Popup) */
            QComboBox QAbstractItemView {{
                background-color: {c['bg_sidebar']} !important;
                background: {c['bg_sidebar']} !important;
                color: {c['text_primary']};
                border: 1px solid {c['accent_main']};
                selection-background-color: {c['accent_main']};
                selection-color: {c['bg_main']};
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

            /* Casillas y radios. El indicador se dibuja aquí: sin estas reglas Qt lo pinta
               con su paleta nativa y desaparece sobre las superficies del tema. */
            QCheckBox, QRadioButton {{
                spacing: 8px;
                background: transparent;
                padding: 2px;
            }}
            QCheckBox::indicator, QRadioButton::indicator {{
                width: 16px;
                height: 16px;
                background-color: {c['bg_input']};
                border: 2px solid {c['border_strong']};
            }}
            QCheckBox::indicator {{ border-radius: 3px; }}
            QRadioButton::indicator {{ border-radius: 10px; }}
            QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
                border-color: {c['accent_main']};
                background-color: {c['bg_panel']};
            }}
            QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
                background-color: {c['accent_main']};
                border-color: {c['accent_main']};
            }}
            QCheckBox::indicator:checked:hover, QRadioButton::indicator:checked:hover {{
                background-color: {c['accent_hover']};
                border-color: {c['accent_hover']};
            }}
            QCheckBox::indicator:indeterminate {{
                background-color: {c['text_dim']};
                border-color: {c['border_strong']};
            }}
            QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
                border-color: {c['border']};
                background-color: {c['bg_panel']};
            }}
            QCheckBox::indicator:checked:disabled {{ background-color: {c['text_dim']}; }}

            /* Botones Estándar */
            QPushButton {{
                background-color: {c['bg_panel']};
                color: {c['text_primary']};
                border: 1px solid {c['border_strong']};
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
                color: {c['bg_main']};
            }}
            QPushButton:disabled {{
                background-color: {c['bg_main']};
                border: 1px solid {c['border']};
                color: {c['text_dim']};
            }}

            /* Tablas y listas. Van sobre la superficie BASE, no sobre la elevada: una
               tabla ocupa media pantalla y `bg_panel` en esa superficie deja el area
               dominante en el nivel mas claro, que es justo lo que cansa la vista. */
            QTableView, QTreeView, QListView, QListWidget, QTableWidget {{
                background-color: {c['bg_main']};
                alternate-background-color: {c['bg_sidebar']};
                border: 1px solid {c['border']};
                gridline-color: {c['border']};
                selection-background-color: {c['accent_main']};
                selection-color: {c['bg_main']};
                outline: none;
            }}
            QTableView::item, QTreeView::item, QListView::item, QListWidget::item {{
                padding: 4px;
                border: none;
            }}
            QTableView::item:hover, QTreeView::item:hover, QListWidget::item:hover {{
                background-color: {c['bg_sidebar']};
            }}
            QHeaderView::section {{
                background-color: {c['bg_sidebar']};
                color: {c['text_secondary']};
                padding: 6px;
                border: none;
                border-right: 1px solid {c['border']};
                border-bottom: 1px solid {c['border_strong']};
                font-weight: bold;
            }}
            QHeaderView::section:hover {{ color: {c['accent_main']}; }}

            /* Pestañas */
            QTabWidget::pane {{
                border: 1px solid {c['border']};
                background-color: {c['bg_panel']};
            }}
            QTabBar::tab {{
                background-color: {c['bg_main']};
                color: {c['text_dim']};
                border: 1px solid {c['border']};
                border-bottom: none;
                padding: 7px 14px;
            }}
            QTabBar::tab:hover {{ color: {c['text_primary']}; }}
            QTabBar::tab:selected {{
                background-color: {c['bg_panel']};
                color: {c['accent_main']};
                border-bottom: 2px solid {c['accent_main']};
            }}

            /* Progreso, menús y tooltips */
            QProgressBar {{
                background-color: {c['bg_input']};
                border: 1px solid {c['border_strong']};
                border-radius: 3px;
                text-align: center;
                color: {c['text_primary']};
            }}
            QProgressBar::chunk {{
                background-color: {c['accent_main']};
                border-radius: 2px;
            }}
            QMenu {{
                background-color: {c['bg_sidebar']};
                border: 1px solid {c['border_strong']};
                color: {c['text_primary']};
            }}
            QMenu::item:selected {{
                background-color: {c['accent_main']};
                color: {c['bg_main']};
            }}
            QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
            QToolTip {{
                background-color: {c['bg_sidebar']};
                color: {c['text_primary']};
                border: 1px solid {c['accent_main']};
                padding: 5px;
            }}

            /* Barras de Desplazamiento (ScrollBars) */
            QScrollBar:vertical {{
                background: {c['bg_main']};
                width: 10px;
                margin: 0;
            }}
            QScrollBar:horizontal {{
                background: {c['bg_main']};
                height: 10px;
                margin: 0;
            }}
            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
                background: {c['border_strong']};
                min-height: 24px;
                min-width: 24px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
                background: {c['accent_main']};
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        """
