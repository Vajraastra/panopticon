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

from PySide6.QtCore import Qt, QSize, QSettings, Signal
from PySide6.QtGui import QAction, QKeyEvent, QMouseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QRadioButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths
from ..logic import file_ops

# Modos (locale-safe: nunca comparar por el texto del botón)
MODE_DELETE = 0
MODE_PICK = 1

_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".gif", ".bmp", ".tiff", ".tif", ".avif",
}

_MAX_RECENT = 10
_MAX_SIDE = 1024  # Regla de 1024px (biblia §4)


def _count_images(folder: Path) -> int:
    """Cuenta imágenes directamente dentro de folder (sin recursión)."""
    try:
        return sum(
            1 for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in _IMAGE_EXTS
        )
    except (PermissionError, OSError):
        return 0


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
        self._settings = QSettings("Panopticon", "Pickler")

        # Estado
        self._images: list[Path] = []
        self._index = 0
        self._mode = MODE_DELETE
        self._kept = 0
        self._discarded = 0
        self._last_op: tuple | None = None  # (action, original, current, index)

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
        self._btn_dest.clicked.connect(self._choose_dest)
        lay.addWidget(self._btn_dest)

        # Mover / Copiar
        lay.addWidget(self._section_label(self.tr("pickler.transfer_mode", "When keeping:")))
        row = QHBoxLayout()
        self._rb_move = QRadioButton(self.tr("pickler.move", "Move"))
        self._rb_copy = QRadioButton(self.tr("pickler.copy", "Copy"))
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
        self._btn_undo.clicked.connect(self._undo)
        lay.addWidget(self._btn_undo)
        self._btn_open_dest = QPushButton(self.tr("pickler.open_dest", "Open destination"))
        self._btn_open_dest.clicked.connect(self._open_dest)
        lay.addWidget(self._btn_open_dest)
        self._btn_open_disc = QPushButton(self.tr("pickler.open_discarded", "Open discarded"))
        self._btn_open_disc.clicked.connect(self._open_discarded)
        lay.addWidget(self._btn_open_disc)

        self._update_dest_label()
        self._update_counters()
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        self._btn_primary = QPushButton()
        self._btn_primary.clicked.connect(self._do_primary)
        self._btn_skip = QPushButton(self.tr("pickler.action_skip", "→  Skip"))
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
        if isinstance(recent, str):
            recent = [recent]
        self._recent_list.clear()
        valid: list[str] = []
        for path_str in recent:
            p = Path(path_str)
            if not p.exists():
                continue
            count = _count_images(p)
            item = QListWidgetItem(f"{p.name}  [{count}]")
            item.setData(Qt.ItemDataRole.UserRole, str(p))
            item.setToolTip(str(p))
            self._recent_list.addItem(item)
            valid.append(path_str)
        if valid != recent:
            self._settings.setValue("recent_folders", valid)
        has = self._recent_list.count() > 0
        self._recent_label.setVisible(has)
        self._recent_list.setVisible(has)

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
        folder = Path(item.data(Qt.ItemDataRole.UserRole))
        if folder.exists():
            self._open_folder(folder)

    # ── Selección de carpeta / destino ────────────────────────────────
    def _browse(self) -> None:
        last = self._settings.value("last_dir", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(
            self, self.tr("pickler.open_folder", "Open folder…"), last)
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder: Path) -> None:
        self._settings.setValue("last_dir", str(folder.parent))
        self._save_recent(folder)
        self._load_recent()
        self.load_folder(folder)

    def _choose_dest(self) -> None:
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
        """Carga las imágenes de folder y muestra la primera."""
        folder = Path(folder)
        self._images = sorted(
            [f for f in folder.iterdir()
             if f.is_file() and f.suffix.lower() in _IMAGE_EXTS],
            key=lambda p: p.name.lower(),
        )
        self._set_folder = folder
        self._custom_dest = None  # cada set vuelve al default picked/ del set
        self._index = 0
        self._last_op = None
        self._update_dest_label()
        self._refresh_view()
        self._canvas.setFocus()

    # ── Modo ──────────────────────────────────────────────────────────
    def _toggle_mode(self) -> None:
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
        if self._images and self._index < len(self._images) - 1:
            self._index += 1
            self._refresh_view()

    def _prev(self) -> None:
        if self._images and self._index > 0:
            self._index -= 1
            self._refresh_view()

    # ── Acción primaria ───────────────────────────────────────────────
    def _do_primary(self) -> None:
        if not self._images:
            return
        if self._mode == MODE_PICK:
            self._pick_current()
        else:
            self._delete_current()

    def _delete_current(self) -> None:
        path = self._images[self._index]
        dest = self._discard_dest()
        if not dest:
            return
        try:
            new_path = file_ops.discard(path, dest)
        except OSError as exc:
            self._set_status(self.tr("pickler.error_move", "Error: {}").format(exc))
            return
        self._last_op = ("discard", path, new_path, self._index)
        self._discarded += 1
        self._notify_library_changed(path.parent, new_path.parent)
        self._consume_current()

    def _pick_current(self) -> None:
        dest = self._keep_dest()
        if not dest:
            return
        path = self._images[self._index]
        try:
            new_path = file_ops.keep(path, dest, self._copy_mode)
        except OSError as exc:
            self._set_status(self.tr("pickler.error_move", "Error: {}").format(exc))
            return
        action = "keep_copy" if self._copy_mode else "keep_move"
        self._last_op = (action, path, new_path, self._index)
        self._kept += 1
        self._notify_library_changed(path.parent, new_path.parent)
        if self._copy_mode:
            # La copia deja el original; solo avanzamos sin quitarlo de la lista.
            self._next() if self._index < len(self._images) - 1 else self._refresh_view()
            self._update_counters()
        else:
            self._consume_current()

    def _consume_current(self) -> None:
        """Quita la imagen actual de la lista (movida) y refresca."""
        self._images.pop(self._index)
        if self._index >= len(self._images):
            self._index = max(0, len(self._images) - 1)
        self._refresh_view()
        self._update_counters()

    # ── Deshacer ──────────────────────────────────────────────────────
    def _undo(self) -> None:
        if not self._last_op:
            return
        action, original, current, index = self._last_op
        try:
            file_ops.undo(action, original, current)
        except OSError as exc:
            self._set_status(self.tr("pickler.error_move", "Error: {}").format(exc))
            return
        if action == "keep_copy":
            self._kept = max(0, self._kept - 1)
        elif action == "keep_move":
            self._kept = max(0, self._kept - 1)
            self._images.insert(min(index, len(self._images)), Path(original))
            self._index = index
        else:  # discard
            self._discarded = max(0, self._discarded - 1)
            self._images.insert(min(index, len(self._images)), Path(original))
            self._index = index
        if action != "keep_copy":
            self._notify_library_changed(Path(original).parent, Path(current).parent)
        self._last_op = None
        self._refresh_view()
        self._update_counters()

    def _notify_library_changed(self, *folders) -> None:
        """Avisa por EventBus que cambiaron carpetas, para que Librarian re-indexe."""
        if not self._event_bus:
            return
        for f in folders:
            if f:
                self._event_bus.publish("library_changed", str(f))

    # ── Menú contextual (acción primaria error-proof) ─────────────────
    def _show_context_menu(self, pos) -> None:
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
        if not self._images:
            self._canvas.img.set_source(None)
            self._canvas.img.setText(self.tr("pickler.no_images", "No images in this folder"))
            self._set_status("")
            return
        path = self._images[self._index]
        px = QPixmap(str(path))
        if px.isNull():
            self._canvas.img.set_source(None)
            self._canvas.img.setText(
                f"{self.tr('pickler.cant_display', 'Cannot display:')}\n{path.name}")
        else:
            self._canvas.img.set_source(px)
        self._set_status(f"{path.name}   [{self._index + 1} / {len(self._images)}]")

    def _set_status(self, text: str) -> None:
        self._canvas.lbl_info.setText(text)

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

    # ── Interfaz estándar (pipeline — fase 2) ─────────────────────────
    def load_images(self, paths: list) -> None:
        """
        Recibe un set de imágenes desde otros módulos (Gallery/Librarian).
        Sin carpeta de set única: picked/discarded usan la carpeta de cada imagen.
        """
        self._images = [Path(p) for p in paths
                        if Path(p).suffix.lower() in _IMAGE_EXTS]
        self._set_folder = None
        self._custom_dest = None
        self._index = 0
        self._last_op = None
        self._update_dest_label()
        self._refresh_view()
        self._canvas.setFocus()
