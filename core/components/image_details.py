"""Panel de detalles común (U9): nombre, carpeta, resolución y tamaño de una imagen.

Los visores (Gallery, Character Recognizer) mostraban estos datos cada uno a su
manera: «Filename: …», «--- x ---», KB en uno y MB en otro. Este panel los
presenta igual en todas partes; cada módulo puede añadir filas propias
(valoración, calidad) con ``add_row``. No lee archivos: el módulo le pasa los
valores que ya obtuvo en su hilo de trabajo.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from core.components.ui_roles import set_role

PLACEHOLDER = '—'
LOADING = '…'


def format_bytes(size):
    """Tamaño legible: KB por debajo de 1 MB, MB con dos decimales, GB desde 1 GB."""
    if size is None:
        return PLACEHOLDER
    kb = size / 1024.0
    if kb >= 1024 * 1024:
        return f"{kb / (1024.0 * 1024.0):.2f} GB"
    return f"{kb / 1024.0:.2f} MB" if kb >= 1024 else f"{kb:.1f} KB"


class ImageDetails(QWidget):
    """Ficha de la imagen mostrada. ``show_path`` al cambiar de imagen;
    ``set_dimensions`` y ``set_bytes`` cuando el módulo tenga los datos."""

    def __init__(self, tr, parent=None):
        super().__init__(parent)
        self._tr = tr
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.lbl_name = QLabel(tr('details.none', 'No image'))
        font = self.lbl_name.font()
        font.setBold(True)
        self.lbl_name.setFont(font)
        self.lbl_name.setWordWrap(True)
        self.lbl_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_name.setToolTip(tr('details.name_tip', 'File name. You can select it to copy it.'))
        layout.addWidget(self.lbl_name)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(3)
        self._grid.setColumnStretch(1, 1)
        self._row_names = {}
        layout.addLayout(self._grid)

        self.lbl_folder = self.add_row(tr('details.folder', 'Folder'),
                                       tr('details.folder_tip', 'Folder that contains the file. Select it to copy it.'))
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_resolution = self.add_row(tr('details.resolution', 'Resolution'),
                                           tr('details.resolution_tip', 'Width × height in pixels.'))
        self.lbl_size = self.add_row(tr('details.size', 'Size'),
                                     tr('details.size_tip', 'File size on disk.'))
        self.clear()

    def add_row(self, label, tip=None, widget=None):
        """Añade una fila «etiqueta  valor». Devuelve el widget del valor
        (un QLabel nuevo si no se pasa ``widget``)."""
        row = self._grid.rowCount()
        name = QLabel(label)
        set_role(name, 'hint')
        name.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        value = widget if widget is not None else QLabel(PLACEHOLDER)
        if tip:
            name.setToolTip(tip)
            value.setToolTip(tip)
        self._grid.addWidget(name, row, 0)
        self._grid.addWidget(value, row, 1)
        self._row_names[id(value)] = name
        return value

    def set_row_visible(self, value, visible):
        """Muestra u oculta una fila completa (etiqueta y valor) creada con ``add_row``."""
        self._row_names[id(value)].setVisible(visible)
        value.setVisible(visible)

    def clear(self, text=None):
        self.lbl_name.setText(text or self._tr('details.none', 'No image'))
        for label in (self.lbl_folder, self.lbl_resolution, self.lbl_size):
            label.setText(PLACEHOLDER)

    def show_path(self, path):
        """Nueva imagen: nombre y carpeta ya; resolución y tamaño quedan «…»."""
        self.lbl_name.setText(os.path.basename(path))
        folder = os.path.dirname(path)
        self.lbl_folder.setText(folder or PLACEHOLDER)
        self.lbl_resolution.setText(LOADING)
        self.lbl_size.setText(LOADING)

    def set_dimensions(self, width, height):
        self.lbl_resolution.setText(f"{width} × {height} px" if width and height else PLACEHOLDER)

    def set_bytes(self, size):
        self.lbl_size.setText(format_bytes(size))

    def set_unavailable(self):
        self.lbl_resolution.setText(PLACEHOLDER)
        self.lbl_size.setText(PLACEHOLDER)
