from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QFileDialog, QMessageBox, QProgressBar, QTextEdit, QApplication, QDialog)
from PySide6.QtCore import Qt, QSettings
from core.base_module import BaseModule
from .logic.logic import process_folder, get_folder_stats
import os

class DummyCreatorModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.view = None
        self.settings = QSettings("Panopticon", "DummyCreator")
        self.last_asset_dir = self.settings.value("last_asset_dir", os.path.expanduser("~"))

    @property
    def name(self):
        return "Dummy Creator"

    @property
    def description(self):
        return "Archive collections by replacing images with tiny 32x32 placeholders."

    @property
    def icon(self):
        return "🎭"

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        # UI Implementation ported from Workshop
        content = self._create_content()
        
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            content,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_content(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 50, 0, 50)
        layout.setAlignment(Qt.AlignCenter)
        
        # Centering Panel
        panel = QWidget()
        panel.setMaximumWidth(800)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setSpacing(20)
        
        lbl_info = QLabel("🎭 Dummy Creator")
        lbl_info.setAlignment(Qt.AlignCenter)
        lbl_info.setStyleSheet("font-size: 28px; font-weight: bold; color: #f1fa8c; margin-bottom: 10px;")
        panel_layout.addWidget(lbl_info)
        
        lbl_desc = QLabel(
            "Archive your scraped collections while preserving scraper state.\n\n"
            "• Moves originals to 'originals/' subfolder\n"
            "• Creates tiny dummy files (32x32 gray images, 1-byte for others)\n"
            "• Incremental processing (only handles new files on re-runs)\n"
            "• No manifest needed - self-detecting system"
        )
        lbl_desc.setAlignment(Qt.AlignCenter)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("font-size: 14px; color: #aaa; line-height: 1.6;")
        panel_layout.addWidget(lbl_desc)
        
        panel_layout.addSpacing(40)
        
        self.btn_run = QPushButton("📂 Select Folder to Dummify")
        self.btn_run.setFixedSize(350, 60)
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.clicked.connect(self.open_dummy_creator_dialog)
        self.btn_run.setStyleSheet("""
            QPushButton {
                background-color: #332211;
                color: #f1fa8c;
                font-size: 18px;
                font-weight: bold;
                border: 2px solid #f1fa8c;
                border-radius: 10px;
            }
            QPushButton:hover {
                background-color: #443322;
                border-color: white;
            }
        """)
        panel_layout.addWidget(self.btn_run, alignment=Qt.AlignCenter)
        panel_layout.addStretch()
        
        layout.addWidget(panel)
        return container

    def open_dummy_creator_dialog(self):
        # Implementation ported and cleaned from Workshop
        folder = QFileDialog.getExistingDirectory(None, "Select Folder to Dummify", self.last_asset_dir)
        if not folder: return
        
        self.last_asset_dir = folder
        self.settings.setValue("last_asset_dir", self.last_asset_dir)
        
        stats = get_folder_stats(folder)
        if not stats:
            QMessageBox.warning(None, "Invalid Path", "Could not access folder.")
            return
            
        preview_msg = (
            f"📊 Folder Analysis\n\n"
            f"Total Files: {stats['total_files']}\n"
            f"Already Dummies: {stats['dummies']}\n"
            f"Originals to Process: {stats['originals']}\n\n"
            f"Action: Move {stats['originals']} files to 'originals/' subfolder and create dummy placeholders.\n\n"
            f"⚠️ This operation cannot be easily undone. Continue?"
        )
        
        reply = QMessageBox.question(None, "Confirm Action", preview_msg, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return

        # Progress Dialog
        dlg = QDialog(None)
        dlg.setWindowTitle("Dummy Creator - Processing")
        dlg.setMinimumSize(600, 400)
        dlg_layout = QVBoxLayout(dlg)
        
        log = QTextEdit()
        log.setReadOnly(True)
        log.setStyleSheet("background: #0a0a0a; color: #00ff00; font-family: Consolas; font-size: 12px;")
        dlg_layout.addWidget(log)
        
        pbar = QProgressBar()
        dlg_layout.addWidget(pbar)
        
        btn_close = QPushButton("Close")
        btn_close.setEnabled(False)
        btn_close.clicked.connect(dlg.accept)
        dlg_layout.addWidget(btn_close)
        
        dlg.show()
        
        def on_progress(current, total, filename):
            pbar.setMaximum(total)
            pbar.setValue(current)
            log.append(f"[{current}/{total}] Processed: {filename}")
            QApplication.instance().processEvents()

        try:
            results = process_folder(folder, progress_callback=on_progress)
            space_mb = results['space_saved_bytes'] / (1024 * 1024)
            summary = (
                f"\n--- MISSION COMPLETE ---\n"
                f"Files Dummified: {results['processed']}\n"
                f"Disk Space Reclaimed: {space_mb:.2f} MB\n"
                f"Errors: {results['errors']}\n"
            )
            log.append(summary)
        except Exception as e:
            log.append(f"\n❌ FATAL ERROR: {str(e)}")
            
        btn_close.setEnabled(True)
        dlg.exec()
