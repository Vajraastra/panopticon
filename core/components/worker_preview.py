"""Read-only image preview; close waits for actual decoder completion without joining Qt."""
from PySide6.QtCore import Qt,QTimer,QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog,QVBoxLayout,QLabel

from core.image_preview import ImagePreview,load_preview
from core.metadata import MetadataBundle


def _load(path,metadata,cancel):
    value=load_preview(path,metadata,cancel)
    if value is not None and not cancel.is_set():
        value['image']=value['image'].scaled(QSize(750,550),Qt.KeepAspectRatio,Qt.SmoothTransformation)
    return value


class WorkerPreviewDialog(QDialog):
    def __init__(self,path,tr,parent=None):
        super().__init__(parent)
        self._tr=tr; self._closing=False; self._result_code=QDialog.Rejected
        self.setWindowTitle(tr('lib.preview.title','Image Preview')); self.resize(800,600)
        self.label=QLabel(tr('common.status.loading','Loading…')); self.label.setAlignment(Qt.AlignCenter)
        layout=QVBoxLayout(self); layout.addWidget(self.label)
        self.preview=ImagePreview(lambda _:MetadataBundle(),self,loader=_load)
        self.preview.result.connect(self._ready)
        self._close_timer=QTimer(self); self._close_timer.setInterval(25)
        self._close_timer.timeout.connect(self._finish)
        self.preview.request(path)

    def _ready(self,ticket,value,error):
        if self._closing:return
        if error is not None or value is None or value['image'].isNull():
            self.label.setText(self._tr('gal.viewer.load_error','Error loading image'))
        else:self.label.setPixmap(QPixmap.fromImage(value['image']))

    def done(self,code):
        self._closing=True; self._result_code=code; self.preview.close()
        self._finish()

    def accept(self):self.done(QDialog.Accepted)
    def reject(self):self.done(QDialog.Rejected)

    def _finish(self):
        if not self.preview.shutdown_ready():
            self._close_timer.start(); return
        self._close_timer.stop()
        super().done(self._result_code)

    def closeEvent(self,event):
        event.ignore(); self.reject()

    def shutdown_ready(self):return self.preview.shutdown_ready()
