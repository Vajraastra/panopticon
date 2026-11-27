"""Taller (U5): Cropper, Optimizer y Watermarker en una ventana con pestañas.

Los módulos no se importan entre sí: el core aloja sus vistas y comparte el
conjunto de imágenes. Cada módulo emite ``image_set_loaded`` al aceptar
imágenes (por su botón, al soltar archivos o desde «Enviar a…») y
``current_image_changed`` al mostrar otra. Al cambiar de pestaña, el Taller
entrega el conjunto a esa herramienta si aún no lo tiene; a las que trabajan
con una sola imagen (``accepts_multiple_images = False``) les pasa la actual.

La entrega es perezosa: solo la pestaña que se abre recibe imágenes, para no
lanzar previsualizaciones en herramientas que no se están usando.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMainWindow, QTabWidget,
                               QVBoxLayout, QWidget)

from core.components.ui_roles import set_role

log = logging.getLogger(__name__)

WORKSHOP_TOOLS = ("Smart Cropper", "Image Optimizer", "Watermarker")


def _key(module, suffix):
    return f"tool.{module.name.lower().replace(' ', '_')}.{suffix}"


class WorkshopWindow(QMainWindow):
    def __init__(self, dashboard, modules):
        super().__init__(dashboard, Qt.Window)
        self._tr = dashboard.tr
        self.tools = []
        self._shared = []
        self._current = None
        self._delivered = {}
        self.setWindowTitle(f"{self._tr('tool.workshop.name', 'Workshop')} — Panopticon")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowModality(Qt.NonModal)
        self.resize(1280, 840)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        strip = QFrame()
        strip.setObjectName('workshop_strip')
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 6, 12, 6)
        self.lbl_shared = QLabel()
        set_role(self.lbl_shared, 'hint')
        self.lbl_shared.setToolTip(self._tr('workshop.shared_tip',
            'The images you open in one tab pass to the others when you switch tabs. '
            'Watermarker receives only the current image.'))
        strip_layout.addWidget(self.lbl_shared, 1)
        layout.addWidget(strip)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        for module in modules:
            try:
                view = module.get_view()
                if view is None:
                    raise RuntimeError('Module returned no view')
            except Exception:
                log.exception('Workshop could not open %s', module.name)
                continue
            index = self.tabs.addTab(view, f"{module.icon or ''} {self._tr(_key(module, 'name'), module.name)}".strip())
            self.tabs.setTabToolTip(index, self._tr(_key(module, 'desc'), module.description))
            self.tools.append(module)
            module.image_set_loaded.connect(lambda paths, m=module: self._on_loaded(m, paths))
            module.current_image_changed.connect(self._on_current)
        self.tabs.currentChanged.connect(self._deliver)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(self._tr(
            "dashboard.module_session_tip",
            "Closing this window keeps its session and jobs running. Reopen it from the dashboard."
        ), 10000)
        self._update_strip()

    # ── conjunto compartido ──────────────────────────────────────────────
    def _on_loaded(self, module, paths):
        paths = [str(path) for path in paths]
        if not module.accepts_multiple_images and len(paths) == 1 and paths[0] in self._shared:
            self._current = paths[0]  # eligió una imagen del conjunto, no lo reemplaza
        else:
            self._shared = paths
            self._current = paths[0] if paths else None
        self._delivered[module.name] = tuple(paths)
        self._update_strip()

    def _on_current(self, path):
        if path in self._shared:
            self._current = path
            self._update_strip()

    def _wanted(self, module):
        if not self._shared:
            return ()
        if module.accepts_multiple_images:
            return tuple(self._shared)
        return (self._current or self._shared[0],)

    def _deliver(self, index):
        if not 0 <= index < len(self.tools):
            return
        module = self.tools[index]
        wanted = self._wanted(module)
        if wanted and self._delivered.get(module.name) != wanted:
            # Si la herramienta lo rechaza (ocupada), no emite y se reintenta al volver.
            module.load_image_set(list(wanted))

    def _update_strip(self):
        if not self._shared:
            self.lbl_shared.setText(self._tr('workshop.shared_empty',
                'Open images in any tab; the others receive them when you switch tabs.'))
            return
        name = os.path.basename(self._current) if self._current else ''
        self.lbl_shared.setText(self._tr('workshop.shared',
            'Shared set: {count} images · current: {name}').format(count=len(self._shared), name=name))

    # ── ventana ──────────────────────────────────────────────────────────
    def module_for_tab(self, index):
        return self.tools[index] if 0 <= index < len(self.tools) else None

    def show_session(self, name=None):
        if name is not None:
            for index, module in enumerate(self.tools):
                if module.name == name:
                    self.tabs.setCurrentIndex(index)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        for module in self.tools:
            prepare_close = getattr(module, 'prepare_close', None)
            if prepare_close is not None and not prepare_close(self.close):
                event.ignore()
                return
        # Solo se oculta: el dashboard es dueño de los módulos hasta salir de la app.
        self.hide()
        event.accept()
        for module in self.tools:
            on_window_closed = getattr(module, 'on_window_closed', None)
            if on_window_closed is not None:
                on_window_closed()
