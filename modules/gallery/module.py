from PySide6.QtWidgets import QWidget, QMenu, QFileDialog
from PySide6.QtGui import QCursor
from PySide6.QtCore import Signal

from core.base_module import BaseModule
from core.image_preview import ImagePreview
from core.library_revision_async import LibraryRevisionWatcher
from core.library_async import LibraryReader
from core.folder_images import FolderImages
from .logic.state import GalleryState
from .logic.query_engine import QueryEngine
from .ui.view import GalleryView


class GalleryModule(BaseModule):
    """Gallery Module — Browse and organize image library."""

    request_open_optimizer = Signal(list)
    request_open_pickler = Signal(list)

    def __init__(self):
        super().__init__()
        self._name = "Gallery"
        self._description = "Browse and organize your image library."
        self._icon = "🖼️"
        self.accent_color = "#00b4ff"  # tono de identidad; el loader le pone el croma del tema

        self.view_widget = None
        self.state = None
        self.engine = None
        self._closed = False
        self.send_targets = frozenset()

    def get_view(self) -> QWidget:
        if self.view_widget:
            return self.view_widget

        self.state  = GalleryState()
        self.engine = QueryEngine(service=self.context.get('data_service'))
        initial_revision = self.engine.db.service.revision_snapshot()
        self._preview_revision = self.engine.db.service.preview_revision_snapshot()
        self.view_widget = GalleryView(self.state, self.engine, self.context)
        self.folder_reader = LibraryReader(FolderImages(), self.view_widget)
        self.folder_reader.result.connect(self._on_folder_images)
        self.state.view_mode_changed.connect(lambda mode: self.folder_reader.invalidate())
        self.revisions = LibraryRevisionWatcher(self.engine.db.service, self.view_widget, initial=initial_revision,
            busy=lambda: self.view_widget.grid.reader._active or self.view_widget.sidebar.tag_completions.busy)
        self.revisions.changed.connect(self._on_native_revision)

        self.view_widget.btn_send.clicked.connect(self.show_send_menu)
        self.view_widget.sidebar.btn_open.clicked.connect(self.open_folder_dialog)

        return self.view_widget

    def _on_native_revision(self, revision):
        if self._closed or self.view_widget is None:
            return
        preview_revision = self.engine.db.service.preview_revision_snapshot()
        if preview_revision != self._preview_revision:
            self._preview_revision = preview_revision
            self.view_widget.grid.loader.invalidate(clear_cache=True)
        self.view_widget.grid.refresh()
        self.view_widget.sidebar.tag_completions.reload_page()

    def open_folder_dialog(self):
        if self._closed: return
        folder = QFileDialog.getExistingDirectory(
            self.view_widget,
            self.tr("common.load_folder", "Open Folder to View")
        )
        if not folder or self._closed:
            return

        self.view_widget.lbl_status.setText(self.tr('gallery.folder.loading', 'Reading this folder…'))
        self.view_widget.lbl_status.setToolTip('')
        self.folder_reader.request('folder_images', folder)

    def _on_folder_images(self, ticket, files, error):
        if self._closed:
            return
        if error is not None:
            key = ('gallery.folder.limit' if 'folder_images_requires_staging' in str(error)
                   else 'gallery.folder.error')
            fallback = ('This folder exceeds the external-set budget (10,000 images / 4 MiB of paths). Use a smaller folder or Native library views. No partial set was opened.'
                        if key.endswith('limit') else
                        'Could not read the complete folder. The previous view was preserved; see details.')
            self.view_widget.lbl_status.setText(self.tr(key, fallback))
            self.view_widget.lbl_status.setToolTip(str(error))
            return
        # Empty is a complete result too: never leave the previous folder looking
        # like the newly selected one. load_image_set invalidates older discovery.
        self.load_image_set(files)
        self.view_widget.lbl_status.setText(self.tr('gallery.folder.loaded', '{count} images read.').format(count=len(files)))
        self.view_widget.lbl_status.setToolTip('')

    def load_image_set(self, paths: list):
        """Called by Librarian or other modules."""
        if self._closed: return
        self.get_view()
        self.folder_reader.invalidate()
        self.state.clear_selection()
        self.state.set_mode(
            self.state.VIEW_CUSTOM,
            custom_paths=list(paths),
            title=self.tr("gallery.imported_set", "Imported Set")
        )

    def set_send_targets(self, targets):
        self.send_targets = frozenset(targets) & {'optimizer', 'pickler'}

    def show_send_menu(self):
        if self._closed: return
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
        act_pick = menu.addAction(self.tr("gallery.send.pickler",   "🥒 Send to Pickler"))
        act_opt.setEnabled('optimizer' in self.send_targets)
        act_pick.setEnabled('pickler' in self.send_targets)
        for action in (act_opt, act_pick):
            action.setToolTip(self.tr('gallery.send.target_tip',
                'Open this tool with the selected images. Unavailable tools are disabled.'))
        menu.setToolTipsVisible(True)

        selected = menu.exec(QCursor.pos())

        paths = list(self.state.selected_paths)
        if not paths or self._closed:
            return

        if selected is None or not selected.isEnabled():
            return
        if selected == act_opt:
            self.request_open_optimizer.emit(paths)
        elif selected == act_pick:
            self.request_open_pickler.emit(paths)

    def run_headless(self, params: dict, input_data) -> None:
        pass

    def on_unload(self):
        self._closed = True
        if hasattr(self, 'revisions'):
            self.revisions.close()
        if self.view_widget is not None:
            from .ui.viewer_window import AdvancedViewer
            for viewer in self.view_widget.findChildren(AdvancedViewer): viewer.close()
            self.view_widget.grid.shutdown()
            self.view_widget.sidebar.shutdown()
            for reader in self.view_widget.findChildren(LibraryReader): reader.close()
            self.view_widget.grid.loader.shutdown()

    def shutdown_ready(self):
        if self.view_widget is None: return True
        return (all(reader.shutdown_ready() for reader in self.view_widget.findChildren(LibraryReader))
                and all(preview.shutdown_ready() for preview in self.view_widget.findChildren(ImagePreview))
                and self.view_widget.grid.loader.shutdown_ready())
