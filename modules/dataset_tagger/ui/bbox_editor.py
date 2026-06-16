"""
Editor de bounding boxes del captioner Ideogram 4 (ventana aparte, patrón del
viewer de Pickler).

El usuario dibuja cajas sobre la imagen, las clasifica (obj / text), las ordena
(1 = más saliente; los `text` se empujan al final al exportar) y edita campos
globales (descripción de alto nivel, fondo, estilo 3D). Todo se persiste al
sidecar de trabajo `<imagen>.pano.json` (datamodel.WorkDoc), reanudable.

Esta fase entrega el editor + persistencia. La captura por VLM (desc/paletas) y
el mini-editor de texto llegan en fases posteriores; aquí los campos se editan a
mano y las cajas se dibujan manualmente.

Coordenadas: la escena usa los píxeles de la imagen ORIGINAL (el pixmap de fondo
va en (0,0) a tamaño completo), así `Element.bbox_px` y `rect()` coinciden 1:1.
La normalización a 0–1000 ocurre solo al exportar (datamodel.to_canonical).
"""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPixmap, QPen, QBrush, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QDialog, QGraphicsScene, QGraphicsView, QGraphicsRectItem,
    QGraphicsPixmapItem, QVBoxLayout, QHBoxLayout, QWidget, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit,
    QRadioButton, QButtonGroup, QFormLayout, QSplitter, QMessageBox,
)

from core.theme import Theme
from ..logic.ideogram.datamodel import WorkDoc, Element, STATUS_DRAFT

log = logging.getLogger(__name__)

# Tipos a dibujar (locale-safe: nunca comparar por texto)
DRAW_OBJ = 0
DRAW_TEXT = 1

OBJ_COLOR = Theme.ACCENT_MAIN       # cyan
TEXT_COLOR = Theme.ACCENT_FASHION   # rosa
MIN_BOX = 6                         # lado mínimo (px de escena) para crear una caja
HANDLE_SCREEN = 9                   # lado del handle de resize, en px de PANTALLA


def _elem_color(etype):
    return OBJ_COLOR if etype == "obj" else TEXT_COLOR


class BBoxItem(QGraphicsRectItem):
    """Caja editable asociada a un `Element`.

    Maneja mover y redimensionar manualmente (en coords de escena, pos en 0,0)
    para evitar la ambigüedad pos()/rect(). Dibuja handles de tamaño constante
    en pantalla y un badge con el número de orden.
    """

    _CORNERS = ("tl", "tr", "bl", "br")

    def __init__(self, element, on_changed=None, on_selected=None):
        super().__init__()
        self.element = element
        self.on_changed = on_changed      # callback() al cambiar geometría
        self.on_selected = on_selected    # callback(item) al hacer clic
        self.order = 0                    # posición 1-based; la fija el editor
        x0, y0, x1, y1 = element.bbox_px
        self.setRect(QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized())
        self.setFlag(QGraphicsRectItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self._drag_mode = None            # 'move' | esquina
        self._start_rect = None
        self._start_scene = None

    # -- apariencia -----------------------------------------------------------
    def _scale(self):
        """Factor de zoom de la vista (para handles de tamaño constante)."""
        views = self.scene().views() if self.scene() else []
        return views[0].transform().m11() if views else 1.0

    def _handle_size(self):
        return HANDLE_SCREEN / max(self._scale(), 1e-6)

    def _corner_points(self):
        r = self.rect()
        return {"tl": r.topLeft(), "tr": r.topRight(),
                "bl": r.bottomLeft(), "br": r.bottomRight()}

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, False)
        color = QColor(_elem_color(self.element.type))
        selected = self.isSelected()
        pen = QPen(color, (2.5 if selected else 1.5) / max(self._scale(), 1e-6))
        painter.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(48 if selected else 24)
        painter.setBrush(QBrush(fill))
        painter.drawRect(self.rect())

        # Badge de orden (número), arriba-izquierda
        hs = self._handle_size()
        r = self.rect()
        font = QFont()
        font.setPixelSize(max(int(hs * 1.6), 1))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(color))
        label = f"{self.order}·{'T' if self.element.type == 'text' else 'O'}"
        painter.drawText(r.topLeft() + QPointF(hs * 0.4, hs * 1.8), label)

        # Handles de esquina (solo si seleccionado)
        if selected:
            painter.setBrush(QBrush(color))
            painter.setPen(QPen(QColor("#000000"), 0))
            for p in self._corner_points().values():
                painter.drawRect(QRectF(p.x() - hs / 2, p.y() - hs / 2, hs, hs))

    def boundingRect(self):
        hs = self._handle_size()
        return self.rect().adjusted(-hs, -hs * 2, hs, hs)

    # -- interacción ----------------------------------------------------------
    def _corner_at(self, pos):
        hs = self._handle_size()
        for name, p in self._corner_points().items():
            if abs(pos.x() - p.x()) <= hs and abs(pos.y() - p.y()) <= hs:
                return name
        return None

    def mousePressEvent(self, event):
        self.setSelected(True)
        if self.on_selected:
            self.on_selected(self)
        self._start_rect = QRectF(self.rect())
        self._start_scene = event.scenePos()
        self._drag_mode = self._corner_at(event.pos()) or "move"
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            return
        delta = event.scenePos() - self._start_scene
        r = QRectF(self._start_rect)
        if self._drag_mode == "move":
            r.translate(delta)
        else:
            if "l" in self._drag_mode:
                r.setLeft(r.left() + delta.x())
            if "r" in self._drag_mode:
                r.setRight(r.right() + delta.x())
            if "t" in self._drag_mode:
                r.setTop(r.top() + delta.y())
            if "b" in self._drag_mode:
                r.setBottom(r.bottom() + delta.y())
        self.prepareGeometryChange()
        self.setRect(r.normalized())

    def mouseReleaseEvent(self, event):
        self._drag_mode = None
        self._sync_to_element()
        if self.on_changed:
            self.on_changed()
        event.accept()

    def _sync_to_element(self):
        """Vuelca el rect actual (clamp al pixmap) a element.bbox_px."""
        r = self.rect().normalized()
        self.element.bbox_px = [round(r.left()), round(r.top()),
                                round(r.right()), round(r.bottom())]


class BBoxScene(QGraphicsScene):
    """Escena con el pixmap de fondo y creación de cajas por arrastre."""

    box_created = Signal(object)  # emite el QRectF de la caja nueva

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._bg = QGraphicsPixmapItem(pixmap)
        self._bg.setZValue(0)
        self.addItem(self._bg)
        self.setSceneRect(QRectF(pixmap.rect()))
        self._draft = None
        self._origin = None

    def mousePressEvent(self, event):
        item = self.itemAt(event.scenePos(), self.views()[0].transform())
        if isinstance(item, BBoxItem):
            super().mousePressEvent(event)  # editar caja existente
            return
        # Zona vacía → empezar a dibujar una caja nueva
        self._origin = event.scenePos()
        self._draft = QGraphicsRectItem(QRectF(self._origin, self._origin))
        self._draft.setPen(QPen(QColor(Theme.TEXT_DIM), 0, Qt.DashLine))
        self._draft.setZValue(20)
        self.addItem(self._draft)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._draft is not None:
            self._draft.setRect(QRectF(self._origin, event.scenePos()).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._draft is not None:
            rect = self._draft.rect().normalized()
            self.removeItem(self._draft)
            self._draft = None
            self._origin = None
            # Clamp al área de la imagen y descarta cajas diminutas (clics sueltos)
            rect = rect.intersected(self.sceneRect())
            if rect.width() >= MIN_BOX and rect.height() >= MIN_BOX:
                self.box_created.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class BBoxEditor(QDialog):
    """Ventana de edición de un .pano.json para una imagen."""

    def __init__(self, image_path, pano_path, locale_manager=None, parent=None):
        super().__init__(parent)
        self._lm = locale_manager
        self.image_path = Path(image_path)
        self.pano_path = Path(pano_path)
        self._draw_type = DRAW_OBJ
        self._items = []          # BBoxItem en orden de la lista
        self._selected = None

        self.setWindowTitle(self.tr("ig4.editor_title", "Ideogram 4 — bbox editor"))
        self.resize(1180, 800)
        self.setStyleSheet(f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY};")

        self._pixmap = QPixmap(str(self.image_path))
        if self._pixmap.isNull():
            raise ValueError(f"No se pudo abrir la imagen: {self.image_path}")

        self._doc = self._load_or_create_doc()
        self._build_ui()
        self._populate_from_doc()

    # ── i18n ────────────────────────────────────────────────────────────
    def tr(self, key, default):
        return self._lm.tr(key, default) if self._lm else default

    # ── carga del documento ─────────────────────────────────────────────
    def _load_or_create_doc(self):
        if self.pano_path.exists():
            try:
                return WorkDoc.load(self.pano_path)
            except (ValueError, OSError, KeyError) as exc:
                log.warning("pano.json ilegible (%s); se crea uno nuevo", exc)
        return WorkDoc(image=self.image_path.name,
                       image_w=self._pixmap.width(), image_h=self._pixmap.height(),
                       status=STATUS_DRAFT)

    # ── construcción de UI ──────────────────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # Lienzo
        self.scene = BBoxScene(self._pixmap)
        self.scene.box_created.connect(self._on_box_created)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.view.setDragMode(QGraphicsView.NoDrag)
        self.view.setBackgroundBrush(QBrush(QColor(Theme.BG_PANEL)))
        splitter.addWidget(self.view)

        splitter.addWidget(self._build_sidebar())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([840, 340])

    def _build_sidebar(self):
        panel = QWidget()
        panel.setStyleSheet(f"background-color: {Theme.BG_SIDEBAR};")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Tipo a dibujar
        lay.addWidget(self._section(self.tr("ig4.draw_type", "DRAW TYPE")))
        row = QHBoxLayout()
        self._rb_obj = QRadioButton(self.tr("ig4.type_obj", "Object"))
        self._rb_text = QRadioButton(self.tr("ig4.type_text", "Text"))
        self._rb_obj.setChecked(True)
        grp = QButtonGroup(panel)
        grp.addButton(self._rb_obj)
        grp.addButton(self._rb_text)
        self._rb_obj.toggled.connect(
            lambda on: setattr(self, "_draw_type", DRAW_OBJ if on else DRAW_TEXT))
        row.addWidget(self._rb_obj)
        row.addWidget(self._rb_text)
        row.addStretch()
        lay.addLayout(row)

        # Lista de elementos (reordenable)
        lay.addWidget(self._section(self.tr("ig4.elements", "ELEMENTS (drag to reorder)")))
        self._list = QListWidget()
        self._list.setDragDropMode(QListWidget.InternalMove)
        self._list.currentRowChanged.connect(self._on_list_row_changed)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        lay.addWidget(self._list, stretch=1)

        del_btn = QPushButton(self.tr("ig4.delete_elem", "Delete selected"))
        del_btn.setStyleSheet(Theme.get_button_style(Theme.ACCENT_WARNING))
        del_btn.clicked.connect(self._delete_selected)
        lay.addWidget(del_btn)

        # Propiedades del elemento seleccionado
        lay.addWidget(self._section(self.tr("ig4.element_props", "SELECTED ELEMENT")))
        form = QFormLayout()
        self._ed_text = QLineEdit()
        self._ed_text.setPlaceholderText(self.tr("ig4.text_literal", "literal text (text type)"))
        self._ed_text.textEdited.connect(self._on_elem_field_changed)
        self._ed_desc = QPlainTextEdit()
        self._ed_desc.setFixedHeight(60)
        self._ed_desc.setPlaceholderText(self.tr("ig4.desc_ph", "description"))
        self._ed_desc.textChanged.connect(self._on_elem_field_changed)
        self._lbl_text = QLabel(self.tr("ig4.text_field", "text"))
        form.addRow(self._lbl_text, self._ed_text)
        form.addRow(QLabel(self.tr("ig4.desc_field", "desc")), self._ed_desc)
        lay.addLayout(form)

        # Campos globales
        lay.addWidget(self._section(self.tr("ig4.global", "GLOBAL")))
        gform = QFormLayout()
        self._ed_hld = QPlainTextEdit()
        self._ed_hld.setFixedHeight(48)
        self._ed_bg = QPlainTextEdit()
        self._ed_bg.setFixedHeight(40)
        self._ed_aes = QLineEdit()
        self._ed_light = QLineEdit()
        self._ed_artstyle = QLineEdit()
        gform.addRow(QLabel(self.tr("ig4.hld", "high level")), self._ed_hld)
        gform.addRow(QLabel(self.tr("ig4.background", "background")), self._ed_bg)
        gform.addRow(QLabel(self.tr("ig4.aesthetics", "aesthetics")), self._ed_aes)
        gform.addRow(QLabel(self.tr("ig4.lighting", "lighting")), self._ed_light)
        gform.addRow(QLabel(self.tr("ig4.art_style", "art_style")), self._ed_artstyle)
        lay.addLayout(gform)

        # Acciones
        save_btn = QPushButton(self.tr("ig4.save", "Save (.pano.json)"))
        save_btn.setStyleSheet(Theme.get_button_style(Theme.ACCENT_SUCCESS))
        save_btn.clicked.connect(self._save)
        lay.addWidget(save_btn)
        close_btn = QPushButton(self.tr("ig4.close", "Close"))
        close_btn.setStyleSheet(Theme.get_button_style())
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn)
        return panel

    def _section(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {Theme.TEXT_SECONDARY}; font-size: 11px; "
            f"font-weight: bold; letter-spacing: 1px;")
        return lbl

    # ── poblar desde el documento ───────────────────────────────────────
    def _populate_from_doc(self):
        self._ed_hld.setPlainText(self._doc.high_level_description)
        self._ed_bg.setPlainText(self._doc.background)
        self._ed_aes.setText(self._doc.style.aesthetics)
        self._ed_light.setText(self._doc.style.lighting)
        self._ed_artstyle.setText(self._doc.style.art_style)
        for elem in self._doc.elements:
            self._add_item(elem)
        self._refresh_orders()

    def _add_item(self, element):
        item = BBoxItem(element, on_changed=self._on_box_geometry_changed,
                        on_selected=self._select_item)
        self.scene.addItem(item)
        self._items.append(item)
        li = QListWidgetItem(self._list_label(element))
        li.setData(Qt.UserRole, element.id)  # ancla estable para reordenar
        self._list.addItem(li)

    def _list_label(self, element):
        kind = "T" if element.type == "text" else "O"
        text = element.text if element.type == "text" else element.desc
        text = (text or "").strip().replace("\n", " ")
        return f"[{kind}] {text[:34]}" if text else f"[{kind}] (empty)"

    # ── eventos de caja ─────────────────────────────────────────────────
    def _on_box_created(self, rect):
        etype = "obj" if self._draw_type == DRAW_OBJ else "text"
        elem = Element(id=self._doc.next_element_id(), type=etype,
                       bbox_px=[round(rect.left()), round(rect.top()),
                                round(rect.right()), round(rect.bottom())])
        self._doc.elements.append(elem)
        self._add_item(elem)
        self._refresh_orders()
        self._list.setCurrentRow(len(self._items) - 1)

    def _on_box_geometry_changed(self):
        # La geometría ya está en element.bbox_px; nada más que refrescar etiquetas.
        pass

    def _select_item(self, item):
        if item in self._items:
            self._list.setCurrentRow(self._items.index(item))

    # ── eventos de la lista ─────────────────────────────────────────────
    def _on_list_row_changed(self, row):
        for it in self._items:
            it.setSelected(False)
        if 0 <= row < len(self._items):
            self._selected = self._items[row]
            self._selected.setSelected(True)
            self._selected.ensureVisible()
            self._load_elem_fields(self._selected.element)
        else:
            self._selected = None

    def _on_rows_moved(self, *args):
        """Reordena _items y doc.elements para igualar el orden visual de la lista.

        InternalMove ya reordenó las filas; re-emparejamos los BBoxItem por su
        etiqueta para reconstruir el orden y mantener doc.elements en sincronía.
        """
        self._items = self._reorder_items_to_list()
        self._doc.elements = [it.element for it in self._items]
        self._refresh_orders()

    def _reorder_items_to_list(self):
        by_id = {it.element.id: it for it in self._items}
        result = []
        for row in range(self._list.count()):
            eid = self._list.item(row).data(Qt.UserRole)
            if eid in by_id:
                result.append(by_id.pop(eid))
        result.extend(by_id.values())  # por si alguno no casó
        return result

    def _refresh_orders(self):
        for i, it in enumerate(self._items, start=1):
            it.order = i
            it.update()

    # ── edición de campos ───────────────────────────────────────────────
    def _load_elem_fields(self, element):
        is_text = element.type == "text"
        self._ed_text.setEnabled(is_text)
        self._lbl_text.setEnabled(is_text)
        self._ed_text.blockSignals(True)
        self._ed_desc.blockSignals(True)
        self._ed_text.setText(element.text if is_text else "")
        self._ed_desc.setPlainText(element.desc)
        self._ed_text.blockSignals(False)
        self._ed_desc.blockSignals(False)

    def _on_elem_field_changed(self):
        if not self._selected:
            return
        elem = self._selected.element
        if elem.type == "text":
            elem.text = self._ed_text.text()
        elem.desc = self._ed_desc.toPlainText()
        row = self._items.index(self._selected)
        self._list.item(row).setText(self._list_label(elem))

    def _delete_selected(self):
        if not self._selected:
            return
        item = self._selected
        row = self._items.index(item)
        self.scene.removeItem(item)
        self._items.remove(item)
        self._doc.elements.remove(item.element)
        self._list.takeItem(row)
        self._selected = None
        self._refresh_orders()

    # ── guardado ────────────────────────────────────────────────────────
    def _collect_globals(self):
        self._doc.high_level_description = self._ed_hld.toPlainText().strip()
        self._doc.background = self._ed_bg.toPlainText().strip()
        self._doc.style.aesthetics = self._ed_aes.text().strip()
        self._doc.style.lighting = self._ed_light.text().strip()
        self._doc.style.art_style = self._ed_artstyle.text().strip()

    def _save(self):
        self._collect_globals()
        try:
            self.pano_path.parent.mkdir(parents=True, exist_ok=True)
            self._doc.save(self.pano_path)
        except OSError as exc:
            QMessageBox.warning(self, self.tr("ig4.save_err_title", "Save error"), str(exc))
            return
        log.info("pano.json guardado: %s", self.pano_path)
        QMessageBox.information(
            self, self.tr("ig4.saved_title", "Saved"),
            self.tr("ig4.saved_msg", "Work saved to:") + f"\n{self.pano_path}")

    def showEvent(self, event):
        super().showEvent(event)
        # Ajustar la imagen al viewport la primera vez que se muestra.
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
