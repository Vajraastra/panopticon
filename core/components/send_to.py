"""Menú compartido «Enviar a…» para mandar un conjunto de imágenes a otra herramienta.

Contrato (lo cablea `MainWindow.setup_integrations`, sin que los módulos se importen):
  - el módulo origen expone `set_send_targets(targets)` y la señal
    `request_open_module = Signal(str, list)` (nombre del destino, rutas);
  - `targets` es una lista de tuplas `(nombre, etiqueta, icono)` de los módulos que
    implementan `load_image_set` (ver `core.module_handoff.image_set_consumers`).
"""
from PySide6.QtWidgets import QMenu

# Pickler primero: es el destino habitual de un lote recién obtenido o filtrado.
PREFERRED_FIRST = "Pickler"


def sort_targets(targets):
    return sorted(targets, key=lambda t: (t[0] != PREFERRED_FIRST, t[1].casefold()))


def populate_send_menu(menu: QMenu, targets, on_pick, tr, tip=None, enabled=True):
    """Añade una acción por destino a `menu`. `on_pick(nombre)` se llama al elegir.

    `tip` puede ser un texto o una función `etiqueta -> texto` para el tooltip.
    Devuelve el número de acciones añadidas; sin destinos deja una acción inactiva.
    """
    menu.setToolTipsVisible(True)
    targets = sort_targets(targets)
    if not targets:
        menu.addAction(tr("common.send.no_targets", "No compatible tools available")).setEnabled(False)
        return 0
    for name, label, icon in targets:
        action = menu.addAction(f"{icon or ''} {label}".strip())
        action.setData(name)
        action.setEnabled(enabled)
        text = tip(label) if callable(tip) else (tip or tr(
            "common.send.target_tip",
            "Open these images in {tool}. Files are not moved or copied.").format(tool=label))
        action.setToolTip(text)
        if on_pick is not None:  # sin callback: quien llama lee `exec()` → action.data()
            action.triggered.connect(lambda checked=False, target=name: on_pick(target))
    return len(targets)
