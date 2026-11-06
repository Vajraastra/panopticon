"""List the confirmed character groups for the current review scope."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget


class ConfirmedCharacters(QWidget):
    delete_requested = Signal(str)

    def __init__(self, t, parent=None):
        super().__init__(parent)
        self._t = t
        self._review_enabled = True
        self._empty = QLabel(t("confirmed_empty", "Confirmed characters for this folder will appear here."))
        self._empty.setWordWrap(True)
        self.character_list = QListWidget()
        self.character_list.setToolTip(t(
            "confirmed_list_tip", "Select a character to review its assigned image count and remove it from this folder's review.",
        ))
        self.character_list.itemSelectionChanged.connect(self._update_controls)
        self.delete_button = QPushButton(t("confirmed_delete", "Delete character…"))
        self.delete_button.setToolTip(t(
            "confirmed_delete_tip", "Remove the selected character's assignments and return its images to review.",
        ))
        self.delete_button.clicked.connect(self._request_delete)

        layout = QVBoxLayout(self)
        layout.addWidget(self._empty)
        layout.addWidget(self.character_list, 1)
        layout.addWidget(self.delete_button, alignment=Qt.AlignmentFlag.AlignRight)
        self.set_characters({})

    def selected_name(self):
        selected = self.character_list.selectedItems()
        return selected[0].data(Qt.ItemDataRole.UserRole) if selected else None

    def set_characters(self, counts: dict[str, int]):
        selected = self.selected_name()
        with QSignalBlocker(self.character_list):
            self.character_list.clear()
            selected_row = -1
            for name in sorted(counts, key=str.casefold):
                text = self._t("group_count", "{name} · {count} images").format(name=name, count=counts[name])
                item = QListWidgetItem(text, self.character_list)
                item.setData(Qt.ItemDataRole.UserRole, name)
                item.setToolTip(text)
                if name == selected:
                    selected_row = self.character_list.row(item)
            self.character_list.setCurrentRow(selected_row)
        self._empty.setVisible(not counts)
        self.character_list.setVisible(bool(counts))
        self._update_controls()

    def set_review_enabled(self, enabled: bool):
        self._review_enabled = enabled
        self.character_list.setEnabled(enabled)
        self._update_controls()

    def _update_controls(self):
        self.delete_button.setEnabled(self._review_enabled and self.selected_name() is not None)

    def _request_delete(self):
        if self.delete_button.isEnabled():
            self.delete_requested.emit(self.selected_name())
