"""Sugerencias compartidas y navegación acotada; SQL fuera de GUI, tickets y cierre propios."""
from PySide6.QtCore import QObject, QTimer, Signal, QStringListModel, Qt
from PySide6.QtWidgets import QCompleter, QWidget, QHBoxLayout, QPushButton, QLabel
from core.library_async import LibraryReader
from core.locale_manager import LocaleManager


class TagVocabulary(QObject):
    changed = Signal(object)
    failed = Signal(object)
    busy_changed = Signal(bool)

    def __init__(self, service, field, parent=None, *, reader=None):
        super().__init__(parent or field)
        self.field = field
        self.reader = reader or LibraryReader(service, self)
        self.reader.result.connect(self._result)
        self.model = QStringListModel([], self)
        self.completer = QCompleter(self.model, field)
        self.completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.popup().setToolTip(LocaleManager().tr('data.tags.suggestions_tip',
            'Suggestions show up to 200 literal matches. Keep typing to narrow them; any tag can still be entered manually.'))
        field.setCompleter(self.completer)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self._request)
        field.textChanged.connect(self._text_changed)
        self.query = field.text()
        self.cursors = [None]
        self.more = False
        self.next_name = None
        self.busy = False
        self.closed = False

    def _clear(self):
        self.reader.invalidate()
        self.model.setStringList([])
        self.more = False
        self.next_name = None
        self.busy = True
        self.busy_changed.emit(True)

    def _text_changed(self, text):
        if self.closed:
            return
        self.query, self.cursors = text, [None]
        self._clear()
        self.timer.start()

    def refresh(self):
        if self.closed:
            return
        self.timer.stop()
        self.query, self.cursors = self.field.text(), [None]
        self._clear()
        self._request()

    def _request(self):
        if not self.closed:
            self.reader.request('get_tags_page', query=self.query, after_name=self.cursors[-1], limit=200)

    def next(self):
        if self.closed or self.busy or not self.more:
            return
        self.cursors.append(self.next_name)
        self._clear()
        self._request()

    def previous(self):
        if self.closed or self.busy or len(self.cursors) < 2:
            return
        self.cursors.pop()
        self._clear()
        self._request()

    def _result(self, ticket, value, error):
        if self.closed:
            return
        self.busy = False
        if error is not None:
            self.busy_changed.emit(False)
            self.failed.emit(error)
            return
        self.model.setStringList(value['names'])
        self.more, self.next_name = value['more'], value['next_name']
        self.busy_changed.emit(False)
        self.changed.emit(value)
        if self.field.hasFocus() and self.field.text() and value['names']:
            self.completer.complete()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.reader.close()
        try:
            self.completer.popup().hide()
        except RuntimeError:
            pass  # The field and its popup may already have been destroyed.


class TagPageBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        tr = LocaleManager().tr
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.previous = QPushButton(tr('data.tags.previous', '◀'), self)
        self.previous.setToolTip(tr('data.tags.previous_tip', 'Return to the previous page of matching tags.'))
        self.next = QPushButton(tr('data.tags.next', '▶'), self)
        self.next.setToolTip(tr('data.tags.next_tip', 'Read the next page of matching tags; no images are changed.'))
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.previous)
        layout.addWidget(self.status, 1)
        layout.addWidget(self.next)
        self.previous.setEnabled(False)
        self.next.setEnabled(False)

    def bind(self, controller):
        self.controller = controller
        self.previous.clicked.connect(controller.previous)
        self.next.clicked.connect(controller.next)
        controller.busy_changed.connect(self._busy)
        controller.changed.connect(self._page)
        controller.failed.connect(self._error)

    def _busy(self, busy):
        self.previous.setEnabled(not busy and len(self.controller.cursors) > 1)
        self.next.setEnabled(not busy and self.controller.more)
        if busy:
            self.status.setText(LocaleManager().tr('data.tags.loading', 'Loading tags…'))

    def _page(self, value):
        self.status.setText(LocaleManager().tr('data.tags.page', 'Page {page}: {count} tags').format(
            page=len(self.controller.cursors), count=len(value['names'])))

    def _error(self, error):
        self.status.setText(LocaleManager().tr('data.tags.error', 'Could not read tags.'))
