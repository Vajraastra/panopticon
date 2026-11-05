from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QSize, Qt
from PySide6.QtGui import QPixmap, QPixmapCache
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate

from ..logic.recognition import THUMBNAIL_SIZE


TILE_SIZE = QSize(THUMBNAIL_SIZE + 16, THUMBNAIL_SIZE + 16)


class PhotoModel(QAbstractListModel):
    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.members = ()
        self.pixmap_keys = {}

    def set_members(self, result, members):
        if result is self.result and members == self.members:
            return False
        self.beginResetModel()
        if result is not self.result:
            self.pixmap_keys.clear()
        self.result, self.members = result, members
        self.endResetModel()
        return True

    def remove(self, row):
        self.beginRemoveRows(QModelIndex(), row, row)
        self.members = self.members[:row] + self.members[row + 1:]
        self.endRemoveRows()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.members)

    def data(self, index, role):
        if not index.isValid() or index.row() >= len(self.members):
            return None
        member = self.members[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return member
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(self.result.paths[member])
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return self.result.paths[member].name
        if role == Qt.ItemDataRole.DecorationRole:
            return self.thumbnail(member)
        return None

    def thumbnail(self, member):
        key = self.pixmap_keys.get(member)
        if key is not None:
            cached = QPixmapCache.find(key)
            if cached is not None:
                return cached
        pixmap = QPixmap()
        if pixmap.loadFromData(self.result.thumbnails[member]):
            self.pixmap_keys[member] = QPixmapCache.insert(pixmap)
        return pixmap


class PhotoTile(QStyledItemDelegate):
    def __init__(self, missing_thumbnail, parent):
        super().__init__(parent)
        self.missing_thumbnail = missing_thumbnail

    def sizeHint(self, option, index):
        return TILE_SIZE

    def paint(self, painter, option, index):
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(rect.adjusted(2, 2, -2, -2), option.palette.highlight())
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if not pixmap.isNull():
            offset = QPoint((rect.width() - pixmap.width()) // 2, (rect.height() - pixmap.height()) // 2)
            painter.drawPixmap(rect.topLeft() + offset, pixmap)
        else:
            text = option.palette.highlightedText() if selected else option.palette.text()
            painter.save()
            painter.setPen(text.color())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.missing_thumbnail)
            painter.restore()


class PhotoGallery(QListView):
    def __init__(self, missing_thumbnail):
        super().__init__()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setGridSize(TILE_SIZE)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setModel(PhotoModel(self))
        self.setItemDelegate(PhotoTile(missing_thumbnail, self))
