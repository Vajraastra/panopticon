from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QPushButton
from core.components.standard_layout import StandardToolLayout
from core.locale_manager import LocaleManager
from core.components.ui_roles import primary, hint
from .sidebar import GallerySidebar
from .grid import GalleryGrid

class GalleryView(QWidget):
    """
    Orchestrator for the Gallery UI.
    """
    def __init__(self, state, query_engine, context):
        super().__init__()
        self.state = state
        self.engine = query_engine
        self.context = context
        
        self.init_ui()
        
    def init_ui(self):
        tr = LocaleManager().tr
        self.sidebar = GallerySidebar(self.state, self.engine)
        self.grid = GalleryGrid(self.state, self.engine)
        self.bottom = self._create_bottom_bar()

        self.layout_manager = StandardToolLayout(
            self.grid,
            self.sidebar,
            self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus'),
            tool_header=("🖼️", tr("tool.gallery.name", "Gallery"),
                    tr("tool.gallery.desc", "Browse, rate, and tag your organized image collections.")),
        )

        from PySide6.QtWidgets import QVBoxLayout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0,0,0,0)
        main_layout.addWidget(self.layout_manager)

    def _create_bottom_bar(self):
        tr = LocaleManager().tr
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_status = hint(tr("gallery.status.ready", "Ready"))

        self.btn_select_page = QPushButton(tr("gallery.select_page", "Select page"))
        self.btn_select_page.setToolTip(tr(
            "gallery.select_page_tip",
            "Adds every image on this page to the selection. The selection is kept when you change page."))
        self.btn_select_page.clicked.connect(
            lambda: self.state.select_paths(self.grid.current_page_paths))

        # Sin selección múltiple: enviar el resultado completo de la búsqueda (≤1.000).
        self.btn_send_all = QPushButton(tr("gallery.send_results", "Send results to…"))
        self.btn_send_all.setToolTip(tr(
            "gallery.send_results_tip",
            "Sends every image matching the current view and filters (up to 1,000) to another tool. "
            "Larger sets are refused, never cut: narrow the filters or use Multiple selection."))

        # Acción principal abajo a la derecha (convención 3)
        self.btn_send = primary(QPushButton(tr("gallery.send_selection", "Send to…")))
        self.btn_send.setToolTip(tr(
            "gallery.send_tip",
            "Sends the selected images to another tool. Opens a menu to pick which one."))

        layout.addWidget(self.lbl_status)
        layout.addStretch()
        layout.addWidget(self.btn_select_page)
        layout.addWidget(self.btn_send_all)
        layout.addWidget(self.btn_send)

        self.state.selection_changed.connect(self.update_bottom_bar)
        self.state.view_mode_changed.connect(lambda _mode: self.update_bottom_bar())
        self.update_bottom_bar()
        return bar

    def update_bottom_bar(self):
        tr = LocaleManager().tr
        count = len(self.state.selected_paths)
        images = self.state.mode != self.state.VIEW_ALBUMS
        picker = self.state.picker_active
        self.btn_select_page.setVisible(picker and images)
        self.btn_send_all.setVisible(not picker and images)
        self.btn_send.setVisible(picker and count > 0)
        if picker:
            self.lbl_status.setText(tr("gallery.status.selected", "{count} selected").format(count=count))
            self.btn_send.setText(tr("gallery.send_count", "Send {count} to…").format(count=count))
        else:
            self.lbl_status.setText(tr("gallery.status.ready", "Ready"))
