import os
from PySide6.QtWidgets import QWidget, QMenu, QFileDialog
from PySide6.QtGui import QCursor
from PySide6.QtCore import Signal

from core.base_module import BaseModule
from .logic.state import GalleryState
from .logic.query_engine import QueryEngine
from .ui.view import GalleryView


class GalleryModule(BaseModule):
    """Gallery Module — Browse and organize image library."""

    request_open_workshop = Signal(list)
    request_open_optimizer = Signal(list)

    def __init__(self):
        super().__init__()
        self._name = "Gallery"
        self._description = "Browse and organize your image library."
        self._icon = "🖼️"
        self.accent_color = "#00ffcc"

        self.view_widget = None
        self.state = None
        self.engine = None

    def get_view(self) -> QWidget:
        if self.view_widget:
            return self.view_widget

        self.state  = GalleryState()
        self.engine = QueryEngine()
        self.view_widget = GalleryView(self.state, self.engine, self.context)

        self.view_widget.btn_send.clicked.connect(self.show_send_menu)
        self.view_widget.sidebar.btn_open.clicked.connect(self.open_folder_dialog)

        return self.view_widget

    def open_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(
            self.view_widget,
            self.tr("common.load_folder", "Open Folder to View")
        )
        if not folder:
            return

        valid_ext = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
        files = []
        for root, _, filenames in os.walk(folder):
            for f in filenames:
                if os.path.splitext(f)[1].lower() in valid_ext:
                    files.append(os.path.join(root, f).replace('\\', '/'))

        if files:
            self.load_image_set(files)

    def load_image_set(self, paths: list):
        """Called by Librarian or other modules."""
        self.get_view()
        self.state.set_mode(
            self.state.VIEW_CUSTOM,
            custom_paths=paths,
            title=self.tr("gallery.imported_set", "Imported Set")
        )

    def show_send_menu(self):
        menu = QMenu(self.view_widget)
        menu.setStyleSheet("""
            QMenu {
                background-color: #222222;
                color: white;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 5px;
            }
            QMenu::item { padding: 5px 20px; border-radius: 4px; }
            QMenu::item:selected { background-color: #00ffcc; color: black; }
        """)

        act_opt  = menu.addAction(self.tr("gallery.send.optimizer", "🚀 Send to Optimizer"))
        act_work = menu.addAction(self.tr("gallery.send.workshop",  "🛠️ Send to Workshop"))

        selected = menu.exec(QCursor.pos())

        paths = list(self.state.selected_paths)
        if not paths:
            return

        if selected == act_opt:
            self.request_open_optimizer.emit(paths)
        elif selected == act_work:
            self.request_open_workshop.emit(paths)

    def run_headless(self, params: dict, input_data) -> None:
        pass
