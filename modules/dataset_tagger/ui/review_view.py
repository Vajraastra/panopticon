"""
Vista de revisión/edición de un dataset ya generado (Fase 4.2).

Se abre como ventana propia desde el tagger, apuntando a una carpeta de salida.
Dos funciones:
- Revisión previa/posterior: ver cada imagen junto a su sidecar .txt y editarlo.
- Edición masiva (tag_tools): buscar/reemplazar/eliminar un tag en todos los
  .txt y aplicar una lista de tags baneadas.

Solo toca los .txt de la carpeta de salida; nunca las imágenes originales.
"""
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QPlainTextEdit, QListWidget, QListWidgetItem, QSplitter, QFrame, QCheckBox,
    QFileDialog, QComboBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from ..logic import tag_tools, tag_db
from ..logic.sidecar import IMAGE_EXTS
from ..logic.templates import CaptionTemplate, available_models
from .tag_completer import TagCompleter

log = logging.getLogger(__name__)


class ReviewView(QWidget):
    """Editor de sidecars de un dataset. Ventana de nivel superior."""

    def __init__(self, context, folder=None, as_tag=True, select=None, model_key=None):
        super().__init__()
        self.context = context
        self.folder = str(folder) if folder else None
        self._current = None  # Path del .txt en edición
        # stem a preseleccionar al cargar (p. ej. la imagen abierta desde el grid)
        self._select_stem = Path(select).stem if select else None
        # arquitectura sugerida (del tagger). Dirige el CSV de autocompletado y
        # el grupo de quality tags. El usuario puede cambiarla en el combo.
        self._model_key = model_key
        self._tagdb_worker = None

        self.setWindowTitle(self._tr("review.title", "Review captions"))
        self.resize(960, 640)

        self._build()
        self.chk_as_tag.setChecked(as_tag)
        if self.folder:
            self._load_folder(self.folder)
        # arranca la carga del CSV si el dataset es de tags
        self._on_arch_changed()

    # -- helpers --------------------------------------------------------------
    def _tr(self, key, default=None):
        lm = self.context.get('locale_manager') if self.context else None
        if lm:
            return lm.tr(key, default)
        return default if default is not None else key

    def _accent(self):
        tm = self.context.get('theme_manager') if self.context else None
        return tm.get_color('accent_main') if tm else "#bd93f9"

    # -- arquitectura / autocompletado de tags --------------------------------
    @staticmethod
    def _tag_models():
        """(key, label) de los modelos de formato 'tags' (los que tienen CSV)."""
        out = []
        for key, _label in available_models(include_deferred=False):
            try:
                if CaptionTemplate(key).format == "tags":
                    out.append((key, _label))
            except KeyError:
                continue
        return out

    def _current_template(self):
        key = self.combo_arch.currentData()
        if not key:
            return None
        try:
            return CaptionTemplate(key)
        except KeyError:
            return None

    def _on_arch_changed(self, *_):
        """Cambió la arquitectura (o se abrió la vista): recarga el CSV asociado
        si el dataset es de tags."""
        tmpl = self._current_template()
        if tmpl and tmpl.quality_prefix:
            self.btn_quality.setToolTip(self._tr(
                "review.add_quality.tip",
                "Prepend this architecture's standard quality tags if missing.")
                + "\n" + tmpl.separator.join(tmpl.quality_prefix))
        if self.chk_as_tag.isChecked():
            self._start_tagdb_load(tmpl)
        else:
            self.completer.set_enabled(False)

    def _on_as_tag_toggled(self, on):
        self.completer.set_enabled(on)
        if on:
            self._start_tagdb_load(self._current_template())

    def _start_tagdb_load(self, tmpl):
        if not tmpl or not tmpl.tag_csv:
            self.lbl_tagdb.setText(self._tr("review.tagdb.none", "(no tag DB for this architecture)"))
            self.completer.set_csv("")
            return
        if tag_db.is_ready(tmpl.tag_csv):
            self._on_tagdb_ready(tmpl.tag_csv, 0)
            return
        if self._tagdb_worker and self._tagdb_worker.isRunning():
            return
        verb = (self._tr("review.tagdb.downloading", "Downloading tag DB…")
                if not tag_db.is_present(tmpl.tag_csv)
                else self._tr("review.tagdb.loading", "Loading tag DB…"))
        self.lbl_tagdb.setText(verb)
        self._tagdb_worker = tag_db.TagDbWorker(tmpl.tag_csv, tmpl.tag_csv_url)
        self._tagdb_worker.ready.connect(self._on_tagdb_ready)
        self._tagdb_worker.failed.connect(self._on_tagdb_failed)
        self._tagdb_worker.start()

    def _on_tagdb_ready(self, csv_name, _n):
        count = tag_db.tag_count(csv_name) or _n
        self.completer.set_csv(csv_name)
        self.completer.set_enabled(self.chk_as_tag.isChecked())
        self.lbl_tagdb.setText(self._tr("review.tagdb.ready", "{0} tags ready")
                               .format(f"{count:,}"))

    def _on_tagdb_failed(self, _csv_name, msg):
        self.completer.set_enabled(False)
        self.lbl_tagdb.setText(self._tr("review.tagdb.error", "Tag DB error: {0}").format(msg))

    def _insert_quality(self):
        """Antepone el grupo de quality tags de la arquitectura si falta (no
        destructivo, dedup case-insensitive). Decisión de David: las quality tags
        de un grupo general van SIEMPRE en los datasets de tags."""
        tmpl = self._current_template()
        if not tmpl or not tmpl.quality_prefix:
            return
        existing = [t.strip() for t in self.editor.toPlainText().split(",") if t.strip()]
        have = {t.lower() for t in existing}
        missing = [q for q in tmpl.quality_prefix if q.lower() not in have]
        if not missing:
            return
        self.editor.setPlainText(tmpl.separator.join(missing + existing))
        self._save_current()

    # -- construcción ---------------------------------------------------------
    def _build(self):
        root = QVBoxLayout(self)

        # cabecera: carpeta + abrir + modo tags
        head = QHBoxLayout()
        self.lbl_folder = QLabel(self._tr("review.no_folder", "No folder loaded"))
        self.lbl_folder.setWordWrap(True)
        btn_open = QPushButton(self._tr("review.open", "Open folder…"))
        btn_open.clicked.connect(self._browse)
        self.chk_as_tag = QCheckBox(self._tr("review.as_tag", "Treat as tags"))
        self.chk_as_tag.toggled.connect(self._on_as_tag_toggled)
        head.addWidget(self.lbl_folder, 1)
        head.addWidget(self.chk_as_tag, 0)
        head.addWidget(btn_open, 0)
        root.addLayout(head)

        # fila de arquitectura: dirige el CSV de autocompletado + quality tags
        arch = QHBoxLayout()
        self.lbl_arch = QLabel(self._tr("review.arch", "Architecture:"))
        self.combo_arch = QComboBox()
        for key, label in self._tag_models():
            self.combo_arch.addItem(label, key)
        if self._model_key:
            i = self.combo_arch.findData(self._model_key)
            if i >= 0:
                self.combo_arch.setCurrentIndex(i)
        self.combo_arch.currentIndexChanged.connect(self._on_arch_changed)
        self.lbl_tagdb = QLabel("")
        self.lbl_tagdb.setStyleSheet("color: #888;")
        arch.addWidget(self.lbl_arch, 0)
        arch.addWidget(self.combo_arch, 0)
        arch.addWidget(self.lbl_tagdb, 1)
        root.addLayout(arch)

        # cuerpo: lista | (preview + editor)
        split = QSplitter(Qt.Horizontal)
        self.file_list = QListWidget()
        self.file_list.currentItemChanged.connect(self._on_select)
        split.addWidget(self.file_list)

        right = QWidget()
        rlay = QVBoxLayout(right)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(240)
        self.preview.setStyleSheet("background: #1a1a1a; border-radius: 8px;")
        rlay.addWidget(self.preview, 1)

        # barra de acciones del caption actual (quality tags)
        actions = QHBoxLayout()
        self.btn_quality = QPushButton(self._tr("review.add_quality", "+ Quality tags"))
        self.btn_quality.setToolTip(self._tr(
            "review.add_quality.tip",
            "Prepend this architecture's standard quality tags if missing."))
        self.btn_quality.clicked.connect(self._insert_quality)
        actions.addWidget(self.btn_quality, 0)
        actions.addStretch(1)
        rlay.addLayout(actions)

        self.editor = QPlainTextEdit()
        rlay.addWidget(self.editor, 1)
        # autocompletado de tags (sin LLM); solo activo en modo tags
        self.completer = TagCompleter(self.editor)
        btn_save = QPushButton(self._tr("review.save", "Save this caption"))
        btn_save.clicked.connect(self._save_current)
        rlay.addWidget(btn_save)
        split.addWidget(right)
        split.setSizes([260, 700])
        root.addWidget(split, 1)

        # herramientas de edición masiva
        tools = QFrame()
        tools.setStyleSheet(f"QFrame {{ border-top: 1px solid {self._accent()}; }}")
        tlay = QVBoxLayout(tools)
        title = QLabel(self._tr("review.tools", "Bulk tag tools"))
        title.setStyleSheet(f"color: {self._accent()}; font-weight: bold;")
        tlay.addWidget(title)

        row1 = QHBoxLayout()
        self.edit_find = QLineEdit()
        self.edit_find.setPlaceholderText(self._tr("review.find", "Tag / text to find"))
        self.edit_replace = QLineEdit()
        self.edit_replace.setPlaceholderText(self._tr("review.replace_with", "Replace with (empty = remove)"))
        btn_replace = QPushButton(self._tr("review.replace_all", "Replace all"))
        btn_replace.clicked.connect(self._replace_all)
        btn_remove = QPushButton(self._tr("review.remove_all", "Remove all"))
        btn_remove.clicked.connect(self._remove_all)
        row1.addWidget(self.edit_find, 2)
        row1.addWidget(self.edit_replace, 2)
        row1.addWidget(btn_replace, 0)
        row1.addWidget(btn_remove, 0)
        tlay.addLayout(row1)

        row2 = QHBoxLayout()
        self.edit_banned = QLineEdit()
        self.edit_banned.setPlaceholderText(self._tr("review.banned", "Banned tags (comma-separated)"))
        btn_banned = QPushButton(self._tr("review.apply_banned", "Apply banned"))
        btn_banned.clicked.connect(self._apply_banned)
        row2.addWidget(self.edit_banned, 1)
        row2.addWidget(btn_banned, 0)
        tlay.addLayout(row2)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        tlay.addWidget(self.status)
        root.addWidget(tools)

    # -- carga de carpeta -----------------------------------------------------
    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, self._tr("review.choose", "Choose output folder"))
        if folder:
            self._load_folder(folder)

    def _load_folder(self, folder):
        self._save_current()  # no perder ediciones pendientes
        self.folder = folder
        self._current = None
        self.editor.clear()
        self.preview.clear()
        files = tag_tools.caption_files(folder)
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for p in files:
            item = QListWidgetItem(p.stem)
            item.setData(Qt.UserRole, str(p))
            self.file_list.addItem(item)
        self.file_list.blockSignals(False)
        self.lbl_folder.setText(self._tr("review.folder", "Folder: {0} ({1} captions)")
                                .format(folder, len(files)))
        if files:
            row = 0
            if self._select_stem:
                for i in range(self.file_list.count()):
                    if self.file_list.item(i).text() == self._select_stem:
                        row = i
                        break
                self._select_stem = None  # solo en la primera carga
            self.file_list.setCurrentRow(row)

    # -- selección / edición individual ---------------------------------------
    def _on_select(self, current, _previous):
        self._save_current()  # autoguarda el anterior
        if not current:
            self._current = None
            return
        path = current.data(Qt.UserRole)
        self._current = path
        self.editor.setPlainText(tag_tools.read(path))
        self._show_preview(path)

    def _show_preview(self, txt_path):
        stem = Path(txt_path).stem
        folder = Path(txt_path).parent
        img = None
        for ext in IMAGE_EXTS:
            cand = folder / (stem + ext)
            if cand.exists():
                img = cand
                break
        if not img:
            self.preview.setText(self._tr("review.no_image", "(image not found)"))
            return
        pix = QPixmap(str(img))
        if pix.isNull():
            self.preview.setText(self._tr("review.no_image", "(image not found)"))
            return
        self.preview.setPixmap(pix.scaled(
            self.preview.width(), self.preview.height(),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _save_current(self):
        if self._current is not None:
            tag_tools.write(self._current, self.editor.toPlainText())

    # -- edición masiva -------------------------------------------------------
    def _as_tag(self):
        return self.chk_as_tag.isChecked()

    def _after_bulk(self, changed):
        self.status.setText(self._tr("review.changed", "{0} caption(s) updated.").format(changed))
        # recarga el archivo actual para reflejar el cambio en el editor
        if self._current:
            self.editor.setPlainText(tag_tools.read(self._current))

    def _replace_all(self):
        if not self.folder:
            return
        self._save_current()
        n = tag_tools.replace(self.folder, self.edit_find.text(),
                              self.edit_replace.text(), as_tag=self._as_tag())
        self._after_bulk(n)

    def _remove_all(self):
        if not self.folder:
            return
        self._save_current()
        n = tag_tools.remove(self.folder, self.edit_find.text(), as_tag=self._as_tag())
        self._after_bulk(n)

    def _apply_banned(self):
        if not self.folder:
            return
        self._save_current()
        banned = [t.strip() for t in self.edit_banned.text().split(",") if t.strip()]
        n = tag_tools.apply_banned(self.folder, banned, as_tag=self._as_tag())
        self._after_bulk(n)

    def closeEvent(self, event):
        self._save_current()
        if self._tagdb_worker and self._tagdb_worker.isRunning():
            self._tagdb_worker.wait(3000)
        super().closeEvent(event)
