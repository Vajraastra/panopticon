import logging

from PySide6.QtCore import QThread, Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from PIL.ImageQt import ImageQt

from core.native_threads import start_worker
from ..logic.recognition import read_image


log = logging.getLogger(__name__)


class PhotoLoadJob(QThread):
    def __init__(self, path, signature, bounds):
        super().__init__()
        self.path, self.signature, self.bounds = path, signature, bounds
        self.image = None

    def run(self):
        try:
            with read_image(self.path, self.signature) as image:
                if not self.isInterruptionRequested():
                    scale = min(self.bounds[0] / image.width, self.bounds[1] / image.height)
                    size = tuple(max(1, round(edge * scale)) for edge in image.size)
                    self.image = ImageQt(image.resize(size)).copy()
        except Exception as error:
            log.warning("Character Separator preview %s: %s", self.path, error)


class PhotoReviewDialog(QDialog):
    DISCARDED = 2

    def __init__(self, path, signature, character_picker, entered_name, t, parent):
        super().__init__(parent)
        self.t = t
        self.name = None
        self.character_picker = character_picker
        self.entered_name = entered_name
        self.dismissed = False
        self.setWindowTitle(t("photo_review_title", "Review image — {name}").format(name=path.name))
        screen = parent.screen().availableGeometry()
        width = min(1400, round(screen.width() * 0.85))
        height = min(1000, round(screen.height() * 0.85))
        self.resize(width, height)

        self.image_label = QLabel(t("photo_review_loading", "Loading original image…"))
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        self.assign_button = QPushButton(t("photo_review_assign", "Assign to another character"))
        self.assign_button.setToolTip(t(
            "assign_photo_tip", "Give the selected image the entered identity and keep it as a reference for that name.",
        ))
        self.assign_button.clicked.connect(self.assign)
        self.discard_button = QPushButton(t("exclude_photo", "Discard selected image"))
        self.discard_button.setToolTip(t(
            "exclude_photo_tip", "Exclude this image from later batches and copies in this session. The original stays on disk.",
        ))
        self.discard_button.clicked.connect(lambda: self.done(self.DISCARDED))
        close_button = QPushButton(t("photo_review_close", "Close"))
        close_button.setToolTip(t("photo_review_close_tip", "Return to the gallery without making a decision."))
        close_button.clicked.connect(self.reject)
        actions = QHBoxLayout()
        for button in (self.assign_button, self.discard_button, close_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        self.assign_button.setEnabled(False)
        self.discard_button.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(character_picker)
        layout.addWidget(self.feedback)
        layout.addLayout(actions)
        close_button.setFocus()

        bounds = (max(1, width - 40), max(1, height - 150))
        # The view retains the worker through native exit even if this dialog closes first.
        self.loader = PhotoLoadJob(path, signature, bounds)
        self.loader.finished.connect(self.show_image)
        self.finished.connect(self.release)
        try:
            start_worker(self.loader, parent)
        except Exception as error:
            log.warning("Character Separator preview worker: %s", error)
            self.loader.requestInterruption()
            self.show_image(failed=True)

    def assign(self):
        self.name = self.entered_name(self.character_picker.currentText(), self.feedback)
        if self.name:
            self.accept()
        else:
            self.feedback.show()
            self.character_picker.setFocus()

    @Slot()
    def show_image(self, failed=False):
        if self.loader is None:
            return
        loader, self.loader = self.loader, None
        loader.deleteLater()
        if self.dismissed:
            self.deleteLater()
        elif failed or loader.image is None or loader.image.isNull():
            self.image_label.setText(self.t("thumbnail_missing", "Preview unavailable"))
        else:
            self.image_label.setPixmap(QPixmap.fromImage(loader.image))
            self.assign_button.setEnabled(True)
            self.discard_button.setEnabled(True)

    def release(self):
        self.dismissed = True
        if self.loader is None:
            self.deleteLater()
        else:
            self.loader.requestInterruption()
