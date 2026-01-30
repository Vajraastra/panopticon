from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QFileDialog, QMessageBox, QProgressBar, QTextEdit, QApplication, QDialog)
from PySide6.QtCore import Qt, QSettings
from core.base_module import BaseModule
from .logic.logic import process_folder, get_folder_stats
import os

class DummyCreatorModule(BaseModule):
    """
    Módulo Dummy Creator (Creador de Marcadores).
    Herramienta diseñada para ahorrar espacio en disco reemplazando imágenes pesadas
    por archivos "dummy" de 32x32 píxeles, manteniendo los originales en una 
    carpeta segura para su posterior restauración si es necesario.
    """
    def __init__(self):
        super().__init__()
        self._name = "Dummy Creator"
        self._description = "Archivado de colecciones reemplazando imágenes por marcadores ligeros."
        self._icon = "🎭"
        
        self.view = None
        self.settings = QSettings("Panopticon", "DummyCreator")
        self.last_asset_dir = self.settings.value("last_asset_dir", os.path.expanduser("~"))

    def get_view(self) -> QWidget:
        """Configura la interfaz centrada en la acción de selección de carpeta."""
        if self.view: return self.view
        
        content = self._create_content() # Botón de acción principal
        
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            content,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_content(self) -> QWidget:
        from core.theme import Theme
        accent = "#f1fa8c"
        
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 50, 0, 50)
        layout.setAlignment(Qt.AlignCenter)
        
        # Centering Panel
        panel = QWidget()
        panel.setMaximumWidth(800)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setSpacing(20)
        
        lbl_info = QLabel(self.tr("dummy.title", "🎭 Dummy Creator"))
        lbl_info.setAlignment(Qt.AlignCenter)
        lbl_info.setStyleSheet(f"font-size: 28px; font-weight: bold; color: {accent}; margin-bottom: 10px;")
        panel_layout.addWidget(lbl_info)
        
        lbl_desc = QLabel(self.tr("dummy.desc", "Archive collections by replacing images with lightweight placeholders."))
        lbl_desc.setAlignment(Qt.AlignCenter)
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"font-size: 14px; color: {Theme.TEXT_DIM}; line-height: 1.6;")
        panel_layout.addWidget(lbl_desc)
        
        panel_layout.addSpacing(40)
        
        self.btn_run = QPushButton(self.tr("dummy.btn_select", "📂 Select Folder to Dummify"))
        self.btn_run.setFixedSize(350, 60)
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.clicked.connect(self.open_dummy_creator_dialog)
        self.btn_run.setStyleSheet(f"""
            QPushButton {{
                background-color: #332211;
                color: {accent};
                font-size: 18px;
                font-weight: bold;
                border: 2px solid {accent};
                border-radius: 10px;
            }}
            QPushButton:hover {{
                background-color: #443322;
                border-color: white;
            }}
        """)
        panel_layout.addWidget(self.btn_run, alignment=Qt.AlignCenter)
        panel_layout.addStretch()
        
        layout.addWidget(panel)
        return container

    def open_dummy_creator_dialog(self):
        # Implementation ported and cleaned from Workshop
        folder = QFileDialog.getExistingDirectory(None, self.tr("common.select_folder", "Select Folder to Dummify"), self.last_asset_dir)
        if not folder: return
        
        self.last_asset_dir = folder
        self.settings.setValue("last_asset_dir", self.last_asset_dir)
        
        stats = get_folder_stats(folder)
        if not stats:
            QMessageBox.warning(None, self.tr("common.error", "Invalid Path"), self.tr("dummy.invalid", "Could not access folder."))
            return
            
        preview_msg = self.tr("dummy.analysis", "📊 Folder Analysis...").format(
            total=stats['total_files'],
            dummies=stats['dummies'],
            originals=stats['originals']
        )
        
        reply = QMessageBox.question(None, self.tr("common.confirm", "Confirm Action"), preview_msg, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return

        # Progress Dialog
        dlg = QDialog(None)
        dlg.setWindowTitle(self.tr("dummy.processing", "Dummy Creator - Processing"))
        dlg.setMinimumSize(600, 400)
        dlg_layout = QVBoxLayout(dlg)
        
        log = QTextEdit()
        log.setReadOnly(True)
        log.setStyleSheet("background: #0a0a0a; color: #00ff00; font-family: Consolas; font-size: 12px;")
        dlg_layout.addWidget(log)
        
        pbar = QProgressBar()
        dlg_layout.addWidget(pbar)
        
        btn_close = QPushButton(self.tr("dummy.close", "Close"))
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
            summary = self.tr("dummy.summary", "\n--- MISSION COMPLETE ---\n").format(
                processed=results['processed'],
                saved=f"{space_mb:.2f}",
                errors=results['errors']
            )
            log.append(summary)
        except Exception as e:
            log.append(f"\n❌ FATAL ERROR: {str(e)}")
            
        btn_close.setEnabled(True)
        dlg.exec()
