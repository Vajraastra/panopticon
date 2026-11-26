"""Roles visuales de la convención de UI (ver UI_DESIGN.md).

El estilo de cada rol vive en `ThemeManager.get_stylesheet()` (selectores `[role="…"]`);
aquí solo se asigna la propiedad, así un módulo nunca pinta colores a mano.
"""
from PySide6.QtWidgets import QLabel, QPushButton


def set_role(widget, role):
    widget.setProperty("role", role)
    # Re-evaluar el QSS si el widget ya estaba pulido (cambio de rol en caliente).
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    return widget


def primary(button: QPushButton):
    """La acción principal de la pantalla: una sola por vista."""
    return set_role(button, "primary")


def danger(button: QPushButton):
    """Acción destructiva o que descarta trabajo."""
    return set_role(button, "danger")


def link(button: QPushButton):
    """Acción secundaria discreta (enlaces del pie, «volver»)."""
    return set_role(button, "link")


def section(text: str, parent=None) -> QLabel:
    """Título de sección de la barra lateral (MAYÚSCULAS pequeñas, tenue)."""
    return set_role(QLabel(text.upper(), parent), "section")


def hint(text: str, parent=None) -> QLabel:
    """Texto de ayuda o estado secundario, con ajuste de línea."""
    label = set_role(QLabel(text, parent), "hint")
    label.setWordWrap(True)
    return label
