"""
Vista del módulo Pickler.

Usa el StandardToolLayout de 3 paneles (biblia §4):
  • Sidebar (320px) : entrada (abrir carpeta + recientes) y ajustes
                      (toggle de modo, carpeta destino, mover/copiar, contadores).
  • Canvas          : imagen escalada a 1024px lado largo, centrada, con franja
                      de color según el modo activo.
  • Action Bar      : botón de acción primaria del modo + omitir.

Dos modos *error-proof* (constantes locale-safe, nunca comparar por texto):
  MODE_DELETE → acción primaria = enviar a papelera recuperable.
  MODE_PICK   → acción primaria = conservar (mover/copiar) a destino.

La acción primaria se dispara solo por clic derecho o botón explícito; el
teclado únicamente omite (→/Espacio) o retrocede (←). Ctrl+Z deshace la última.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, QSettings, Signal, QTimer
from PySide6.QtGui import QAction, QKeyEvent, QMouseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QRadioButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths
from core.image_preview import ImagePreview
from core.metadata.bundle import MetadataBundle
from core.native_threads import native_done, start_worker
from ..logic.worker import PicklerWorker

# Modos (locale-safe: nunca comparar por el texto del botón)
MODE_DELETE = 0
MODE_PICK = 1

_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".gif", ".bmp", ".tiff", ".tif", ".avif",
}

_MAX_RECENT = 10
_MAX_SIDE = 1024  # Regla de 1024px (biblia §4)



class _ScaledLabel(QLabel):
    """QLabel que muestra su pixmap centrado, con el lado largo a 1024px máx."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_source(self, pixmap: QPixmap | None) -> None:
        self._source = pixmap
        self._refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self._source is None or self._source.isNull():
            if self._source is None:
                super().clear()
            return
        # Caja límite: el menor entre el viewport y 1024px en cada lado.
        box_w = min(max(self.width(), 1), _MAX_SIDE)
        box_h = min(max(self.height(), 1), _MAX_SIDE)
        src = self._source
        if src.width() <= box_w and src.height() <= box_h:
            super().setPixmap(src)  # no ampliar imágenes pequeñas (evita borrosidad)
        else:
            super().setPixmap(src.scaled(
                QSize(box_w, box_h),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class _Canvas(QWidget):
    """
    Área central: franja de modo + imagen + barra de info inferior.
    Emite señales de navegación; el menú contextual lo construye PicklerView
    (depende del modo activo).
    """

    skip_requested = Signal()
    prev_requested = Signal()
    undo_requested = Signal()
    context_menu_requested = Signal(object)  # QPoint global

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.mode_stripe = QWidget()
        self.mode_stripe.setFixedHeight(4)
        root.addWidget(self.mode_stripe)

        self.img = _ScaledLabel()
        root.addWidget(self.img, stretch=1)

        info = QFrame()
        info.setFixedHeight(34)
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(12, 0, 12, 0)
        self.lbl_info = QLabel("")
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info_layout.addWidget(self.lbl_info)
        root.addWidget(info)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Right):
            self.skip_requested.emit()
        elif key == Qt.Key.Key_Left:
            self.prev_requested.emit()
        elif key == Qt.Key.Key_Z and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.undo_requested.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.context_menu_requested.emit(event.globalPosition().toPoint())
        else:
            super().mousePressEvent(event)


class PicklerView(StandardToolLayout):
    """Controlador + vista del módulo Pickler."""

    def __init__(self, context: dict | None = None) -> None:
        self.context = context or {}
        self._theme = self.context.get('theme_manager')
        self._lm = self.context.get('locale_manager')
        self._event_bus = self.context.get('event_bus')
        self._service = self.context.get('data_service')
        self._closing = False
        self._worker = None
        self._job = None
        self._delivery = None
        self._pending_start = False
        self._preview = None
        self._retired_previews = []
        self._preview_ticket = None
        self._dialogs = []
        self._settings = QSettings("Panopticon", "Pickler")

        # Estado
        self._images: list[Path] = []
        self._index = 0
        self._mode = MODE_DELETE
        self._kept = 0
        self._discarded = 0
        self._last_op: dict | None = None  # confirmed transfer + original selection index

        # Carpeta del set actual y override de destino. Por defecto los archivos
        # van a 'picked/' y 'discarded/' DENTRO del set; "Elegir destino" sustituye
        # solo el destino de conservar y se reinicia al abrir otro set.
        self._set_folder: Path | None = None
        self._custom_dest: Path | None = None
        self._copy_mode = self._settings.value("copy_mode", False, type=bool)

        # Construir los 3 paneles y armar el layout estándar
        self._canvas = _Canvas()
        sidebar = self._build_sidebar()
        action_bar = self._build_action_bar()
        super().__init__(
            self._canvas,
            sidebar_widget=sidebar,
            bottom_widget=action_bar,
            theme_manager=self._theme,
            event_bus=self.context.get('event_bus'),
        )

        self._job_timer = QTimer(self)
        self._job_timer.setInterval(25)
        self._job_timer.timeout.connect(self._poll_job)
        self._new_preview()

        # Señales del canvas
        self._canvas.skip_requested.connect(self._next)
        self._canvas.prev_requested.connect(self._prev)
        self._canvas.undo_requested.connect(self._undo)
        self._canvas.context_menu_requested.connect(self._show_context_menu)

        self._load_recent()
        self._apply_mode_visuals()
        self._refresh_view()

    # ── i18n ──────────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    # ── Construcción del sidebar ──────────────────────────────────────
    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)

        # Entrada: abrir carpeta + recientes
        lay.addWidget(self._section_label(self.tr("pickler.input", "INPUT")))
        self._btn_open = QPushButton(self.tr("pickler.open_folder", "Open folder…"))
        self._btn_open.setToolTip(self.tr(
            "pickler.open_folder_tip",
            "Choose the folder of images to review. It is walked in order and nothing is "
            "changed until you decide on each image."))
        self._btn_open.clicked.connect(self._browse)
        lay.addWidget(self._btn_open)

        self._recent_label = self._section_label(self.tr("pickler.recent", "Recent"))
        lay.addWidget(self._recent_label)
        self._recent_list = QListWidget()
        self._recent_list.setMaximumHeight(150)
        self._recent_list.itemActivated.connect(self._on_recent_activated)
        self._recent_list.itemDoubleClicked.connect(self._on_recent_activated)
        lay.addWidget(self._recent_list)

        lay.addSpacing(6)
        lay.addWidget(self._divider())

        # Ajustes: modo
        lay.addWidget(self._section_label(self.tr("pickler.settings", "SETTINGS")))
        self._btn_mode = QPushButton()
        self._btn_mode.setToolTip(self.tr(
            "pickler.mode_tip",
            "Switches the working MODE, it does not act on the current image. In «Keep» "
            "the big button keeps the image; in «Discard» it discards it. This is the top "
            "button: the bottom one is the one that acts."))
        self._btn_mode.clicked.connect(self._toggle_mode)
        lay.addWidget(self._btn_mode)

        # Destino (conservar)
        lay.addWidget(self._section_label(self.tr("pickler.dest_folder", "Destination (keep)")))
        self._lbl_dest = QLabel()
        self._lbl_dest.setWordWrap(True)
        self._lbl_dest.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._lbl_dest)
        self._btn_dest = QPushButton(self.tr("pickler.choose_dest", "Choose destination…"))
        self._btn_dest.setToolTip(self.tr(
            "pickler.choose_dest_tip",
            "Folder where the images you keep end up. Discards do NOT go here: they go to "
            "«discarded» inside the source folder."))
        self._btn_dest.clicked.connect(self._choose_dest)
        lay.addWidget(self._btn_dest)

        # Mover / Copiar
        lay.addWidget(self._section_label(self.tr("pickler.transfer_mode", "When keeping:")))
        row = QHBoxLayout()
        self._rb_move = QRadioButton(self.tr("pickler.move", "Move"))
        self._rb_move.setToolTip(self.tr(
            "pickler.move_tip",
            "The images you keep are MOVED to the destination: they leave the source "
            "folder."))
        self._rb_copy = QRadioButton(self.tr("pickler.copy", "Copy"))
        self._rb_copy.setToolTip(self.tr(
            "pickler.copy_tip",
            "The images you keep are COPIED to the destination: the original stays where "
            "it is."))
        grp = QButtonGroup(panel)
        grp.addButton(self._rb_move)
        grp.addButton(self._rb_copy)
        (self._rb_copy if self._copy_mode else self._rb_move).setChecked(True)
        self._rb_move.toggled.connect(self._on_transfer_toggled)
        row.addWidget(self._rb_move)
        row.addWidget(self._rb_copy)
        row.addStretch()
        lay.addLayout(row)

        lay.addStretch()

        # Contadores
        self._lbl_counters = QLabel()
        lay.addWidget(self._lbl_counters)

        # Footer: abrir destino / papelera / deshacer
        self._btn_undo = QPushButton(self.tr("pickler.undo", "Undo last (Ctrl+Z)"))
        self._btn_undo.setToolTip(self.tr(
            "pickler.undo_tip",
            "Undoes ONLY the last decision and puts the image back. It is not a history: "
            "it does not chain several steps back."))
        self._btn_undo.clicked.connect(self._undo)
        lay.addWidget(self._btn_undo)
        self._btn_open_dest = QPushButton(self.tr("pickler.open_dest", "Open destination"))
        self._btn_open_dest.setToolTip(self.tr(
            "pickler.open_dest_tip",
            "Opens the folder where the images you keep are saved."))
        self._btn_open_dest.clicked.connect(self._open_dest)
        lay.addWidget(self._btn_open_dest)
        self._btn_open_disc = QPushButton(self.tr("pickler.open_discarded", "Open discarded"))
        self._btn_open_disc.setToolTip(self.tr(
            "pickler.open_discarded_tip",
            "Opens the source's «discarded» folder, where the discarded images are. They "
            "are still there: nothing was really deleted."))
        self._btn_open_disc.clicked.connect(self._open_discarded)
        lay.addWidget(self._btn_open_disc)
        self._btn_recovery = QPushButton(self.tr("pickler.recovery", "Review interrupted transfers…"))
        self._btn_recovery.setToolTip(self.tr("pickler.recovery_tip", "Restore the source of a pending transfer, keeping its current curation. Verified copy undo can resume; confirmed transfers are retained."))
        self._btn_recovery.clicked.connect(lambda: self._start_job('review', ()))
        lay.addWidget(self._btn_recovery)

        self._update_dest_label()
        self._update_counters()
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        self._btn_primary = QPushButton()
        self._btn_primary.setToolTip(self.tr(
            "pickler.action_tip",
            "Applies the current mode to the image on screen and moves to the next one. "
            "Discarding does NOT delete: it moves the file to «discarded», and it can be "
            "undone."))
        self._btn_primary.clicked.connect(self._do_primary)
        self._btn_skip = QPushButton(self.tr("pickler.action_skip", "→  Skip"))
        self._btn_skip.setToolTip(self.tr(
            "pickler.action_skip_tip",
            "Moves to the next image without touching it or deciding anything about it."))
        self._btn_skip.clicked.connect(self._next)
        lay.addWidget(self._btn_primary)
        lay.addWidget(self._btn_skip)
        lay.addStretch()
        return bar

    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {self._color('text_secondary', '#aaa')}; "
            f"font-size: 11px; font-weight: bold; letter-spacing: 1px;")
        return lbl

    def _divider(self) -> QFrame:
        d = QFrame()
        d.setFixedHeight(1)
        d.setStyleSheet(f"background-color: {self._color('border', '#333')};")
        return d

    # ── Carpetas recientes ────────────────────────────────────────────
    def _load_recent(self) -> None:
        recent = self._settings.value("recent_folders", []) or []
        if isinstance(recent, str): recent = [recent]
        self._recent_list.clear()
        for value in recent[:_MAX_RECENT]:
            if not isinstance(value,str) or not value:continue
            path=Path(value)
            item=QListWidgetItem(path.name)
            item.setData(Qt.ItemDataRole.UserRole,str(path)); item.setToolTip(str(path))
            self._recent_list.addItem(item)
        has=self._recent_list.count()>0
        self._recent_label.setVisible(has); self._recent_list.setVisible(has)

    def _save_recent(self, folder: Path) -> None:
        recent = self._settings.value("recent_folders", []) or []
        if isinstance(recent, str):
            recent = [recent]
        s = str(folder)
        if s in recent:
            recent.remove(s)
        recent.insert(0, s)
        self._settings.setValue("recent_folders", recent[:_MAX_RECENT])

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        if self._closing or self._worker is not None:return
        self._open_folder(Path(item.data(Qt.ItemDataRole.UserRole)))

    # ── Selección de carpeta / destino ────────────────────────────────
    def _browse(self) -> None:
        if self._closing or self._worker is not None:return
        last = self._settings.value("last_dir", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("pickler.open_folder", "Open folder…"), last)
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder: Path) -> None:
        if self._closing or self._worker is not None:return
        self._settings.setValue("last_dir", str(folder.parent))
        self._save_recent(folder)
        self._load_recent()
        self.load_folder(folder)

    def _choose_dest(self) -> None:
        if self._closing or self._worker is not None:return
        start = str(self._custom_dest) if self._custom_dest else \
            str(self._set_folder or self._settings.value("last_dir", str(Path.home())))
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("pickler.choose_dest", "Choose destination…"), start)
        if folder:
            self._custom_dest = Path(folder)
            self._update_dest_label()

    def _keep_dest(self) -> Path | None:
        """Destino para conservar: override si existe, si no 'picked/' en el set."""
        if self._custom_dest:
            return self._custom_dest
        base = self._set_folder or (self._images[self._index].parent if self._images else None)
        return (base / "picked") if base else None

    def _discard_dest(self) -> Path | None:
        """Destino para descartar: siempre 'discarded/' en el set/carpeta origen."""
        base = self._set_folder or (self._images[self._index].parent if self._images else None)
        return (base / "discarded") if base else None

    def _update_dest_label(self) -> None:
        dest = self._keep_dest()
        if self._custom_dest:
            self._lbl_dest.setText(str(self._custom_dest))
        elif dest:
            self._lbl_dest.setText(str(dest))
        else:
            self._lbl_dest.setText(self.tr("pickler.dest_default", "<set>/picked"))

    def _on_transfer_toggled(self) -> None:
        self._copy_mode = self._rb_copy.isChecked()
        self._settings.setValue("copy_mode", self._copy_mode)

    # ── API pública ───────────────────────────────────────────────────
    def load_folder(self, folder: Path) -> None:
        self._start_job('scan',(Path(folder),))

    # ── Modo ──────────────────────────────────────────────────────────
    def _toggle_mode(self) -> None:
        if self._closing or self._worker is not None:return
        self._mode = MODE_PICK if self._mode == MODE_DELETE else MODE_DELETE
        self._apply_mode_visuals()

    def _apply_mode_visuals(self) -> None:
        if self._mode == MODE_PICK:
            color = self._color('accent_success', '#00ff66')
            self._btn_mode.setText(self.tr("pickler.mode_pick", "✓ Picker"))
            self._btn_primary.setText(self.tr("pickler.action_pick", "✓  Keep"))
        else:
            color = self._color('accent_warning', '#ff3333')
            self._btn_mode.setText(self.tr("pickler.mode_delete", "🗑 Delete"))
            self._btn_primary.setText(self.tr("pickler.action_delete", "🗑  Discard"))
        self._canvas.mode_stripe.setStyleSheet(f"background-color: {color};")
        self._btn_mode.setStyleSheet(f"color: {color}; border-color: {color};")
        self._btn_primary.setStyleSheet(f"color: {color}; border-color: {color};")

    # ── Navegación ────────────────────────────────────────────────────
    def _next(self) -> None:
        if self._closing or self._worker is not None:return
        if self._images and self._index < len(self._images) - 1:
            self._index += 1
            self._refresh_view()

    def _prev(self) -> None:
        if self._closing or self._worker is not None:return
        if self._images and self._index > 0:
            self._index -= 1
            self._refresh_view()

    # ── Acción primaria ───────────────────────────────────────────────
    def _do_primary(self) -> None:
        if self._closing or self._worker is not None:return
        if not self._images:
            return
        if self._mode == MODE_PICK:
            self._pick_current()
        else:
            self._delete_current()

    def _delete_current(self) -> None:
        if self._closing or self._worker is not None or not self._images:return
        destination=self._discard_dest()
        if destination:self._start_job('transfer',(self._images[self._index],destination,'discard'))

    def _pick_current(self) -> None:
        if self._closing or self._worker is not None or not self._images:return
        destination=self._keep_dest()
        if destination:self._start_job('transfer',(self._images[self._index],destination,'copy' if self._copy_mode else 'move'))

    def _consume_current(self) -> None:
        """Quita la imagen actual de la lista (movida) y refresca."""
        self._images.pop(self._index)
        if self._index >= len(self._images):
            self._index = max(0, len(self._images) - 1)
        self._refresh_view()
        self._update_counters()

    # ── Deshacer ──────────────────────────────────────────────────────
    def _undo(self) -> None:
        if self._last_op:self._start_job('undo',(self._last_op,))

    def _notify_library_changed(self, *folders) -> None:
        """Publicar carpetas sólo tras confirmación; no depender de reindexar para curación."""
        if not self._event_bus:
            return
        for f in folders:
            if f:
                self._event_bus.publish("library_changed", str(f))

    # ── Menú contextual (acción primaria error-proof) ─────────────────
    def _show_context_menu(self, pos) -> None:
        if self._closing or self._worker is not None:return
        if not self._images:
            return
        menu = QMenu(self)
        if self._mode == MODE_PICK:
            act = QAction(self.tr("pickler.action_pick", "✓  Keep"), self)
            act.triggered.connect(self._pick_current)
        else:
            act = QAction(self.tr("pickler.action_delete", "🗑  Discard"), self)
            act.triggered.connect(self._delete_current)
        skip = QAction(self.tr("pickler.action_skip", "→  Skip"), self)
        skip.triggered.connect(self._next)
        menu.addAction(act)
        menu.addAction(skip)
        menu.exec(pos)

    # ── Refresco de UI ────────────────────────────────────────────────
    def _refresh_view(self) -> None:
        if self._closing or self._worker is not None:return
        self._set_busy(False)
        if not self._images:
            self._canvas.img.set_source(None)
            self._canvas.img.setText(self.tr("pickler.no_images", "No images in this folder"))
            self._set_status(""); return
        path=self._images[self._index]
        self._canvas.img.set_source(None)
        self._set_status(f"{path.name}   [{self._index + 1} / {len(self._images)}]")
        if self._preview:self._preview_ticket=self._preview.request(str(path))

    def _set_status(self, text: str) -> None:
        self._canvas.lbl_info.setText(text)
        self._canvas.lbl_info.setToolTip(text)

    def _update_counters(self) -> None:
        kept = self.tr("pickler.kept", "Kept")
        disc = self.tr("pickler.deleted", "Discarded")
        self._lbl_counters.setText(
            f"✓ {kept}: {self._kept}    🗑 {disc}: {self._discarded}")

    def _open_dest(self) -> None:
        dest = self._keep_dest()
        if dest and dest.exists():
            CachePaths.open_folder(dest)

    def _open_discarded(self) -> None:
        disc = self._discard_dest()
        if disc and disc.exists():
            CachePaths.open_folder(disc)

    def _new_preview(self):
        if self._closing:return
        self._preview=ImagePreview(lambda _: MetadataBundle(),self)
        preview=self._preview
        preview.result.connect(lambda ticket,value,error: self._preview_ready(preview,ticket,value,error),Qt.QueuedConnection)

    def _preview_ready(self,preview,ticket,value,error):
        if self._closing or self._worker is not None or preview is not self._preview or ticket != self._preview_ticket:return
        if error is not None or value is None or value['image'].isNull():
            self._canvas.img.set_source(None)
            self._canvas.img.setText(self.tr('pickler.cant_display','Cannot display:'))
        else:self._canvas.img.set_source(QPixmap.fromImage(value['image']))

    def _set_busy(self,busy):
        enabled=not busy and not self._closing
        for name in ('_btn_open','_recent_list','_btn_dest','_btn_mode','_rb_move','_rb_copy','_btn_recovery'):
            widget=getattr(self,name,None)
            if widget:widget.setEnabled(enabled)
        self._canvas.setEnabled(enabled)
        self._btn_primary.setEnabled(enabled and bool(self._images))
        self._btn_skip.setEnabled(enabled and bool(self._images))
        self._btn_undo.setEnabled(enabled and self._last_op is not None)

    def _start_job(self,kind,args):
        if self._closing or self._worker is not None:return
        if self._preview:
            self._preview.close(); self._retired_previews.append(self._preview); self._preview=None
        self._job=dict(kind=kind,args=args,index=self._index)
        self._delivery=None
        self._worker=PicklerWorker(kind,self._service,args,journal_root=self.context.get('pickler_journal_root'))
        self._worker.finished_signal.connect(self._job_delivered,Qt.QueuedConnection)
        self._pending_start=True
        self._set_busy(True)
        self._set_status(self.tr('pickler.processing','Processing…'))
        self._job_timer.start(); self._poll_job()

    def _job_delivered(self,result,error):
        self._delivery=(result,error); self._poll_job()

    def _poll_job(self):
        for preview in tuple(self._retired_previews):
            if preview.shutdown_ready():
                self._retired_previews.remove(preview); preview.deleteLater()
        if self._worker is None:return
        if self._pending_start:
            if self._retired_previews:return
            self._pending_start=False
            if self._closing:self._delivery=(None,'pickler_cancelled')
            else:
                try:start_worker(self._worker,self)
                except Exception as exc:self._delivery=(None,str(exc))
                else:return
        if self._delivery is None or not native_done(self._worker):return
        worker,self._worker=self._worker,None
        job,self._job=self._job,None
        result,error=self._delivery; self._delivery=None
        worker.deleteLater(); self._job_timer.stop()
        if self._closing:return
        self._new_preview(); self._set_busy(False)
        if error is not None:
            self._refresh_view()
            self._set_status(self.tr('pickler.error_move','Error: {}').format(error)); return
        kind=job['kind']
        if kind in ('scan','images'):
            self._images=result['images']; self._set_folder=result['folder']; self._custom_dest=None
            self._index=0; self._last_op=None; self._update_dest_label()
        elif kind=='transfer':
            result['index']=job['index']; self._last_op=result
            if result['action']=='discard':self._discarded+=1
            else:self._kept+=1
            self._notify_library_changed(Path(result['original']).parent,Path(result['current']).parent)
            if result['action']=='copy':
                if self._index<len(self._images)-1:self._index+=1
            else:
                self._images.pop(self._index); self._index=min(self._index,max(0,len(self._images)-1))
        elif kind=='undo':
            previous=job['args'][0]; action=previous['request']['action']
            if action=='discard':self._discarded=max(0,self._discarded-1)
            else:self._kept=max(0,self._kept-1)
            if action!='copy':
                index=min(previous['index'],len(self._images))
                self._images.insert(index,Path(previous['original'])); self._index=index
            self._notify_library_changed(Path(previous['original']).parent,Path(previous['current']).parent)
            self._last_op=None
        elif kind=='review':
            self._refresh_view(); self._show_recovery(result); return
        elif kind=='recover':
            self._last_op=None
            request=result['request']
            self._notify_library_changed(Path(request['source_path']).parent,Path(request['result_path']).parent)
        self._refresh_view(); self._update_counters()
        if kind=='recover':self._set_status(self.tr('pickler.recovery_done','Recovery verified. Reload the folder to update the set.'))

    def _show_recovery(self,result):
        dialog=QDialog(self); dialog.setWindowTitle(self.tr('pickler.recovery','Review interrupted transfers…'))
        layout=QVBoxLayout(dialog); listing=QListWidget()
        listing.setToolTip(self.tr('pickler.recovery_list_tip','Choose the pending operation to resolve; modified or ambiguous files remain protected.'))
        for row in result['records']:
            request=row.get('request')
            text=(request['source_path']+' → '+request['result_path']) if request else row['error']
            item=QListWidgetItem(text); item.setToolTip(text); item.setData(Qt.UserRole,row)
            listing.addItem(item)
        layout.addWidget(listing)
        if not result['records']:
            layout.addWidget(QLabel(self.tr('pickler.recovery_empty','No pending transfers in this reviewed set.')))
        if result['truncated']:
            layout.addWidget(QLabel(self.tr('pickler.recovery_partial','The review limit was reached; only this bounded set is shown.')))
        restore=QPushButton(self.tr('pickler.recovery_restore','Resolve selected operation'))
        restore.setToolTip(self.tr('pickler.recovery_restore_tip','A pending transfer restores the source and preserves current curation. Copy undo resumes only for unchanged files. Confirmed transfers are retained.'))
        restore.setEnabled(False); layout.addWidget(restore)
        listing.currentItemChanged.connect(lambda current,_:restore.setEnabled(current is not None and 'request' in current.data(Qt.UserRole)))
        chosen=[]
        def select():
            if self._closing:return
            row=listing.currentItem().data(Qt.UserRole)
            question=self.tr('pickler.recovery_confirm','Resolve this operation? A pending transfer restores the source; an interrupted copy undo removes only its verified unchanged copy.')
            confirmation=QMessageBox(QMessageBox.Question,self.tr('pickler.recovery','Review interrupted transfers…'),question,QMessageBox.Yes|QMessageBox.No,dialog)
            self._dialogs.append(confirmation)
            try:answer=confirmation.exec()
            finally:self._dialogs.remove(confirmation); confirmation.deleteLater()
            if answer==QMessageBox.Yes and not self._closing:
                chosen.append(row['path']); dialog.accept()
        restore.clicked.connect(select)
        self._dialogs.append(dialog)
        try:dialog.exec()
        finally:self._dialogs.remove(dialog); dialog.deleteLater()
        if chosen and not self._closing:self._start_job('recover',(chosen[0],))

    def shutdown(self):
        if self._closing:return
        self._closing=True
        if self._worker:self._worker.stop()
        if self._preview:self._preview.close()
        for preview in self._retired_previews:preview.close()
        for dialog in tuple(self._dialogs):dialog.close()

    def shutdown_ready(self):
        return (self._worker is None and not self._pending_start and
            (self._preview is None or self._preview.shutdown_ready()) and
            all(preview.shutdown_ready() for preview in self._retired_previews) and not self._dialogs)

    def closeEvent(self,event):
        self.shutdown()
        if not self.shutdown_ready():event.ignore(); QTimer.singleShot(25,self,self.close)
        else:super().closeEvent(event)

    # ── Interfaz estándar (pipeline — fase 2) ─────────────────────────
    def load_images(self, paths: list) -> None:
        if self._closing or self._worker is not None:return
        self._start_job('images',(tuple(paths),))
