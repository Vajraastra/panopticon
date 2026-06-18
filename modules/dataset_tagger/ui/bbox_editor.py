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

from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QThread
from PySide6.QtGui import (
    QPixmap, QPen, QBrush, QColor, QFont, QPainter, QPainterPath,
    QShortcut, QKeySequence,
)
from PySide6.QtWidgets import (
    QDialog, QGraphicsScene, QGraphicsView, QGraphicsRectItem,
    QGraphicsPixmapItem, QVBoxLayout, QHBoxLayout, QWidget, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit,
    QRadioButton, QButtonGroup, QFormLayout, QSplitter, QMessageBox,
    QColorDialog,
)

from PIL import Image

from core.theme import Theme
from ..logic.ideogram.datamodel import WorkDoc, Element, STATUS_DRAFT
from .text_panel import TextComposerDialog, pil_to_qpixmap

log = logging.getLogger(__name__)

# Tipos a dibujar (locale-safe: nunca comparar por texto)
DRAW_OBJ = 0
DRAW_TEXT = 1

OBJ_COLOR = Theme.ACCENT_MAIN       # cyan (fallback)
TEXT_COLOR = Theme.ACCENT_FASHION   # rosa (fallback)
MIN_BOX = 6                         # lado mínimo (px de escena) para crear una caja
HANDLE_SCREEN = 11                  # lado VISUAL del handle de resize, en px de PANTALLA
GRAB_SCREEN = 20                    # zona de AGARRE de la esquina, en px de PANTALLA;
                                    # mayor que el handle visual para atrapar las
                                    # esquinas sin acabar arrastrando la caja entera

# Paleta de cajas: 12 tonos saturados y bien separados en el círculo cromático
# para que cada bbox se distinga de las demás (el tipo obj/text se indica con el
# badge O/T, no con el color). El color lo asigna el editor por orden de saliencia.
BOX_PALETTE = [
    "#FF3B30", "#FF9500", "#FFCC00", "#A2E300", "#34C759", "#00C7BE",
    "#32ADE6", "#007AFF", "#5856D6", "#AF52DE", "#FF2D92", "#FF6482",
]


def order_color(order):
    """Color de la caja en posición `order` (1-based), cíclico sobre la paleta."""
    return BOX_PALETTE[(max(order, 1) - 1) % len(BOX_PALETTE)]


def clip_to(rect, bounds):
    """Recorta `rect` para que no se salga de `bounds` (área de la imagen).

    Pensado para REDIMENSIONAR: ajusta los bordes que se pasen al límite. Una
    bbox fuera de la imagen rompería las coordenadas normalizadas del JSON, así
    que ninguna operación debe permitir que la caja exceda el lienzo."""
    r = QRectF(rect)
    r.setLeft(max(r.left(), bounds.left()))
    r.setTop(max(r.top(), bounds.top()))
    r.setRight(min(r.right(), bounds.right()))
    r.setBottom(min(r.bottom(), bounds.bottom()))
    return r


def shift_into(rect, bounds):
    """Empuja `rect` dentro de `bounds` sin cambiar su tamaño (para MOVER)."""
    r = QRectF(rect)
    if r.left() < bounds.left():
        r.translate(bounds.left() - r.left(), 0)
    if r.top() < bounds.top():
        r.translate(0, bounds.top() - r.top())
    if r.right() > bounds.right():
        r.translate(bounds.right() - r.right(), 0)
    if r.bottom() > bounds.bottom():
        r.translate(0, bounds.bottom() - r.bottom())
    return r


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
        self.draw_color = OBJ_COLOR       # color por saliencia; lo fija el editor
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

    def _grab_size(self):
        """Tolerancia de agarre de la esquina, en px de escena (constante en pantalla)."""
        return GRAB_SCREEN / max(self._scale(), 1e-6)

    def _corner_points(self):
        r = self.rect()
        return {"tl": r.topLeft(), "tr": r.topRight(),
                "bl": r.bottomLeft(), "br": r.bottomRight()}

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, False)
        color = QColor(self.draw_color)
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
        g = self._grab_size()
        return self.rect().adjusted(-g, -g, g, g)

    def shape(self):
        """Área interactiva: el rect, más cuadrados de agarre en las esquinas
        cuando está seleccionada. Así las esquinas se atrapan aunque queden
        ligeramente fuera del rect, y solo la caja activa reclama esa zona."""
        path = QPainterPath()
        path.addRect(self.rect())
        if self.isSelected():
            g = self._grab_size()
            for p in self._corner_points().values():
                path.addRect(QRectF(p.x() - g, p.y() - g, 2 * g, 2 * g))
        return path

    # -- interacción ----------------------------------------------------------
    def _corner_at(self, pos):
        g = self._grab_size()
        for name, p in self._corner_points().items():
            if abs(pos.x() - p.x()) <= g and abs(pos.y() - p.y()) <= g:
                return name
        return None

    def mousePressEvent(self, event):
        was_selected = self.isSelected()
        self.setSelected(True)
        if self.on_selected:
            self.on_selected(self)
        # Solo la caja YA seleccionada se mueve/redimensiona en este mismo gesto.
        # Una caja recién tocada únicamente se selecciona: así, al editar una caja,
        # rozar otra no la arrastra por accidente (hay que seleccionarla primero).
        if not was_selected:
            self._drag_mode = None
            event.accept()
            return
        self._start_rect = QRectF(self.rect())
        self._start_scene = event.scenePos()
        self._drag_mode = self._corner_at(event.pos()) or "move"
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._drag_mode:
            return
        delta = event.scenePos() - self._start_scene
        r = QRectF(self._start_rect)
        bounds = self.scene().sceneRect() if self.scene() else None
        if self._drag_mode == "move":
            r.translate(delta)
            if bounds is not None:
                r = shift_into(r, bounds)   # mover: conserva tamaño, no se sale
        else:
            if "l" in self._drag_mode:
                r.setLeft(r.left() + delta.x())
            if "r" in self._drag_mode:
                r.setRight(r.right() + delta.x())
            if "t" in self._drag_mode:
                r.setTop(r.top() + delta.y())
            if "b" in self._drag_mode:
                r.setBottom(r.bottom() + delta.y())
            r = r.normalized()
            if bounds is not None:
                r = clip_to(r, bounds)      # redimensionar: borde tope en el límite
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

    box_created = Signal(object)   # emite el QRectF de la caja nueva
    color_picked = Signal(int, int)  # x,y de escena del clic en modo eyedropper

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._bg = QGraphicsPixmapItem(pixmap)
        self._bg.setZValue(0)
        self.addItem(self._bg)
        self.setSceneRect(QRectF(pixmap.rect()))
        self._draft = None
        self._origin = None
        self._eyedropper = False

    def set_background(self, pixmap):
        """Reemplaza el pixmap de fondo (tras componer texto sobre la imagen)."""
        self._bg.setPixmap(pixmap)

    def set_eyedropper(self, on):
        """Activa el cuentagotas: el próximo clic muestrea un píxel en vez de
        dibujar/seleccionar una caja."""
        self._eyedropper = bool(on)

    def mousePressEvent(self, event):
        if self._eyedropper:
            p = event.scenePos()
            self.color_picked.emit(int(p.x()), int(p.y()))
            event.accept()
            return
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


class ZoomView(QGraphicsView):
    """Vista con zoom (rueda, centrado en el cursor) y paneo (botón central).

    El zoom con rueda permite dibujar cajas con precisión en imágenes grandes;
    el paneo con el botón central (o arrastre con la rueda pulsada) desplaza sin
    interferir con el dibujo (botón izquierdo) ni con la selección de cajas."""

    MIN_SCALE = 0.05
    MAX_SCALE = 16.0

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._panning = False
        self._pan_anchor = None

    def zoom_by(self, factor):
        """Escala relativa con tope, conservando el ratio 1:1 de la escena."""
        cur = self.transform().m11()
        new = cur * factor
        if new < self.MIN_SCALE or new > self.MAX_SCALE:
            return
        self.scale(factor, factor)

    def wheelEvent(self, event):
        self.zoom_by(1.25 if event.angleDelta().y() > 0 else 1 / 1.25)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_anchor = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            pos = event.position().toPoint()
            delta = pos - self._pan_anchor
            self._pan_anchor = pos
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _AutoDetectWorker(QThread):
    """Corre YOLO sobre UNA imagen fuera del hilo GUI (regla #4)."""

    done = Signal(list)      # lista de dets (ver autobox.AutoBoxer.detect)
    failed = Signal(str)

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.image_path = image_path

    def run(self):
        try:
            from ..logic.ideogram.autobox import get_shared_boxer
            self.done.emit(get_shared_boxer().detect(self.image_path))
        except Exception as exc:  # noqa: BLE001 — se reporta a la UI
            self.failed.emit(str(exc))


class BBoxEditor(QDialog):
    """Ventana de edición de un .pano.json para una imagen."""

    def __init__(self, image_path, pano_path, locale_manager=None,
                 style_defaults=None, parent=None):
        super().__init__(parent)
        self._lm = locale_manager
        self._style_defaults = style_defaults or {}
        self.image_path = Path(image_path)
        self.pano_path = Path(pano_path)
        self._draw_type = DRAW_OBJ
        self._items = []          # BBoxItem en orden de la lista
        self._selected = None
        self._autodet = None      # _AutoDetectWorker en curso (auto-bbox)
        self._eyedropper_active = False  # modo cuentagotas (color de texto)

        self.setWindowTitle(self.tr("ig4.editor_title", "Ideogram 4 — bbox editor"))
        self.resize(1180, 800)
        self.setStyleSheet(f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY};")

        self._pixmap = QPixmap(str(self.image_path))
        if self._pixmap.isNull():
            raise ValueError(f"No se pudo abrir la imagen: {self.image_path}")
        # Imagen de trabajo (PIL): se modifica al componer texto (Caso A). La
        # copia de salida con el texto se escribe junto al .pano.json al guardar.
        self._work_image = Image.open(self.image_path).convert("RGB")
        self._composited = False

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
        doc = WorkDoc(image=self.image_path.name,
                      image_w=self._pixmap.width(), image_h=self._pixmap.height(),
                      status=STATUS_DRAFT)
        # Hereda el estilo compartido del set (solo en docs nuevos).
        sd = self._style_defaults
        doc.style.art_style = sd.get("art_style", "") or doc.style.art_style
        doc.style.aesthetics = sd.get("aesthetics", "") or doc.style.aesthetics
        doc.style.lighting = sd.get("lighting", "") or doc.style.lighting
        return doc

    # ── construcción de UI ──────────────────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # Lienzo, con una instrucción siempre visible encima (a prueba de fallos:
        # el usuario no tiene que adivinar cómo se dibuja).
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        instr = QLabel(self.tr(
            "ig4.canvas_help",
            "Drag on the image to draw a box. Click a box to select it; drag its "
            "corners to resize, or drag its center to move it."))
        instr.setWordWrap(True)
        instr.setStyleSheet(
            f"background-color: {Theme.BG_PANEL}; color: {Theme.TEXT_SECONDARY}; "
            f"padding: 6px 10px; font-size: 11px;")
        lv.addWidget(instr)

        # Barra de zoom (rueda = zoom; botón central = paneo).
        lv.addWidget(self._build_zoom_bar())

        self.scene = BBoxScene(self._pixmap)
        self.scene.box_created.connect(self._on_box_created)
        self.scene.color_picked.connect(self._on_color_picked)
        self.view = ZoomView(self.scene)
        self.view.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.view.setDragMode(QGraphicsView.NoDrag)
        self.view.setBackgroundBrush(QBrush(QColor(Theme.BG_PANEL)))
        self.view.setToolTip(self.tr(
            "ig4.tip.canvas",
            "Drawing area. Empty space = draw a new box; on a box = select/move/resize. "
            "Mouse wheel = zoom, middle-button drag = pan."))
        lv.addWidget(self.view)
        splitter.addWidget(left)

        splitter.addWidget(self._build_sidebar())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([840, 340])

        self._install_shortcuts()

    def _install_shortcuts(self):
        """Atajos para agilizar el flujo de ×N imágenes:
          Supr/Backspace = borrar la caja seleccionada (solo sobre el lienzo o la
                           lista, para no chocar con la edición de texto en campos),
          Flechas        = mover la caja 1 px; Shift+flechas = 10 px (solo sobre el
                           lienzo, para no robarle la navegación a la lista),
          Ctrl+D         = duplicar la caja seleccionada,
          Ctrl+S         = guardar y cerrar,
          Esc            = cerrar sin guardar (default de QDialog → reject)."""
        for seq in (QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)):
            for widget in (self.view, self._list):
                sc = QShortcut(seq, widget)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(self._delete_selected)
        # Mover con flechas: solo en el lienzo (la lista conserva su navegación).
        arrows = ((Qt.Key_Left, -1, 0), (Qt.Key_Right, 1, 0),
                  (Qt.Key_Up, 0, -1), (Qt.Key_Down, 0, 1))
        for key, dx, dy in arrows:
            for mod, step in ((Qt.NoModifier, 1), (Qt.ShiftModifier, 10)):
                sc = QShortcut(QKeySequence(mod | key), self.view)
                sc.setContext(Qt.WidgetWithChildrenShortcut)
                sc.activated.connect(
                    lambda dx=dx, dy=dy, s=step: self._nudge_selected(dx * s, dy * s))
        dup_sc = QShortcut(QKeySequence("Ctrl+D"), self)
        dup_sc.setContext(Qt.WindowShortcut)
        dup_sc.activated.connect(self._duplicate_selected)
        save_sc = QShortcut(QKeySequence.Save, self)  # Ctrl+S
        save_sc.setContext(Qt.WindowShortcut)
        save_sc.activated.connect(self._save_and_close)

    def _build_zoom_bar(self):
        """Fila compacta de controles de zoom encima del lienzo."""
        bar = QWidget()
        bar.setStyleSheet(f"background-color: {Theme.BG_PANEL};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)

        def mk(label, tip, slot):
            b = QPushButton(label)
            b.setFixedHeight(24)
            b.setStyleSheet(Theme.get_button_style())
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        row.addWidget(mk(self.tr("ig4.zoom_out", "−"),
                         self.tr("ig4.tip.zoom_out", "Zoom out"),
                         lambda: self.view.zoom_by(1 / 1.25)))
        row.addWidget(mk(self.tr("ig4.zoom_in", "+"),
                         self.tr("ig4.tip.zoom_in", "Zoom in"),
                         lambda: self.view.zoom_by(1.25)))
        row.addWidget(mk(self.tr("ig4.zoom_fit", "Fit"),
                         self.tr("ig4.tip.zoom_fit", "Fit the whole image in the view"),
                         self._zoom_fit))
        row.addWidget(mk(self.tr("ig4.zoom_100", "100%"),
                         self.tr("ig4.tip.zoom_100", "Actual size (1:1 pixels)"),
                         self._zoom_100))
        hint = QLabel(self.tr("ig4.zoom_hint", "wheel = zoom · middle drag = pan"))
        hint.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
        row.addWidget(hint)
        row.addStretch()
        return bar

    def _zoom_fit(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def _zoom_100(self):
        self.view.resetTransform()

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
        self._rb_obj.setToolTip(self.tr(
            "ig4.tip.type_obj",
            "Object: a visual element (character, prop, panel, frame). On Start the "
            "VLM describes it and k-means extracts its colors."))
        self._rb_text.setToolTip(self.tr(
            "ig4.tip.type_text",
            "Text: literal words in the image (titles, dialog). You type the exact "
            "string; it is ground truth and never sent to the VLM."))
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

        # Auto-detección de cajas (YOLO): pre-crea objetos/personajes/animales que
        # el usuario repasa. Acelera el grueso; el texto se sigue marcando a mano.
        self._btn_autodet = QPushButton(self.tr("ig4.auto_detect", "Auto-detect boxes"))
        self._btn_autodet.setStyleSheet(Theme.get_button_style(Theme.ACCENT_MAIN))
        self._btn_autodet.setToolTip(self.tr(
            "ig4.tip.auto_detect",
            "Detect characters, animals and objects with YOLO and add them as boxes "
            "(type 'object', category pre-filled in desc). Review and adjust them: "
            "resize, add missing ones, or delete extras. Text is still drawn by hand."))
        self._btn_autodet.clicked.connect(self._auto_detect)
        lay.addWidget(self._btn_autodet)

        # Lista de elementos (reordenable)
        lay.addWidget(self._section(self.tr("ig4.elements", "ELEMENTS (drag to reorder)")))
        self._list = QListWidget()
        self._list.setDragDropMode(QListWidget.InternalMove)
        self._list.setToolTip(self.tr(
            "ig4.tip.list",
            "Every box you draw. Order = prominence: #1 is the most salient. Drag "
            "rows to reorder — it matters for the caption. Text boxes are moved to "
            "the end automatically on export."))
        self._list.currentRowChanged.connect(self._on_list_row_changed)
        self._list.model().rowsMoved.connect(self._on_rows_moved)
        lay.addWidget(self._list, stretch=1)

        del_btn = QPushButton(self.tr("ig4.delete_elem", "Delete selected"))
        del_btn.setStyleSheet(Theme.get_button_style(Theme.ACCENT_WARNING))
        del_btn.setToolTip(self.tr(
            "ig4.tip.delete", "Remove the selected box from the list and the image."))
        del_btn.clicked.connect(self._delete_selected)
        lay.addWidget(del_btn)

        # Propiedades del elemento seleccionado
        lay.addWidget(self._section(self.tr("ig4.element_props", "SELECTED ELEMENT")))
        form = QFormLayout()
        self._ed_text = QLineEdit()
        self._ed_text.setPlaceholderText(self.tr("ig4.text_literal", "literal text (text type)"))
        self._ed_text.setToolTip(self.tr(
            "ig4.tip.elem_text",
            "The exact words shown in this text box (ground truth). Enabled only for "
            "'text' elements."))
        self._ed_text.textEdited.connect(self._on_elem_field_changed)
        self._ed_desc = QPlainTextEdit()
        self._ed_desc.setFixedHeight(60)
        self._ed_desc.setPlaceholderText(self.tr("ig4.desc_ph", "description"))
        self._ed_desc.setToolTip(self.tr(
            "ig4.tip.elem_desc",
            "Short description of this element. For objects the VLM fills it on Start "
            "if empty; anything you write here is kept (not overwritten)."))
        self._ed_desc.textChanged.connect(self._on_elem_field_changed)
        self._lbl_text = QLabel(self.tr("ig4.text_field", "text"))
        form.addRow(self._lbl_text, self._ed_text)
        form.addRow(QLabel(self.tr("ig4.desc_field", "desc")), self._ed_desc)
        lay.addLayout(form)

        # Color del texto (Caso B: texto YA presente en la imagen). El cuentagotas
        # muestrea el hex exacto del glifo; el swatch permite ponerlo a mano. Es
        # el color ground-truth del texto (el VLM nunca lo ve). Solo para 'text'.
        color_row = QHBoxLayout()
        self._lbl_color = QLabel(self.tr("ig4.text_color", "color"))
        self._swatch = QPushButton()
        self._swatch.setFixedSize(30, 22)
        self._swatch.setToolTip(self.tr(
            "ig4.tip.text_color",
            "Current text color (ground truth). Click to pick it by hand."))
        self._swatch.clicked.connect(self._pick_color_manual)
        self._btn_eyedrop = QPushButton(self.tr("ig4.eyedropper", "Eyedropper"))
        self._btn_eyedrop.setCheckable(True)
        self._btn_eyedrop.setToolTip(self.tr(
            "ig4.tip.eyedropper",
            "Sample the EXACT color of existing text: click here, then click the "
            "glyph on the image. The sampled hex becomes this text's color."))
        self._btn_eyedrop.clicked.connect(self._toggle_eyedropper)
        color_row.addWidget(self._lbl_color)
        color_row.addWidget(self._swatch)
        color_row.addWidget(self._btn_eyedrop)
        color_row.addStretch()
        lay.addLayout(color_row)
        self._color_widgets = (self._lbl_color, self._swatch, self._btn_eyedrop)

        # Compositing de texto (Caso A: texto nuevo en flyers). Solo para 'text'.
        self._btn_compose = QPushButton(self.tr("ig4.compose_text", "Compose text…"))
        self._btn_compose.setStyleSheet(Theme.get_button_style(Theme.ACCENT_FASHION))
        self._btn_compose.setToolTip(self.tr(
            "ig4.tip.compose_text",
            "Render NEW text onto the image (titles, flyers), optionally inside a "
            "speech bubble. The box and color become ground truth. Select a 'text' "
            "element first."))
        self._btn_compose.clicked.connect(self._compose_text)
        self._btn_compose.setEnabled(False)
        lay.addWidget(self._btn_compose)

        # Compositing de marco/forma. Solo para 'obj' (enmarcar imagen, paneles).
        self._btn_shape = QPushButton(self.tr("ig4.compose_shape", "Compose frame…"))
        self._btn_shape.setStyleSheet(Theme.get_button_style(Theme.ACCENT_MAIN))
        self._btn_shape.setToolTip(self.tr(
            "ig4.tip.compose_shape",
            "Draw a frame, panel or speech-bubble shape onto the image. Select an "
            "'object' element first."))
        self._btn_shape.clicked.connect(self._compose_shape)
        self._btn_shape.setEnabled(False)
        lay.addWidget(self._btn_shape)

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
        self._ed_artstyle.setPlaceholderText(self.tr(
            "ig4.art_style_req_ph", "required, e.g. stylized 3D character render"))
        self._ed_hld.setToolTip(self.tr(
            "ig4.tip.hld",
            "One or two sentences describing the whole scene. The VLM fills this on "
            "Start if you leave it empty."))
        self._ed_bg.setToolTip(self.tr(
            "ig4.tip.background",
            "Describes ONLY the background. The VLM fills this on Start if empty."))
        self._ed_aes.setToolTip(self.tr(
            "ig4.tip.aesthetics", "Optional. Mood/aesthetics of the set's style (e.g. 'clean studio')."))
        self._ed_light.setToolTip(self.tr(
            "ig4.tip.lighting", "Optional. Lighting of the set's style (e.g. 'soft key light')."))
        self._ed_artstyle.setToolTip(self.tr(
            "ig4.tip.art_style",
            "REQUIRED to export. The set's art style (e.g. 'stylized 3D character "
            "render'). Inherited from the panel — keep it identical across the set."))
        gform.addRow(QLabel(self.tr("ig4.hld", "high level")), self._ed_hld)
        gform.addRow(QLabel(self.tr("ig4.background", "background")), self._ed_bg)
        gform.addRow(QLabel(self.tr("ig4.aesthetics", "aesthetics")), self._ed_aes)
        gform.addRow(QLabel(self.tr("ig4.lighting", "lighting")), self._ed_light)
        gform.addRow(QLabel(self.tr("ig4.art_style_req", "art_style *")), self._ed_artstyle)
        lay.addLayout(gform)

        # Acciones
        save_btn = QPushButton(self.tr("ig4.save_close", "Save & close"))
        save_btn.setStyleSheet(Theme.get_button_style(Theme.ACCENT_SUCCESS))
        save_btn.setToolTip(self.tr(
            "ig4.tip.save",
            "Save boxes and fields to the work file (.pano.json) and close the editor. "
            "You can reopen and continue later; capture (Start) reads these files."))
        save_btn.clicked.connect(self._save_and_close)
        lay.addWidget(save_btn)
        close_btn = QPushButton(self.tr("ig4.close_nosave", "Close without saving"))
        close_btn.setStyleSheet(Theme.get_button_style())
        close_btn.setToolTip(self.tr(
            "ig4.tip.close", "Close the editor discarding unsaved changes since the last save."))
        close_btn.clicked.connect(self.reject)
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
        # Estado inicial del panel: selecciona la primera caja (si la hay) para que
        # se vean solo sus controles; si la imagen está vacía, ocúltalos todos.
        if self._items:
            self._list.setCurrentRow(0)
        else:
            self._update_elem_panel_visibility(None)

    def _add_item(self, element):
        item = BBoxItem(element, on_changed=self._on_box_geometry_changed,
                        on_selected=self._select_item)
        self.scene.addItem(item)
        self._items.append(item)
        order = self._list.count() + 1
        li = QListWidgetItem(self._list_label(element, order))
        li.setData(Qt.UserRole, element.id)  # ancla estable para reordenar
        self._list.addItem(li)

    def _list_label(self, element, order):
        kind = "T" if element.type == "text" else "O"
        text = element.text if element.type == "text" else element.desc
        text = (text or "").strip().replace("\n", " ")
        body = text[:34] if text else self.tr("ig4.empty", "(empty)")
        return f"{order} · [{kind}] {body}"

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
            it.setZValue(10)
        if 0 <= row < len(self._items):
            self._selected = self._items[row]
            self._selected.setSelected(True)
            self._selected.setZValue(11)   # encima de las demás → su esquina gana el clic
            self._selected.ensureVisible()
            self._load_elem_fields(self._selected.element)
        else:
            self._selected = None
            self._update_elem_panel_visibility(None)

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
            it.draw_color = order_color(i)
            it.update()
            li = self._list.item(i - 1)
            if li is not None:
                li.setText(self._list_label(it.element, i))
                li.setForeground(QColor(order_color(i)))

    # ── edición de campos ───────────────────────────────────────────────
    def _update_elem_panel_visibility(self, element):
        """Muestra solo los controles del tipo activo, sin mezclar texto y objeto.

        - 'text'  → texto literal + color (swatch/cuentagotas) + "Componer texto…".
        - 'obj'   → "Componer forma…".
        - nada    → se ocultan todos (al deseleccionar / imagen vacía).
        El campo `desc` aplica a ambos tipos y permanece siempre visible.
        """
        is_text = element is not None and element.type == "text"
        is_obj = element is not None and element.type == "obj"
        self._ed_text.setVisible(is_text)
        self._lbl_text.setVisible(is_text)
        self._btn_compose.setVisible(is_text)
        self._btn_shape.setVisible(is_obj)
        self._btn_compose.setEnabled(is_text)
        self._btn_shape.setEnabled(is_obj)
        for w in self._color_widgets:
            w.setVisible(is_text)
        if not is_text:                 # salir del cuentagotas al cambiar de tipo
            self._set_eyedropper(False)

    def _load_elem_fields(self, element):
        is_text = element.type == "text"
        self._update_elem_panel_visibility(element)
        self._ed_text.blockSignals(True)
        self._ed_desc.blockSignals(True)
        self._ed_text.setText(element.text if is_text else "")
        self._ed_desc.setPlainText(element.desc)
        self._ed_text.blockSignals(False)
        self._ed_desc.blockSignals(False)
        if is_text:
            self._set_swatch_color(element.color_palette[0]
                                   if element.color_palette else None)

    def _on_elem_field_changed(self):
        if not self._selected:
            return
        elem = self._selected.element
        if elem.type == "text":
            elem.text = self._ed_text.text()
        elem.desc = self._ed_desc.toPlainText()
        row = self._items.index(self._selected)
        self._list.item(row).setText(self._list_label(elem, row + 1))

    # ── color del texto (Caso B) ────────────────────────────────────────
    def _set_swatch_color(self, hexv):
        """Pinta el swatch con `hexv` (o un estado 'sin color' si es None)."""
        if hexv:
            self._swatch.setStyleSheet(
                f"background-color: {hexv}; border: 1px solid {Theme.TEXT_DIM};")
            self._swatch.setText("")
        else:
            self._swatch.setStyleSheet(
                f"border: 1px dashed {Theme.TEXT_DIM}; color: {Theme.TEXT_DIM};")
            self._swatch.setText("—")

    def _apply_text_color(self, hexv):
        """Fija el color en el elemento text seleccionado y refresca el swatch."""
        if not self._selected or self._selected.element.type != "text":
            return
        self._selected.element.color_palette = [hexv]
        self._set_swatch_color(hexv)

    def _pick_color_manual(self):
        """Selector de color manual para el texto (alternativa al cuentagotas)."""
        if not self._selected or self._selected.element.type != "text":
            return
        cur = self._selected.element.color_palette
        initial = QColor(cur[0]) if cur else QColor("#FFFFFF")
        color = QColorDialog.getColor(initial, self, self.tr("ig4.text_color", "color"))
        if color.isValid():
            self._apply_text_color(color.name().upper())

    def _toggle_eyedropper(self):
        self._set_eyedropper(not self._eyedropper_active)

    def _set_eyedropper(self, on):
        """Activa/desactiva el cuentagotas (muestreo de un píxel de la imagen)."""
        self._eyedropper_active = bool(on)
        self.scene.set_eyedropper(self._eyedropper_active)
        self._btn_eyedrop.setChecked(self._eyedropper_active)
        self.view.viewport().setCursor(
            Qt.CrossCursor if self._eyedropper_active else Qt.ArrowCursor)

    def _on_color_picked(self, x, y):
        """Muestrea el píxel (x,y) de la imagen de trabajo y lo aplica al texto."""
        w, h = self._work_image.width, self._work_image.height
        x = max(0, min(x, w - 1))
        y = max(0, min(y, h - 1))
        rgb = self._work_image.getpixel((x, y))[:3]
        self._apply_text_color("#%02X%02X%02X" % rgb)
        self._set_eyedropper(False)   # cuentagotas de un solo uso

    def _compose_text(self):
        """Abre el mini-editor de texto y compone sobre la imagen de trabajo.

        Al aplicar: actualiza la imagen de fondo, fija en el elemento el string,
        color, bbox AUTOMÁTICA y los parámetros de render, y mueve la caja a esa
        bbox. La copia con texto se escribe al guardar.
        """
        if not self._selected or self._selected.element.type != "text":
            return
        elem = self._selected.element
        x0, y0 = elem.bbox_px[0], elem.bbox_px[1]
        dlg = TextComposerDialog(self._work_image, (x0, y0), elem.text, self._lm, self)
        if dlg.exec() != QDialog.Accepted or dlg.result_image is None:
            return

        self._work_image = dlg.result_image
        self.scene.set_background(pil_to_qpixmap(self._work_image))
        self._composited = True

        data = dlg.result_data
        elem.text = data["text"]
        elem.color_palette = [data["color"]]
        elem.bbox_px = self._clamp_bbox(data["bbox_px"])
        elem.render = data["render"]
        bx0, by0, bx1, by1 = elem.bbox_px
        self._selected.prepareGeometryChange()
        self._selected.setRect(QRectF(QPointF(bx0, by0), QPointF(bx1, by1)))
        self._load_elem_fields(elem)
        row = self._items.index(self._selected)
        self._list.item(row).setText(self._list_label(elem, row + 1))

    def _compose_shape(self):
        """Abre el mini-editor de formas y compone un marco/globo sobre la imagen.

        Solo para elementos 'obj'. Fija en el elemento la bbox AUTOMÁTICA y los
        parámetros de render; mueve la caja a esa bbox. La copia con la forma se
        escribe al guardar (igual que el texto compuesto).
        """
        if not self._selected or self._selected.element.type != "obj":
            return
        from .shape_panel import ShapeComposerDialog
        elem = self._selected.element
        dlg = ShapeComposerDialog(self._work_image, tuple(elem.bbox_px), self._lm, self)
        if dlg.exec() != QDialog.Accepted or dlg.result_image is None:
            return

        self._work_image = dlg.result_image
        self.scene.set_background(pil_to_qpixmap(self._work_image))
        self._composited = True

        data = dlg.result_data
        elem.bbox_px = self._clamp_bbox(data["bbox_px"])
        elem.render = data["render"]
        bx0, by0, bx1, by1 = elem.bbox_px
        self._selected.prepareGeometryChange()
        self._selected.setRect(QRectF(QPointF(bx0, by0), QPointF(bx1, by1)))
        row = self._items.index(self._selected)
        self._list.item(row).setText(self._list_label(elem, row + 1))

    # ── auto-detección de cajas (YOLO) ──────────────────────────────────
    def _auto_detect(self):
        """Lanza la detección YOLO de la imagen actual en un hilo aparte."""
        if self._autodet is not None and self._autodet.isRunning():
            return
        self._btn_autodet.setEnabled(False)
        self._btn_autodet.setText(self.tr("ig4.auto_detecting", "Detecting…"))
        self.setCursor(Qt.WaitCursor)
        self._autodet = _AutoDetectWorker(str(self.image_path), self)
        self._autodet.done.connect(self._on_auto_detected)
        self._autodet.failed.connect(self._on_auto_failed)
        self._autodet.start()

    def _restore_autodet_button(self):
        self.unsetCursor()
        self._btn_autodet.setEnabled(True)
        self._btn_autodet.setText(self.tr("ig4.auto_detect", "Auto-detect boxes"))

    def _on_auto_detected(self, dets):
        self._restore_autodet_button()
        added = 0
        for d in dets:
            # type=obj, desc vacío: el VLM lo captura al Iniciar (no precategorizar).
            elem = Element(id=self._doc.next_element_id(), type="obj",
                           bbox_px=self._clamp_bbox(d["bbox_px"]))
            self._doc.elements.append(elem)
            self._add_item(elem)
            added += 1
        self._refresh_orders()
        if added:
            self._list.setCurrentRow(len(self._items) - 1)
        else:
            QMessageBox.information(
                self, self.tr("ig4.auto_detect", "Auto-detect boxes"),
                self.tr("ig4.auto_none", "No objects detected. Draw the boxes by hand."))

    def _on_auto_failed(self, msg):
        self._restore_autodet_button()
        QMessageBox.warning(
            self, self.tr("ig4.auto_err_title", "Detection error"), msg)

    def _clamp_bbox(self, bbox):
        """Recorta una bbox [x0,y0,x1,y1] al área de la imagen (lista de ints)."""
        x0, y0, x1, y1 = bbox
        r = clip_to(QRectF(QPointF(x0, y0), QPointF(x1, y1)).normalized(),
                    self.scene.sceneRect())
        return [round(r.left()), round(r.top()), round(r.right()), round(r.bottom())]

    def _nudge_selected(self, dx, dy):
        """Mueve la caja seleccionada `dx,dy` px de escena, con clamp a la imagen."""
        if not self._selected:
            return
        item = self._selected
        r = shift_into(item.rect().translated(dx, dy), self.scene.sceneRect())
        item.prepareGeometryChange()
        item.setRect(r)
        item._sync_to_element()

    def _duplicate_selected(self):
        """Crea una copia de la caja seleccionada, desplazada para no taparla.

        Copia tipo, descripción y texto literal; NO copia el render compuesto ni la
        paleta (esos implican píxeles ya pintados en la imagen original, que no se
        re-componen en la copia). La copia queda seleccionada al final de la lista."""
        if not self._selected:
            return
        src = self._selected.element
        off = 12
        x0, y0, x1, y1 = src.bbox_px
        bounds = self.scene.sceneRect()
        # Desplaza la copia y la mantiene dentro de la imagen.
        if x1 + off <= bounds.right() and y1 + off <= bounds.bottom():
            x0, y0, x1, y1 = x0 + off, y0 + off, x1 + off, y1 + off
        elem = Element(id=self._doc.next_element_id(), type=src.type,
                       bbox_px=[x0, y0, x1, y1], desc=src.desc,
                       text=(src.text if src.type == "text" else ""))
        self._doc.elements.append(elem)
        self._add_item(elem)
        self._refresh_orders()
        self._list.setCurrentRow(len(self._items) - 1)

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
        """Persiste el .pano.json. Devuelve True si guardó sin error.

        No muestra diálogo de confirmación: en una sesión de 40+ imágenes ese
        aviso por imagen era tedioso. El éxito se refleja cerrando el editor
        (ver `_save_and_close`); solo los errores interrumpen con un aviso.
        """
        self._collect_globals()
        try:
            self.pano_path.parent.mkdir(parents=True, exist_ok=True)
            self._doc.save(self.pano_path)
            # Si se compuso texto, la copia de salida (con el texto) acompaña al
            # sidecar en la misma carpeta; el original nunca se toca (Regla #9).
            if self._composited:
                out_img = self.pano_path.parent / self.image_path.name
                self._work_image.save(out_img)
        except OSError as exc:
            QMessageBox.warning(self, self.tr("ig4.save_err_title", "Save error"), str(exc))
            return False
        log.info("pano.json guardado: %s", self.pano_path)
        return True

    def _save_and_close(self):
        """Guarda y, si todo fue bien, cierra el editor (flujo de captura masiva)."""
        if self._save():
            self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        # Ajustar la imagen al viewport la primera vez que se muestra.
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    def closeEvent(self, event):
        # Espera a que termine la detección en curso (si la hay) para no destruir
        # el QThread mientras corre.
        if self._autodet is not None and self._autodet.isRunning():
            self._autodet.wait()
        super().closeEvent(event)
