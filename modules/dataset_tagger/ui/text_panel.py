"""
Mini-editor de texto (compositing) del captioner Ideogram 4 — Caso A: texto
NUEVO sobre flyers.

Diálogo con controles mínimos (string, fuente, tamaño, color, efecto, contorno,
posición) y preview en vivo. Al aplicar, compone el texto sobre una COPIA de la
imagen (Pillow, vía text_render) y expone:
  - result_image : PIL.Image compuesta (la nueva copia de salida).
  - result_data  : dict con text, color (#RRGGBB), bbox_px AUTOMÁTICA y render
                   (fuente/tamaño/efecto…) para reproducibilidad.

La bbox no se pide al usuario: la calcula text_render del texto realmente
pintado (ground truth: imagen ↔ caption concuerdan).
"""
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QComboBox, QSpinBox, QSlider, QPushButton, QColorDialog, QCheckBox, QWidget,
)

from core.theme import Theme
from ..logic.ideogram import text_render as tr
from ..logic.ideogram import shape_render as sh

log = logging.getLogger(__name__)

_PREVIEW_MAX = 520


def pil_to_qpixmap(img):
    """Convierte un PIL.Image a QPixmap (copia, sin retener el buffer)."""
    img = img.convert("RGBA")
    qimg = QImage(img.tobytes("raw", "RGBA"), img.width, img.height,
                  QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class TextComposerDialog(QDialog):
    """Compone un bloque de texto sobre la imagen y devuelve imagen + datos."""

    def __init__(self, base_image, init_position=(0, 0), init_text="",
                 locale_manager=None, parent=None):
        super().__init__(parent)
        self._lm = locale_manager
        self._base = base_image.convert("RGB")
        self._color = "#FFFFFF"
        self._stroke_color = "#000000"
        self._frame_color = "#000000"
        self._frame_fill_color = "#FFFFFF"
        self.result_image = None
        self.result_data = None

        self.setWindowTitle(self.tr("ig4.text_title", "Compose text"))
        self.setStyleSheet(f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_PRIMARY};")
        self.resize(900, 620)

        self._build_ui(init_position, init_text)
        self._update_preview()

    def tr(self, key, default):
        return self._lm.tr(key, default) if self._lm else default

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self, init_position, init_text):
        root = QHBoxLayout(self)

        # Preview
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumWidth(_PREVIEW_MAX)
        self._preview.setStyleSheet(f"background-color: {Theme.BG_PANEL}; border-radius: 6px;")
        root.addWidget(self._preview, stretch=1)

        # Controles
        panel = QWidget()
        panel.setFixedWidth(300)
        lay = QVBoxLayout(panel)
        form = QFormLayout()

        self._ed_text = QLineEdit(init_text)
        self._ed_text.setPlaceholderText(self.tr("ig4.text_literal", "literal text"))
        self._ed_text.textChanged.connect(self._update_preview)
        form.addRow(self.tr("ig4.text_field", "text"), self._ed_text)

        self._cb_font = QComboBox()
        for family, path in tr.list_fonts():
            self._cb_font.addItem(family, path)
        self._cb_font.currentIndexChanged.connect(self._update_preview)
        form.addRow(self.tr("ig4.font", "font"), self._cb_font)

        self._sp_size = QSpinBox()
        self._sp_size.setRange(8, 600)
        self._sp_size.setValue(72)
        self._sp_size.valueChanged.connect(self._update_preview)
        form.addRow(self.tr("ig4.size", "size"), self._sp_size)

        self._cb_effect = QComboBox()
        self._cb_effect.addItem(self.tr("ig4.fx_straight", "Straight"), tr.STRAIGHT)
        self._cb_effect.addItem(self.tr("ig4.fx_arc", "Arc (up)"), tr.ARC)
        self._cb_effect.addItem(self.tr("ig4.fx_arc_down", "Arc (down)"), tr.ARC_DOWN)
        self._cb_effect.addItem(self.tr("ig4.fx_persp", "Perspective"), tr.PERSPECTIVE)
        self._cb_effect.addItem(self.tr("ig4.fx_wave", "Wave"), tr.WAVE)
        self._cb_effect.addItem(self.tr("ig4.fx_circle", "Circle"), tr.CIRCLE)
        self._cb_effect.addItem(self.tr("ig4.fx_rotate", "Rotate / tilt"), tr.ROTATE)
        self._cb_effect.addItem(self.tr("ig4.fx_vertical", "Vertical"), tr.VERTICAL)
        self._cb_effect.currentIndexChanged.connect(self._on_effect_changed)
        form.addRow(self.tr("ig4.effect", "effect"), self._cb_effect)

        self._sl_strength = QSlider(Qt.Horizontal)
        self._sl_strength.setRange(5, 80)   # 0.05..0.80
        self._sl_strength.setValue(40)
        self._sl_strength.valueChanged.connect(self._update_preview)
        self._lbl_strength = QLabel(self.tr("ig4.strength", "strength"))
        form.addRow(self._lbl_strength, self._sl_strength)

        self._sp_stroke = QSpinBox()
        self._sp_stroke.setRange(0, 40)
        self._sp_stroke.setValue(0)
        self._sp_stroke.valueChanged.connect(self._update_preview)
        form.addRow(self.tr("ig4.stroke", "outline"), self._sp_stroke)

        self._sp_x = QSpinBox(); self._sp_x.setRange(0, self._base.width)
        self._sp_y = QSpinBox(); self._sp_y.setRange(0, self._base.height)
        self._sp_x.setValue(int(init_position[0]))
        self._sp_y.setValue(int(init_position[1]))
        self._sp_x.valueChanged.connect(self._update_preview)
        self._sp_y.valueChanged.connect(self._update_preview)
        pos = QHBoxLayout(); pos.addWidget(self._sp_x); pos.addWidget(self._sp_y)
        pos_w = QWidget(); pos_w.setLayout(pos)
        form.addRow(self.tr("ig4.position", "x / y"), pos_w)

        lay.addLayout(form)

        # Alineación rápida: mueve el bloque de texto a una posición canónica del
        # lienzo (izq/centro/der · arriba/medio/abajo) midiendo lo realmente
        # pintado. Los spinboxes x/y siguen disponibles para ajuste fino.
        lay.addWidget(self._align_label(self.tr("ig4.align", "align on canvas")))
        hrow = QHBoxLayout()
        for key, default, where in (("ig4.align_left", "◀ Left", "left"),
                                    ("ig4.align_hcenter", "● Center", "center"),
                                    ("ig4.align_right", "Right ▶", "right")):
            hrow.addWidget(self._align_btn(key, default, self._anchor_h, where))
        lay.addLayout(hrow)
        vrow = QHBoxLayout()
        for key, default, where in (("ig4.align_top", "▲ Top", "top"),
                                    ("ig4.align_vcenter", "● Middle", "middle"),
                                    ("ig4.align_bottom", "Bottom ▼", "bottom")):
            vrow.addWidget(self._align_btn(key, default, self._anchor_v, where))
        lay.addLayout(vrow)

        # Colores
        self._btn_color = QPushButton(self.tr("ig4.text_color", "Text color…"))
        self._btn_color.clicked.connect(self._pick_color)
        self._btn_stroke_color = QPushButton(self.tr("ig4.stroke_color", "Outline color…"))
        self._btn_stroke_color.clicked.connect(self._pick_stroke_color)
        lay.addWidget(self._btn_color)
        lay.addWidget(self._btn_stroke_color)
        self._sync_color_buttons()

        # Marco / globo detrás del texto (diálogos). Auto-envuelve el texto: la
        # geometría sale de la bbox del texto + padding, el usuario solo elige
        # forma, borde y relleno.
        fform = QFormLayout()
        self._cb_frame = QComboBox()
        self._cb_frame.addItem(self.tr("ig4.frame_none", "No frame"), None)
        self._cb_frame.addItem(self.tr("ig4.shape_rect", "Rectangle"), sh.RECT)
        self._cb_frame.addItem(self.tr("ig4.shape_rounded", "Rounded rectangle"), sh.ROUNDED)
        self._cb_frame.addItem(self.tr("ig4.shape_ellipse", "Ellipse / circle"), sh.ELLIPSE)
        self._cb_frame.addItem(self.tr("ig4.shape_bubble", "Speech bubble"), sh.BUBBLE)
        self._cb_frame.currentIndexChanged.connect(self._update_preview)
        fform.addRow(self.tr("ig4.frame", "frame"), self._cb_frame)
        self._sp_frame_stroke = QSpinBox()
        self._sp_frame_stroke.setRange(0, 60)
        self._sp_frame_stroke.setValue(4)
        self._sp_frame_stroke.valueChanged.connect(self._update_preview)
        fform.addRow(self.tr("ig4.frame_stroke", "frame border"), self._sp_frame_stroke)
        lay.addLayout(fform)
        self._chk_frame_fill = QCheckBox(self.tr("ig4.frame_fill", "Fill frame"))
        self._chk_frame_fill.setChecked(True)
        self._chk_frame_fill.toggled.connect(self._update_preview)
        lay.addWidget(self._chk_frame_fill)
        self._btn_frame_color = QPushButton(self.tr("ig4.frame_color", "Frame border color…"))
        self._btn_frame_color.clicked.connect(self._pick_frame_color)
        self._btn_frame_fill_color = QPushButton(self.tr("ig4.frame_fill_color", "Frame fill color…"))
        self._btn_frame_fill_color.clicked.connect(self._pick_frame_fill_color)
        lay.addWidget(self._btn_frame_color)
        lay.addWidget(self._btn_frame_fill_color)
        self._sync_frame_buttons()

        lay.addStretch()

        # Acciones
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
        self._on_effect_changed()

    def _set_tooltips(self):
        tips = {
            self._ed_text: self.tr("ig4.tip.tx_text",
                "The exact words to render (this becomes the element's ground-truth text)."),
            self._cb_font: self.tr("ig4.tip.tx_font",
                "Typeface. Bundled OFL/Apache fonts — same look on any machine."),
            self._sp_size: self.tr("ig4.tip.tx_size", "Text size in pixels."),
            self._cb_effect: self.tr("ig4.tip.tx_effect",
                "Layout style: straight, arc, wave, circle, perspective, tilt or vertical."),
            self._sl_strength: self.tr("ig4.tip.tx_strength",
                "Intensity of the chosen effect (curvature/amplitude/angle). Disabled for Straight and Vertical."),
            self._sp_stroke: self.tr("ig4.tip.tx_stroke",
                "Outline thickness around the glyphs (0 = none). Helps readability over busy images."),
            self._sp_x: self.tr("ig4.tip.tx_pos", "Top-left X position of the text block, in pixels."),
            self._sp_y: self.tr("ig4.tip.tx_pos_y", "Top-left Y position of the text block, in pixels."),
            self._btn_color: self.tr("ig4.tip.tx_color",
                "Glyph color. Saved as the element's ground-truth color (#RRGGBB)."),
            self._btn_stroke_color: self.tr("ig4.tip.tx_stroke_color", "Outline color."),
            self._cb_frame: self.tr("ig4.tip.tx_frame",
                "Optional shape drawn BEHIND the text (speech bubble for dialog). It auto-wraps the text."),
            self._sp_frame_stroke: self.tr("ig4.tip.tx_frame_stroke", "Border thickness of the frame/bubble."),
            self._chk_frame_fill: self.tr("ig4.tip.tx_frame_fill",
                "Fill the frame with a solid color so the text reads on top (on by default for bubbles)."),
            self._btn_frame_color: self.tr("ig4.tip.tx_frame_color", "Frame/bubble border color."),
            self._btn_frame_fill_color: self.tr("ig4.tip.tx_frame_fill_color", "Frame/bubble fill color."),
            self._btn_apply: self.tr("ig4.tip.tx_apply",
                "Bake the text (and frame) onto the output copy and return. The box is computed automatically."),
        }
        for w, t in tips.items():
            w.setToolTip(t)

    # ── alineación / posición ───────────────────────────────────────────
    def _align_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 11px;")
        return lbl

    def _align_btn(self, key, default, slot, where):
        b = QPushButton(self.tr(key, default))
        b.setToolTip(self.tr("ig4.tip.align",
            "Snap the text block to this position on the image (fine-tune with x/y)."))
        b.clicked.connect(lambda: slot(where))
        return b

    def _measure_box(self):
        """bbox real [x0,y0,x1,y1] del texto pintado con los ajustes actuales, o None.

        Mide lo realmente compuesto (incluye efectos y marco), de modo que el
        anclaje cuadra con lo que el usuario ve, no con métricas teóricas."""
        try:
            _, bbox = self._compose()
        except (OSError, ValueError) as exc:
            log.warning("medición de texto falló: %s", exc)
            return None
        return bbox

    def _anchor_h(self, where):
        bbox = self._measure_box()
        if not bbox:
            return
        w = bbox[2] - bbox[0]
        off = bbox[0] - self._sp_x.value()       # desfase render↔x pedido (efectos)
        margin = max(int(self._base.width * 0.02), 8)
        if where == "left":
            target = margin
        elif where == "center":
            target = max((self._base.width - w) // 2, 0)
        else:  # right
            target = max(self._base.width - w - margin, 0)
        self._sp_x.setValue(max(int(target - off), 0))

    def _anchor_v(self, where):
        bbox = self._measure_box()
        if not bbox:
            return
        h = bbox[3] - bbox[1]
        off = bbox[1] - self._sp_y.value()
        margin = max(int(self._base.height * 0.02), 8)
        if where == "top":
            target = margin
        elif where == "middle":
            target = max((self._base.height - h) // 2, 0)
        else:  # bottom
            target = max(self._base.height - h - margin, 0)
        self._sp_y.setValue(max(int(target - off), 0))

    def _on_effect_changed(self):
        # La intensidad solo aplica a efectos con parámetro continuo.
        has_strength = self._cb_effect.currentData() not in (tr.STRAIGHT, tr.VERTICAL)
        self._sl_strength.setEnabled(has_strength)
        self._lbl_strength.setEnabled(has_strength)
        self._update_preview()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, self.tr("ig4.text_color", "Text color"))
        if c.isValid():
            self._color = c.name().upper()
            self._sync_color_buttons()
            self._update_preview()

    def _pick_stroke_color(self):
        c = QColorDialog.getColor(QColor(self._stroke_color), self,
                                  self.tr("ig4.stroke_color", "Outline color"))
        if c.isValid():
            self._stroke_color = c.name().upper()
            self._sync_color_buttons()
            self._update_preview()

    def _sync_color_buttons(self):
        for btn, col in ((self._btn_color, self._color),
                         (self._btn_stroke_color, self._stroke_color)):
            btn.setStyleSheet(
                f"background-color: {col}; color: #000; border-radius: 6px; "
                f"padding: 6px; font-weight: bold;")

    def _pick_frame_color(self):
        c = QColorDialog.getColor(QColor(self._frame_color), self,
                                  self.tr("ig4.frame_color", "Frame border color"))
        if c.isValid():
            self._frame_color = c.name().upper()
            self._sync_frame_buttons()
            self._update_preview()

    def _pick_frame_fill_color(self):
        c = QColorDialog.getColor(QColor(self._frame_fill_color), self,
                                  self.tr("ig4.frame_fill_color", "Frame fill color"))
        if c.isValid():
            self._frame_fill_color = c.name().upper()
            self._chk_frame_fill.setChecked(True)
            self._sync_frame_buttons()
            self._update_preview()

    def _sync_frame_buttons(self):
        for btn, col in ((self._btn_frame_color, self._frame_color),
                         (self._btn_frame_fill_color, self._frame_fill_color)):
            btn.setStyleSheet(
                f"background-color: {col}; color: #000; border-radius: 6px; "
                f"padding: 6px; font-weight: bold;")

    # ── render ──────────────────────────────────────────────────────────
    def _text_kwargs(self):
        return dict(
            text=self._ed_text.text(),
            font_path=self._cb_font.currentData(), size=self._sp_size.value(),
            color=self._color, position=(self._sp_x.value(), self._sp_y.value()),
            effect=self._cb_effect.currentData(),
            stroke_width=self._sp_stroke.value(), stroke_color=self._stroke_color,
            strength=self._sl_strength.value() / 100.0,
        )

    def _compose(self):
        """Devuelve (imagen_compuesta, bbox_px) con los parámetros actuales.

        Sin marco: bbox = caja del texto. Con marco (globo de diálogo): el marco
        se dibuja DETRÁS auto-envolviendo el texto (bbox del texto + padding) y la
        bbox del elemento pasa a ser la envolvente del marco.
        """
        kw = self._text_kwargs()
        frame = self._cb_frame.currentData()
        if frame is None:
            return tr.render_text(self._base, **kw)

        _, tbox = tr.render_text(self._base, **kw)  # bbox del texto para envolver
        if tbox is None:
            return tr.render_text(self._base, **kw)
        pad = max(int(self._sp_size.value() * 0.45), 16)
        rect = (max(tbox[0] - pad, 0), max(tbox[1] - pad, 0),
                min(tbox[2] + pad, self._base.width), min(tbox[3] + pad, self._base.height))
        framed, fbbox = sh.render_shape(
            self._base, shape=frame, rect=rect,
            stroke_width=self._sp_frame_stroke.value(), stroke_color=self._frame_color,
            fill_color=self._frame_fill_color if self._chk_frame_fill.isChecked() else None,
            radius=pad)
        out, _ = tr.render_text(framed, **kw)
        return out, (fbbox if fbbox else tbox)

    def _update_preview(self):
        try:
            img, _ = self._compose()
        except (OSError, ValueError) as exc:
            log.warning("preview de texto falló: %s", exc)
            return
        px = pil_to_qpixmap(img)
        if px.width() > _PREVIEW_MAX or px.height() > _PREVIEW_MAX:
            px = px.scaled(_PREVIEW_MAX, _PREVIEW_MAX, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        self._preview.setPixmap(px)

    def _apply(self):
        if not self._ed_text.text().strip():
            self.reject()
            return
        img, bbox = self._compose()
        if bbox is None:
            self.reject()
            return
        frame = self._cb_frame.currentData()
        render = {
            "font": Path(self._cb_font.currentData()).name,
            "size": self._sp_size.value(),
            "effect": self._cb_effect.currentData(),
            "strength": self._sl_strength.value() / 100.0,
            "stroke_width": self._sp_stroke.value(),
            "stroke_color": self._stroke_color,
            "composited": True,
        }
        if frame is not None:
            render["frame"] = {
                "shape": frame,
                "stroke_width": self._sp_frame_stroke.value(),
                "stroke_color": self._frame_color,
                "fill_color": self._frame_fill_color if self._chk_frame_fill.isChecked() else None,
            }
        self.result_image = img
        self.result_data = {
            "text": self._ed_text.text(),
            "color": self._color,
            "bbox_px": [int(v) for v in bbox],
            "render": render,
        }
        self.accept()
