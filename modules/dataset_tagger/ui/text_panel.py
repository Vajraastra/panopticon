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
    QComboBox, QSpinBox, QSlider, QPushButton, QColorDialog, QWidget,
)

from core.theme import Theme
from ..logic.ideogram import text_render as tr

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
        self._cb_effect.addItem(self.tr("ig4.fx_arc", "Arc"), tr.ARC)
        self._cb_effect.addItem(self.tr("ig4.fx_persp", "Perspective"), tr.PERSPECTIVE)
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

        # Colores
        self._btn_color = QPushButton(self.tr("ig4.text_color", "Text color…"))
        self._btn_color.clicked.connect(self._pick_color)
        self._btn_stroke_color = QPushButton(self.tr("ig4.stroke_color", "Outline color…"))
        self._btn_stroke_color.clicked.connect(self._pick_stroke_color)
        lay.addWidget(self._btn_color)
        lay.addWidget(self._btn_stroke_color)
        self._sync_color_buttons()

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
        self._on_effect_changed()

    def _on_effect_changed(self):
        is_fx = self._cb_effect.currentData() != tr.STRAIGHT
        self._sl_strength.setEnabled(is_fx)
        self._lbl_strength.setEnabled(is_fx)
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

    # ── render ──────────────────────────────────────────────────────────
    def _compose(self):
        """Devuelve (imagen_compuesta, bbox_px) con los parámetros actuales."""
        return tr.render_text(
            self._base, text=self._ed_text.text(),
            font_path=self._cb_font.currentData(), size=self._sp_size.value(),
            color=self._color, position=(self._sp_x.value(), self._sp_y.value()),
            effect=self._cb_effect.currentData(),
            stroke_width=self._sp_stroke.value(), stroke_color=self._stroke_color,
            strength=self._sl_strength.value() / 100.0,
        )

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
        self.result_image = img
        self.result_data = {
            "text": self._ed_text.text(),
            "color": self._color,
            "bbox_px": [int(v) for v in bbox],
            "render": {
                "font": Path(self._cb_font.currentData()).name,
                "size": self._sp_size.value(),
                "effect": self._cb_effect.currentData(),
                "strength": self._sl_strength.value() / 100.0,
                "stroke_width": self._sp_stroke.value(),
                "stroke_color": self._stroke_color,
                "composited": True,
            },
        }
        self.accept()
