import os
from collections import Counter
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QFileDialog, QTextEdit, QProgressBar
from PySide6.QtCore import QThread, Signal
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout

class ScannerWorker(QThread):
    progress = Signal(int, int)  # actual, total
    finished = Signal(dict)
    log = Signal(str)

    def __init__(self, root_path, log_start_msg):
        super().__init__()
        self.root_path = root_path
        self.log_start_msg = log_start_msg
        self.is_running = True

    def run(self):
        stats = Counter()
        self.log.emit(self.log_start_msg)

        file_list = []
        for root, _, files in os.walk(self.root_path):
            if not self.is_running:
                break
            for f in files:
                file_list.append(os.path.join(root, f))

        total = len(file_list)
        for i, path in enumerate(file_list):
            if not self.is_running:
                break
            ext = os.path.splitext(path)[1].lower()
            if ext in ('.png', '.jpg', '.jpeg', '.webp', '.avif', '.bmp', '.tiff', '.gif', '.ico'):
                stats[ext] += 1

            if i % 100 == 0:
                self.progress.emit(i, total)

        self.finished.emit(dict(stats))


class FormatScannerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Format Scanner"
        self._description = "Analyzes the distribution of image formats for statistical review."
        self._icon = "📊"
        self.accent_color = "#f1fa8c"
        self.view = None

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        content = self._create_content()
        sidebar = self._create_sidebar()

        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_content(self) -> QWidget:
        theme = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg = theme.get_color('bg_panel') if theme else "#111111"
        fg = theme.get_color('text_primary') if theme else "#f8f8f2"

        container = QWidget()
        layout = QVBoxLayout(container)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(
            f"background-color: {bg}; color: {fg}; font-family: 'Consolas';"
        )
        layout.addWidget(self.txt_log)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        return container

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setSpacing(20)

        btn_select = QPushButton(self.tr("fscanner.btn.select", "📂 Select Folder"))
        btn_select.clicked.connect(self.start_scan)
        layout.addWidget(btn_select)

        layout.addStretch()
        return container

    def start_scan(self):
        path = QFileDialog.getExistingDirectory(
            self.view,
            self.tr("fscanner.dialog.title", "Select Folder for Statistics")
        )
        if not path:
            return

        self.txt_log.clear()
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        log_start = self.tr("fscanner.log.start", "Starting scan in: {path}").format(path=path)
        self.worker = ScannerWorker(path, log_start)
        self.worker.progress.connect(
            lambda a, t: self.progress_bar.setValue(int((a / t) * 100) if t > 0 else 0)
        )
        self.worker.log.connect(lambda m: self.txt_log.append(f"> {m}"))
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_finished(self, stats):
        self.progress_bar.setVisible(False)
        self.txt_log.append(self.tr("fscanner.results.title", "\n--- STATISTICAL RESULTS ---"))
        total = sum(stats.values())
        if total == 0:
            self.txt_log.append(self.tr("fscanner.results.no_images", "No images found."))
            return

        for ext, count in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            percent = (count / total) * 100
            self.txt_log.append(f"{ext.upper()}: {count} ({percent:.2f}%)")

        self.txt_log.append(
            self.tr("fscanner.results.total", "\nTotal images analyzed: {total}").format(total=total)
        )
