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
from PySide6.QtCore import Qt, QThread, Signal, QSize, QRect
from PySide6.QtGui import (
    QPixmap, QIcon, QColor, QImageReader, QPainter, QPen, QBrush, QFont,
)

from ..logic import sidecar

log = logging.getLogger(__name__)

THUMB = 132                      # lado de la miniatura (px)
MAX_ITEMS = 500                  # tope de imágenes mostradas (no de procesadas)

# estados de una imagen en modo Ideogram v4 (insignia en la miniatura):
#   draft     → existe <stem>.pano.json pero aún no el <stem>.json final
#   exported  → existe el <stem>.json final (capturado y validado)
#   error     → la captura de esta imagen falló en la última corrida
ST_DRAFT, ST_EXPORTED, ST_ERROR = "draft", "exported", "error"

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
        self._thumb_by_path = {}           # path -> QImage limpia (para re-pintar insignias)
        self._state_by_path = {}           # path -> ST_* (estado ig4; ausente = sin empezar)
        self._ig4_out_dir = None           # carpeta de salida ig4; None = otros modos
        self._n_dirs = 0                   # subcarpetas del listado actual (para el resumen)

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
    def set_ig4_output_dir(self, out_dir):
        """Activa el marcado de preproceso de Ideogram v4.

        Si `out_dir` apunta a la carpeta de salida ig4, las imágenes que ya
        tengan un `<stem>.pano.json` dentro se marcan con una insignia en la
        esquina inferior derecha de la miniatura. `None` desactiva el marcado
        (los demás modos siguen marcando el .txt previo en verde).
        """
        self._ig4_out_dir = Path(out_dir) if out_dir else None

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

        self._n_dirs = len(subdirs)
        self._add_images(images)
        self._update_summary(len(images), self._count_marked(images), len(subdirs))
        self._start_thumbs(images)

    def show_images(self, images):
        """Muestra un set explícito de imágenes (modo 'imágenes sueltas')."""
        self._reset()
        paths = [Path(p) for p in images]
        self._n_dirs = 0
        self._add_images(paths)
        self._update_summary(len(paths), self._count_marked(paths), 0)
        self._start_thumbs(paths)

    def refresh_preprocess(self):
        """Re-evalúa el estado (borrador/exportado) de cada imagen desde el disco
        y actualiza insignias, tooltips y el resumen SIN re-decodificar miniaturas.
        Descarta marcas de error previas (vuelve a la verdad del disco). Se llama
        tras editar las cajas de una imagen y al iniciar una corrida."""
        if self._ig4_out_dir is None:
            return
        self._state_by_path = {
            p: self._disk_state(Path(p))
            for p in self._items_by_path
            if self._disk_state(Path(p))
        }
        for path, it in self._items_by_path.items():
            self._repaint_item(path, it)
        self._update_summary_ig4()

    def mark_path(self, image_path):
        """Re-evalúa el estado de UNA imagen desde el disco (p. ej. tras exportarla
        en una corrida: aparece su <stem>.json → pasa a 'exportada')."""
        path = str(Path(image_path))
        it = self._items_by_path.get(path)
        if it is None:
            return
        state = self._disk_state(Path(path))
        if state:
            self._state_by_path[path] = state
        else:
            self._state_by_path.pop(path, None)
        self._repaint_item(path, it)
        self._update_summary_ig4()

    def mark_error(self, image_path):
        """Marca una imagen como fallida en la última corrida (insignia roja)."""
        path = str(Path(image_path))
        it = self._items_by_path.get(path)
        if it is None:
            return
        self._state_by_path[path] = ST_ERROR
        self._repaint_item(path, it)

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
        self._thumb_by_path.clear()
        self._state_by_path.clear()

    def _add_images(self, images):
        ig4 = self._ig4_out_dir is not None
        shown = images[:MAX_ITEMS]
        for img in shown:
            it = QListWidgetItem(self._file_icon, img.name)
            it.setData(ROLE_PATH, str(img))
            it.setData(ROLE_KIND, "img")
            if ig4:
                # En Ideogram v4 lo relevante NO es el .txt sino el estado del
                # preproceso/exportado en la carpeta de salida. Se marca con una
                # insignia en la miniatura (ver _repaint_item / _with_badge).
                state = self._disk_state(img)
                if state:
                    self._state_by_path[str(img)] = state
                self._apply_img_marking(it, str(img), state)
            elif sidecar.has_sidecar(img):
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

    def _disk_state(self, img):
        """Estado de una imagen ig4 según los archivos en la carpeta de salida:
        ST_EXPORTED si existe el <stem>.json final; ST_DRAFT si solo existe el
        <stem>.pano.json; None si no hay nada (sin empezar)."""
        if self._ig4_out_dir is None:
            return None
        stem = Path(img).stem
        if (self._ig4_out_dir / (stem + ".json")).exists():
            return ST_EXPORTED
        if (self._ig4_out_dir / (stem + ".pano.json")).exists():
            return ST_DRAFT
        return None

    def _apply_img_marking(self, item, path, state):
        """Fija el tooltip (y deja el nombre limpio) de una imagen en modo ig4.
        La insignia visual se pinta sobre la miniatura en `_repaint_item`."""
        item.setText(Path(path).name)
        item.setToolTip(self._tooltip_for(state))

    def _tooltip_for(self, state):
        if state == ST_EXPORTED:
            return self._tr(
                "tagger.grid.exported",
                "Already exported (final JSON ready) — double-click to re-edit")
        if state == ST_DRAFT:
            return self._tr(
                "tagger.grid.preprocessed",
                "Boxes drawn (draft) — run to capture, or double-click to edit")
        if state == ST_ERROR:
            return self._tr(
                "tagger.grid.errored",
                "Capture failed on the last run — double-click to review / retry")
        return self._tr(
            "tagger.grid.preprocess_hint",
            "Double-click to draw boxes and create its preprocess JSON")

    def _repaint_item(self, path, item):
        """Actualiza tooltip + icono (con insignia del estado) de un item ig4."""
        state = self._state_by_path.get(path)
        item.setToolTip(self._tooltip_for(state))
        img = self._thumb_by_path.get(path)
        if img is not None:
            item.setIcon(self._icon_for(path, img))

    def _count_marked(self, images):
        """Cuenta imágenes 'marcadas': con preproceso en ig4, con .txt si no."""
        if self._ig4_out_dir is not None:
            return sum(1 for p in images if self._disk_state(p))
        return sum(1 for p in images if sidecar.has_sidecar(p))

    def _update_summary(self, n_imgs, n_marked, n_dirs):
        if self._ig4_out_dir is not None:
            self._n_dirs = n_dirs
            self._update_summary_ig4(n_imgs)
            return
        tpl = self._tr(
            "tagger.grid.summary", "{0} images · {1} with caption · {2} subfolders")
        self.summary.setText(tpl.format(n_imgs, n_marked, n_dirs))

    def _update_summary_ig4(self, n_imgs=None):
        if n_imgs is None:
            n_imgs = len(self._items_by_path)
        drawn = len(self._state_by_path)
        done = sum(1 for s in self._state_by_path.values() if s == ST_EXPORTED)
        tpl = self._tr(
            "tagger.grid.summary_ig4",
            "{0} images · {1} drawn · {2} exported · {3} subfolders")
        self.summary.setText(tpl.format(n_imgs, drawn, done, self._n_dirs))

    def _start_thumbs(self, images):
        paths = [str(p) for p in images[:MAX_ITEMS]]
        if not paths:
            return
        self._worker = ThumbnailWorker(paths)
        self._worker.ready.connect(self._on_thumb)
        self._worker.start()

    def _on_thumb(self, path, image):
        it = self._items_by_path.get(path)
        if it is None or image.isNull():
            return
        self._thumb_by_path[path] = image     # copia limpia para re-pintar insignias
        it.setIcon(self._icon_for(path, image))

    def _icon_for(self, path, image):
        """QIcon de la miniatura, con insignia del estado ig4 si corresponde."""
        pm = QPixmap.fromImage(image)
        state = self._state_by_path.get(path)
        if state:
            pm = self._with_badge(pm, state)
        return QIcon(pm)

    def _with_badge(self, pixmap, state):
        """Pinta una insignia de estado en la esquina inferior derecha:
        borrador = '{}' en acento; exportada = '✓' en verde; error = '!' en rojo."""
        if state == ST_EXPORTED:
            bg, glyph = QColor(self._success), "✓"
        elif state == ST_ERROR:
            bg, glyph = QColor("#ff4d4d"), "!"
        else:                                  # ST_DRAFT
            bg, glyph = QColor(self._accent), "{}"
        pm = QPixmap(pixmap)                    # copia: no mutar la del cache
        side = max(18, int(pm.width() * 0.30))
        margin = 3
        x = pm.width() - side - margin
        y = pm.height() - side - margin
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(bg))
        p.setPen(QPen(QColor(0, 0, 0, 180), 1))
        radius = side // 4
        p.drawRoundedRect(x, y, side, side, radius, radius)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(side * 0.55))
        p.setFont(f)
        p.setPen(QPen(QColor("#10141a")))      # glifo oscuro sobre fondo claro
        p.drawText(QRect(x, y, side, side), Qt.AlignCenter, glyph)
        p.end()
        return pm

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
