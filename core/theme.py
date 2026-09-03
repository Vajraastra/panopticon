"""Fachada de compatibilidad sobre el tema activo.

`Theme.BG_PANEL` y compania eran constantes: una segunda paleta, fija y ajena al
ThemeManager, que nueve de los trece modulos seguian usando. El resultado era que
esos modulos se pintaban con el gris de Dracula sobre el tema elegido, y ninguna de
sus superficies respetaba el contraste que el ThemeManager si garantiza.

Aqui esos nombres pasan a ser vistas del tema activo: mismo nombre, mismo uso, pero
el color sale de `ThemeManager.get_color()`. Asi los modulos que ya la usan heredan
el tema sin tocarlos.

Para codigo nuevo, pedir el color al `theme_manager` inyectado en `on_load` — es la
via directa; esta fachada existe solo para lo que ya estaba escrito.
"""
from core.theme_manager import ThemeManager


class _ThemeMeta(type):
    """Resuelve los nombres historicos contra la paleta activa.

    `__getattr__` de la metaclase solo se consulta cuando el atributo NO existe en la
    clase, que es justo por lo que aqui no quedan constantes definidas.
    """

    # Nombre historico -> clave de la paleta. ACCENT_ACTION y ACCENT_FASHION eran el
    # amarillo y el rosa de Dracula, sin equivalente semantico: se mapean al acento
    # que mas se les parece en funcion (llamar la atencion / decorar).
    _ALIASES = {
        "BG_MAIN": "bg_main",
        "BG_SIDEBAR": "bg_sidebar",
        "BG_PANEL": "bg_panel",
        "BG_INPUT": "bg_input",
        "BORDER": "border",
        "BORDER_HOVER": "border_strong",
        "TEXT_PRIMARY": "text_primary",
        "TEXT_SECONDARY": "text_secondary",
        "TEXT_DIM": "text_dim",
        "ACCENT_MAIN": "accent_main",
        "ACCENT_INFO": "accent_info",
        "ACCENT_SUCCESS": "accent_success",
        "ACCENT_WARNING": "accent_warning",
        "ACCENT_ACTION": "accent_warning",
        "ACCENT_FASHION": "accent_info",
    }

    def __getattr__(cls, name):
        key = cls._ALIASES.get(name)
        if key is None:
            raise AttributeError(f"Theme no define {name!r}")
        return ThemeManager.active().get_color(key)


class Theme(metaclass=_ThemeMeta):
    """Colores y estilos del tema activo (ver el docstring del modulo)."""

    @staticmethod
    def _c(key):
        return ThemeManager.active().get_color(key)

    @staticmethod
    def get_button_style(accent_color=None):
        """Boton secundario: fondo de panel, acento en el texto y en el borde al pasar."""
        accent = accent_color or Theme._c("accent_main")
        return f"""
            QPushButton {{
                background-color: {Theme._c('bg_panel')};
                color: {accent};
                border-radius: 6px;
                font-weight: bold;
                font-size: 11px;
                padding: 5px 10px;
                border: 1px solid {Theme._c('border_strong')};
            }}
            QPushButton:hover {{
                background-color: {Theme._c('bg_sidebar')};
                border: 1px solid {accent};
            }}
            QPushButton:pressed {{
                background-color: {Theme._c('bg_input')};
            }}
            QPushButton:disabled {{
                background-color: {Theme._c('bg_main')};
                border: 1px solid {Theme._c('border')};
                color: {Theme._c('text_dim')};
            }}
        """

    @staticmethod
    def get_action_button_style(bg_color=None, text_color=None):
        """Boton principal: el acento como fondo.

        El texto va sobre el acento, asi que su color NO puede ser negro fijo: en los
        temas claros el acento es oscuro y el negro encima queda ilegible. Por defecto
        se usa el fondo base, que contrasta con el acento en ambas polaridades.
        """
        bg = bg_color or Theme._c("accent_main")
        fg = text_color or Theme._c("bg_main")
        return f"""
            QPushButton {{
                background-color: {bg};
                color: {fg};
                border: none;
                border-radius: 8px;
                font-size: 14px;
                font-weight: bold;
                padding: 10px;
            }}
            QPushButton:hover {{
                background-color: {Theme._c('accent_hover')};
            }}
            QPushButton:disabled {{
                background-color: {Theme._c('bg_panel')};
                color: {Theme._c('text_dim')};
            }}
        """

    @staticmethod
    def get_input_style(accent_color=None):
        """Campo de entrada. Borde fuerte: es un control, no una superficie."""
        accent = accent_color or Theme._c("accent_main")
        return f"""
            QLineEdit, QSpinBox, QComboBox {{
                background: {Theme._c('bg_input')};
                color: {Theme._c('text_primary')};
                border: 1px solid {Theme._c('border_strong')};
                padding: 5px;
                border-radius: 4px;
                selection-background-color: {accent};
                selection-color: {Theme._c('bg_main')};
            }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
                border: 1px solid {accent};
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QComboBox QAbstractItemView {{
                background-color: {Theme._c('bg_sidebar')};
                color: {Theme._c('text_primary')};
                border: 1px solid {Theme._c('border_strong')};
                selection-background-color: {accent};
                selection-color: {Theme._c('bg_main')};
                outline: none;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background: transparent;
            }}
        """

    @staticmethod
    def get_card_style(accent_color=None):
        """Tarjeta: superficie elevada con borde que responde al puntero."""
        accent = accent_color or Theme._c("accent_main")
        return f"""
            QFrame {{
                background-color: {Theme._c('bg_panel')};
                border: 2px solid {Theme._c('border')};
                border-radius: 15px;
            }}
            QFrame:hover {{
                border: 2px solid {accent};
                background-color: {Theme._c('bg_sidebar')};
            }}
        """
