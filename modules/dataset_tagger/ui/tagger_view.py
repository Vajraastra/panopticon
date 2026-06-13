"""
Vista principal del Dataset Tagger (Fase 4).

Configura el captioning de una carpeta (o set de imágenes) con un VLM local
compatible con la API de OpenAI y lanza el CaptionWorker. La salida SIEMPRE va
a carpeta(s) nueva(s) hermana(s) del origen (sidecars .txt estilo kohya).

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
    QLineEdit, QPlainTextEdit, QCheckBox, QListWidget, QListWidgetItem,
    QProgressBar, QFrame, QFileDialog, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths

from ..logic.templates import available_models, CaptionTemplate
from ..logic.providers.openai_compat import OpenAICompatProvider
from ..logic.providers.discovery import discover_local
from ..logic.caption_worker import CaptionWorker
from ..logic import sidecar

log = logging.getLogger(__name__)

# --- constantes locale-safe (nunca comparar combos por texto) ----------------
MODE_TAGS, MODE_NATURAL, MODE_BOTH = 0, 1, 2

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


class DatasetTaggerView(QWidget):
    """Vista del Dataset Tagger: configuración + cola + ejecución."""

    def __init__(self, context):
        super().__init__()
        self.context = context

        # estado del origen
        self._source_folder = None   # str | None
        self._images = None          # list[str] | None (modo imágenes sueltas)

        # workers (referencias para evitar GC)
        self._discovery = None
        self._conn = None
        self._caption = None

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

        # cola / lista de imágenes (pre-confirmación; la revisión rica es review_view)
        self.queue_list = QListWidget()
        lay.addWidget(self.queue_list, 1)

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
        lay.addWidget(self._section(self._tr("tagger.sec.tokens", "Trigger & affixes")))
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
        self.box_prompt_tags = self._prompt_box(
            self._tr("tagger.prompt.tags", "Tags prompt"), is_tags=True)
        self.box_prompt_nat = self._prompt_box(
            self._tr("tagger.prompt.natural", "Natural prompt"), is_tags=False)
        lay.addWidget(self.box_prompt_tags["frame"])
        lay.addWidget(self.box_prompt_nat["frame"])

        # --- opciones de salida ---
        lay.addWidget(self._section(self._tr("tagger.sec.output", "Output")))
        self.combo_policy = QComboBox()
        for key, default, _val in POLICIES:
            self.combo_policy.addItem(self._tr(key, default))
        lay.addWidget(QLabel(self._tr("tagger.policy", "If a caption exists:")))
        lay.addWidget(self.combo_policy)
        self.chk_recursive = QCheckBox(self._tr("tagger.recursive", "Include subfolders"))
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
        btn_reset.setMaximumWidth(70)
        head.addStretch()
        head.addWidget(btn_reset)
        v.addLayout(head)
        editor = QPlainTextEdit()
        editor.setMaximumHeight(90)
        v.addWidget(editor)
        box = {"frame": frame, "editor": editor, "is_tags": is_tags}
        btn_reset.clicked.connect(lambda: self._reset_prompt(box))
        return box

    def _create_bottom(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.btn_run = QPushButton(self._tr("tagger.run", "Start captioning"))
        self.btn_run.clicked.connect(self._run)
        self.btn_cancel = QPushButton(self._tr("tagger.cancel", "Cancel"))
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
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
        show_tags = mode in (MODE_TAGS, MODE_BOTH)
        show_nat = mode in (MODE_NATURAL, MODE_BOTH)
        self.lbl_model_tags.setVisible(show_tags)
        self.combo_model_tags.setVisible(show_tags)
        self.box_prompt_tags["frame"].setVisible(show_tags)
        self.lbl_model_nat.setVisible(show_nat)
        self.combo_model_nat.setVisible(show_nat)
        self.box_prompt_nat["frame"].setVisible(show_nat)
        self._refresh_prompts()
        self._update_run_enabled()

    def _reset_prompt(self, box):
        combo = self.combo_model_tags if box["is_tags"] else self.combo_model_nat
        key = combo.currentData()
        if key:
            box["editor"].setPlainText(CaptionTemplate(key).meta_prompt())

    def _refresh_prompts(self):
        """Autocompleta cada editor con el meta-prompt default si está vacío."""
        for box, combo in ((self.box_prompt_tags, self.combo_model_tags),
                           (self.box_prompt_nat, self.combo_model_nat)):
            if not box["frame"].isVisible():
                continue
            key = combo.currentData()
            if key and not box["editor"].toPlainText().strip():
                box["editor"].setPlainText(CaptionTemplate(key).meta_prompt())

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
        try:
            imgs = sidecar.list_images(folder, self.chk_recursive.isChecked())
        except Exception:  # noqa: BLE001
            imgs = []
        self.lbl_source.setText(self._tr("tagger.source_folder", "Folder: {0} ({1} images)")
                                .format(folder, len(imgs)))
        self._populate_queue([str(p) for p in imgs])

    def _set_source_images(self, imgs):
        self._images = list(imgs)
        # carpeta de origen para nombrar la salida = carpeta del primer archivo
        self._source_folder = str(Path(imgs[0]).parent)
        self.lbl_source.setText(self._tr("tagger.source_images", "{0} images selected")
                                .format(len(imgs)))
        self._populate_queue(imgs)

    def _populate_queue(self, imgs):
        self.queue_list.clear()
        for p in imgs[:200]:
            self.queue_list.addItem(QListWidgetItem(Path(p).name))
        if len(imgs) > 200:
            self.queue_list.addItem(QListWidgetItem(
                self._tr("tagger.more", "+ {0} more…").format(len(imgs) - 200)))
        self._update_run_enabled()

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
    def _update_run_enabled(self):
        ready = bool(self._source_folder
                     and self.combo_endpoint_model.currentData()
                     and self._active_template_keys()
                     and (self._caption is None))
        self.btn_run.setEnabled(ready)

    def _run(self):
        keys = self._active_template_keys()
        model = self.combo_endpoint_model.currentData()
        if not (self._source_folder and model and keys):
            return

        # política (locale-safe: por índice -> valor)
        policy = POLICIES[self.combo_policy.currentIndex()][2]

        # prompts custom por plantilla activa (pre-resueltos en el hilo GUI)
        overrides = {}
        if self.box_prompt_tags["frame"].isVisible():
            k = self.combo_model_tags.currentData()
            if k:
                overrides[k] = self.box_prompt_tags["editor"].toPlainText()
        if self.box_prompt_nat["frame"].isVisible():
            k = self.combo_model_nat.currentData()
            if k:
                overrides[k] = self.box_prompt_nat["editor"].toPlainText()

        provider = OpenAICompatProvider(
            self.edit_endpoint.text().strip(),
            self.edit_apikey.text().strip() or None,
            model=model, timeout=120)

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
        # ofrece abrir la(s) carpeta(s) de salida (cross-platform, Regla de Oro #1)
        try:
            model = self.combo_endpoint_model.currentData()
            for key in self._active_template_keys():
                out = sidecar.output_dir(self._source_folder, model, CaptionTemplate(key).format)
                if Path(out).exists():
                    CachePaths.open_folder(str(out))
                    break
        except Exception as e:  # noqa: BLE001
            log.debug("No se pudo abrir la carpeta de salida: %s", e)

    def _cancel(self):
        if self._caption:
            self._caption.cancel()
            self.status.setText(self._tr("tagger.cancelling", "Cancelling…"))

    # -- integración con otros módulos (Fase 6) ------------------------------
    def load_images(self, paths):
        """Recibe un set de imágenes desde otros módulos vía load_image_set()."""
        imgs = [str(p) for p in paths if Path(p).suffix.lower() in sidecar.IMAGE_EXTS]
        if imgs:
            self._set_source_images(imgs)
