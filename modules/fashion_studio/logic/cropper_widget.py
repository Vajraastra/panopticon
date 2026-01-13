from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QPixmap

class ImageCropperWidget(QWidget):
    """
    An interactive widget to select a crop area on an image.
    Supports fixed aspect ratios and draggable handles.
    """
    selection_changed = Signal(QRectF)  # Relative coordinates (0.0 to 1.0)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.image = QPixmap()
        self._selection = QRectF(0.1, 0.1, 0.8, 0.8) # Normalized (0-1)
        self._aspect_ratio = None # None or float (width/height)
        
        self.active_handle = None
        self.handle_size = 10
        self._dragging = False
        self._last_mouse_pos = QPointF()

    def set_image(self, pixmap):
        self.image = pixmap
        self.update()

    def set_fixed_aspect_ratio(self, ratio):
        """ratio: float (width/height) or None"""
        self._aspect_ratio = ratio
        if self._aspect_ratio:
            self._enforce_ratio()
        self.update()

    def _get_image_rect(self):
        """Calculates where the image is drawn within the widget (maintaining aspect ratio)."""
        if self.image.isNull():
            return self.rect()
        
        iw, ih = self.image.width(), self.image.height()
        ww, wh = self.width(), self.height()
        
        if ww / wh > iw / ih:
            # Widget is wider than image
            nh = wh
            nw = (iw * wh) / ih
        else:
            # Widget is taller than image
            nw = ww
            nh = (ih * ww) / iw
            
        x = (ww - nw) / 2
        y = (wh - nh) / 2
        return QRectF(x, y, nw, nh)

    def _normalized_to_pixel(self, rect_norm):
        img_rect = self._get_image_rect()
        return QRectF(
            img_rect.x() + rect_norm.x() * img_rect.width(),
            img_rect.y() + rect_norm.y() * img_rect.height(),
            rect_norm.width() * img_rect.width(),
            rect_norm.height() * img_rect.height()
        )

    def _pixel_to_normalized(self, point_px):
        img_rect = self._get_image_rect()
        return QPointF(
            (point_px.x() - img_rect.x()) / img_rect.width(),
            (point_px.y() - img_rect.y()) / img_rect.height()
        )

    def _enforce_ratio_smart(self, handle):
        """Adjusts selection to match ratio without pushing outside [0, 1]."""
        if not self._aspect_ratio or self.image.isNull(): return
        iw, ih = self.image.width(), self.image.height()
        s = self._selection
        
        if handle in ['l', 'r', 'move']:
            target_h = (s.width() * iw) / (ih * self._aspect_ratio)
            if s.y() + target_h > 1.0:
                target_h = 1.0 - s.y()
                s.setWidth((target_h * ih * self._aspect_ratio) / iw)
            s.setHeight(target_h)
        else: 
            target_w = (s.height() * ih * self._aspect_ratio) / iw
            if s.x() + target_w > 1.0:
                target_w = 1.0 - s.x()
                s.setHeight((target_w * iw) / (ih * self._aspect_ratio))
            s.setWidth(target_w)

    def _enforce_ratio(self):
        self._enforce_ratio_smart('r')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        img_rect = self._get_image_rect()
        painter.fillRect(self.rect(), QColor(10, 10, 10))
        
        if self.image.isNull(): return
        painter.drawPixmap(img_rect.toRect(), self.image)
        
        sel_px = self._normalized_to_pixel(self._selection)
        
        painter.setBrush(QBrush(QColor(0, 0, 0, 180)))
        painter.setPen(Qt.NoPen)
        painter.drawRect(img_rect.x(), img_rect.y(), img_rect.width(), sel_px.y() - img_rect.y())
        painter.drawRect(img_rect.x(), sel_px.bottom(), img_rect.width(), img_rect.bottom() - sel_px.bottom())
        painter.drawRect(img_rect.x(), sel_px.y(), sel_px.x() - img_rect.x(), sel_px.height())
        painter.drawRect(sel_px.right(), sel_px.y(), img_rect.right() - sel_px.right(), sel_px.height())
        
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(0, 255, 204), 2, Qt.SolidLine))
        painter.drawRect(sel_px)
        
        painter.setBrush(QBrush(QColor(0, 255, 204)))
        h_size = self.handle_size
        handles = self._get_handles(sel_px)
        for h in handles.values():
            painter.drawEllipse(h.center(), h_size/2, h_size/2)

    def _get_handles(self, rect_px):
        s = self.handle_size
        return {
            'tl': QRectF(rect_px.left() - s/2, rect_px.top() - s/2, s, s),
            'tr': QRectF(rect_px.right() - s/2, rect_px.top() - s/2, s, s),
            'bl': QRectF(rect_px.left() - s/2, rect_px.bottom() - s/2, s, s),
            'br': QRectF(rect_px.right() - s/2, rect_px.bottom() - s/2, s, s),
            't': QRectF(rect_px.center().x() - s/2, rect_px.top() - s/2, s, s),
            'b': QRectF(rect_px.center().x() - s/2, rect_px.bottom() - s/2, s, s),
            'l': QRectF(rect_px.left() - s/2, rect_px.center().y() - s/2, s, s),
            'r': QRectF(rect_px.right() - s/2, rect_px.center().y() - s/2, s, s)
        }

    def mousePressEvent(self, event):
        if self.image.isNull(): return
        pos = event.position()
        sel_px = self._normalized_to_pixel(self._selection)
        handles = self._get_handles(sel_px)
        self.active_handle = None
        for name, rect in handles.items():
            if rect.contains(pos):
                self.active_handle = name
                break
        if not self.active_handle and sel_px.contains(pos):
            self.active_handle = 'move'
        self._dragging = (self.active_handle is not None)
        self._last_mouse_pos = pos
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        if not self._dragging:
            sel_px = self._normalized_to_pixel(self._selection)
            handles = self._get_handles(sel_px)
            found = False
            for name, rect in handles.items():
                if rect.contains(pos):
                    if name in ['tl', 'br']: self.setCursor(Qt.SizeFDiagCursor)
                    elif name in ['tr', 'bl']: self.setCursor(Qt.SizeBDiagCursor)
                    elif name in ['t', 'b']: self.setCursor(Qt.SizeVerCursor)
                    elif name in ['l', 'r']: self.setCursor(Qt.SizeHorCursor)
                    found = True
                    break
            if not found:
                if sel_px.contains(pos): self.setCursor(Qt.SizeAllCursor)
                else: self.setCursor(Qt.ArrowCursor)
            return

        delta = self._pixel_to_normalized(pos) - self._pixel_to_normalized(self._last_mouse_pos)
        s = self._selection
        if self.active_handle == 'move':
            new_x = max(0.0, min(s.x() + delta.x(), 1.0 - s.width()))
            new_y = max(0.0, min(s.y() + delta.y(), 1.0 - s.height()))
            s.moveTo(new_x, new_y)
        else:
            if 't' in self.active_handle:
                s.setTop(max(0.0, min(s.top() + delta.y(), s.bottom() - 0.05)))
            if 'b' in self.active_handle:
                s.setBottom(max(s.top() + 0.05, min(s.bottom() + delta.y(), 1.0)))
            if 'l' in self.active_handle:
                s.setLeft(max(0.0, min(s.left() + delta.x(), s.right() - 0.05)))
            if 'r' in self.active_handle:
                s.setRight(max(s.left() + 0.05, min(s.right() + delta.x(), 1.0)))
            
            if self._aspect_ratio:
                self._enforce_ratio_smart(self.active_handle)

        self._last_mouse_pos = pos
        self.selection_changed.emit(self._selection)
        self.update()

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self.active_handle = None
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()
