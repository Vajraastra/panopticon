"""
Vista principal del Dataset Tagger (Fase 4).

Configura el captioning de una carpeta (o set de imágenes) con un VLM local
compatible con la API de OpenAI y lanza el CaptionWorker. La salida SIEMPRE va
a carpeta(s) nueva(s) ANIDADA(s) dentro del origen (sidecars .txt estilo kohya).

Reglas obligatorias respetadas aquí:
- Acento SIEMPRE vía theme_manager (sin colores nuevos hardcodeados).
- Nada de red/IO en el hilo GUI: descubrimiento, test de conexión y captioning
  corren en QThread. El descubrimiento NUNCA se dispara en el arranque.
- QComboBox locale-safe: se elige por dato (UserRole / itemData), nunca por texto.
- Strings de worker pre-traducidos en la vista; el worker emite datos crudos.
"""
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QPlainTextEdit, QCheckBox, QSpinBox,
    QProgressBar, QFrame, QFileDialog, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPainter, QColor

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths

from ..logic.templates import available_models, CaptionTemplate, NSFW_SUFFIX
from ..logic.providers.openai_compat import OpenAICompatProvider
from ..logic.providers.wd_tagger import WDTaggerProvider
from ..logic.providers.discovery import discover_local
from ..logic.caption_worker import CaptionWorker
from ..logic import sidecar
from ..logic.ideogram import layout
from ..logic.wd_presets import available_taggers, TaggerPreset
from ..logic import wd_download
from .source_grid import SourceGrid

log = logging.getLogger(__name__)

# --- constantes locale-safe (nunca comparar combos por texto) ----------------
MODE_TAGS, MODE_NATURAL, MODE_BOTH, MODE_IDEOGRAM4 = 0, 1, 2, 3
# motor para datasets de TAGS: clasificador WD local vs VLM del endpoint
ENGINE_WD, ENGINE_VLM = 0, 1

# Carpeta de salida del captioner Ideogram 4: <set>_ideogram4_json
IG4_OUTPUT_TAG = "ideogram4"
# Prompts VLM por defecto del captioner Ideogram 4 (editables en la UI).
IG4_PROMPT_HLD = ("Describe this image in one or two sentences: the overall "
                  "scene, composition, and main colors.")
IG4_PROMPT_BG = ("Describe ONLY the background of this image in one sentence, "
                 "ignoring any subjects or text.")
IG4_PROMPT_OBJ = ("Describe only what is actually visible in this cropped "
                  "region for a dataset caption, in one concise sentence. The "
                  "subject may be a person, an object, or a material: describe "
                  "exactly what you see and do NOT invent a person, clothing, or "
                  "details that are not present. Name the main colors of the "
                  "visible subject and its salient parts.")
IG4_PROMPT_STYLE = ("Identify the visual style of this image for a dataset. "
                    "Answer in exactly this format, short phrases: "
                    "art_style: <phrase>; aesthetics: <phrase>; lighting: <phrase>.")

# Política ante captions existentes -> valor de sidecar.*
POLICIES = [
    ("tagger.policy.skip", "Skip existing", sidecar.SKIP),
    ("tagger.policy.overwrite", "Overwrite", sidecar.OVERWRITE),
    ("tagger.policy.append", "Append", sidecar.APPEND),
]


class DropFrame(QFrame):
    """QFrame con drag & drop para una carpeta o imágenes sueltas."""
    paths_dropped = Signal(list)  # rutas locales crudas (carpetas o imágenes)

    def __init__(self, accent, border, parent=None):
        super().__init__(parent)
        self._accent = accent
        self._border = border
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(120)
        self._idle()

    def _idle(self):
        self.setStyleSheet(
            f"QFrame {{ border: 2px dashed {self._border}; border-radius: 12px; }}"
        )

    def _hover(self):
        self.setStyleSheet(
            f"QFrame {{ border: 2px solid {self._accent}; border-radius: 12px; }}"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover()

    def dragLeaveEvent(self, event):
        self._idle()

    def dropEvent(self, event):
        self._idle()
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
        event.acceptProposedAction()


class DiscoveryWorker(QThread):
    """Escanea endpoints locales conocidos (timeout corto) fuera del hilo GUI."""
    result = Signal(list)  # lista de dicts {label, base_url, models, vision_models}

    def run(self):
        try:
            self.result.emit(discover_local())
        except Exception as e:  # noqa: BLE001 — superficie de error a la UI
            log.warning("Fallo en el descubrimiento local: %s", e)
            self.result.emit([])


class ConnectionWorker(QThread):
    """Prueba un endpoint y lista sus modelos sin bloquear la GUI."""
    result = Signal(bool, str, list)  # ok, mensaje, model_ids

    def __init__(self, base_url, api_key, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key

    def run(self):
        prov = OpenAICompatProvider(self.base_url, self.api_key or None, timeout=8)
        ok, msg = prov.test_connection()
        models = []
        if ok:
            try:
                models = prov.list_models()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
        self.result.emit(ok, msg, models)


class WDDownloadWorker(QThread):
    """Descarga el modelo WD (ONNX + CSV) sin bloquear la GUI (Regla #4)."""
    progress = Signal(str)        # nombre del archivo en curso
    done = Signal(bool, str)      # ok, mensaje

    def __init__(self, tagger_key, parent=None):
        super().__init__(parent)
        self.tagger_key = tagger_key

    def run(self):
        try:
            preset = TaggerPreset(self.tagger_key)
            wd_download.download(preset, progress_cb=self.progress.emit)
            self.done.emit(True, "")
        except Exception as e:  # noqa: BLE001 — superficie de error para la UI
            self.done.emit(False, str(e))


class ToggleSwitch(QCheckBox):
    """Interruptor deslizante on/off (autocontenido, sin imágenes).

    Pinta una pista redondeada + perilla; izquierda = off, derecha = on.
    El color de la pista activa usa el acento del tema (inyectado), el resto
    un gris neutro. Se usa para el tono de contenido SFW (off) / NSFW (on)."""

    def __init__(self, accent, off_color, parent=None):
        super().__init__(parent)
        self._on = QColor(accent)
        self._off = QColor(off_color)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(QSize(52, 26))

    def sizeHint(self):
        return QSize(52, 26)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        radius = r.height() / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(self._on if self.isChecked() else self._off)
        p.drawRoundedRect(r, radius, radius)
        d = r.height() - 6
        x = (r.right() - d - 2) if self.isChecked() else (r.left() + 3)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(int(x), r.top() + 3, d, d)
        p.end()


class DatasetTaggerView(QWidget):
    """Vista del Dataset Tagger: configuración + cola + ejecución."""

    def __init__(self, context):
        super().__init__()
        self.context = context

        # estado del origen
        self._source_folder = None   # str | None
        self._images = None          # list[str] | None (modo imágenes sueltas)

        # workers / ventanas (referencias para evitar GC)
        self._discovery = None
        self._conn = None
        self._caption = None
        self._ig4 = None           # worker del captioner Ideogram 4
        self._autobatch = None     # worker de pre-detección de cajas por lote
        self._bbox_editor = None   # ventana del editor de cajas (modal)
        self._review = None
        self._wd_dl = None         # worker de descarga del modelo WD
        self._last_output = None   # (carpeta, as_tag) del último dataset generado

        # taggers WD disponibles (key, label)
        self._taggers = available_taggers()

        # modelos VLM disponibles en el endpoint actual
        self._endpoint_models = []

        # plantillas (key, label) agrupadas por formato del preset
        self._tags_models = []
        self._natural_models = []
        for key, label in available_models():
            if CaptionTemplate(key).format == "natural":
                self._natural_models.append((key, label))
            else:
                self._tags_models.append((key, label))

        content = self._create_content()
        sidebar = self._create_sidebar()
        bottom = self._create_bottom()

        self.layout_manager = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            bottom_widget=bottom,
            theme_manager=self.context.get('theme_manager') if self.context else None,
            event_bus=self.context.get('event_bus') if self.context else None,
            sidebar_width=380,   # campos largos (motor/thresholds) sin scroll horizontal
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.layout_manager)

        self._on_mode_changed()       # estado inicial coherente
        self._refresh_prompts()
        self._update_run_enabled()

    # -- helpers de contexto --------------------------------------------------
    def _tr(self, key, default=None):
        lm = self.context.get('locale_manager') if self.context else None
        if lm:
            return lm.tr(key, default)
        return default if default is not None else key

    def _accent(self):
        tm = self.context.get('theme_manager') if self.context else None
        return tm.get_color('accent_main') if tm else "#bd93f9"

    def _color(self, key, fallback):
        tm = self.context.get('theme_manager') if self.context else None
        return tm.get_color(key) if tm else fallback

    def _section(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {self._accent()}; font-weight: bold; margin-top: 8px;")
        return lbl

    # -- construcción de la UI ------------------------------------------------
    def _create_content(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)

        title = QLabel(self._tr("tagger.title", "Dataset Tagger"))
        title.setStyleSheet(f"color: {self._accent()}; font-size: 22px; font-weight: bold;")
        subtitle = QLabel(self._tr(
            "tagger.subtitle",
            "Caption image folders with a vision LLM (tags + natural language)."))
        subtitle.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(subtitle)

        # zona de drop
        self.drop = DropFrame(self._accent(), self._color('border', "#444"))
        drop_inner = QVBoxLayout(self.drop)
        drop_lbl = QLabel(self._tr(
            "tagger.drop", "Drag a folder or images here, or use Browse."))
        drop_lbl.setAlignment(Qt.AlignCenter)
        drop_inner.addWidget(drop_lbl)
        self.drop.paths_dropped.connect(self._on_paths_dropped)
        lay.addWidget(self.drop)

        # fila origen + browse
        row = QHBoxLayout()
        self.lbl_source = QLabel(self._tr("tagger.no_source", "No source selected"))
        self.lbl_source.setWordWrap(True)
        btn_browse = QPushButton(self._tr("tagger.browse", "Browse folder…"))
        btn_browse.clicked.connect(self._browse_folder)
        row.addWidget(self.lbl_source, 1)
        row.addWidget(btn_browse, 0)
        lay.addLayout(row)

        # grid de validación visual del set (miniaturas + marca de .txt previo +
        # subcarpetas navegables). La revisión/edición rica sigue en review_view.
        self.grid = SourceGrid(
            self._tr, self._accent(), self._color('accent_success', "#00ff66"))
        self.grid.folder_activated.connect(self._set_source_folder)
        self.grid.caption_requested.connect(self._on_grid_image_activated)
        lay.addWidget(self.grid, 1)

        # progreso + estado
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        return w

    def _create_sidebar(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        # --- formato / modo dual ---
        lay.addWidget(self._section(self._tr("tagger.sec.format", "Caption format")))
        self.combo_mode = QComboBox()
        self.combo_mode.addItem(self._tr("tagger.mode.tags", "Tags (booru)"), MODE_TAGS)
        self.combo_mode.addItem(self._tr("tagger.mode.natural", "Natural language"), MODE_NATURAL)
        self.combo_mode.addItem(self._tr("tagger.mode.both", "Both (two datasets)"), MODE_BOTH)
        self.combo_mode.addItem(
            self._tr("tagger.mode.ideogram4", "Ideogram v4 (structured JSON)"), MODE_IDEOGRAM4)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        lay.addWidget(self.combo_mode)

        # selector de plantilla de TAGS
        self.lbl_model_tags = QLabel(self._tr("tagger.model.tags", "Tags model"))
        self.combo_model_tags = QComboBox()
        for key, label in self._tags_models:
            self.combo_model_tags.addItem(label, key)
        self.combo_model_tags.currentIndexChanged.connect(self._refresh_prompts)
        lay.addWidget(self.lbl_model_tags)
        lay.addWidget(self.combo_model_tags)

        # --- motor de TAGS: WD tagger local (booru real) vs VLM del endpoint ---
        self.lbl_tags_engine = QLabel(self._tr("tagger.engine", "Tags engine"))
        self.combo_tags_engine = QComboBox()
        self.combo_tags_engine.addItem(
            self._tr("tagger.engine.wd", "WD tagger (local · real booru tags)"), ENGINE_WD)
        self.combo_tags_engine.addItem(
            self._tr("tagger.engine.vlm", "Vision LLM (endpoint)"), ENGINE_VLM)
        self.combo_tags_engine.currentIndexChanged.connect(self._on_tags_engine_changed)
        lay.addWidget(self.lbl_tags_engine)
        lay.addWidget(self.combo_tags_engine)

        # bloque WD (selector de tagger + thresholds + descarga); visible si WD
        self.frame_wd = QFrame()
        wd = QVBoxLayout(self.frame_wd)
        wd.setContentsMargins(0, 0, 0, 0)
        wd.setSpacing(4)
        self.combo_wd_tagger = QComboBox()
        self.combo_wd_tagger.blockSignals(True)
        for key, label in self._taggers:
            self.combo_wd_tagger.addItem(label, key)
        self.combo_wd_tagger.blockSignals(False)
        wd.addWidget(self.combo_wd_tagger)
        # Los umbrales de confianza se fijan desde el preset (calibrados para
        # priorizar al personaje); no se exponen para no romper el resultado.
        # descarga del modelo + estado
        self.btn_wd_download = QPushButton(self._tr("tagger.wd.download", "Download model"))
        self.btn_wd_download.clicked.connect(self._download_wd)
        wd.addWidget(self.btn_wd_download)
        self.lbl_wd_status = QLabel("")
        self.lbl_wd_status.setWordWrap(True)
        self.lbl_wd_status.setStyleSheet("font-size: 11px;")
        wd.addWidget(self.lbl_wd_status)
        lay.addWidget(self.frame_wd)
        self.combo_wd_tagger.currentIndexChanged.connect(self._on_wd_tagger_changed)
        self._refresh_wd_status()

        # selector de plantilla NATURAL
        self.lbl_model_nat = QLabel(self._tr("tagger.model.natural", "Natural model"))
        self.combo_model_nat = QComboBox()
        for key, label in self._natural_models:
            self.combo_model_nat.addItem(label, key)
        self.combo_model_nat.currentIndexChanged.connect(self._refresh_prompts)
        lay.addWidget(self.lbl_model_nat)
        lay.addWidget(self.combo_model_nat)

        # --- provider / endpoint ---
        lay.addWidget(self._section(self._tr("tagger.sec.provider", "Provider (local OpenAI-compatible)")))
        self.edit_endpoint = QLineEdit("http://localhost:1234/v1")
        self.edit_endpoint.setPlaceholderText("http://localhost:1234/v1")
        lay.addWidget(self.edit_endpoint)
        self.edit_apikey = QLineEdit()
        self.edit_apikey.setPlaceholderText(self._tr("tagger.apikey", "API key (optional)"))
        self.edit_apikey.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.edit_apikey)

        btn_row = QHBoxLayout()
        self.btn_discover = QPushButton(self._tr("tagger.discover", "Discover"))
        self.btn_discover.clicked.connect(self._discover)
        self.btn_test = QPushButton(self._tr("tagger.test", "Test / list"))
        self.btn_test.clicked.connect(self._test_connection)
        btn_row.addWidget(self.btn_discover)
        btn_row.addWidget(self.btn_test)
        lay.addLayout(btn_row)

        self.lbl_endpoint_model = QLabel(self._tr("tagger.endpoint_model", "Endpoint model (vision)"))
        self.combo_endpoint_model = QComboBox()
        self.combo_endpoint_model.currentIndexChanged.connect(self._on_endpoint_model_changed)
        lay.addWidget(self.lbl_endpoint_model)
        lay.addWidget(self.combo_endpoint_model)
        self.lbl_vision_warn = QLabel("")
        self.lbl_vision_warn.setWordWrap(True)
        self.lbl_vision_warn.setStyleSheet(
            f"color: {self._color('warning', '#ffb86c')}; font-size: 11px;")
        lay.addWidget(self.lbl_vision_warn)

        # --- trigger / prefix / suffix ---
        self._sec_tokens = self._section(self._tr("tagger.sec.tokens", "Trigger & affixes"))
        lay.addWidget(self._sec_tokens)
        self.edit_trigger = QLineEdit()
        self.edit_trigger.setPlaceholderText(self._tr("tagger.trigger", "Trigger word (optional)"))
        self.edit_prefix = QLineEdit()
        self.edit_prefix.setPlaceholderText(self._tr("tagger.prefix", "Prefix (optional)"))
        self.edit_suffix = QLineEdit()
        self.edit_suffix.setPlaceholderText(self._tr("tagger.suffix", "Suffix (optional)"))
        for e in (self.edit_trigger, self.edit_prefix, self.edit_suffix):
            lay.addWidget(e)

        # --- prompts custom (uno por formato activo) ---
        lay.addWidget(self._section(self._tr("tagger.sec.prompt", "VLM meta-prompt")))
        # tono de contenido: switch SFW (izq) / NSFW (der). Sube el tono del
        # meta-prompt del VLM (el WD lo ignora; ya da NSFW de fábrica).
        lay.addWidget(QLabel(self._tr("tagger.content", "Content tone (VLM)")))
        content_row = QHBoxLayout()
        content_row.addWidget(QLabel(self._tr("tagger.content.sfw", "SFW")))
        self.switch_content = ToggleSwitch(
            self._accent(), self._color('border', "#555"))
        content_row.addWidget(self.switch_content)
        content_row.addWidget(QLabel(self._tr("tagger.content.nsfw", "NSFW")))
        content_row.addStretch()
        lay.addLayout(content_row)
        self.box_prompt_tags = self._prompt_box(
            self._tr("tagger.prompt.tags", "Tags prompt"), is_tags=True)
        self.box_prompt_nat = self._prompt_box(
            self._tr("tagger.prompt.natural", "Natural prompt"), is_tags=False)
        lay.addWidget(self.box_prompt_tags["frame"])
        lay.addWidget(self.box_prompt_nat["frame"])
        # WYSIWYG: al cambiar el switch, refleja el sufijo NSFW en los editores.
        self.switch_content.toggled.connect(self._sync_content_tone)

        # --- panel del modo Ideogram v4 (JSON estructurado) ---
        lay.addWidget(self._build_ig4_panel())

        # --- opciones de salida ---
        lay.addWidget(self._section(self._tr("tagger.sec.output", "Output")))
        self.combo_policy = QComboBox()
        for key, default, _val in POLICIES:
            self.combo_policy.addItem(self._tr(key, default))
        lay.addWidget(QLabel(self._tr("tagger.policy", "If a caption exists:")))
        lay.addWidget(self.combo_policy)
        self.chk_recursive = QCheckBox(self._tr("tagger.recursive", "Include subfolders"))
        self.chk_recursive.toggled.connect(self._on_recursive_toggled)
        lay.addWidget(self.chk_recursive)

        lay.addStretch()
        return w

    def _prompt_box(self, title, is_tags):
        """Editor de meta-prompt con botón 'reset' al default del preset."""
        frame = QFrame()
        v = QVBoxLayout(frame)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel(title))
        btn_reset = QPushButton(self._tr("tagger.reset", "Reset"))
        head.addStretch()
        head.addWidget(btn_reset)
        v.addLayout(head)
        editor = QPlainTextEdit()
        editor.setMaximumHeight(90)
        v.addWidget(editor)
        box = {"frame": frame, "editor": editor, "is_tags": is_tags}
        btn_reset.clicked.connect(lambda: self._reset_prompt(box))
        return box

    def _build_ig4_panel(self):
        """Panel del captioner Ideogram 4: estilo del set, padding, prompts VLM.

        El estilo (art_style/aesthetics/lighting) es compartido por el set y se
        usa como DEFAULT al crear el .pano.json de una imagen nueva; cada imagen
        puede ajustarlo en el editor de cajas. Las cajas se dibujan por imagen
        (doble clic en el grid).
        """
        self.frame_ig4 = QFrame()
        v = QVBoxLayout(self.frame_ig4)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(self._section(self._tr("ig4.sec.style", "Ideogram v4 — set style")))

        # Tipo de dataset. ESTILO (off): un único estilo global para todo el set
        # (coherencia del LoRA), escrito a mano o inferido por el VLM de una imagen.
        # PERSONAJE (on): cada imagen tiene su propio estilo, que el VLM infiere
        # por imagen. Este switch gobierna qué controles de estilo se muestran.
        # Switch deslizante (mismo patrón que el tono SFW/NSFW) para que sea
        # obvio y visible: izquierda = Estilo, derecha = Personaje.
        type_tip = self._tr(
            "ig4.tip.is_character",
            "Style = one global art_style for the whole set (best for a consistent "
            "style LoRA). Character = the subject appears in many styles, so the VLM "
            "infers the style of each image individually.")
        v.addWidget(QLabel(self._tr("ig4.dataset_type", "Dataset type")))
        type_row = QHBoxLayout()
        self.lbl_type_style = QLabel(self._tr("ig4.type.style", "Style"))
        self.lbl_type_char = QLabel(self._tr("ig4.type.character", "Character"))
        self.ig4_is_character = ToggleSwitch(
            self._accent(), self._color('border', "#555"))
        self.ig4_is_character.toggled.connect(self._refresh_ig4_style_ui)
        for wgt in (self.lbl_type_style, self.ig4_is_character, self.lbl_type_char):
            wgt.setToolTip(type_tip)
            type_row.addWidget(wgt)
        type_row.addStretch()
        v.addLayout(type_row)

        # Grupo de estilo GLOBAL — visible solo en modo ESTILO. Se rellena a mano
        # o, si 'inferir' está activo, lo deduce el VLM (campos deshabilitados).
        self.ig4_style_group = QFrame()
        sg = QVBoxLayout(self.ig4_style_group)
        sg.setContentsMargins(0, 0, 0, 0)
        sg.setSpacing(4)
        self.ig4_artstyle = QLineEdit()
        self.ig4_artstyle.setPlaceholderText(
            self._tr("ig4.artstyle_ph", "art_style * — e.g. stylized 3D character render"))
        self.ig4_artstyle.setToolTip(self._tr(
            "ig4.tip.set_artstyle",
            "REQUIRED in Style mode. The art style shared by the whole set. Write it "
            "once; it is inherited by every image. Keep it identical so the LoRA learns "
            "a single consistent style. (Leave to the VLM with 'Infer global style'.)"))
        self.ig4_aesthetics = QLineEdit()
        self.ig4_aesthetics.setPlaceholderText(self._tr("ig4.aesthetics_ph", "aesthetics (optional)"))
        self.ig4_aesthetics.setToolTip(self._tr(
            "ig4.tip.set_aesthetics", "Optional. Shared mood/aesthetics (e.g. 'clean studio, soft palette')."))
        self.ig4_lighting = QLineEdit()
        self.ig4_lighting.setPlaceholderText(self._tr("ig4.lighting_ph", "lighting (optional)"))
        self.ig4_lighting.setToolTip(self._tr(
            "ig4.tip.set_lighting", "Optional. Shared lighting (e.g. 'soft three-point key light')."))
        for e in (self.ig4_artstyle, self.ig4_aesthetics, self.ig4_lighting):
            sg.addWidget(e)

        # Inferir el estilo GLOBAL con el VLM (modo estilo): se deduce de UNA imagen
        # y se reusa idéntico en todas. Alternativa a escribir art_style a mano.
        self.ig4_infer_style = QCheckBox(self._tr(
            "ig4.infer_style", "Infer the global style with the VLM (from one image, for all)"))
        self.ig4_infer_style.setToolTip(self._tr(
            "ig4.tip.infer_style",
            "Style mode only. OFF: you type art_style by hand. ON: the VLM reads one "
            "image, derives art_style/aesthetics/lighting and reuses the SAME style for "
            "the whole set — still consistent, just automatic."))
        self.ig4_infer_style.toggled.connect(self._refresh_ig4_style_ui)
        sg.addWidget(self.ig4_infer_style)
        v.addWidget(self.ig4_style_group)

        pad_row = QHBoxLayout()
        pad_lbl = QLabel(self._tr("ig4.padding", "Crop padding %"))
        pad_row.addWidget(pad_lbl)
        self.ig4_padding = QSpinBox()
        self.ig4_padding.setRange(0, 50)
        self.ig4_padding.setValue(8)
        pad_tip = self._tr(
            "ig4.tip.padding",
            "Extra margin added around each object box before it is cropped and sent "
            "to the VLM. More padding = more surrounding context, but less isolation.")
        pad_lbl.setToolTip(pad_tip)
        self.ig4_padding.setToolTip(pad_tip)
        pad_row.addWidget(self.ig4_padding)
        pad_row.addStretch()
        v.addLayout(pad_row)

        # Prompts VLM (high_level / background / object / style)
        self.ig4_prompt_hld = self._ig4_prompt_editor(
            self._tr("ig4.prompt.hld", "High-level prompt"), IG4_PROMPT_HLD,
            self._tr("ig4.tip.prompt_hld",
                     "Instruction sent to the VLM to describe the whole image "
                     "(high_level_description). Edit only if you know what you want."))
        self.ig4_prompt_bg = self._ig4_prompt_editor(
            self._tr("ig4.prompt.bg", "Background prompt"), IG4_PROMPT_BG,
            self._tr("ig4.tip.prompt_bg",
                     "Instruction sent to the VLM to describe only the background."))
        self.ig4_prompt_obj = self._ig4_prompt_editor(
            self._tr("ig4.prompt.obj", "Object prompt"), IG4_PROMPT_OBJ,
            self._tr("ig4.tip.prompt_obj",
                     "Instruction sent to the VLM for each object crop "
                     "(its 'desc'). Applied per object box."))
        self.ig4_prompt_style = self._ig4_prompt_editor(
            self._tr("ig4.prompt.style", "Style prompt (VLM inference)"), IG4_PROMPT_STYLE,
            self._tr("ig4.tip.prompt_style",
                     "Only used when 'Infer set style' is on. Asks the VLM for "
                     "art_style/aesthetics/lighting; the reply is parsed into those fields."))
        for ed in (self.ig4_prompt_hld, self.ig4_prompt_bg, self.ig4_prompt_obj,
                   self.ig4_prompt_style):
            v.addWidget(ed["frame"])

        self.ig4_prompt_style["frame"].setVisible(False)

        self.ig4_recapture = QCheckBox(
            self._tr("ig4.recapture", "Re-capture (overwrite existing fields)"))
        self.ig4_recapture.setToolTip(self._tr(
            "ig4.tip.recapture",
            "OFF (resume): only empty fields are captured, so re-running is cheap and "
            "won't re-spend the VLM. ON: re-asks the VLM and overwrites everything "
            "already captured."))
        v.addWidget(self.ig4_recapture)

        # Pre-detección por lote: corre YOLO sobre todo el set y deja un borrador
        # .pano.json por imagen (sin tocar las que ya tienen uno). El usuario luego
        # repasa imagen por imagen ajustando/añadiendo/borrando.
        self.ig4_batch_detect = QPushButton(
            self._tr("ig4.batch_detect", "Pre-detect boxes (whole set)"))
        self.ig4_batch_detect.setToolTip(self._tr(
            "ig4.tip.batch_detect",
            "Run YOLO over every image in the set and save a draft .pano.json with "
            "detected characters/animals/objects. Images that already have boxes are "
            "left untouched. Then double-click each image to review and adjust."))
        self.ig4_batch_detect.clicked.connect(self._run_ig4_autobatch)
        v.addWidget(self.ig4_batch_detect)

        hint = QLabel(self._tr(
            "ig4.hint",
            "Steps: 1) fill art_style above  2) double-click an image to draw boxes  "
            "3) press Start to capture and export."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        v.addWidget(hint)
        return self.frame_ig4

    def _ig4_prompt_editor(self, title, default_text, tooltip=None):
        frame = QFrame()
        vv = QVBoxLayout(frame)
        vv.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        vv.addWidget(lbl)
        editor = QPlainTextEdit(default_text)
        editor.setMaximumHeight(64)
        vv.addWidget(editor)
        if tooltip:
            lbl.setToolTip(tooltip)
            editor.setToolTip(tooltip)
        return {"frame": frame, "editor": editor}

    # -- estilo Ideogram v4: tipo de dataset (estilo vs personaje) -----------
    def _ig4_is_character(self):
        return self.ig4_is_character.isChecked()

    def _ig4_infer_active(self):
        """¿El VLM va a inferir el estilo? Personaje (siempre) o estilo+inferir."""
        if self._ig4_is_character():
            return True
        return self.ig4_infer_style.isChecked()

    def _refresh_ig4_style_ui(self):
        """Muestra/oculta los controles de estilo según el tipo de dataset.

        Personaje: sin estilo global (el VLM infiere por imagen). Estilo: grupo
        de estilo global visible; manual, o inferido por el VLM de una imagen
        (en cuyo caso los campos quedan deshabilitados, los rellena el VLM)."""
        is_char = self._ig4_is_character()
        # el estilo global solo aplica al dataset de estilo
        self.ig4_style_group.setVisible(not is_char)
        # en modo estilo+inferir, el VLM rellena los campos: deshabilitados
        infer_global = (not is_char) and self.ig4_infer_style.isChecked()
        for e in (self.ig4_artstyle, self.ig4_aesthetics, self.ig4_lighting):
            e.setEnabled(not infer_global)
        # el style-prompt solo se usa cuando el VLM infiere (personaje o estilo+inferir)
        self.ig4_prompt_style["frame"].setVisible(self._ig4_infer_active())

    def _create_bottom(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.btn_review = QPushButton(self._tr("tagger.review", "Review / edit captions"))
        self.btn_review.clicked.connect(self._open_review)
        self.btn_run = QPushButton(self._tr("tagger.run", "Start captioning"))
        self.btn_run.clicked.connect(self._run)
        self.btn_cancel = QPushButton(self._tr("tagger.cancel", "Cancel"))
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        lay.addWidget(self.btn_review)
        lay.addStretch()
        lay.addWidget(self.btn_cancel)
        lay.addWidget(self.btn_run)
        return w

    # -- lógica de modo / plantillas -----------------------------------------
    def _mode(self):
        return self.combo_mode.currentData()

    def _active_template_keys(self):
        """Plantillas a generar según el modo (cada una = un dataset/carpeta)."""
        mode = self._mode()
        keys = []
        if mode in (MODE_TAGS, MODE_BOTH) and self.combo_model_tags.count():
            keys.append(self.combo_model_tags.currentData())
        if mode in (MODE_NATURAL, MODE_BOTH) and self.combo_model_nat.count():
            keys.append(self.combo_model_nat.currentData())
        return [k for k in keys if k]

    def _on_mode_changed(self):
        mode = self._mode()
        is_ig4 = mode == MODE_IDEOGRAM4
        show_tags = mode in (MODE_TAGS, MODE_BOTH)
        show_nat = mode in (MODE_NATURAL, MODE_BOTH)
        self.lbl_model_tags.setVisible(show_tags)
        self.combo_model_tags.setVisible(show_tags)
        self.lbl_tags_engine.setVisible(show_tags)
        self.combo_tags_engine.setVisible(show_tags)
        self.lbl_model_nat.setVisible(show_nat)
        self.combo_model_nat.setVisible(show_nat)
        self.box_prompt_nat["frame"].setVisible(show_nat)
        # Trigger/affixes solo aplican a tags/natural; el panel ig4 sustituye los
        # controles propios del flujo de captioning de texto plano.
        self._sec_tokens.setVisible(not is_ig4)
        for e in (self.edit_trigger, self.edit_prefix, self.edit_suffix):
            e.setVisible(not is_ig4)
        self.frame_ig4.setVisible(is_ig4)
        self._refresh_ig4_style_ui()
        self._refresh_engine_ui()
        self._refresh_prompts()
        # el grid marca .pano.json (ig4) o .txt (resto); re-dibujar al cambiar de modo
        self._sync_grid_ig4()
        self._reload_grid()
        self._update_run_enabled()

    # -- motor de tags (WD vs VLM) -------------------------------------------
    def _tags_engine(self):
        return self.combo_tags_engine.currentData()

    def _uses_wd(self):
        """¿La corrida usa el WD tagger para el dataset de tags?"""
        return (self._mode() in (MODE_TAGS, MODE_BOTH)
                and self._tags_engine() == ENGINE_WD)

    def _needs_vlm(self):
        """¿La corrida necesita el endpoint VLM (natural o tags-via-VLM)?"""
        if self._mode() in (MODE_NATURAL, MODE_BOTH):
            return True
        return self._tags_engine() == ENGINE_VLM

    def _model_name_for(self, key):
        """Nombre de modelo para nombrar la salida y para el provider, según el
        motor de cada plantilla (tagger WD para tags-WD; VLM del endpoint si no)."""
        if CaptionTemplate(key).format != "natural" and self._tags_engine() == ENGINE_WD:
            return self.combo_wd_tagger.currentData()
        return self.combo_endpoint_model.currentData()

    def _refresh_engine_ui(self):
        """Visibilidad WD vs meta-prompt de tags según modo + motor."""
        show_tags = self._mode() in (MODE_TAGS, MODE_BOTH)
        wd = show_tags and self._tags_engine() == ENGINE_WD
        self.frame_wd.setVisible(wd)
        # el meta-prompt de tags solo aplica al VLM (el clasificador no lo usa)
        self.box_prompt_tags["frame"].setVisible(
            show_tags and self._tags_engine() == ENGINE_VLM)
        if wd:
            self._refresh_wd_status()

    def _on_tags_engine_changed(self):
        self._refresh_engine_ui()
        self._update_run_enabled()

    def _on_wd_tagger_changed(self):
        if not self.combo_wd_tagger.currentData():
            return
        self._refresh_wd_status()
        self._update_run_enabled()

    def _refresh_wd_status(self):
        key = self.combo_wd_tagger.currentData()
        if not key:
            return
        preset = TaggerPreset(key)
        if wd_download.is_downloaded(preset):
            self.lbl_wd_status.setText(self._tr("tagger.wd.ready", "✓ Model ready (offline)"))
            self.lbl_wd_status.setStyleSheet(
                f"color: {self._color('accent_success', '#00ff66')}; font-size: 11px;")
            self.btn_wd_download.setEnabled(False)
        elif self._wd_dl is not None:
            self.btn_wd_download.setEnabled(False)
        else:
            self.lbl_wd_status.setText(self._tr(
                "tagger.wd.missing", "Model not downloaded (~1.2 GB)."))
            self.lbl_wd_status.setStyleSheet(
                f"color: {self._color('warning', '#ffb86c')}; font-size: 11px;")
            self.btn_wd_download.setEnabled(True)

    def _download_wd(self):
        key = self.combo_wd_tagger.currentData()
        if not key or self._wd_dl is not None:
            return
        self.btn_wd_download.setEnabled(False)
        self.lbl_wd_status.setText(self._tr("tagger.wd.downloading", "Downloading…"))
        self.lbl_wd_status.setStyleSheet("font-size: 11px;")
        self._wd_dl = WDDownloadWorker(key)
        self._wd_dl.progress.connect(
            lambda f: self.lbl_wd_status.setText(
                self._tr("tagger.wd.downloading_file", "Downloading {0}…").format(f)))
        self._wd_dl.done.connect(self._on_wd_downloaded)
        self._wd_dl.start()

    def _on_wd_downloaded(self, ok, msg):
        self._wd_dl = None
        if ok:
            self._refresh_wd_status()
        else:
            self.lbl_wd_status.setText(self._tr(
                "tagger.wd.dl_error", "Download failed: {0}").format(msg))
            self.lbl_wd_status.setStyleSheet(
                f"color: {self._color('error', '#ff5555')}; font-size: 11px;")
            self.btn_wd_download.setEnabled(True)
        self._update_run_enabled()

    def _reset_prompt(self, box):
        combo = self.combo_model_tags if box["is_tags"] else self.combo_model_nat
        key = combo.currentData()
        if key:
            box["editor"].setPlainText(CaptionTemplate(key).meta_prompt())
            self._sync_content_tone()

    def _refresh_prompts(self):
        """Autocompleta cada editor con el meta-prompt default si está vacío."""
        for box, combo in ((self.box_prompt_tags, self.combo_model_tags),
                           (self.box_prompt_nat, self.combo_model_nat)):
            if not box["frame"].isVisible():
                continue
            key = combo.currentData()
            if key and not box["editor"].toPlainText().strip():
                box["editor"].setPlainText(CaptionTemplate(key).meta_prompt())
        self._sync_content_tone()

    def _sync_content_tone(self):
        """Refleja en vivo el modificador NSFW en los editores de prompt visibles.

        Lo que se ve en el editor es exactamente lo que se manda al VLM: al
        encender el switch se concatena `NSFW_SUFFIX`, al apagarlo se quita.
        El WD tagger ignora el prompt (ya da NSFW de fábrica). Idempotente.
        """
        nsfw = self.switch_content.isChecked()
        for box in (self.box_prompt_tags, self.box_prompt_nat):
            if not box["frame"].isVisible():
                continue
            editor = box["editor"]
            text = editor.toPlainText()
            has = NSFW_SUFFIX in text
            if nsfw and not has and text.strip():
                editor.setPlainText(f"{text.rstrip()} {NSFW_SUFFIX}")
            elif not nsfw and has:
                editor.setPlainText(text.replace(NSFW_SUFFIX, "").rstrip())

    # -- origen (drop / browse) ----------------------------------------------
    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, self._tr("tagger.choose_folder", "Choose image folder"))
        if folder:
            self._set_source_folder(folder)

    def _on_paths_dropped(self, paths):
        dirs = [p for p in paths if Path(p).is_dir()]
        if dirs:
            self._set_source_folder(dirs[0])
            return
        imgs = [p for p in paths if Path(p).suffix.lower() in sidecar.IMAGE_EXTS]
        if imgs:
            self._set_source_images(imgs)

    def _set_source_folder(self, folder):
        self._source_folder = folder
        self._images = None
        self.lbl_source.setText(self._tr("tagger.source_folder", "Folder: {0} ({1} images)")
                                .format(folder, self._source_count()))
        self._sync_grid_ig4()
        self.grid.show_folder(folder)
        self._update_run_enabled()

    def _set_source_images(self, imgs):
        self._images = list(imgs)
        # carpeta de origen para nombrar la salida = carpeta del primer archivo
        self._source_folder = str(Path(imgs[0]).parent)
        self.lbl_source.setText(self._tr("tagger.source_images", "{0} images selected")
                                .format(len(imgs)))
        self._sync_grid_ig4()
        self.grid.show_images(imgs)
        self._update_run_enabled()

    def _sync_grid_ig4(self):
        """Indica al grid si debe marcar las imágenes ya preprocesadas (con
        .pano.json). Solo en modo Ideogram v4; en los demás el grid marca el
        .txt previo como siempre."""
        is_ig4 = self._mode() == MODE_IDEOGRAM4
        out_dir = None
        if is_ig4 and self._source_folder:
            out_dir = Path(sidecar.output_dir(self._source_folder, IG4_OUTPUT_TAG, "json"))
        self.grid.set_ig4_output_dir(out_dir)

    def _reload_grid(self):
        """Re-dibuja el grid del set actual (p. ej. al cambiar de modo, para
        que aparezcan/desaparezcan las marcas de preproceso)."""
        if self._images is not None:
            self.grid.show_images(self._images)
        elif self._source_folder:
            self.grid.show_folder(self._source_folder)

    def _source_count(self):
        """Conteo de imágenes a procesar (respeta el alcance recursivo)."""
        if not self._source_folder:
            return 0
        try:
            return len(sidecar.list_images(self._source_folder, self.chk_recursive.isChecked()))
        except Exception:  # noqa: BLE001
            return 0

    def _on_recursive_toggled(self):
        """Refresca el conteo de la etiqueta de origen al cambiar el alcance."""
        if self._source_folder and self._images is None:
            self.lbl_source.setText(self._tr("tagger.source_folder", "Folder: {0} ({1} images)")
                                    .format(self._source_folder, self._source_count()))

    # -- descubrimiento / conexión -------------------------------------------
    def _discover(self):
        self.btn_discover.setEnabled(False)
        self.status.setText(self._tr("tagger.discovering", "Scanning local endpoints…"))
        self._discovery = DiscoveryWorker()
        self._discovery.result.connect(self._on_discovered)
        self._discovery.start()

    def _on_discovered(self, found):
        self.btn_discover.setEnabled(True)
        if not found:
            self.status.setText(self._tr("tagger.none_found", "No local endpoints found."))
            return
        # prioriza un endpoint con al menos un modelo de visión
        best = next((f for f in found if f["vision_models"]), found[0])
        self.edit_endpoint.setText(best["base_url"])
        self._set_endpoint_models(best["models"], prefer_vision=True)
        self.status.setText(self._tr("tagger.found", "Found {0} at {1}")
                            .format(best["label"], best["base_url"]))

    def _test_connection(self):
        self.btn_test.setEnabled(False)
        self.status.setText(self._tr("tagger.testing", "Testing connection…"))
        self._conn = ConnectionWorker(self.edit_endpoint.text().strip(),
                                      self.edit_apikey.text().strip())
        self._conn.result.connect(self._on_tested)
        self._conn.start()

    def _on_tested(self, ok, msg, models):
        self.btn_test.setEnabled(True)
        self.status.setText(msg)
        if ok:
            self._set_endpoint_models(models, prefer_vision=True)

    def _set_endpoint_models(self, models, prefer_vision=False):
        self._endpoint_models = list(models)
        self.combo_endpoint_model.blockSignals(True)
        self.combo_endpoint_model.clear()
        for m in models:
            self.combo_endpoint_model.addItem(m, m)
        self.combo_endpoint_model.blockSignals(False)
        if prefer_vision:
            for i, m in enumerate(models):
                from ..logic.providers.base_provider import looks_like_vision
                if looks_like_vision(m):
                    self.combo_endpoint_model.setCurrentIndex(i)
                    break
        self._on_endpoint_model_changed()

    def _on_endpoint_model_changed(self):
        model = self.combo_endpoint_model.currentData()
        from ..logic.providers.base_provider import looks_like_vision
        if model and not looks_like_vision(model):
            self.lbl_vision_warn.setText(self._tr(
                "tagger.not_vision",
                "⚠ This model may not support vision. Captioning could fail."))
        else:
            self.lbl_vision_warn.setText("")
        self._update_run_enabled()

    # -- ejecución ------------------------------------------------------------
    def _ig4_jobs(self):
        """Lista (image_path, pano_path) de las imágenes del set que ya tienen
        un .pano.json (cajas dibujadas) en la carpeta de salida ig4."""
        if not self._source_folder:
            return []
        out_dir = Path(sidecar.output_dir(self._source_folder, IG4_OUTPUT_TAG, "json"))
        if self._images is not None:
            imgs = [Path(p) for p in self._images]
        else:
            try:
                imgs = sidecar.list_images(self._source_folder, self.chk_recursive.isChecked())
            except OSError:
                return []
        jobs = []
        for img in imgs:
            pano = layout.pano_path(out_dir, img)
            if pano.exists():
                jobs.append((str(img), str(pano)))
        return jobs

    def _ig4_all_images(self):
        """Todas las imágenes del set (con o sin .pano.json). Para el pre-pase."""
        if self._images is not None:
            return [str(p) for p in self._images]
        if not self._source_folder:
            return []
        try:
            return [str(p) for p in
                    sidecar.list_images(self._source_folder, self.chk_recursive.isChecked())]
        except OSError:
            return []

    # -- pre-detección de cajas por lote (YOLO) ------------------------------
    def _run_ig4_autobatch(self):
        """Lanza la pre-detección YOLO sobre todo el set en un hilo aparte."""
        if self._autobatch is not None and self._autobatch.isRunning():
            return
        images = self._ig4_all_images()
        if not images:
            self.status.setText(self._tr("ig4.batch_no_images", "No images in the set."))
            return
        out_dir = Path(sidecar.output_dir(self._source_folder, IG4_OUTPUT_TAG, "json"))
        # Mismo criterio de herencia de estilo que el editor de cajas.
        if self._ig4_infer_active():
            style_defaults = {"art_style": "", "aesthetics": "", "lighting": ""}
        else:
            style_defaults = {
                "art_style": self.ig4_artstyle.text().strip(),
                "aesthetics": self.ig4_aesthetics.text().strip(),
                "lighting": self.ig4_lighting.text().strip(),
            }
        from ..logic.ideogram.autobox import AutoBoxBatchWorker
        self._autobatch = AutoBoxBatchWorker(images, out_dir, style_defaults)
        self._autobatch.progress.connect(self._on_autobatch_progress)
        self._autobatch.finished_ok.connect(self._on_autobatch_done)
        self._autobatch.failed.connect(self._on_autobatch_failed)
        self.ig4_batch_detect.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self._update_run_enabled()
        self._autobatch.start()

    def _on_autobatch_progress(self, done, total, name):
        self.status.setText(self._tr(
            "ig4.batch_progress", "Detecting boxes… {0}/{1}: {2}").format(done, total, name))

    def _on_autobatch_done(self, created, skipped):
        self._autobatch = None
        self.ig4_batch_detect.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.status.setText(self._tr(
            "ig4.batch_done", "Pre-detection done — {0} drafts created, {1} skipped.")
            .format(created, skipped))
        self.grid.refresh_preprocess()   # aparecen las insignias de borrador
        self._update_run_enabled()

    def _on_autobatch_failed(self, msg):
        self._autobatch = None
        self.ig4_batch_detect.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.status.setText(self._tr("ig4.batch_err", "Detection error: {0}").format(msg))
        self._update_run_enabled()

    def _ig4_missing_artstyle(self, jobs):
        """Cuenta cuántos .pano.json de `jobs` no tienen art_style (no exportables)."""
        from ..logic.ideogram.datamodel import WorkDoc
        missing = 0
        for _img, pano in jobs:
            try:
                doc = WorkDoc.load(pano)
            except (ValueError, OSError, KeyError):
                continue
            if not (doc.style.art_style or "").strip():
                missing += 1
        return missing

    def _update_run_enabled(self):
        busy = (self._caption is not None or self._ig4 is not None
                or self._autobatch is not None)
        if self._mode() == MODE_IDEOGRAM4:
            self.btn_run.setEnabled(bool(
                self._source_folder and not busy
                and self.combo_endpoint_model.currentData() and self._ig4_jobs()))
            # El pre-pase por lote solo necesita un set cargado (no usa el VLM).
            self.ig4_batch_detect.setEnabled(bool(self._source_folder and not busy))
            return
        if not (self._source_folder and self._active_template_keys() and not busy):
            self.btn_run.setEnabled(False)
            return
        # el endpoint VLM solo es requisito si la corrida lo usa
        if self._needs_vlm() and not self.combo_endpoint_model.currentData():
            self.btn_run.setEnabled(False)
            return
        # el modelo WD debe estar descargado si se usa
        if self._uses_wd():
            preset = TaggerPreset(self.combo_wd_tagger.currentData())
            if not wd_download.is_downloaded(preset):
                self.btn_run.setEnabled(False)
                return
        self.btn_run.setEnabled(True)

    def _run(self):
        if self._mode() == MODE_IDEOGRAM4:
            self._run_ideogram()
            return
        keys = self._active_template_keys()
        if not (self._source_folder and keys):
            return
        if self._needs_vlm() and not self.combo_endpoint_model.currentData():
            return
        model = self.combo_endpoint_model.currentData()  # None si la corrida es solo WD

        # política (locale-safe: por índice -> valor)
        policy = POLICIES[self.combo_policy.currentIndex()][2]

        # prompts custom por plantilla activa (pre-resueltos en el hilo GUI).
        # box_prompt_tags solo está visible cuando el motor de tags es el VLM.
        overrides = {}
        if self.box_prompt_tags["frame"].isVisible():
            k = self.combo_model_tags.currentData()
            if k:
                overrides[k] = self.box_prompt_tags["editor"].toPlainText()
        if self.box_prompt_nat["frame"].isVisible():
            k = self.combo_model_nat.currentData()
            if k:
                overrides[k] = self.box_prompt_nat["editor"].toPlainText()

        # provider VLM por defecto (natural y/o tags-via-VLM)
        provider = OpenAICompatProvider(
            self.edit_endpoint.text().strip(),
            self.edit_apikey.text().strip() or None,
            model=model, timeout=120)

        # provider + nombre de modelo por plantilla (motor por dataset)
        providers, model_names = {}, {}
        for key in keys:
            model_names[key] = self._model_name_for(key)
        if self._uses_wd():
            tkey = self.combo_model_tags.currentData()
            if tkey in keys:
                preset = TaggerPreset(self.combo_wd_tagger.currentData())
                mp, cp = wd_download.local_paths(preset)
                # umbrales del preset (calibrados para priorizar al personaje)
                providers[tkey] = WDTaggerProvider(
                    mp, cp,
                    general_threshold=preset.general_threshold,
                    character_threshold=preset.character_threshold)

        self._caption = CaptionWorker(
            provider=provider,
            model=model,
            source_folder=self._source_folder,
            template_keys=keys,
            policy=policy,
            trigger=self.edit_trigger.text().strip(),
            prefix=self.edit_prefix.text().strip(),
            suffix=self.edit_suffix.text().strip(),
            recursive=self.chk_recursive.isChecked(),
            images=self._images,
            prompt_overrides=overrides,
            providers=providers,
            model_names=model_names,
            nsfw=self.switch_content.isChecked(),
        )
        self._caption.progress.connect(self._on_progress)
        self._caption.error.connect(self._on_error)
        self._caption.finished_all.connect(self._on_finished)
        self._caption.start()

        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.status.setText(self._tr("tagger.starting", "Captioning…"))

    def _on_progress(self, done, total, fname):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.status.setText(self._tr("tagger.progress", "{0}/{1} — {2}")
                            .format(done, total, fname))

    def _on_error(self, path, msg):
        name = Path(path).name if path else "?"
        self.status.setText(self._tr("tagger.err", "Error on {0}: {1}").format(name, msg))

    def _on_finished(self, processed, skipped):
        self.btn_cancel.setEnabled(False)
        self._caption = None
        self.status.setText(self._tr(
            "tagger.done", "Done — {0} processed, {1} skipped.").format(processed, skipped))
        self._update_run_enabled()
        # recuerda la última carpeta generada para el botón de revisión y ofrece
        # abrirla (cross-platform, Regla de Oro #1)
        try:
            for key in self._active_template_keys():
                fmt = CaptionTemplate(key).format
                out = sidecar.output_dir(self._source_folder, self._model_name_for(key), fmt)
                if Path(out).exists():
                    self._last_output = (str(out), fmt == "tags")
                    CachePaths.open_folder(str(out))
                    break
        except Exception as e:  # noqa: BLE001
            log.debug("No se pudo abrir la carpeta de salida: %s", e)

    # -- ejecución del captioner Ideogram 4 ----------------------------------
    def _run_ideogram(self):
        jobs = self._ig4_jobs()
        model = self.combo_endpoint_model.currentData()
        if not (jobs and model):
            self.status.setText(self._tr(
                "ig4.no_jobs", "Draw boxes on at least one image first (double-click)."))
            return
        # Fail-safe: art_style es obligatorio para exportar. Avisa ANTES de gastar
        # el VLM si falta (en vez de fallar imagen por imagen), salvo que el VLM lo
        # vaya a inferir (modo personaje o estilo+inferir).
        if not self._ig4_infer_active():
            missing = self._ig4_missing_artstyle(jobs)
            if missing:
                from PySide6.QtWidgets import QMessageBox
                ret = QMessageBox.warning(
                    self, self._tr("ig4.artstyle_missing_title", "Missing art_style"),
                    self._tr("ig4.artstyle_missing_msg",
                             "{0} image(s) have no art_style and cannot be exported "
                             "(it is required). Fill art_style in the panel before "
                             "drawing, or in each box editor, or enable 'Infer set "
                             "style'. Continue and skip them?").format(missing),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ret != QMessageBox.Yes:
                    return
        from ..logic.templates import with_content_mode
        from ..logic.ideogram.ig4_worker import Ig4Worker
        provider = OpenAICompatProvider(
            self.edit_endpoint.text().strip(),
            self.edit_apikey.text().strip() or None, model=model, timeout=180)
        nsfw = self.switch_content.isChecked()
        self._ig4 = Ig4Worker(
            provider, model, jobs,
            prompt_hld=with_content_mode(self.ig4_prompt_hld["editor"].toPlainText().strip(), nsfw),
            prompt_background=self.ig4_prompt_bg["editor"].toPlainText().strip(),
            prompt_obj=with_content_mode(self.ig4_prompt_obj["editor"].toPlainText().strip(), nsfw),
            default_padding=self.ig4_padding.value() / 100.0,
            recapture=self.ig4_recapture.isChecked(),
            infer_style=self._ig4_infer_active(),
            style_per_image=self._ig4_is_character(),
            prompt_style=self.ig4_prompt_style["editor"].toPlainText().strip())
        # set de imágenes de la corrida: el worker también emite error() para
        # crops temporales fallidos; solo marcamos en rojo las imágenes reales.
        self._ig4_job_imgs = {str(Path(img)) for img, _ in jobs}
        self.grid.refresh_preprocess()      # limpia marcas de error de corridas previas
        self._ig4.progress.connect(self._on_progress)
        self._ig4.error.connect(self._on_error)
        self._ig4.error.connect(self._on_ig4_error)
        self._ig4.image_done.connect(self._on_ig4_image_done)
        self._ig4.warnings.connect(self._on_ig4_warnings)
        self._ig4.finished_all.connect(self._on_ig4_finished)
        self._ig4.start()
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.status.setText(self._tr("ig4.capturing", "Capturing Ideogram 4 captions…"))

    def _on_ig4_image_done(self, image_path, _json_path):
        """Una imagen se exportó OK: el grid pasa a 'exportada' (verde ✓) y se
        descarta cualquier marca de error transitoria que hubiera recibido."""
        self.grid.mark_path(image_path)

    def _on_ig4_error(self, path, _msg):
        """Marca en rojo solo si el fallo es de una imagen real del lote (no de
        un crop temporal). Si luego se exporta, image_done la pasa a verde."""
        if path in getattr(self, "_ig4_job_imgs", ()):
            self.grid.mark_error(path)

    def _on_ig4_warnings(self, path, warns):
        log.warning("ig4 schema warnings en %s: %s", Path(path).name, warns)
        self.status.setText(self._tr("ig4.warn", "{0}: {1} schema warning(s)")
                            .format(Path(path).name, len(warns)))

    def _on_ig4_finished(self, exported, skipped):
        self.btn_cancel.setEnabled(False)
        self._ig4 = None
        self.status.setText(self._tr("ig4.done", "Done — {0} exported, {1} skipped.")
                            .format(exported, skipped))
        self._update_run_enabled()
        try:
            out = sidecar.output_dir(self._source_folder, IG4_OUTPUT_TAG, "json")
            if Path(out).exists():
                self._last_output = (str(out), False)
                CachePaths.open_folder(str(out))
        except Exception as e:  # noqa: BLE001
            log.debug("No se pudo abrir la salida ig4: %s", e)

    def _open_review(self):
        """Abre el editor de captions sobre el último dataset o una carpeta a elegir."""
        from .review_view import ReviewView
        folder, as_tag = (self._last_output or (None, True))
        self._review = ReviewView(self.context, folder=folder, as_tag=as_tag)
        self._review.show()
        self._review.raise_()

    def _on_grid_image_activated(self, image_path):
        """Doble clic en una imagen: editor de cajas (Ideogram v4) o de captions."""
        if self._mode() == MODE_IDEOGRAM4:
            self._open_bbox_editor(image_path)
        else:
            self._edit_caption(image_path)

    def _open_bbox_editor(self, image_path):
        """Abre el editor de bboxes para una imagen, creando/cargando su
        .pano.json en <set>_ideogram4_json/drafts/ (subcarpeta de borradores)."""
        if not self._source_folder:
            return
        from .bbox_editor import BBoxEditor
        out_dir = Path(sidecar.output_dir(self._source_folder, IG4_OUTPUT_TAG, "json"))
        pano_path = layout.pano_path(out_dir, image_path)
        # Si el VLM va a inferir el estilo (personaje o estilo+inferir), no se
        # hereda nada manual: el worker lo rellenará. Solo en estilo+manual se
        # propaga lo escrito en el panel.
        if self._ig4_infer_active():
            style_defaults = {"art_style": "", "aesthetics": "", "lighting": ""}
        else:
            style_defaults = {
                "art_style": self.ig4_artstyle.text().strip(),
                "aesthetics": self.ig4_aesthetics.text().strip(),
                "lighting": self.ig4_lighting.text().strip(),
            }
        lm = self.context.get('locale_manager') if self.context else None
        try:
            self._bbox_editor = BBoxEditor(
                image_path, pano_path, locale_manager=lm,
                style_defaults=style_defaults,
                images=self._ig4_all_images(), out_dir=out_dir, parent=self)
        except ValueError as e:
            self.status.setText(str(e))
            return
        self._bbox_editor.exec()           # modal; al cerrar, el .pano.json quedó guardado
        self.grid.refresh_preprocess()     # marca la imagen recién preprocesada
        self._update_run_enabled()         # pudo aparecer un nuevo .pano.json

    def _edit_caption(self, image_path):
        """Doble clic en una imagen del grid: abre el editor de captions sobre su
        carpeta, preseleccionando ese .txt para revisarlo/corregirlo."""
        from .review_view import ReviewView
        folder = str(Path(image_path).parent)
        # si el grid muestra el último dataset generado, respeta su formato
        as_tag = self._last_output[1] if (
            self._last_output and self._last_output[0] == folder) else True
        self._review = ReviewView(self.context, folder=folder,
                                  as_tag=as_tag, select=image_path)
        self._review.show()
        self._review.raise_()

    def _cancel(self):
        if self._caption:
            self._caption.cancel()
            self.status.setText(self._tr("tagger.cancelling", "Cancelling…"))
        if self._ig4:
            self._ig4.cancel()
            self.status.setText(self._tr("tagger.cancelling", "Cancelling…"))
        if self._autobatch:
            self._autobatch.cancel()
            self.status.setText(self._tr("tagger.cancelling", "Cancelling…"))

    def closeEvent(self, event):
        # detiene workers vivos para no destruir un QThread en ejecución
        try:
            self.grid.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._wd_dl is not None:
            self._wd_dl.wait(3000)
        if self._ig4 is not None:
            self._ig4.cancel()
            self._ig4.wait(3000)
        if self._autobatch is not None:
            self._autobatch.cancel()
            self._autobatch.wait(3000)
        super().closeEvent(event)

    # -- integración con otros módulos (Fase 6) ------------------------------
    def load_images(self, paths):
        """Recibe un set de imágenes desde otros módulos vía load_image_set()."""
        imgs = [str(p) for p in paths if Path(p).suffix.lower() in sidecar.IMAGE_EXTS]
        if imgs:
            self._set_source_images(imgs)
