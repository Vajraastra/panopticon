"""
Vista del módulo Pickler.

Usa el StandardToolLayout de 3 paneles (biblia §4):
  • Sidebar (320px) : entrada (abrir carpeta + recientes) y ajustes
                      (acción, carpeta destino y contadores).
  • Canvas          : ayuda de teclas arriba, imagen escalada a 1024px lado largo
                      con franja de color según el modo, y debajo nombre + tipo + posición.
  • Action Bar      : botones de marcar/omitir/aplicar y, debajo, la ficha del archivo
                      (tipo, resolución, peso, caption; contenido si es comprimido)
                      junto al resumen del conjunto.

El panel selecciona la acción principal: mover, copiar o descartar.
D marca para descartar. Aplicar o cerrar confirma las decisiones pendientes.
Los ZIP/RAR se revisan como un archivo más: la vista previa muestra su primera imagen.
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic

from PySide6.QtCore import Qt, QSize, QSettings, Signal, QTimer
from PySide6.QtGui import QAction, QKeyEvent, QMouseEvent, QPainter, QPixmap, QResizeEvent, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from core.archive_preview import MAX_MEMBER_BYTES, is_archive
from core.components.file_badge import FileBadge
from core.components.image_details import LOADING, PLACEHOLDER, ImageDetails, format_bytes
from core.components.standard_layout import StandardToolLayout
from core.components.ui_roles import hint, primary, section, set_role
from core.file_kinds import ANIMATED, ARCHIVE, category, file_kind, format_size
from core.paths import CachePaths
from core.image_preview import ImagePreview, load_archive_preview, load_preview, _identity as preview_identity
from core.metadata.bundle import MetadataBundle
from core.native_threads import native_done, start_worker
from ..logic.worker import PicklerWorker

_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".gif", ".bmp", ".tiff", ".tif", ".avif",
}

_MAX_RECENT = 10
_MAX_SIDE = 1024  # Regla de 1024px (biblia §4)


def _load_pickler_preview(path, metadata_loader, cancel_event):
    if is_archive(path):
        # Un comprimido no lleva caption: la transferencia tampoco lo busca.
        preview = load_archive_preview(path, cancel_event)
        if preview is not None:
            preview['caption_identity'] = None
        return preview
    preview = load_preview(path, metadata_loader, cancel_event)
    if preview is not None and not preview['image'].isNull():
        preview['caption_identity'] = preview_identity(Path(path).with_suffix('.txt'))
    return preview


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


class _ElidedLabel(QLabel):
    """Etiqueta de una línea que recorta el centro con «…» si no cabe (el texto completo
    queda en ``text()`` y en el tooltip)."""

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle, rect.width())
        self.style().drawItemText(painter, rect, int(self.alignment()), self.palette(),
                                  self.isEnabled(), text, self.foregroundRole())


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

    def __init__(self, help_widget: QWidget | None = None, tr=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.mode_stripe = QWidget()
        self.mode_stripe.setFixedHeight(4)
        root.addWidget(self.mode_stripe)

        if help_widget is not None:
            holder = QHBoxLayout()
            holder.setContentsMargins(12, 10, 12, 4)
            holder.addWidget(help_widget)
            root.addLayout(holder)

        self.img = _ScaledLabel()
        self.img.setWordWrap(True)
        root.addWidget(self.img, stretch=1)

        # Bajo la imagen: nombre (destacado) · distintivo de tipo · posición en el conjunto.
        info = QFrame()
        info.setFixedHeight(38)
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(12, 0, 12, 0)
        info_layout.setSpacing(10)
        info_layout.addStretch(1)
        self.lbl_info = set_role(_ElidedLabel(""), 'emphasis')
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_info.setTextFormat(Qt.TextFormat.PlainText)
        info_layout.addWidget(self.lbl_info)
        self.badge = FileBadge(tr)
        info_layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_position = hint("")
        self.lbl_position.setWordWrap(False)
        info_layout.addWidget(self.lbl_position)
        info_layout.addStretch(1)
        root.addWidget(info)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            event.accept()
            return
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
        self._primary_action = 'discard'
        self._kept = 0
        self._discarded = 0
        self._undo_stack: list[dict] = []
        self._staged: list[dict] = []
        self._pending_counts = dict(move=0, copy=0, discard=0)
        self._reviewed_file = None
        self._apply_recovery = None
        self._after_apply = None
        self._changed_library_folders: dict[str, None] = {}
        self._flush_library_when_idle = False
        self._decision_ready = False
        self._visible_since = None
        self._shortcuts = []

        # Carpeta del set actual y override de destino. Por defecto los archivos
        # van a 'picked/' y 'discarded/' DENTRO del set; "Elegir destino" sustituye
        # solo el destino de conservar y se reinicia al abrir otro set.
        self._set_folder: Path | None = None
        self._custom_dest: Path | None = None
        self._set_total_bytes: int | None = None
        self._set_summary_cache = None

        # Construir los 3 paneles y armar el layout estándar
        self._canvas = _Canvas(self._build_help(), self.tr)
        sidebar = self._build_sidebar()
        action_bar = self._build_action_bar()
        super().__init__(
            self._canvas,
            sidebar_widget=sidebar,
            bottom_widget=action_bar,
            theme_manager=self._theme,
            event_bus=self.context.get('event_bus'),
            tool_header=("🥒", self.tr("tool.pickler.name", "Pickler"),
                         self.tr("tool.pickler.desc",
                                 "Mark images to move, copy, or discard, then apply the decisions together.")),
        )

        self._job_timer = QTimer(self)
        self._job_timer.setInterval(25)
        self._job_timer.timeout.connect(self._poll_job)
        self._new_preview()
        self._decision_timer = QTimer(self)
        self._decision_timer.setSingleShot(True)
        self._decision_timer.timeout.connect(self._arm_decisions)
        for sequence, callback in (
            ('D', lambda: self._mark_current('discard')),
            ('Right', self._next), ('Space', self._next),
            ('Left', self._prev), ('Ctrl+Z', self._undo),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

        # Señales del canvas
        self._canvas.skip_requested.connect(self._next)
        self._canvas.prev_requested.connect(self._prev)
        self._canvas.undo_requested.connect(self._undo)
        self._canvas.context_menu_requested.connect(self._show_context_menu)

        self._load_recent()
        self._update_primary_action()
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
            "Choose a folder to review. Apply or undo pending decisions before switching sets."))
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

        # Acción del botón principal
        lay.addWidget(self._section_label(self.tr("pickler.settings", "SETTINGS")))
        lay.addWidget(self._section_label(self.tr('tool.pickler.action_choice', 'Action to mark')))
        row = QHBoxLayout()
        self._rb_move = QRadioButton(self.tr('pickler.move', 'Move'))
        self._rb_move.setToolTip(self.tr('pickler.move_tip',
            'Applying a move takes the image and its caption out of the source folder.'))
        self._rb_copy = QRadioButton(self.tr('pickler.copy', 'Copy'))
        self._rb_copy.setToolTip(self.tr('pickler.copy_tip',
            'Applying a copy keeps the original and puts a copy in the selected destination.'))
        self._rb_discard = QRadioButton(self.tr('tool.pickler.discard', 'Discard'))
        self._rb_discard.setToolTip(self.tr('tool.pickler.discard_tip',
            'Applying a discard moves the image and its caption into the source’s discarded folder.'))
        grp = QButtonGroup(panel)
        self._action_buttons = {'move': self._rb_move, 'copy': self._rb_copy, 'discard': self._rb_discard}
        self._rb_discard.setChecked(True)
        for action, button in self._action_buttons.items():
            grp.addButton(button)
            button.toggled.connect(lambda checked, action=action: self._on_action_toggled(action, checked))
            row.addWidget(button)
        row.addStretch()
        lay.addLayout(row)

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

        lay.addStretch()

        # Contadores
        self._lbl_counters = QLabel()
        lay.addWidget(self._lbl_counters)
        self._lbl_pending = QLabel()
        self._lbl_pending.setWordWrap(True)
        lay.addWidget(self._lbl_pending)

        # Footer: abrir destino / papelera / deshacer
        self._btn_undo = QPushButton()
        self._btn_undo.setToolTip(self.tr(
            "pickler.undo_tip",
            "Undo the newest pending mark immediately. After applying, undo restores a verified "
            "move or removes an unchanged copy. Loading another set clears the history."))
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
        self._btn_recovery.clicked.connect(self._review_transfers)
        lay.addWidget(self._btn_recovery)

        self._update_dest_label()
        self._update_counters()
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        root = QVBoxLayout(bar)
        root.setContentsMargins(0, 0, 0, 0)
        lay = QHBoxLayout()
        root.addLayout(lay)
        self._btn_primary = QPushButton()
        self._btn_primary.setToolTip(self.tr(
            "pickler.action_tip",
            "Marks the displayed image and advances. Apply decisions finalizes the image and caption."))
        self._btn_primary.clicked.connect(self._do_primary)
        self._btn_skip = QPushButton(self.tr("pickler.action_skip", "→  Skip"))
        self._btn_skip.setToolTip(self.tr(
            "pickler.action_skip_tip",
            "Moves to the next image without touching it or deciding anything about it."))
        self._btn_skip.clicked.connect(self._next)
        self._btn_apply = primary(QPushButton())
        self._btn_apply.setToolTip(self.tr('tool.pickler.apply_tip',
            'Run the marked moves, copies and discards in order. Closing Pickler also applies pending decisions.'))
        self._btn_apply.clicked.connect(self._toggle_apply)
        self._apply_bar = QProgressBar()
        self._apply_bar.setMinimumWidth(180)
        self._apply_bar.setFormat(self.tr('tool.pickler.apply_progress', 'Applying · %v/%m · %p%'))
        self._apply_bar.hide()
        lay.addWidget(self._btn_primary)
        lay.addWidget(self._btn_skip)
        lay.addStretch(1)
        lay.addWidget(self._apply_bar, 1)
        lay.addWidget(self._btn_apply)

        # Debajo de los botones: ficha del archivo (izquierda) y resumen del conjunto (derecha).
        data = QHBoxLayout()
        data.setContentsMargins(0, 8, 0, 0)
        data.setSpacing(24)
        root.addLayout(data)
        self._details = ImageDetails(self.tr)
        self._details.lbl_name.hide()  # el nombre ya va destacado bajo la imagen
        set_role(self._details.lbl_size, 'emphasis')
        self._row_type = self._details.add_row(self.tr('tool.pickler.details_type', 'Type'))
        self._row_caption = self._details.add_row(
            self.tr('tool.pickler.details_caption', 'Caption'),
            self.tr('tool.pickler.details_caption_tip', 'The .txt with the same name travels with the image.'))
        self._row_content = self._details.add_row(
            self.tr('tool.pickler.details_content', 'Contents'),
            self.tr('tool.pickler.details_content_tip', 'Images inside the archive / total files.'))
        self._row_unpacked = self._details.add_row(
            self.tr('tool.pickler.details_unpacked', 'Uncompressed'),
            self.tr('tool.pickler.details_unpacked_tip', 'Combined size of its images once extracted.'))
        self._row_shown = self._details.add_row(
            self.tr('tool.pickler.details_shown', 'Preview'),
            self.tr('tool.pickler.details_shown_tip', 'Which image of the archive is shown.'))
        for row in (self._row_shown, self._row_content, self._row_unpacked):
            row.setWordWrap(True)
        data.addWidget(self._details, 3)

        summary = QVBoxLayout()
        summary.setSpacing(4)
        summary.addWidget(section(self.tr('tool.pickler.set_title', 'Set')))
        self._lbl_set_summary = QLabel()
        self._lbl_set_summary.setWordWrap(True)
        summary.addWidget(self._lbl_set_summary)
        self._lbl_set_weight = hint('')
        summary.addWidget(self._lbl_set_weight)
        self._lbl_undo_next = hint('')
        self._lbl_undo_next.setTextFormat(Qt.TextFormat.PlainText)
        summary.addWidget(self._lbl_undo_next)
        self._lbl_keyboard_state = set_role(QLabel(), 'emphasis')
        self._lbl_keyboard_state.setWordWrap(True)
        summary.addWidget(self._lbl_keyboard_state)
        summary.addStretch(1)
        data.addLayout(summary, 2)
        self._set_row_archive(False)
        return bar

    def _build_help(self) -> QWidget:
        """Recuadro de uso sobre la imagen: teclas como chapas y, debajo, dónde va cada cosa."""
        frame = set_role(QFrame(), 'help')
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        keys = QHBoxLayout()
        keys.setSpacing(6)
        for key, name, default in (
            ('D', 'tool.pickler.help_discard', 'Mark for discard'),
            ('←', 'tool.pickler.help_prev', 'Previous'),
            (self.tr('tool.pickler.help_skip_keys', '→ / Space'), 'tool.pickler.help_skip', 'Skip'),
            ('Ctrl+Z', 'tool.pickler.help_undo', 'Undo last mark'),
        ):
            keys.addWidget(set_role(QLabel(key), 'key'))
            keys.addWidget(QLabel(self.tr(name, default)))
            keys.addSpacing(12)
        keys.addStretch(1)
        lay.addLayout(keys)
        self._lbl_keys = hint('')
        self._lbl_keys.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self._lbl_keys)
        self._lbl_recover = hint(self.tr(
            'tool.pickler.recover_help',
            'Ctrl+Z removes the latest mark. Apply decisions or close Pickler to carry out the pending actions.'))
        lay.addWidget(self._lbl_recover)
        self._update_keyboard_help()
        return frame

    def _section_label(self, text: str) -> QLabel:
        return section(text)

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
        base = self._source_folder()
        return (base / "picked") if base else None

    def _discard_dest(self) -> Path | None:
        """Destino para descartar: siempre 'discarded/' en el set/carpeta origen."""
        base = self._source_folder()
        return (base / "discarded") if base else None

    def _source_folder(self) -> Path | None:
        if self._set_folder:
            return self._set_folder
        if self._images:
            return self._images[self._index].parent
        if self._staged:
            return self._staged[-1]['path'].parent
        if self._last_op:
            return Path(self._last_op['original']).parent
        return None

    def _update_dest_label(self) -> None:
        dest = self._keep_dest()
        if self._custom_dest:
            self._lbl_dest.setText(str(self._custom_dest))
        elif dest:
            self._lbl_dest.setText(str(dest))
        else:
            self._lbl_dest.setText(self.tr("pickler.dest_default", "<set>/picked"))
        self._update_keyboard_help()

    def _update_keyboard_help(self) -> None:
        if not hasattr(self, '_lbl_keys'):
            return
        discarded = str(self._discard_dest() or 'discarded/')
        self._lbl_keys.setText(self.tr(
            'tool.pickler.keyboard_where',
            'Discards go to {discarded}. To move or copy, choose the action in the sidebar and use the main button.'
        ).format(discarded=discarded))

    # ── API pública ───────────────────────────────────────────────────
    def load_folder(self, folder: Path) -> None:
        self._start_job('scan',(Path(folder),))

    def _on_action_toggled(self, action, checked):
        if checked:
            self._primary_action = action
            self._update_primary_action()

    def _update_primary_action(self) -> None:
        label = self._action_buttons[self._primary_action].text()
        if self._primary_action == 'discard':
            color = self._color('accent_warning', '#ff3333')
            label += ' (D)'
        else:
            color = self._color('accent_success', '#00ff66')
        self._btn_primary.setText(label)
        self._canvas.mode_stripe.setStyleSheet(f"background-color: {color};")
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
        self._mark_current(self._primary_action)

    def _mark_current(self, action):
        if (not self._decision_ready or self._closing or self._worker is not None
                or self._apply_recovery is not None or not self._images or self._reviewed_file is None):
            return
        path, identity, caption = self._reviewed_file
        if path != self._images[self._index] or identity is None:
            return
        destination = self._discard_dest() if action == 'discard' else self._keep_dest()
        self._staged.append(dict(path=path, destination=destination, action=action, index=self._index,
                                 reviewed_identity=identity, reviewed_caption=caption))
        self._pending_counts[action] += 1
        self._consume_current()

    def _restore_mark(self, decision):
        index = min(decision['index'], len(self._images))
        self._images.insert(index, decision['path'])
        self._index = index

    def _consume_current(self) -> None:
        """Retira del recorrido una imagen marcada y muestra la siguiente."""
        self._images.pop(self._index)
        if self._index >= len(self._images):
            self._index = max(0, len(self._images) - 1)
        self._refresh_view()
        self._update_counters()

    # ── Deshacer ──────────────────────────────────────────────────────
    def _undo(self) -> None:
        if self._closing or self._worker is not None or self._apply_recovery is not None:
            return
        if self._staged:
            decision = self._staged.pop()
            self._pending_counts[decision['action']] -= 1
            self._restore_mark(decision)
            self._refresh_view()
            self._update_counters()
        elif self._undo_stack:
            self._start_job('undo',(self._undo_stack[-1],))

    @property
    def _last_op(self):
        return self._undo_stack[-1] if self._undo_stack else None

    def _arm_decisions(self):
        if (self._closing or self._worker is not None or self._visible_since is None
                or self._apply_recovery is not None):
            return
        remaining = 0.3 - (monotonic() - self._visible_since)
        if remaining > 0:
            self._decision_timer.start(max(1, int(remaining * 1000) + 1))
            return
        self._decision_ready = True
        self._lbl_keyboard_state.setText(self.tr('tool.pickler.keys_ready', 'D ready to mark for discard.'))
        self._set_busy(False)

    def _remember_library_changes(self, kind, args, result, *, notify_previews=True) -> None:
        if kind == 'recover':
            request = result['request']
            paths = (request['source_path'], request['result_path'])
        elif kind in ('transfer', 'undo'):
            operation = args[0] if kind == 'undo' else result
            paths = (operation['original'], operation['current'])
        else:
            return
        folders = tuple(str(Path(path).parent) for path in paths)
        self._changed_library_folders.update(dict.fromkeys(folders))
        if self._event_bus and notify_previews:
            self._event_bus.publish('library_previews_changed', folders)

    def flush_library_changes(self) -> None:
        """Announce confirmed folders once the current operation has settled."""
        if self._worker is not None:
            self._flush_library_when_idle = True
            return
        self._flush_library_when_idle = False
        folders, self._changed_library_folders = self._changed_library_folders, {}
        if self._event_bus:
            for folder in folders:
                self._event_bus.publish('library_changed', folder)

    # ── Menú contextual (acción primaria error-proof) ─────────────────
    def _show_context_menu(self, pos) -> None:
        if self._closing or self._worker is not None:return
        if not self._images:
            return
        menu = QMenu(self)
        act = QAction(self._btn_primary.text(), menu)
        act.triggered.connect(self._do_primary)
        act.setEnabled(self._decision_ready)
        skip = QAction(self.tr("pickler.action_skip", "→  Skip"), self)
        skip.triggered.connect(self._next)
        menu.addAction(act)
        menu.addAction(skip)
        menu.exec(pos)

    # ── Refresco de UI ────────────────────────────────────────────────
    def _refresh_view(self) -> None:
        if self._closing or self._worker is not None:return
        self._decision_ready = False
        self._reviewed_file = None
        self._visible_since = None
        self._decision_timer.stop()
        self._update_keyboard_help()
        self._lbl_keyboard_state.setText(self.tr('tool.pickler.keys_wait', 'Marking is available 0.3 s after the image appears.'))
        self._set_busy(False)
        self._update_set_summary()
        if not self._images:
            self._lbl_keyboard_state.setText('')
            self._canvas.img.set_source(None)
            text = (self.tr('tool.pickler.marking_done', 'Apply the pending decisions to finish.')
                    if self._staged else self.tr('pickler.no_images', 'No images in this folder'))
            self._canvas.img.setText(text)
            self._canvas.lbl_position.setText('')
            self._details.clear(PLACEHOLDER)
            for row in (self._row_type, self._row_caption):
                row.setText(PLACEHOLDER)
            self._set_row_archive(False)
            self._set_status(""); return
        path=self._images[self._index]
        self._canvas.img.set_source(None)
        self._set_status(path.name, path)
        self._canvas.lbl_position.setText(f"{self._index + 1} / {len(self._images)}")
        self._show_file_details(path)
        if self._preview:self._preview_ticket=self._preview.request(str(path))

    def _set_status(self, text: str, path: Path | None = None) -> None:
        """Línea bajo la imagen: el nombre del archivo (con su distintivo) o un aviso."""
        self._canvas.lbl_info.setText(text)
        self._canvas.lbl_info.setToolTip(str(path) if path is not None else text)
        self._canvas.badge.set_path(path)
        self._canvas.lbl_info.update()

    # ── Ficha del archivo y resumen del conjunto ──────────────────────
    def _set_row_archive(self, visible: bool) -> None:
        for row in (self._row_content, self._row_unpacked, self._row_shown):
            self._details.set_row_visible(row, visible)

    def _type_text(self, path) -> str:
        icon, label, kind = file_kind(path)
        if kind == ARCHIVE:
            return self.tr('tool.pickler.type_archive', '{icon} {label} archive').format(icon=icon, label=label)
        if kind == ANIMATED:
            return self.tr('tool.pickler.type_animated', '{icon} {label} image (first frame)').format(
                icon=icon, label=label)
        return self.tr('tool.pickler.type_image', '{icon} {label} image').format(icon=icon, label=label)

    def _show_file_details(self, path: Path) -> None:
        """Carpeta y tipo ya; resolución, peso y contenido llegan con la vista previa."""
        self._details.show_path(str(path))
        self._row_type.setText(self._type_text(path))
        archive = is_archive(path)
        self._set_row_archive(archive)
        self._details.set_row_visible(self._row_caption, not archive)  # un comprimido no lleva caption
        self._row_caption.setText(LOADING)
        for row in (self._row_content, self._row_unpacked, self._row_shown):
            row.setText(LOADING)

    def _caption_text(self, identity) -> str:
        if identity is None:
            return self.tr('tool.pickler.caption_none', 'None')
        return self.tr('tool.pickler.caption_yes', 'Yes · {size}').format(size=format_size(identity[2]))

    def _archive_problem_text(self, problem) -> str:
        texts = {
            'encrypted': ('tool.pickler.archive_encrypted', 'Password protected: its contents cannot be read.'),
            'multivolume': ('tool.pickler.archive_multivolume', 'Split into volumes: parts are not opened separately.'),
            'no_images': ('tool.pickler.archive_no_images', 'It contains no images.'),
            'too_large': ('tool.pickler.archive_too_large', 'Its first image is over {limit}; it is not shown.'),
            'preview_pixel_limit': ('tool.pickler.archive_pixels', 'Its first image is too large to display.'),
            'undecodable': ('tool.pickler.archive_undecodable', 'Its first image could not be decoded.'),
            'archive_entry_limit': ('tool.pickler.archive_entries', 'It has too many entries for a preview.'),
        }
        key, default = texts.get(problem, ('tool.pickler.archive_damaged', 'Damaged or incomplete archive.'))
        return self.tr(key, default).format(limit=format_bytes(MAX_MEMBER_BYTES))

    def _show_archive_details(self, info) -> None:
        if info.get('format') is None:  # dañado: no se pudo ni listar
            for row in (self._row_content, self._row_unpacked):
                row.setText(PLACEHOLDER)
        else:
            self._row_content.setText(self.tr(
                'tool.pickler.archive_content', 'Images: {images} · Files: {entries}').format(
                images=info['image_count'], entries=info['entry_count']))
            self._row_unpacked.setText(format_bytes(info['unpacked_bytes']) if info['image_count'] else PLACEHOLDER)
        if info.get('problem'):
            self._row_shown.setText(self._archive_problem_text(info['problem']))
        elif info.get('solid'):
            self._row_shown.setText(self.tr(
                'tool.pickler.archive_shown_solid',
                '{name} — first stored image (solid RAR: reading by name would unpack it all)').format(
                name=info['first_name']))
        else:
            self._row_shown.setText(self.tr('tool.pickler.archive_shown', '{name} — first by name').format(
                name=info['first_name']))

    def _update_set_summary(self) -> None:
        count = len(self._images)
        cache = self._set_summary_cache
        if cache is None or cache[0] is not self._images or cache[1] != count:
            archives = sum(1 for path in self._images if category(path) == ARCHIVE)
            self._set_summary_cache = cache = (self._images, count, archives)
        archives = cache[2]
        self._lbl_set_summary.setText(self.tr(
            'tool.pickler.set_summary', 'To review: {count} · Images: {images} · Archives: {archives}').format(
            count=count, images=count - archives, archives=archives))
        self._lbl_set_weight.setText(self.tr(
            'tool.pickler.set_weight', 'Folder size when opened: {size}').format(
            size=format_bytes(self._set_total_bytes)) if self._set_total_bytes is not None else '')

    def _update_counters(self) -> None:
        kept = self.tr("pickler.kept", "Kept")
        disc = self.tr("pickler.deleted", "Discarded")
        self._lbl_counters.setText(
            f"✓ {kept}: {self._kept}    🗑 {disc}: {self._discarded}")
        self._lbl_pending.setText(self.tr('tool.pickler.pending_counts',
            'Pending · Move {move} · Copy {copy} · Discard {discard}').format(**self._pending_counts))
        if hasattr(self, '_btn_undo'):
            count = len(self._undo_stack) + len(self._staged)
            self._btn_undo.setText(self.tr('tool.pickler.undo_count', 'Undo ({count}) — Ctrl+Z').format(count=count))
        if hasattr(self, '_lbl_undo_next'):
            path = self._staged[-1]['path'] if self._staged else self._last_op['original'] if self._last_op else None
            self._lbl_undo_next.setText(self.tr('tool.pickler.undo_next', 'Next Ctrl+Z: {path}').format(
                path=path) if path is not None else '')
        self._update_apply_button()

    def _update_apply_button(self):
        if not hasattr(self, '_btn_apply'):
            return
        applying = self._worker is not None and self._job['kind'] == 'apply'
        self._lbl_pending.setVisible(not applying)
        if applying:
            label = self.tr('tool.pickler.apply_stop', 'Stop batch')
            tip = self.tr('tool.pickler.apply_stop_tip', 'Keep confirmed changes and stop before the next decision.')
        else:
            label = self.tr('tool.pickler.apply', 'Apply decisions ({count})').format(count=len(self._staged))
            tip = self.tr('tool.pickler.apply_tip',
                'Run the marked moves, copies and discards in order. Closing Pickler also applies pending decisions.')
        self._btn_apply.setText(label)
        self._btn_apply.setToolTip(tip)
        self._btn_apply.setEnabled(not self._closing and (applying or (
            self._worker is None and bool(self._staged) and self._apply_recovery is None)))

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
        self._preview=ImagePreview(lambda _: MetadataBundle(),self, loader=_load_pickler_preview)
        preview=self._preview
        preview.result.connect(lambda ticket,value,error: self._preview_ready(preview,ticket,value,error),Qt.QueuedConnection)

    def _preview_ready(self,preview,ticket,value,error):
        if self._closing or self._worker is not None or preview is not self._preview or ticket != self._preview_ticket:return
        archive = value.get('archive') if value is not None and error is None else None
        if value is not None and error is None:
            self._details.set_bytes(value.get('bytes'))
            self._details.set_dimensions(value.get('width'), value.get('height'))
            if archive is None:
                self._row_caption.setText(self._caption_text(value.get('caption_identity')))
            else:
                self._show_archive_details(archive)
        else:
            self._details.set_unavailable()
            for row in (self._row_caption, self._row_content, self._row_unpacked, self._row_shown):
                row.setText(PLACEHOLDER)
        if archive is not None and value['image'].isNull():
            # Sin vista previa, pero es un archivo real: se puede marcar igual.
            self._canvas.img.set_source(None)
            self._canvas.img.setText(self.tr(
                'tool.pickler.archive_no_preview', '📦  No preview\n{reason}\nYou can still mark it.').format(
                reason=self._archive_problem_text(archive.get('problem'))))
            self._arm_reviewed(value)
        elif error is not None or value is None or value['image'].isNull():
            self._canvas.img.set_source(None)
            self._canvas.img.setText(self.tr('pickler.cant_display','Cannot display:'))
            self._lbl_keyboard_state.setText(self.tr('tool.pickler.keys_unavailable', 'Image could not be displayed. Skip to review the next image.'))
        else:
            self._canvas.img.set_source(QPixmap.fromImage(value['image']))
            self._arm_reviewed(value)

    def _arm_reviewed(self, value):
        self._reviewed_file = (Path(value['path']), value.get('identity'), value.get('caption_identity'))
        self._visible_since = monotonic()
        self._decision_timer.start(300)

    def _set_busy(self,busy):
        enabled=not busy and not self._closing
        for name in ('_btn_open','_recent_list','_btn_dest','_rb_move','_rb_copy','_rb_discard','_btn_recovery'):
            widget=getattr(self,name,None)
            if widget:widget.setEnabled(enabled)
        self._canvas.setEnabled(enabled)
        self._btn_primary.setEnabled(enabled and bool(self._images) and self._decision_ready)
        self._btn_skip.setEnabled(enabled and bool(self._images))
        self._btn_undo.setEnabled(enabled and self._apply_recovery is None and bool(self._staged or self._undo_stack))
        self._update_apply_button()

    def _review_transfers(self):
        args = (self._apply_recovery,) if self._apply_recovery is not None else ()
        self._start_job('review', args)

    def _toggle_apply(self):
        if self._worker is not None and self._job['kind'] == 'apply':
            self._worker.stop()
            self._btn_apply.setEnabled(False)
        else:
            self._apply_pending()

    def _apply_pending(self):
        if self._closing or self._worker is not None or not self._staged:
            return
        if self._apply_recovery is not None:
            self._set_status(self.tr('tool.pickler.apply_review_required',
                'Resolve the interrupted transfer before applying more decisions.'))
            return
        decisions = tuple((item['path'], item['destination'], item['action'], item['reviewed_identity'],
                           item['reviewed_caption']) for item in self._staged)
        self._start_job('apply', (decisions,))

    def prepare_close(self, retry):
        """Apply pending marks and retry closing only after confirmation."""
        if not self._staged:
            return True
        if self._apply_recovery is not None:
            self._set_status(self.tr('tool.pickler.apply_review_required',
                'Resolve the interrupted transfer before applying more decisions.'))
            return False
        if self._worker is None or self._job['kind'] == 'apply':
            self._after_apply = retry
            self._apply_pending()
        return False

    def _start_job(self,kind,args):
        if self._closing or self._worker is not None:return
        if kind in ('scan', 'images') and self._staged:
            self._set_status(self.tr('tool.pickler.apply_before_load',
                'Apply or undo pending decisions before opening another set.'))
            return
        self._decision_ready = False
        self._visible_since = None
        self._decision_timer.stop()
        if self._preview:
            self._preview.close(); self._retired_previews.append(self._preview); self._preview=None
        self._job=dict(kind=kind,args=args,index=self._index)
        self._delivery=None
        self._worker=PicklerWorker(kind,self._service,args,journal_root=self.context.get('pickler_journal_root'))
        self._worker.finished_signal.connect(self._job_delivered,Qt.QueuedConnection)
        if kind == 'apply':
            self._worker.progress_signal.connect(self._apply_progress, Qt.QueuedConnection)
        self._pending_start=True
        self._set_busy(True)
        self._lbl_keyboard_state.setText(self.tr('tool.pickler.keys_busy', 'Operation in progress. Keys are ignored; no actions are queued.'))
        if kind == 'apply':
            self._set_status('')
            self._apply_progress(0, len(args[0]))
        else:
            self._set_status(self.tr('pickler.processing', 'Processing…'))
        self._job_timer.start(); self._poll_job()

    def _apply_progress(self, completed, total):
        if self._worker is not None and self._job['kind'] == 'apply':
            self._apply_bar.setRange(0, total)
            self._apply_bar.setValue(completed)
            self._apply_bar.show()

    def _job_delivered(self,result,error):
        self._delivery=(result,error); self._poll_job()

    def _count_applied(self, action):
        self._pending_counts[action] -= 1
        if action == 'discard':
            self._discarded += 1
        else:
            self._kept += 1

    def _finish_apply(self, result, error):
        self._apply_bar.hide()
        result = result or {}
        completed = result.get('completed', [])
        for decision, operation in zip(self._staged, completed):
            operation['index'] = decision['index']
            self._undo_stack.append(operation)
            self._count_applied(operation['action'])
            self._remember_library_changes('transfer', (), operation, notify_previews=False)
        del self._staged[:len(completed)]
        if completed and self._event_bus:
            self._event_bus.publish('library_previews_changed', tuple(self._changed_library_folders))
        if result.get('recovery_required'):
            self._apply_recovery = result['recovery_journal']
        self.flush_library_changes()
        retry, self._after_apply = self._after_apply, None
        if self._closing:
            return
        self._new_preview()
        self._refresh_view()
        self._update_counters()
        if error is None:
            self._set_status(self.tr('tool.pickler.apply_done',
                '{count} decisions applied.').format(count=len(completed)))
            if retry is not None:
                QTimer.singleShot(0, self, retry)
        else:
            self._show_apply_error(result.get('failed_path'), error, len(completed))

    def _source_check_message(self, path, error):
        if error == 'transfer_source_lookup_busy':
            message = self.tr('tool.pickler.source_lookup_busy',
                'The library changed while checking {file}. Try again.')
        else:
            message = self.tr('tool.pickler.source_identity_review',
                'The library identity of {file} needs review. The file is still in place.')
        return message.format(file=Path(path).name)

    def _show_apply_error(self, path, error, completed):
        if self._apply_recovery is not None:
            detail = self.tr('tool.pickler.apply_review_required',
                'Resolve the interrupted transfer before applying more decisions.')
        elif error == 'pickler_cancelled':
            detail = self.tr('tool.pickler.apply_stopped', 'Application stopped.')
        elif path and error in ('path_identity_review_required', 'transfer_source_lookup_busy'):
            detail = self._source_check_message(path, error)
            path = None
        elif error in ('pickler_reviewed_image_changed', 'pickler_reviewed_caption_changed'):
            detail = self.tr('tool.pickler.apply_source_changed',
                'The image or caption changed after review. Undo the mark and review the current file.')
        else:
            detail = error
        if path:
            detail = f'{Path(path).name} · {detail}'
        self._set_status(self.tr('tool.pickler.apply_incomplete',
            '{completed} applied · {pending} pending. {detail}').format(
                completed=completed, pending=len(self._staged), detail=detail))

    def _settle_apply_recovery(self, path, result):
        if self._apply_recovery is None or Path(path) != Path(self._apply_recovery):
            return
        self._apply_recovery = None
        if result['status'] == 'already_confirmed':
            decision = self._staged.pop(0)
            self._count_applied(decision['action'])
        elif result['status'] == 'source_restored':
            self._staged[0].update(reviewed_identity=result['reviewed_identity'],
                                   reviewed_caption=result['reviewed_caption'])

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
        kind=job['kind']
        if kind == 'apply':
            self._finish_apply(result, error)
            return
        if error is None:
            self._remember_library_changes(kind, job['args'], result)
        if self._closing or self._flush_library_when_idle:
            self.flush_library_changes()
        if self._closing:return
        self._new_preview(); self._set_busy(False)
        if error is not None:
            self._refresh_view()
            if kind in ('transfer', 'undo') and error in ('path_identity_review_required', 'transfer_source_lookup_busy'):
                path = job['args'][0]['current'] if kind == 'undo' else job['args'][0]
                self._set_status(self._source_check_message(path, error))
            else:
                self._set_status(self.tr('pickler.error_move','Error: {}').format(error))
            return
        if kind in ('scan','images'):
            self.flush_library_changes()
            self._images=result['images']; self._set_folder=result['folder']; self._custom_dest=None
            self._set_total_bytes=result.get('total_bytes')
            self._index=0; self._undo_stack.clear(); self._kept=0; self._discarded=0; self._update_dest_label()
        elif kind=='undo':
            previous=job['args'][0]; action=previous['request']['action']
            if action=='discard':self._discarded=max(0,self._discarded-1)
            else:self._kept=max(0,self._kept-1)
            index=min(previous['index'],len(self._images))
            self._images.insert(index,Path(previous['original'])); self._index=index
            self._undo_stack.pop()
        elif kind=='review':
            self._refresh_view(); self._show_recovery(result); return
        elif kind=='recover':
            self._undo_stack.clear()
            self._settle_apply_recovery(job['args'][0], result)
        self._refresh_view(); self._update_counters()
        if not self._images:
            self.flush_library_changes()
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
        self._decision_timer.stop()
        if self._worker:self._worker.stop()
        self.flush_library_changes()
        if self._preview:self._preview.close()
        for preview in self._retired_previews:preview.close()
        for dialog in tuple(self._dialogs):dialog.close()

    def shutdown_ready(self):
        return (self._worker is None and not self._pending_start and
            (self._preview is None or self._preview.shutdown_ready()) and
            all(preview.shutdown_ready() for preview in self._retired_previews) and not self._dialogs)

    def closeEvent(self,event):
        if not self._closing and not self.prepare_close(self.close):
            event.ignore()
            return
        self.shutdown()
        if not self.shutdown_ready():event.ignore(); QTimer.singleShot(25,self,self.close)
        else:super().closeEvent(event)

    # ── Interfaz estándar (pipeline — fase 2) ─────────────────────────
    def load_images(self, paths: list) -> None:
        if self._closing:return
        if self._worker is not None:
            dialog=QMessageBox(QMessageBox.Warning,self.tr('tool.pickler.name','Pickler'),
                self.tr('tool.pickler.selection_busy',
                    'The new image set was not loaded because Pickler is processing another operation. '
                    'Send the set again after it finishes.'),QMessageBox.Ok,self)
            dialog.button(QMessageBox.Ok).setToolTip(self.tr('tool.pickler.selection_busy_tip',
                'Close this notice. The current operation continues.'))
            self._dialogs.append(dialog)
            try:dialog.exec()
            finally:self._dialogs.remove(dialog); dialog.deleteLater()
            return
        self._start_job('images',(tuple(paths),))
