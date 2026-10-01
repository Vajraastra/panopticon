from core.native_threads import start_worker, native_done, owner_threads_ready
import os
import html
import threading
from collections import Counter
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton, QFileDialog, QTextEdit, QProgressBar
from PySide6.QtCore import QThread, Signal
from core.base_module import BaseModule
from core.theme import Theme

_LIVE_WORKERS = set()
from core.components.standard_layout import StandardToolLayout

class ScannerWorker(QThread):
    progress = Signal(int, int)  # actual, total
    finished_signal = Signal(dict)
    log = Signal(str)

    def __init__(self, root_path, log_start_msg):
        super().__init__()
        self.root_path = root_path
        self.log_start_msg = log_start_msg
        self.cancel_event = threading.Event()
        self.outcome = None
        self.error = ''

    def stop(self):
        self.cancel_event.set()

    def run(self):
        stats, count = Counter(), 0
        self.outcome = 'complete'
        def failed(error): raise error
        try:
            self.log.emit(self.log_start_msg)
            # Stream filenames; no list proportional to the entire tree.
            for root, _, files in os.walk(self.root_path, onerror=failed):
                if self.cancel_event.is_set(): break
                for filename in files:
                    if self.cancel_event.is_set(): break
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in ('.png','.jpg','.jpeg','.webp','.avif','.bmp','.tiff','.gif','.ico'):
                        stats[ext] += 1
                    count += 1
                    if count % 100 == 0: self.progress.emit(count,0)
            if self.cancel_event.is_set(): self.outcome = 'cancelled'
        except Exception as error:
            self.outcome,self.error = 'error',str(error)[:1000]
        finally:
            self.finished_signal.emit(dict(stats))


class FormatScannerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Format Scanner"
        self._description = "Analyzes the distribution of image formats for statistical review."
        self._icon = "📊"
        self.accent_color = "#f1fa8c"
        self.view = None
        self.worker = None
        self._closed = False
        self._dialog = False
        self._scanning = False

    def on_unload(self):
        self._closed = True
        if self.worker is not None: self.worker.stop()

    def shutdown_ready(self):
        return owner_threads_ready(self) and native_done(self.worker)

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
        bg = theme.get_color('bg_panel') if theme else Theme.BG_PANEL
        fg = theme.get_color('text_primary') if theme else Theme.TEXT_PRIMARY

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

        self.btn_select = btn_select = QPushButton(self.tr("fscanner.btn.select", "📂 Select Folder"))
        btn_select.setToolTip(self.tr(
            "fscanner.select_tip",
            "Folder to analyse. It only counts formats and sizes to give you a report: it "
            "converts and touches nothing."))
        btn_select.clicked.connect(self.start_scan)
        layout.addWidget(btn_select)

        layout.addStretch()
        return container

    def start_scan(self):
        if self._closed or self._dialog or self._scanning or (self.worker and self.worker.isRunning()): return
        self._dialog = True
        try:
            path = QFileDialog.getExistingDirectory(self.view,self.tr("fscanner.dialog.title","Select Folder for Statistics"))
        finally:
            self._dialog = False
        if not path or self._closed or self._scanning or (self.worker and self.worker.isRunning()): return
        self.txt_log.clear()
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0,0)
        self._scanning = True
        self.btn_select.setEnabled(False)

        log_start = self.tr("fscanner.log.start", "Starting scan in: {path}").format(path=path)
        self.worker = ScannerWorker(path, log_start)
        _LIVE_WORKERS.add(self.worker)
        self.worker.finished.connect(lambda worker=self.worker: _LIVE_WORKERS.discard(worker))
        self.worker.finished.connect(self._on_worker_stopped)
        self.worker.log.connect(self._on_log)
        self.worker.finished_signal.connect(self.on_finished)
        start_worker(self.worker, self)

    def _on_log(self,message):
        if not self._closed: self.txt_log.append(html.escape('> '+message))

    def _on_worker_stopped(self):
        self._scanning = False
        if not self._closed: self.btn_select.setEnabled(True)

    def on_finished(self, stats):
        if self._closed or self.sender() is not self.worker: return
        self.progress_bar.setVisible(False)
        if self.worker.outcome != 'complete':
            reason = self.worker.error or self.tr('tool.format_scanner.cancelled','Scan cancelled; results are incomplete.')
            self.txt_log.append(html.escape(self.tr('tool.format_scanner.error','Scan incomplete: {reason}').format(reason=reason)))
            return
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
