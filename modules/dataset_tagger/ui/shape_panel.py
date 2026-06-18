"""
Mini-editor de FORMAS/MARCOS del captioner Ideogram 4.

Diálogo con controles mínimos (forma, posición/tamaño, borde, relleno, cola del
globo) y preview en vivo. Al aplicar, compone la forma sobre una COPIA de la
imagen (Pillow, vía shape_render) y expone:
  - result_image : PIL.Image compuesta (la nueva copia de salida).
  - result_data  : dict con shape, bbox_px AUTOMÁTICA y render (parámetros) para
                   reproducibilidad.

La forma se materializa como un elemento `obj` (marco/panel/globo). La bbox no se
pide: la calcula shape_render de lo realmente pintado (incluida la cola).
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QSpinBox, QSlider, QPushButton, QCheckBox, QColorDialog, QWidget,
)

from core.theme import Theme
from ..logic.ideogram import shape_render as sh
from .text_panel import pil_to_qpixmap

log = logging.getLogger(__name__)

_PREVIEW_MAX = 520


class ShapeComposerDialog(QDialog):
    """Compone un marco/forma sobre la imagen y devuelve imagen + datos."""

    def __init__(self, base_image, init_rect=(0, 0, 200, 120),
                 locale_manager=None, parent=None):
        super().__init__(parent)
        self._lm = locale_manager
        self._base = base_image.convert("RGB")
        self._stroke_color = "#000000"
        self._fill_color = "#FFFFFF"
        self.result_image = None
        self.result_data = None

        self.setWindowTitle(self.tr("ig4.shape_title", "Compose frame / shape"))
        self.setStyleSheet(f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY};")
        self.resize(900, 640)

        self._build_ui(init_rect)
        self._update_preview()

    def tr(self, key, default):
        return self._lm.tr(key, default) if self._lm else default

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self, init_rect):
        root = QHBoxLayout(self)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumWidth(_PREVIEW_MAX)
        self._preview.setStyleSheet(f"background-color: {Theme.BG_PANEL}; border-radius: 6px;")
        root.addWidget(self._preview, stretch=1)

        panel = QWidget()
        panel.setFixedWidth(320)
        lay = QVBoxLayout(panel)
        form = QFormLayout()

        self._cb_shape = QComboBox()
        self._cb_shape.addItem(self.tr("ig4.shape_rect", "Rectangle"), sh.RECT)
        self._cb_shape.addItem(self.tr("ig4.shape_rounded", "Rounded rectangle"), sh.ROUNDED)
        self._cb_shape.addItem(self.tr("ig4.shape_ellipse", "Ellipse / circle"), sh.ELLIPSE)
        self._cb_shape.addItem(self.tr("ig4.shape_bubble", "Speech bubble"), sh.BUBBLE)
        self._cb_shape.currentIndexChanged.connect(self._on_shape_changed)
        form.addRow(self.tr("ig4.shape", "shape"), self._cb_shape)

        x0, y0, x1, y1 = sh._norm_rect(init_rect)
        w, h = self._base.width, self._base.height
        self._sp_x = self._spin(0, w, x0)
        self._sp_y = self._spin(0, h, y0)
        self._sp_w = self._spin(2, w, max(x1 - x0, 2))
        self._sp_h = self._spin(2, h, max(y1 - y0, 2))
        pos = QHBoxLayout(); pos.addWidget(self._sp_x); pos.addWidget(self._sp_y)
        pos_w = QWidget(); pos_w.setLayout(pos)
        size = QHBoxLayout(); size.addWidget(self._sp_w); size.addWidget(self._sp_h)
        size_w = QWidget(); size_w.setLayout(size)
        form.addRow(self.tr("ig4.position", "x / y"), pos_w)
        form.addRow(self.tr("ig4.size_wh", "w / h"), size_w)

        self._sp_stroke = self._spin(0, 60, 4)
        form.addRow(self.tr("ig4.stroke", "outline"), self._sp_stroke)

        self._sp_radius = self._spin(0, 400, 24)
        self._lbl_radius = QLabel(self.tr("ig4.radius", "corner radius"))
        form.addRow(self._lbl_radius, self._sp_radius)

        # Cola del globo (solo BUBBLE): punta hacia el hablante.
        self._sp_tail_x = self._spin(0, w, x0)
        self._sp_tail_y = self._spin(0, h, min(y1 + 40, h))
        tail = QHBoxLayout(); tail.addWidget(self._sp_tail_x); tail.addWidget(self._sp_tail_y)
        self._tail_w = QWidget(); self._tail_w.setLayout(tail)
        self._lbl_tail = QLabel(self.tr("ig4.tail", "tail x / y"))
        form.addRow(self._lbl_tail, self._tail_w)

        lay.addLayout(form)

        self._btn_stroke_color = QPushButton(self.tr("ig4.stroke_color", "Outline color…"))
        self._btn_stroke_color.clicked.connect(self._pick_stroke_color)
        lay.addWidget(self._btn_stroke_color)

        self._chk_fill = QCheckBox(self.tr("ig4.fill", "Fill"))
        self._chk_fill.toggled.connect(self._update_preview)
        lay.addWidget(self._chk_fill)
        self._btn_fill_color = QPushButton(self.tr("ig4.fill_color", "Fill color…"))
        self._btn_fill_color.clicked.connect(self._pick_fill_color)
        lay.addWidget(self._btn_fill_color)
        self._sl_alpha = QSlider(Qt.Horizontal)
        self._sl_alpha.setRange(0, 255)
        self._sl_alpha.setValue(255)
        self._sl_alpha.valueChanged.connect(self._update_preview)
        lay.addWidget(QLabel(self.tr("ig4.fill_alpha", "fill opacity")))
        lay.addWidget(self._sl_alpha)

        self._sync_color_buttons()
        for sp in (self._sp_x, self._sp_y, self._sp_w, self._sp_h,
                   self._sp_stroke, self._sp_radius, self._sp_tail_x, self._sp_tail_y):
            sp.valueChanged.connect(self._update_preview)

        lay.addStretch()

        self._btn_apply = QPushButton(self.tr("ig4.apply", "Apply (composite)"))
        self._btn_apply.setStyleSheet(Theme.get_button_style(Theme.ACCENT_SUCCESS))
        self._btn_apply.clicked.connect(self._apply)
        cancel = QPushButton(self.tr("ig4.cancel", "Cancel"))
        cancel.setStyleSheet(Theme.get_button_style())
        cancel.clicked.connect(self.reject)
        lay.addWidget(self._btn_apply)
        lay.addWidget(cancel)

        root.addWidget(panel)
        self._set_tooltips()
        self._on_shape_changed()

    def _set_tooltips(self):
        tips = {
            self._cb_shape: self.tr("ig4.tip.sh_shape",
                "Frame type: rectangle, rounded rectangle, ellipse, or speech bubble (with tail)."),
            self._sp_x: self.tr("ig4.tip.sh_x", "Top-left X of the shape, in pixels."),
            self._sp_y: self.tr("ig4.tip.sh_y", "Top-left Y of the shape, in pixels."),
            self._sp_w: self.tr("ig4.tip.sh_w", "Shape width, in pixels."),
            self._sp_h: self.tr("ig4.tip.sh_h", "Shape height, in pixels."),
            self._sp_stroke: self.tr("ig4.tip.sh_stroke", "Border thickness (0 = no outline)."),
            self._sp_radius: self.tr("ig4.tip.sh_radius",
                "Corner roundness (rounded rectangle and bubble only)."),
            self._sp_tail_x: self.tr("ig4.tip.sh_tail_x",
                "Speech-bubble tail tip X — point it toward the speaker (bubble only)."),
            self._sp_tail_y: self.tr("ig4.tip.sh_tail_y", "Speech-bubble tail tip Y (bubble only)."),
            self._btn_stroke_color: self.tr("ig4.tip.sh_stroke_color", "Border color."),
            self._chk_fill: self.tr("ig4.tip.sh_fill",
                "Fill the shape with a solid color (e.g. white bubble so text reads)."),
            self._btn_fill_color: self.tr("ig4.tip.sh_fill_color", "Fill color (used when Fill is on)."),
            self._sl_alpha: self.tr("ig4.tip.sh_alpha",
                "Fill opacity: 0 = transparent, 255 = solid."),
            self._btn_apply: self.tr("ig4.tip.sh_apply",
                "Bake the shape onto the output copy and return. The box is computed automatically."),
        }
        for w, t in tips.items():
            w.setToolTip(t)

    def _spin(self, lo, hi, val):
        sp = QSpinBox(); sp.setRange(lo, hi); sp.setValue(int(val))
        return sp

    def _on_shape_changed(self):
        shape = self._cb_shape.currentData()
        is_round = shape in (sh.ROUNDED, sh.BUBBLE)
        is_bubble = shape == sh.BUBBLE
        self._sp_radius.setEnabled(is_round)
        self._lbl_radius.setEnabled(is_round)
        self._tail_w.setEnabled(is_bubble)
        self._lbl_tail.setEnabled(is_bubble)
        self._update_preview()

    def _pick_stroke_color(self):
        c = QColorDialog.getColor(QColor(self._stroke_color), self,
                                  self.tr("ig4.stroke_color", "Outline color"))
        if c.isValid():
            self._stroke_color = c.name().upper()
            self._sync_color_buttons()
            self._update_preview()

    def _pick_fill_color(self):
        c = QColorDialog.getColor(QColor(self._fill_color), self,
                                  self.tr("ig4.fill_color", "Fill color"))
        if c.isValid():
            self._fill_color = c.name().upper()
            self._chk_fill.setChecked(True)
            self._sync_color_buttons()
            self._update_preview()

    def _sync_color_buttons(self):
        for btn, col in ((self._btn_stroke_color, self._stroke_color),
                         (self._btn_fill_color, self._fill_color)):
            btn.setStyleSheet(
                f"background-color: {col}; color: #000; border-radius: 6px; "
                f"padding: 6px; font-weight: bold;")

    # ── render ──────────────────────────────────────────────────────────
    def _rect(self):
        x0, y0 = self._sp_x.value(), self._sp_y.value()
        return (x0, y0, x0 + self._sp_w.value(), y0 + self._sp_h.value())

    def _compose(self):
        shape = self._cb_shape.currentData()
        tail = (self._sp_tail_x.value(), self._sp_tail_y.value()) if shape == sh.BUBBLE else None
        return sh.render_shape(
            self._base, shape=shape, rect=self._rect(),
            stroke_width=self._sp_stroke.value(), stroke_color=self._stroke_color,
            fill_color=self._fill_color if self._chk_fill.isChecked() else None,
            fill_alpha=self._sl_alpha.value(), radius=self._sp_radius.value(),
            tail=tail)

    def _update_preview(self):
        try:
            img, _ = self._compose()
        except (OSError, ValueError) as exc:
            log.warning("preview de forma falló: %s", exc)
            return
        px = pil_to_qpixmap(img)
        if px.width() > _PREVIEW_MAX or px.height() > _PREVIEW_MAX:
            px = px.scaled(_PREVIEW_MAX, _PREVIEW_MAX, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self._preview.setPixmap(px)

    def _apply(self):
        img, bbox = self._compose()
        if bbox is None:
            self.reject()
            return
        shape = self._cb_shape.currentData()
        self.result_image = img
        self.result_data = {
            "bbox_px": [int(v) for v in bbox],
            "render": {
                "shape": shape,
                "stroke_width": self._sp_stroke.value(),
                "stroke_color": self._stroke_color,
                "fill_color": self._fill_color if self._chk_fill.isChecked() else None,
                "fill_alpha": self._sl_alpha.value(),
                "radius": self._sp_radius.value(),
                "tail": [self._sp_tail_x.value(), self._sp_tail_y.value()] if shape == sh.BUBBLE else None,
                "composited": True,
            },
        }
        self.accept()
