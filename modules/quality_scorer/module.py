import os
import shutil
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
    QStackedWidget, QGridLayout, QComboBox, QCheckBox, QGroupBox,
    QSplitter, QApplication,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QFont

from core.base_module import BaseModule
from core.theme import Theme
from core.paths import CachePaths
from core.components.standard_layout import StandardToolLayout
from modules.quality_scorer.logic.quality_scorer import PROFILES, DEFAULT_PROFILE

log = logging.getLogger(__name__)

# Colores para las tres categorías
CAT_COLORS = {
    "keeper": "#00cc88",
    "review": "#ffaa00",
    "slop":   "#cc3333",
}
EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.avif')


# ============================================================================
# WIDGET DE THUMBNAIL CON LABEL DE SCORE
# ============================================================================

class ThumbnailCard(QFrame):
    """Miniatura de imagen con borde de color según categoría."""
    clicked = Signal(str)   # emite el path al hacer click

    def __init__(self, path: str, label: str, scores: dict, size: int = 110):
        super().__init__()
        self.path = path
        color = CAT_COLORS.get(label, "#666")

        self.setFixedSize(size + 8, size + 28)
        self.setStyleSheet(
            f"QFrame {{ border: 2px solid {color}; border-radius: 6px; "
            f"background: #0d0d0d; }}"
        )
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)

        img_lbl = QLabel()
        img_lbl.setFixedSize(size, size)
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setStyleSheet("border: none; background: transparent;")
        pix = QPixmap(path)
        if not pix.isNull():
            img_lbl.setPixmap(
                pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        layout.addWidget(img_lbl)

        score_lbl = QLabel(f"{scores.get('combined', 0):.2f}")
        score_lbl.setAlignment(Qt.AlignCenter)
        score_lbl.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: bold; border: none;"
        )
        layout.addWidget(score_lbl)

    def mousePressEvent(self, event):
        self.clicked.emit(self.path)
        super().mousePressEvent(event)


# ============================================================================
# MÓDULO PRINCIPAL
# ============================================================================

class QualityScorerModule(BaseModule):

    def __init__(self):
        super().__init__()
        self._name        = "Quality Scorer"
        self._description = "Filtra slop anatómico y rankea calidad técnica para datasets de IA."
        self._icon        = "📊"
        self.accent_color = "#00cc88"

        self.view = None

        # Estado compartido
        self.image_paths:  list = []
        self.base_folder:  str  = None
        self.phase2_folder: str = None   # puede diferir de base_folder
        self.last_dir:     str  = os.path.expanduser("~")

        # Resultados Fase 1  {path → {label, scores}}
        self.phase1_results: dict = {}

        # Resultados Fase 2  [lista de dicts ordenada por score desc]
        self.phase2_results: list = []

        # Workers activos
        self._slop_worker = None
        self._rank_worker = None

        # Paginación de resultados
        self._page_size    = 50
        self._pages: dict  = {"keeper": 0, "review": 0, "slop": 0}
        self._phase2_page  = 0

    # ------------------------------------------------------------------ #
    # Construcción de la vista
    # ------------------------------------------------------------------ #

    def get_view(self) -> QWidget:
        if self.view:
            return self.view
        content = self._create_content()
        sidebar = self._create_sidebar()
        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus'),
        )
        return self.view

    # ------------------------------------------------------------------ #
    # SIDEBAR
    # ------------------------------------------------------------------ #

    def _create_sidebar(self) -> QWidget:
        theme    = self.context.get('theme_manager')
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY
        border   = theme.get_color('border')         if theme else Theme.BORDER

        grp_style = f"""
            QGroupBox {{
                color: {text_sec}; font-weight: bold;
                border: 1px solid {border}; border-radius: 6px;
                margin-top: 10px; padding-top: 10px;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; }}
        """

        container = QWidget()
        layout    = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Título
        lbl_title = QLabel(self.tr("qs.title", "📊 QUALITY SCORER"))
        lbl_title.setStyleSheet(
            f"color: {self.accent_color}; font-weight: bold; font-size: 14px;"
        )
        layout.addWidget(lbl_title)

        lbl_desc = QLabel(self.tr("qs.desc",
            "Filtra slop anatómico (Fase 1) y rankea calidad técnica (Fase 2)."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        layout.addWidget(lbl_desc)

        layout.addSpacing(4)

        # Carga de carpeta
        self.btn_load = QPushButton(self.tr("qs.load_folder", "📂 Cargar Carpeta"))
        self.btn_load.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_load.setFixedHeight(36)
        self.btn_load.clicked.connect(self._load_folder_dialog)
        layout.addWidget(self.btn_load)

        self.lbl_loaded = QLabel(self.tr("qs.no_folder", "Sin carpeta cargada."))
        self.lbl_loaded.setWordWrap(True)
        self.lbl_loaded.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(self.lbl_loaded)

        layout.addSpacing(6)

        # ── FASE 1 ──────────────────────────────────────────────────── #
        grp1 = QGroupBox(self.tr("qs.phase1.title", "⚡ Fase 1 — Slop Filter"))
        grp1.setStyleSheet(grp_style)
        lay1 = QVBoxLayout(grp1)

        lbl_preset = QLabel(self.tr("qs.phase1.preset", "Preset de filtrado:"))
        lbl_preset.setStyleSheet(f"color: {text_sec}; font-size: 11px;")
        lay1.addWidget(lbl_preset)

        self.combo_preset = QComboBox()
        self.combo_preset.addItem(self.tr("qs.preset.strict",   "Estricto"),   "strict")
        self.combo_preset.addItem(self.tr("qs.preset.balanced", "Balanceado"), "balanced")
        self.combo_preset.addItem(self.tr("qs.preset.lenient",  "Permisivo"),  "lenient")
        self.combo_preset.setCurrentIndex(1)
        self.combo_preset.setStyleSheet(self._combo_style(border))
        lay1.addWidget(self.combo_preset)

        lay1.addSpacing(4)
        lbl_models = QLabel(self.tr("qs.phase1.models", "Modelos activos:"))
        lbl_models.setStyleSheet(f"color: {text_sec}; font-size: 11px;")
        lay1.addWidget(lbl_models)

        self.chk_face      = QCheckBox(self.tr("qs.model.face",      "Rostro (YuNet)"))
        self.chk_body      = QCheckBox(self.tr("qs.model.body",      "Cuerpo (YOLOv8)"))
        self.chk_hands     = QCheckBox(self.tr("qs.model.hands",     "Manos (MediaPipe)"))
        self.chk_aesthetic = QCheckBox(self.tr("qs.model.aesthetic", "Estética (CLIP)"))
        for chk in (self.chk_face, self.chk_body, self.chk_hands, self.chk_aesthetic):
            chk.setChecked(True)
            chk.setStyleSheet(f"color: {text_sec}; font-size: 11px;")
            lay1.addWidget(chk)

        lay1.addSpacing(6)
        self.btn_run1 = QPushButton(self.tr("qs.phase1.run", "▶ Iniciar Fase 1"))
        self.btn_run1.setEnabled(False)
        self.btn_run1.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_run1.setFixedHeight(38)
        self.btn_run1.clicked.connect(self._run_phase1)
        lay1.addWidget(self.btn_run1)

        self.btn_stop1 = QPushButton(self.tr("qs.stop", "⏹ Detener"))
        self.btn_stop1.setVisible(False)
        self.btn_stop1.setStyleSheet(Theme.get_button_style("#884400"))
        self.btn_stop1.setFixedHeight(32)
        self.btn_stop1.clicked.connect(self._stop_phase1)
        lay1.addWidget(self.btn_stop1)

        # Acciones post-Fase1 (ocultas hasta que termine)
        self.btn_move_slop = QPushButton(self.tr("qs.phase1.move_slop", "🗑️ Mover SLOP → /slop/"))
        self.btn_move_slop.setVisible(False)
        self.btn_move_slop.setStyleSheet(Theme.get_button_style("#883333"))
        self.btn_move_slop.setFixedHeight(34)
        self.btn_move_slop.clicked.connect(self._move_slop)
        lay1.addWidget(self.btn_move_slop)

        self.btn_to_phase2 = QPushButton(self.tr("qs.phase1.to_phase2", "→ Pasar KEEPERs a Fase 2"))
        self.btn_to_phase2.setVisible(False)
        self.btn_to_phase2.setStyleSheet(Theme.get_button_style(self.accent_color))
        self.btn_to_phase2.setFixedHeight(34)
        self.btn_to_phase2.clicked.connect(self._keepers_to_phase2)
        lay1.addWidget(self.btn_to_phase2)

        layout.addWidget(grp1)

        # ── FASE 2 ──────────────────────────────────────────────────── #
        grp2 = QGroupBox(self.tr("qs.phase2.title", "📊 Fase 2 — Quality Rank"))
        grp2.setStyleSheet(grp_style)
        lay2 = QVBoxLayout(grp2)

        lbl_prof = QLabel(self.tr("qs.phase2.profile", "Perfil de contenido:"))
        lbl_prof.setStyleSheet(f"color: {text_sec}; font-size: 11px;")
        lay2.addWidget(lbl_prof)

        self.combo_profile = QComboBox()
        for key, prof in PROFILES.items():
            self.combo_profile.addItem(prof["name"], key)
        self.combo_profile.setCurrentIndex(0)
        self.combo_profile.setStyleSheet(self._combo_style(border))
        lay2.addWidget(self.combo_profile)

        lay2.addSpacing(4)
        lbl_f2 = QLabel(self.tr("qs.phase2.folder_label", "Carpeta de entrada:"))
        lbl_f2.setStyleSheet(f"color: {text_sec}; font-size: 11px;")
        lay2.addWidget(lbl_f2)

        self.lbl_phase2_folder = QLabel(self.tr("qs.phase2.no_folder", "— (usa carpeta de Fase 1)"))
        self.lbl_phase2_folder.setWordWrap(True)
        self.lbl_phase2_folder.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        lay2.addWidget(self.lbl_phase2_folder)

        self.btn_change_f2 = QPushButton(self.tr("qs.phase2.change_folder", "Cambiar carpeta…"))
        self.btn_change_f2.setStyleSheet(Theme.get_button_style("#444"))
        self.btn_change_f2.setFixedHeight(30)
        self.btn_change_f2.clicked.connect(self._change_phase2_folder)
        lay2.addWidget(self.btn_change_f2)

        lay2.addSpacing(6)
        self.btn_run2 = QPushButton(self.tr("qs.phase2.run", "▶ Iniciar Fase 2"))
        self.btn_run2.setEnabled(False)
        self.btn_run2.setStyleSheet(Theme.get_action_button_style("#4499ff", "#ffffff"))
        self.btn_run2.setFixedHeight(38)
        self.btn_run2.clicked.connect(self._run_phase2)
        lay2.addWidget(self.btn_run2)

        self.btn_stop2 = QPushButton(self.tr("qs.stop", "⏹ Detener"))
        self.btn_stop2.setVisible(False)
        self.btn_stop2.setStyleSheet(Theme.get_button_style("#884400"))
        self.btn_stop2.setFixedHeight(32)
        self.btn_stop2.clicked.connect(self._stop_phase2)
        lay2.addWidget(self.btn_stop2)

        layout.addWidget(grp2)
        layout.addStretch()

        lbl_note = QLabel(self.tr("qs.note",
            "Los originales nunca se modifican. El SLOP se mueve a /slop/."))
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        layout.addWidget(lbl_note)

        return container

    def _combo_style(self, border: str) -> str:
        return f"""
            QComboBox {{
                background: #222; color: #eee;
                border: 1px solid {border}; border-radius: 6px; padding: 6px;
            }}
            QComboBox::drop-down {{ border: none; }}
            QComboBox QAbstractItemView {{
                background: #222; color: white;
                selection-background-color: {self.accent_color}; selection-color: black;
            }}
        """

    # ------------------------------------------------------------------ #
    # CONTENT AREA
    # ------------------------------------------------------------------ #

    def _create_content(self) -> QWidget:
        theme    = self.context.get('theme_manager')
        bg_panel = theme.get_color('bg_panel')     if theme else Theme.BG_PANEL
        border   = theme.get_color('border')       if theme else Theme.BORDER
        text_dim = theme.get_color('text_dim')     if theme else Theme.TEXT_DIM
        text_pri = theme.get_color('text_primary') if theme else Theme.TEXT_PRIMARY

        self.content_stack = QStackedWidget()

        # ── Página 0: Drop zone ──────────────────────────────────────── #
        dropzone = QFrame()
        dropzone.setStyleSheet(
            f"border: 2px dashed {border}; border-radius: 20px; background: {bg_panel};"
        )
        dz_layout = QVBoxLayout(dropzone)
        dz_layout.setAlignment(Qt.AlignCenter)
        lbl_dz = QLabel(self.tr("qs.dropzone",
            "📥 Arrastra una carpeta aquí\no usa el botón 'Cargar Carpeta'"))
        lbl_dz.setAlignment(Qt.AlignCenter)
        lbl_dz.setStyleSheet(
            f"color: {text_dim}; font-size: 18px; font-weight: bold; border: none;"
        )
        dz_layout.addWidget(lbl_dz)
        self.content_stack.addWidget(dropzone)          # index 0

        # ── Página 1: Procesando (Fase 1 o 2) ───────────────────────── #
        proc_page   = QWidget()
        proc_layout = QVBoxLayout(proc_page)
        proc_layout.setAlignment(Qt.AlignCenter)
        self.lbl_proc_title = QLabel("")
        self.lbl_proc_title.setAlignment(Qt.AlignCenter)
        self.lbl_proc_title.setStyleSheet(
            f"color: {self.accent_color}; font-size: 16px; font-weight: bold;"
        )
        proc_layout.addWidget(self.lbl_proc_title)

        self.lbl_proc_file = QLabel("")
        self.lbl_proc_file.setAlignment(Qt.AlignCenter)
        self.lbl_proc_file.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        proc_layout.addWidget(self.lbl_proc_file)

        self.proc_bar = QProgressBar()
        self.proc_bar.setFixedWidth(500)
        self.proc_bar.setFixedHeight(10)
        proc_layout.addWidget(self.proc_bar, 0, Qt.AlignCenter)

        # Contadores en vivo (Fase 1)
        counters_w  = QWidget()
        counters_lay = QHBoxLayout(counters_w)
        counters_lay.setSpacing(40)
        self.lbl_cnt = {}
        for label, color in CAT_COLORS.items():
            lbl = QLabel("0")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                f"color: {color}; font-size: 22px; font-weight: bold; border: none;"
            )
            sub = QVBoxLayout()
            sub.addWidget(lbl)
            cap = QLabel(label.upper())
            cap.setAlignment(Qt.AlignCenter)
            cap.setStyleSheet(f"color: {color}; font-size: 10px; border: none;")
            sub.addWidget(cap)
            counters_lay.addLayout(sub)
            self.lbl_cnt[label] = lbl
        proc_layout.addWidget(counters_w)
        self.content_stack.addWidget(proc_page)         # index 1

        # ── Página 2: Resultados Fase 1 (3 columnas) ────────────────── #
        res1_page   = QWidget()
        res1_layout = QVBoxLayout(res1_page)

        # Header con resumen
        self.lbl_res1_summary = QLabel("")
        self.lbl_res1_summary.setStyleSheet(
            f"color: {self.accent_color}; font-size: 13px; font-weight: bold; padding: 6px;"
        )
        res1_layout.addWidget(self.lbl_res1_summary)

        splitter = QSplitter(Qt.Horizontal)
        self._bucket_grids   = {}   # label → QGridLayout
        self._bucket_pages   = {}   # label → current page
        self._bucket_paths   = {}   # label → [paths] (ordenados)
        self._bucket_btns    = {}   # label → (btn_prev, btn_next, lbl_page)

        for label, color in CAT_COLORS.items():
            col_widget = QWidget()
            col_layout = QVBoxLayout(col_widget)
            col_layout.setContentsMargins(4, 4, 4, 4)
            col_layout.setSpacing(4)

            icons = {"keeper": "✅", "review": "⚠️", "slop": "🗑️"}
            lbl_h = QLabel(f"{icons[label]}  {label.upper()}  (0)")
            lbl_h.setAlignment(Qt.AlignCenter)
            lbl_h.setStyleSheet(
                f"color: {color}; font-weight: bold; font-size: 13px; "
                f"border-bottom: 1px solid {color}; padding-bottom: 4px;"
            )
            col_layout.addWidget(lbl_h)
            self._bucket_header = getattr(self, '_bucket_headers', {})
            self._bucket_headers = getattr(self, '_bucket_headers', {})
            self._bucket_headers[label] = lbl_h

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("border: none; background: transparent;")
            grid_container = QWidget()
            grid = QGridLayout(grid_container)
            grid.setSpacing(6)
            grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            scroll.setWidget(grid_container)
            col_layout.addWidget(scroll)
            self._bucket_grids[label] = grid

            # Paginación
            pag_w   = QWidget()
            pag_lay = QHBoxLayout(pag_w)
            pag_lay.setContentsMargins(0, 0, 0, 0)
            btn_prev = QPushButton("◀")
            btn_next = QPushButton("▶")
            lbl_pag  = QLabel("1/1")
            lbl_pag.setAlignment(Qt.AlignCenter)
            lbl_pag.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
            for b in (btn_prev, btn_next):
                b.setStyleSheet(Theme.get_button_style("#444"))
                b.setFixedHeight(24)
            btn_prev.clicked.connect(lambda _, lb=label: self._bucket_prev(lb))
            btn_next.clicked.connect(lambda _, lb=label: self._bucket_next(lb))
            pag_lay.addWidget(btn_prev)
            pag_lay.addWidget(lbl_pag)
            pag_lay.addWidget(btn_next)
            col_layout.addWidget(pag_w)
            self._bucket_btns[label]  = (btn_prev, btn_next, lbl_pag)
            self._bucket_pages[label] = 0
            self._bucket_paths[label] = []

            splitter.addWidget(col_widget)

        res1_layout.addWidget(splitter)
        self.content_stack.addWidget(res1_page)         # index 2

        # ── Página 3: Resultados Fase 2 (grid ordenado) ─────────────── #
        res2_page   = QWidget()
        res2_layout = QVBoxLayout(res2_page)

        self.lbl_res2_summary = QLabel("")
        self.lbl_res2_summary.setStyleSheet(
            f"color: #4499ff; font-size: 13px; font-weight: bold; padding: 6px;"
        )
        res2_layout.addWidget(self.lbl_res2_summary)

        # Paginación Fase 2
        pag2_w   = QWidget()
        pag2_lay = QHBoxLayout(pag2_w)
        self.btn2_prev = QPushButton("◀ " + self.tr("common.prev", "Anterior"))
        self.btn2_next = QPushButton(self.tr("common.next", "Siguiente") + " ▶")
        self.lbl2_page = QLabel("1/1")
        self.lbl2_page.setAlignment(Qt.AlignCenter)
        self.lbl2_page.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        for b in (self.btn2_prev, self.btn2_next):
            b.setStyleSheet(Theme.get_button_style("#444"))
            b.setFixedHeight(28)
        self.btn2_prev.clicked.connect(self._phase2_prev)
        self.btn2_next.clicked.connect(self._phase2_next)
        pag2_lay.addWidget(self.btn2_prev)
        pag2_lay.addStretch()
        pag2_lay.addWidget(self.lbl2_page)
        pag2_lay.addStretch()
        pag2_lay.addWidget(self.btn2_next)
        res2_layout.addWidget(pag2_w)

        scroll2 = QScrollArea()
        scroll2.setWidgetResizable(True)
        scroll2.setStyleSheet("border: none; background: transparent;")
        self.grid2_container = QWidget()
        self.grid2_layout    = QGridLayout(self.grid2_container)
        self.grid2_layout.setSpacing(8)
        self.grid2_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll2.setWidget(self.grid2_container)
        res2_layout.addWidget(scroll2)
        self.content_stack.addWidget(res2_page)         # index 3

        # ── Wrapper con barra de estado ──────────────────────────────── #
        wrapper = QWidget()
        w_layout = QVBoxLayout(wrapper)
        w_layout.setContentsMargins(0, 0, 0, 0)
        w_layout.addWidget(self.content_stack)

        self.lbl_status = QLabel(self.tr("common.status.ready", "Listo."))
        self.lbl_status.setStyleSheet(
            f"color: {self.accent_color}; font-weight: bold; padding: 4px;"
        )
        w_layout.addWidget(self.lbl_status)

        wrapper.setAcceptDrops(True)
        wrapper.dragEnterEvent = self._drag_enter
        wrapper.dropEvent      = self._drop

        return wrapper

    # ------------------------------------------------------------------ #
    # DRAG & DROP
    # ------------------------------------------------------------------ #

    def _drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def _drop(self, event):
        paths   = [url.toLocalFile() for url in event.mimeData().urls()]
        folders = [p for p in paths if os.path.isdir(p)]
        if folders:
            self._load_folder(folders[0])

    # ------------------------------------------------------------------ #
    # CARGA DE CARPETA
    # ------------------------------------------------------------------ #

    def _load_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(
            self.view, self.tr("common.select_folder", "Seleccionar Carpeta"), self.last_dir
        )
        if folder:
            self._load_folder(folder)

    def _load_folder(self, folder: str):
        self.last_dir    = folder
        self.base_folder = folder

        self.image_paths = [
            str(p) for p in Path(folder).rglob("*")
            if p.suffix.lower() in EXTENSIONS
            and "slop" not in p.parts
        ]
        count = len(self.image_paths)

        if count == 0:
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "Error"),
                self.tr("qs.msg.no_images", "No se encontraron imágenes en la carpeta.")
            )
            return

        short = Path(folder).name
        self.lbl_loaded.setText(
            self.tr("qs.loaded", "📁 {folder} ({count} imgs)").format(
                folder=short, count=count
            )
        )
        # Fase 2 usa la misma carpeta por defecto
        self.phase2_folder = folder
        self.lbl_phase2_folder.setText(short)

        self.btn_run1.setEnabled(True)
        self.btn_run2.setEnabled(True)
        self.content_stack.setCurrentIndex(0)
        self.lbl_status.setText(
            self.tr("qs.status.loaded", "{count} imágenes listas.").format(count=count)
        )

    # ------------------------------------------------------------------ #
    # FASE 1 — SLOP FILTER
    # ------------------------------------------------------------------ #

    def _run_phase1(self):
        if not self.image_paths:
            return
        from modules.quality_scorer.logic.workers import SlopFilterWorker

        # Reset counters
        self.phase1_results = {}
        for lbl in CAT_COLORS:
            self._bucket_paths[lbl] = []
            self.lbl_cnt[lbl].setText("0")

        preset = self.combo_preset.currentData() or "balanced"
        self._slop_worker = SlopFilterWorker(
            paths         = self.image_paths,
            preset        = preset,
            use_face      = self.chk_face.isChecked(),
            use_body      = self.chk_body.isChecked(),
            use_hands     = self.chk_hands.isChecked(),
            use_aesthetic = self.chk_aesthetic.isChecked(),
        )
        self._slop_worker.progress.connect(self._on_phase1_progress)
        self._slop_worker.image_done.connect(self._on_image_classified)
        self._slop_worker.finished.connect(self._on_phase1_finished)
        self._slop_worker.error.connect(self._on_worker_error)

        self.proc_bar.setRange(0, len(self.image_paths))
        self.proc_bar.setValue(0)
        self.lbl_proc_title.setText(self.tr("qs.phase1.running", "⚡ Ejecutando Fase 1 — Slop Filter…"))
        self.content_stack.setCurrentIndex(1)

        self.btn_run1.setEnabled(False)
        self.btn_stop1.setVisible(True)
        self.btn_move_slop.setVisible(False)
        self.btn_to_phase2.setVisible(False)

        self._slop_worker.start()

    def _stop_phase1(self):
        if self._slop_worker:
            self._slop_worker.stop()
        self.btn_stop1.setVisible(False)
        self.btn_run1.setEnabled(True)

    def _on_phase1_progress(self, current: int, total: int, filename: str):
        self.proc_bar.setValue(current)
        self.lbl_proc_file.setText(filename)

    def _on_image_classified(self, path: str, label: str, scores: dict):
        self.phase1_results[path] = {"label": label, "scores": scores}
        self._bucket_paths[label].append(path)
        # Actualizar contador en vivo
        self.lbl_cnt[label].setText(str(len(self._bucket_paths[label])))

    def _on_phase1_finished(self, counts: dict):
        self.btn_stop1.setVisible(False)
        self.btn_run1.setEnabled(True)
        self.btn_move_slop.setVisible(counts.get("slop", 0) > 0)
        self.btn_to_phase2.setVisible(counts.get("keeper", 0) > 0)

        total = sum(counts.values())
        self.lbl_res1_summary.setText(
            self.tr("qs.phase1.summary",
                    "Total: {total}  ✅ {keeper}  ⚠️ {review}  🗑️ {slop}").format(
                total=total, **counts
            )
        )

        # Construir grids de las 3 columnas
        for lbl in CAT_COLORS:
            self._bucket_pages[lbl] = 0
            self._refresh_bucket_grid(lbl)

        self.content_stack.setCurrentIndex(2)
        self.lbl_status.setText(
            self.tr("qs.phase1.done",
                    "Fase 1 completada: {keeper} keepers, {review} a revisar, {slop} slop.").format(
                **counts
            )
        )

    # ── Grids de columnas Fase 1 ────────────────────────────────────── #

    def _refresh_bucket_grid(self, label: str):
        grid  = self._bucket_grids[label]
        paths = self._bucket_paths[label]
        page  = self._bucket_pages[label]

        # Limpiar grid
        while grid.count():
            item = grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        start = page * self._page_size
        end   = min(start + self._page_size, len(paths))
        cols  = 3

        for i, path in enumerate(paths[start:end]):
            row, col = divmod(i, cols)
            info     = self.phase1_results.get(path, {})
            card = ThumbnailCard(path, label, info.get("scores", {}))
            card.clicked.connect(self._on_thumbnail_click)
            grid.addWidget(card, row, col)

        # Paginación
        total_pages = max(1, -(-len(paths) // self._page_size))
        btn_prev, btn_next, lbl_pag = self._bucket_btns[label]
        lbl_pag.setText(f"{page + 1}/{total_pages}")
        btn_prev.setEnabled(page > 0)
        btn_next.setEnabled(page < total_pages - 1)

        # Header con count
        icons  = {"keeper": "✅", "review": "⚠️", "slop": "🗑️"}
        color  = CAT_COLORS[label]
        header = self._bucket_headers[label]
        header.setText(f"{icons[label]}  {label.upper()}  ({len(paths)})")

    def _bucket_prev(self, label: str):
        if self._bucket_pages[label] > 0:
            self._bucket_pages[label] -= 1
            self._refresh_bucket_grid(label)

    def _bucket_next(self, label: str):
        paths = self._bucket_paths[label]
        total = max(1, -(-len(paths) // self._page_size))
        if self._bucket_pages[label] < total - 1:
            self._bucket_pages[label] += 1
            self._refresh_bucket_grid(label)

    def _on_thumbnail_click(self, path: str):
        """Muestra diálogo con scores de la imagen clickeada."""
        info   = self.phase1_results.get(path, {})
        scores = info.get("scores", {})
        label  = info.get("label", "?")
        msg    = (
            f"<b>{Path(path).name}</b><br><br>"
            f"Categoría: <b>{label.upper()}</b><br><br>"
            f"Rostro:    {scores.get('face',      '—')}<br>"
            f"Cuerpo:    {scores.get('body',      '—')}<br>"
            f"Manos:     {scores.get('hands',     '—')}<br>"
            f"Estética:  {scores.get('aesthetic', '—')}<br>"
            f"<b>Combined: {scores.get('combined', '—')}</b>"
        )
        QMessageBox.information(self.view, self.tr("qs.scores_title", "Scores de imagen"), msg)

    # ── Acciones post-Fase1 ─────────────────────────────────────────── #

    def _move_slop(self):
        slop_paths = self._bucket_paths.get("slop", [])
        if not slop_paths:
            return
        slop_dir = Path(self.base_folder) / "slop"
        reply = QMessageBox.warning(
            self.view,
            self.tr("qs.move_slop.title", "Mover SLOP"),
            self.tr("qs.move_slop.msg",
                    "Se moverán {count} imágenes a:\n{path}\n\n"
                    "¿Continuar?").format(count=len(slop_paths), path=slop_dir),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        slop_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for path in slop_paths:
            try:
                dest = slop_dir / Path(path).name
                shutil.move(path, dest)
                moved += 1
            except Exception as e:
                log.warning(f"[QS] Error moviendo {path}: {e}")

        self.lbl_status.setText(
            self.tr("qs.move_slop.done",
                    "{moved} imágenes movidas a /slop/.").format(moved=moved)
        )
        self._bucket_paths["slop"] = []
        self._refresh_bucket_grid("slop")

        # Ofrecer borrar la carpeta /slop/ al finalizar
        ask_del = QMessageBox.question(
            self.view,
            self.tr("qs.delete_slop.title", "¿Borrar carpeta slop?"),
            self.tr("qs.delete_slop.msg",
                    "¿Deseas borrar permanentemente la carpeta /slop/ y su contenido?\n"
                    "Esta acción no se puede deshacer."),
            QMessageBox.Yes | QMessageBox.No,
        )
        if ask_del == QMessageBox.Yes:
            try:
                shutil.rmtree(str(slop_dir))
                self.lbl_status.setText(
                    self.tr("qs.delete_slop.done", "Carpeta /slop/ eliminada.")
                )
            except Exception as e:
                log.warning(f"[QS] Error borrando /slop/: {e}")

        self.btn_move_slop.setVisible(False)

    def _keepers_to_phase2(self):
        """Carga los keepers de Fase 1 en el contexto de Fase 2."""
        keeper_paths = self._bucket_paths.get("keeper", [])
        if not keeper_paths:
            return
        self.image_paths   = keeper_paths
        self.phase2_folder = self.base_folder
        self.lbl_phase2_folder.setText(
            self.tr("qs.phase2.from_phase1",
                    "{n} keepers de Fase 1").format(n=len(keeper_paths))
        )
        self.btn_run2.setEnabled(True)
        self.lbl_status.setText(
            self.tr("qs.keepers_ready",
                    "{n} keepers listos para Fase 2.").format(n=len(keeper_paths))
        )

    # ------------------------------------------------------------------ #
    # FASE 2 — QUALITY RANK
    # ------------------------------------------------------------------ #

    def _change_phase2_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self.view, self.tr("common.select_folder", "Seleccionar Carpeta"), self.last_dir
        )
        if not folder:
            return
        self.phase2_folder = folder
        paths = [
            str(p) for p in Path(folder).rglob("*")
            if p.suffix.lower() in EXTENSIONS
        ]
        self.image_paths = paths
        self.lbl_phase2_folder.setText(
            f"{Path(folder).name} ({len(paths)} imgs)"
        )
        self.btn_run2.setEnabled(bool(paths))

    def _run_phase2(self):
        if not self.image_paths:
            return
        from modules.quality_scorer.logic.workers import QualityRankWorker

        self.phase2_results = []
        profile = self.combo_profile.currentData() or DEFAULT_PROFILE

        self._rank_worker = QualityRankWorker(
            paths   = self.image_paths,
            profile = profile,
        )
        self._rank_worker.progress.connect(self._on_phase2_progress)
        self._rank_worker.finished.connect(self._on_phase2_finished)
        self._rank_worker.error.connect(self._on_worker_error)

        self.proc_bar.setRange(0, len(self.image_paths))
        self.proc_bar.setValue(0)
        self.lbl_proc_title.setText(self.tr("qs.phase2.running", "📊 Ejecutando Fase 2 — Quality Rank…"))
        self.content_stack.setCurrentIndex(1)

        self.btn_run2.setEnabled(False)
        self.btn_stop2.setVisible(True)

        self._rank_worker.start()

    def _stop_phase2(self):
        if self._rank_worker:
            self._rank_worker.stop()
        self.btn_stop2.setVisible(False)
        self.btn_run2.setEnabled(True)

    def _on_phase2_progress(self, current: int, total: int, filename: str):
        self.proc_bar.setValue(current)
        self.lbl_proc_file.setText(filename)

    def _on_phase2_finished(self, results: list):
        self.phase2_results = results
        self.btn_stop2.setVisible(False)
        self.btn_run2.setEnabled(True)

        total = len(results)
        avg   = sum(r.get("composite_score", 0) for r in results) / max(total, 1)
        self.lbl_res2_summary.setText(
            self.tr("qs.phase2.summary",
                    "📊 {total} imágenes rankeadas  |  Score promedio: {avg:.1f}/100").format(
                total=total, avg=avg
            )
        )

        self._phase2_page = 0
        self._refresh_phase2_grid()
        self.content_stack.setCurrentIndex(3)
        self.lbl_status.setText(
            self.tr("qs.phase2.done",
                    "Fase 2 completada. {total} imágenes rankeadas.").format(total=total)
        )

    def _refresh_phase2_grid(self):
        # Limpiar grid
        while self.grid2_layout.count():
            item = self.grid2_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        results     = self.phase2_results
        page        = self._phase2_page
        start       = page * self._page_size
        end         = min(start + self._page_size, len(results))
        cols        = 5
        total_pages = max(1, -(-len(results) // self._page_size))

        for i, res in enumerate(results[start:end]):
            row, col = divmod(i, cols)
            path     = res.get("path", "")
            score    = res.get("composite_score", 0)

            # Determinar color por score: verde > 75, amarillo > 50, rojo resto
            if score >= 75:
                color = "#00cc88"
            elif score >= 50:
                color = "#ffaa00"
            else:
                color = "#cc3333"

            card = QFrame()
            card.setFixedSize(128, 148)
            card.setStyleSheet(
                f"QFrame {{ border: 2px solid {color}; border-radius: 6px; "
                f"background: #0d0d0d; }}"
            )
            lay_c = QVBoxLayout(card)
            lay_c.setContentsMargins(3, 3, 3, 3)
            lay_c.setSpacing(2)

            img_lbl = QLabel()
            img_lbl.setFixedSize(118, 118)
            img_lbl.setAlignment(Qt.AlignCenter)
            img_lbl.setStyleSheet("border: none; background: transparent;")
            pix = QPixmap(path)
            if not pix.isNull():
                img_lbl.setPixmap(
                    pix.scaled(118, 118, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            lay_c.addWidget(img_lbl)

            sc_lbl = QLabel(f"{score}/100")
            sc_lbl.setAlignment(Qt.AlignCenter)
            sc_lbl.setStyleSheet(
                f"color: {color}; font-size: 10px; font-weight: bold; border: none;"
            )
            lay_c.addWidget(sc_lbl)

            self.grid2_layout.addWidget(card, row, col)

        self.lbl2_page.setText(f"{page + 1}/{total_pages}")
        self.btn2_prev.setEnabled(page > 0)
        self.btn2_next.setEnabled(page < total_pages - 1)

    def _phase2_prev(self):
        if self._phase2_page > 0:
            self._phase2_page -= 1
            self._refresh_phase2_grid()

    def _phase2_next(self):
        total = max(1, -(-len(self.phase2_results) // self._page_size))
        if self._phase2_page < total - 1:
            self._phase2_page += 1
            self._refresh_phase2_grid()

    # ------------------------------------------------------------------ #
    # ERROR HANDLER
    # ------------------------------------------------------------------ #

    def _on_worker_error(self, msg: str):
        log.error(f"[QualityScorer] Worker error: {msg}")
        QMessageBox.warning(
            self.view,
            self.tr("common.error", "Error"),
            self.tr("qs.msg.fail", "La operación falló: {error}").format(error=msg)
        )
        self.content_stack.setCurrentIndex(0)
        self.btn_run1.setEnabled(bool(self.image_paths))
        self.btn_run2.setEnabled(bool(self.image_paths))
        self.btn_stop1.setVisible(False)
        self.btn_stop2.setVisible(False)

    # ------------------------------------------------------------------ #
    # Interfaz BaseModule
    # ------------------------------------------------------------------ #

    def load_image_set(self, paths: list):
        if paths:
            folder = str(Path(paths[0]).parent)
            self.image_paths   = [str(p) for p in paths]
            self.base_folder   = folder
            self.phase2_folder = folder
            short = Path(folder).name
            if hasattr(self, 'lbl_loaded'):
                self.lbl_loaded.setText(f"📁 {short} ({len(paths)} imgs)")
            if hasattr(self, 'btn_run1'):
                self.btn_run1.setEnabled(True)
                self.btn_run2.setEnabled(True)

    def run_headless(self, params: dict, input_data) -> None:
        pass
