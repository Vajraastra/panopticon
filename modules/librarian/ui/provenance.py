"""Read-only provenance for the selected indexed image."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QVBoxLayout
from core.library_async import LibraryReader


class ProvenancePanel(QFrame):
    def __init__(self, service, translate, parent=None):
        super().__init__(parent)
        # The tool sidebar is fixed at 320 px; leave room for margins and scrollbar.
        self.setMaximumWidth(280)
        self.tr_text = translate
        self.path = None
        self._closed = False
        self.reader = LibraryReader(service, self)
        self.reader.result.connect(self._received)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(QLabel(translate('lib.provenance.title', 'Selected image')))
        self.fields = {}
        for key, label, tip in (
            ('path', 'File', 'Full path of the selected image. Select text to copy it.'),
            ('artist', 'Artist', 'Artist recorded in the library for this image. Select text to copy it.'),
            ('origin_url', 'Source URL', 'Original URL recorded in the library. Select text to copy it; no website is opened.')):
            field = QLineEdit(self)
            field.setReadOnly(True)
            field.setAccessibleName(translate('lib.provenance.'+key, label))
            field.setPlaceholderText(translate('lib.provenance.'+key, label))
            field.setToolTip(translate('lib.provenance.'+key+'_tip', tip))
            layout.addWidget(field)
            self.fields[key] = field
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.clear()

    def clear(self):
        self.reader.invalidate()
        self.path = None
        for field in self.fields.values():
            field.clear()
        self.status.setText(self.tr_text('lib.provenance.select', 'Click an image to see its recorded origin.'))

    def show_path(self, path):
        if self._closed:
            return
        self.clear()
        self.path = path
        self.fields['path'].setText(path)
        self.fields['path'].setCursorPosition(0)
        self.status.setText(self.tr_text('lib.provenance.loading', 'Reading recorded origin…'))
        self.reader.request('get_file_provenance', path)

    def _received(self, ticket, value, error):
        if self._closed:
            return
        if error is not None:
            self.status.setText(self.tr_text('lib.provenance.error', 'Could not read the recorded origin. Select the image again to retry.'))
            return
        if value is None:
            self.status.setText(self.tr_text('lib.provenance.absent', 'This path has no active indexed copy.'))
            return
        for key in ('artist', 'origin_url'):
            self.fields[key].setText(value[key] or '')
            self.fields[key].setCursorPosition(0)
        self.status.setText(self.tr_text('lib.provenance.missing', 'No artist or source URL recorded.')
                            if not value['artist'] and not value['origin_url'] else '')
        if value['truncated']:
            self.status.setText(self.tr_text('lib.provenance.truncated', 'The recorded value is too long; a shortened preview is shown.'))

    def shutdown(self):
        self._closed = True
        self.reader.close()
