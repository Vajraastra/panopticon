"""One persistent, non-modal window for each module session."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMainWindow


class ModuleWindow(QMainWindow):
    def __init__(self, dashboard, module, view):
        super().__init__(dashboard, Qt.Window)
        key = f"tool.{module.name.lower().replace(' ', '_')}.name"
        self.setWindowTitle(f"{dashboard.tr(key, module.name)} — Panopticon")
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowModality(Qt.NonModal)
        self.resize(1280, 800)
        self.setCentralWidget(view)
        self.statusBar().showMessage(dashboard.tr(
            "dashboard.module_session_tip",
            "Closing this window keeps its session and jobs running. Reopen it from the dashboard."
        ))

    def show_session(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        # Hide only: the dashboard owns the module/runtime until app shutdown.
        # Destroying a view here could orphan a multi-day Cherry download.
        self.hide()
        event.accept()
