"""Navegador de imágenes compartido (U8): «◀ Anterior», contador y «Siguiente ▶».

Un solo componente para los visores que recorren un conjunto de imágenes, para
que todos se vean y respondan igual: mismos textos, contador «N / total» y
atajos de teclado (←/→ por defecto). El módulo decide qué significa moverse
(guardar antes, esperar a un trabajo, dar la vuelta) escuchando ``navigate``.

Los atajos usan ``QShortcut`` en el contexto de la ventana: los campos de texto
siguen recibiendo las flechas, porque Qt les deja reclamar esas teclas antes
que cualquier atajo.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from core.components.ui_roles import set_role

KEYS_ARROWS = ('Left', 'Right')
KEYS_ALT_ARROWS = ('Alt+Left', 'Alt+Right')


class ImageNavigator(QWidget):
    """Barra «◀ Anterior · N / total · Siguiente ▶».

    ``navigate`` emite -1 o +1. ``set_position`` actualiza contador y estado;
    con ``wrap=True`` (visor de Gallery) ambos botones siguen activos en los
    extremos y es el módulo quien da la vuelta.
    """

    navigate = Signal(int)

    def __init__(self, tr, parent=None, *, prev_tip=None, next_tip=None,
                 keys=KEYS_ARROWS, wrap=False, show_name=False):
        super().__init__(parent)
        self._tr = tr
        self._wrap = wrap
        self._show_name = show_name
        self._enabled = True
        self._index = 0
        self._total = 0
        self._shortcuts = []
        self._keys = tuple(keys or ())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.btn_prev = QPushButton(tr('nav.prev', '◀ Previous'))
        self.btn_next = QPushButton(tr('nav.next', 'Next ▶'))
        self.lbl_position = QLabel()
        set_role(self.lbl_position, 'hint')
        self.lbl_position.setAlignment(Qt.AlignCenter)
        self.lbl_position.setToolTip(tr(
            'nav.position_tip', 'Position of the displayed image in the loaded set.'))

        prev_tip = prev_tip or tr('nav.prev_tip', 'Show the previous image.')
        next_tip = next_tip or tr('nav.next_tip', 'Show the next image.')
        if self._keys:
            prev_tip = f"{prev_tip} ({self._key_label(self._keys[0])})"
            next_tip = f"{next_tip} ({self._key_label(self._keys[1])})"
        self.btn_prev.setToolTip(prev_tip)
        self.btn_next.setToolTip(next_tip)
        for button in (self.btn_prev, self.btn_next):
            button.setFocusPolicy(Qt.NoFocus)  # las flechas no mueven el foco a los botones
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(1))

        layout.addWidget(self.btn_prev)
        layout.addWidget(self.lbl_position)
        layout.addWidget(self.btn_next)
        self.set_position(0, 0)

    @staticmethod
    def _key_label(sequence):
        return sequence.replace('Left', '←').replace('Right', '→')

    def install_shortcuts(self, window):
        """Activa los atajos en ``window`` (la ventana o diálogo del visor)."""
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        for sequence, step in zip(self._keys, (-1, 1)):
            shortcut = QShortcut(QKeySequence(sequence), window)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(lambda step=step: self._step(step))
            self._shortcuts.append(shortcut)

    def _step(self, step):
        button = self.btn_prev if step < 0 else self.btn_next
        if button.isEnabled():
            self.navigate.emit(step)

    def set_position(self, index, total, name=None, enabled=True):
        """Muestra «index+1 / total» y habilita según extremos y ``enabled``."""
        self._index, self._total, self._enabled = index, total, enabled
        current = index + 1 if total else 0
        text = self._tr('nav.position', '{current} / {total}').format(current=current, total=total)
        if self._show_name and name:
            text = f"{text}  ·  {name}"
        self.lbl_position.setText(text)
        if self._wrap:
            can_prev = can_next = total > 1
        else:
            can_prev = index > 0
            can_next = index + 1 < total
        self.btn_prev.setEnabled(enabled and can_prev)
        self.btn_next.setEnabled(enabled and can_next)
