"""
Grid de thumbnails autocontenido del Dataset Tagger.

Permite VALIDAR VISUALMENTE el set antes de captionar:
- muestra las imágenes de la carpeta como miniaturas en rejilla,
- marca en verde las que YA tienen un .txt hermano (caption previo),
- lista las subcarpetas (navegables) para ver la estructura del dataset.

Respeta las reglas del framework:
- Decodificación de imágenes SIEMPRE en QThread (nunca en el hilo GUI).
- Módulo independiente: no importa nada de otros módulos (solo PySide6,
  stdlib y la lógica del propio dataset_tagger).
- Acentos vía colores inyectados desde la vista (theme_manager), sin hardcode.
"""
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QListWidget, QListWidgetItem, QLabel, QStyle,
)
from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QIcon, QColor, QImageReader

from ..logic import sidecar

log = logging.getLogger(__name__)

THUMB = 132                      # lado de la miniatura (px)
MAX_ITEMS = 500                  # tope de imágenes mostradas (no de procesadas)

# roles de dato en los items
ROLE_PATH = Qt.UserRole          # ruta del archivo/carpeta
ROLE_KIND = Qt.UserRole + 1      # "dir" | "img" | "up" | "info"


class ThumbnailWorker(QThread):
    """Decodifica miniaturas a tamaño reducido fuera del hilo GUI.

    Emite QImage (no QPixmap: QPixmap solo puede crearse en el hilo GUI).
    """
    ready = Signal(str, object)  # (path, QImage)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        for p in self._paths:
            if self._cancel:
                return
            try:
                reader = QImageReader(p)
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid():
                    size.scale(THUMB, THUMB, Qt.KeepAspectRatio)
                    reader.setScaledSize(size)
                img = reader.read()
            except Exception as e:  # noqa: BLE001 — una miniatura mala no corta el lote
                log.debug("No se pudo leer miniatura %s: %s", p, e)
                continue
            if not self._cancel and not img.isNull():
                self.ready.emit(p, img)


class SourceGrid(QWidget):
    """Rejilla de validación del set de origen (carpetas + imágenes)."""

    folder_activated = Signal(str)   # el usuario abrió una subcarpeta / subió un nivel
    caption_requested = Signal(str)  # doble clic en una imagen: revisar/editar su .txt

    def __init__(self, tr, accent, success, parent=None):
        super().__init__(parent)
        self._tr = tr                      # callable(key, default)
        self._accent = accent
        self._success = success
        self._worker = None
        self._items_by_path = {}           # path -> QListWidgetItem (para el thumb async)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: %s; font-size: 11px;" % self._accent)
        lay.addWidget(self.summary)

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setGridSize(QSize(THUMB + 24, THUMB + 38))
        self.list.setSpacing(8)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)
        self.list.itemDoubleClicked.connect(self._on_activated)
        lay.addWidget(self.list, 1)

        self._dir_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        self._file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        self._up_icon = self.style().standardIcon(QStyle.SP_FileDialogToParent)

    # -- API pública ----------------------------------------------------------
    def show_folder(self, folder):
        """Muestra subcarpetas + imágenes directas de `folder` (no recursivo:
        el grid es para navegar/validar; el alcance de procesado lo da el
        checkbox 'incluir subcarpetas')."""
        folder = Path(folder)
        self._reset()

        subdirs, images = [], []
        try:
            for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir():
                    subdirs.append(entry)
                elif entry.is_file() and entry.suffix.lower() in sidecar.IMAGE_EXTS:
                    images.append(entry)
        except OSError as e:
            log.warning("No se pudo listar %s: %s", folder, e)

        # entrada para subir un nivel (si hay padre distinto)
        if folder.parent != folder:
            up = QListWidgetItem(self._up_icon, self._tr("tagger.grid.up", "⬆ Up"))
            up.setData(ROLE_PATH, str(folder.parent))
            up.setData(ROLE_KIND, "up")
            self.list.addItem(up)

        for d in subdirs:
            it = QListWidgetItem(self._dir_icon, d.name)
            it.setData(ROLE_PATH, str(d))
            it.setData(ROLE_KIND, "dir")
            self.list.addItem(it)

        self._add_images(images)
        self._update_summary(len(images), self._count_captioned(images), len(subdirs))
        self._start_thumbs(images)

    def show_images(self, images):
        """Muestra un set explícito de imágenes (modo 'imágenes sueltas')."""
        self._reset()
        paths = [Path(p) for p in images]
        self._add_images(paths)
        self._update_summary(len(paths), self._count_captioned(paths), 0)
        self._start_thumbs(paths)

    def stop(self):
        """Cancela el worker (llamar al cerrar / reemplazar el set)."""
        if self._worker:
            self._worker.cancel()
            self._worker.wait(1500)
            self._worker = None

    # -- internos -------------------------------------------------------------
    def _reset(self):
        self.stop()
        self.list.clear()
        self._items_by_path.clear()

    def _add_images(self, images):
        shown = images[:MAX_ITEMS]
        for img in shown:
            it = QListWidgetItem(self._file_icon, img.name)
            it.setData(ROLE_PATH, str(img))
            it.setData(ROLE_KIND, "img")
            if sidecar.has_sidecar(img):
                it.setForeground(QColor(self._success))
                it.setText("✓ " + img.name)
                it.setToolTip(self._tr(
                    "tagger.grid.captioned",
                    "Already has a .txt caption — double-click to edit"))
            else:
                it.setToolTip(self._tr(
                    "tagger.grid.edit_hint", "Double-click to review / edit captions"))
            self.list.addItem(it)
            self._items_by_path[str(img)] = it
        if len(images) > MAX_ITEMS:
            more = QListWidgetItem(
                self._tr("tagger.more", "+ {0} more…").format(len(images) - MAX_ITEMS))
            more.setData(ROLE_KIND, "info")
            more.setFlags(Qt.NoItemFlags)
            self.list.addItem(more)

    @staticmethod
    def _count_captioned(images):
        return sum(1 for p in images if sidecar.has_sidecar(p))

    def _update_summary(self, n_imgs, n_capt, n_dirs):
        self.summary.setText(self._tr(
            "tagger.grid.summary", "{0} images · {1} with caption · {2} subfolders")
            .format(n_imgs, n_capt, n_dirs))

    def _start_thumbs(self, images):
        paths = [str(p) for p in images[:MAX_ITEMS]]
        if not paths:
            return
        self._worker = ThumbnailWorker(paths)
        self._worker.ready.connect(self._on_thumb)
        self._worker.start()

    def _on_thumb(self, path, image):
        it = self._items_by_path.get(path)
        if it is not None and not image.isNull():
            it.setIcon(QIcon(QPixmap.fromImage(image)))

    def _on_activated(self, item):
        kind = item.data(ROLE_KIND)
        if kind in ("dir", "up"):
            target = item.data(ROLE_PATH)
            if target:
                self.folder_activated.emit(target)
        elif kind == "img":
            path = item.data(ROLE_PATH)
            if path:
                self.caption_requested.emit(path)
