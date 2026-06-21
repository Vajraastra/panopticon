"""
Autocompletado de tags estilo booru sobre un QPlainTextEdit (sin LLM).

Observa el "token" bajo el cursor (texto entre comas) y, al escribir ≥2 chars,
muestra un popup con sugerencias del `tag_engine` (coloreadas por categoría
Danbooru, ordenadas por popularidad). Al elegir, reemplaza el token por la tag
canónica + separador.

Diseño: el popup NO roba el foco (el usuario sigue tecleando); la navegación
(↑/↓/Enter/Tab/Esc) se enruta con un event filter sobre el editor. Resuelve
alias: si el usuario escribe 'tetas', sugiere 'breasts' (alias mostrado en gris).
"""
from PySide6.QtCore import QObject, Qt, QEvent
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QApplication

from ..logic import tag_engine

# Colores por categoría Danbooru (0 general, 1 artist, 3 copyright, 4 character,
# 5 meta). Convención de tablones booru; independiente del tema de la app.
_CAT_COLOR = {
    0: "#9ad1ff",   # general  — azul claro
    1: "#ff8a65",   # artist   — naranja
    3: "#d98cff",   # copyright— magenta
    4: "#7ee787",   # character— verde
    5: "#e3c77b",   # meta     — ocre
}
_MIN_CHARS = 2       # disparo del popup
_MAX_RESULTS = 10


def _fmt_count(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n//1000}k"
    return str(n)


class TagCompleter(QObject):
    """Adjunta autocompletado de tags a un QPlainTextEdit."""

    def __init__(self, text_edit, separator=", ", parent=None):
        super().__init__(parent or text_edit)
        self.edit = text_edit
        self.separator = separator
        self.sep_char = separator.strip() or ","   # carácter de corte de token
        self._csv = ""
        self._enabled = False

        self.popup = QListWidget()
        # No debe robar el foco del editor: el usuario sigue tecleando para
        # refinar la búsqueda. WindowDoesNotAcceptFocus se lo pide al gestor de
        # ventanas (más fuerte que NoFocus, interno de Qt) — clave en Wayland,
        # donde WA_ShowWithoutActivating no siempre se respeta.
        self.popup.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        self.popup.setAttribute(Qt.WA_ShowWithoutActivating)   # no roba foco
        self.popup.setFocusPolicy(Qt.NoFocus)
        self.popup.setMouseTracking(True)
        self.popup.setUniformItemSizes(True)
        self.popup.itemClicked.connect(self._accept_item)
        # Filtramos también los eventos del popup: si pese a las banderas el
        # foco se le cuela (Wayland), reenviamos la escritura al editor.
        self.popup.installEventFilter(self)
        self.popup.setStyleSheet(
            "QListWidget { background:#1b1b1f; color:#ddd; border:1px solid #444; "
            "outline:0; } QListWidget::item:selected { background:#3a3a50; }")
        self.popup.hide()

        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._maybe_complete)

    # -- configuración --------------------------------------------------------
    def set_csv(self, csv_name):
        """Fija el CSV activo. Si está vacío o no cargado, el popup no aparece."""
        self._csv = csv_name or ""
        self._enabled = bool(self._csv) and tag_engine.is_loaded(self._csv)
        if not self._enabled:
            self.popup.hide()

    def set_enabled(self, on):
        self._enabled = bool(on) and bool(self._csv) and tag_engine.is_loaded(self._csv)
        if not self._enabled:
            self.popup.hide()

    # -- token bajo el cursor -------------------------------------------------
    def _token_bounds(self):
        """(inicio, fin, texto) del token entre el separador previo y el cursor."""
        cursor = self.edit.textCursor()
        pos = cursor.position()
        text = self.edit.toPlainText()
        start = pos
        while start > 0 and text[start - 1] != self.sep_char and text[start - 1] != "\n":
            start -= 1
        token = text[start:pos]
        return start, pos, token.lstrip()

    # -- disparo --------------------------------------------------------------
    def _maybe_complete(self):
        if not self._enabled:
            return
        _, _, token = self._token_bounds()
        token = token.strip()
        if len(token) < _MIN_CHARS:
            self.popup.hide()
            return
        results = tag_engine.search(self._csv, token, limit=_MAX_RESULTS)
        if not results:
            self.popup.hide()
            return
        self._fill(results)
        self._place()

    def _fill(self, results):
        self.popup.clear()
        for r in results:
            label = r["name"]
            alias = r.get("matched_alias")
            extra = f"  ({alias})" if alias and alias != r["name"] else ""
            item = QListWidgetItem(f"{label}{extra}   ·  {_fmt_count(r['post_count'])}")
            item.setData(Qt.UserRole, r["name"])
            item.setForeground(Qt.GlobalColor.white)
            color = _CAT_COLOR.get(r.get("category", 0))
            if color:
                from PySide6.QtGui import QColor
                item.setForeground(QColor(color))
            self.popup.addItem(item)
        self.popup.setCurrentRow(0)

    def _place(self):
        rect = self.edit.cursorRect()
        gpos = self.edit.viewport().mapToGlobal(rect.bottomLeft())
        rows = min(self.popup.count(), 8)
        h = self.popup.sizeHintForRow(0) * rows + 6 if rows else 0
        self.popup.setFixedHeight(max(h, 24))
        self.popup.setFixedWidth(max(self.edit.width() // 2, 260))
        self.popup.move(gpos)
        self.popup.show()

    # -- aceptación -----------------------------------------------------------
    def _accept_item(self, item):
        name = item.data(Qt.UserRole)
        if not name:
            return
        start, end, _ = self._token_bounds()
        cursor = self.edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
        cursor.insertText(name + self.separator)
        self.edit.setTextCursor(cursor)
        self.popup.hide()
        self.edit.setFocus()

    def _accept_current(self):
        item = self.popup.currentItem()
        if item:
            self._accept_item(item)

    # -- navegación por teclado (popup sin foco) ------------------------------
    def eventFilter(self, obj, event):
        if (event.type() == QEvent.KeyPress and self.popup.isVisible()
                and obj in (self.edit, self.popup)):
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self.popup.currentRow()
                row += 1 if key == Qt.Key_Down else -1
                row = max(0, min(self.popup.count() - 1, row))
                self.popup.setCurrentRow(row)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                self._accept_current()
                return True
            if key == Qt.Key_Escape:
                self.popup.hide()
                return True
            # Cualquier otra tecla (letras, backspace, coma, espacio): la
            # escritura manual NUNCA se bloquea en favor de la selección. Si el
            # foco quedó en el popup, reenviamos la tecla al editor para que el
            # texto se edite y la búsqueda se refine.
            if obj is self.popup:
                QApplication.sendEvent(self.edit, event)
                return True
        return super().eventFilter(obj, event)
