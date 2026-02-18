import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, 
                               QHBoxLayout, QFileDialog, QMessageBox, QProgressBar, 
                               QFrame, QScrollArea, QSlider, QComboBox, QCheckBox)
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap
from core.base_module import BaseModule
from core.components.standard_layout import StandardToolLayout
from .logic.deduplicator import Deduplicator

class DeduplicationWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(dict)
    
    def __init__(self, folder, mode, threshold=5):
        super().__init__()
        self.folder = folder
        self.mode = mode
        self.threshold = threshold
        self.engine = Deduplicator()

    def run(self):
        if self.mode == "hash":
            res = self.engine.find_duplicates_by_hash(self.folder, self.progress.emit)
        else:
            res = self.engine.find_duplicates_visual(self.folder, self.threshold, self.progress.emit)
        self.finished.emit(res)

    def stop(self):
        self.engine.stop()

class DuplicateItem(QFrame):
    """Representa una miniatura con su checkbox y metadatos."""
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.setFixedSize(140, 180)
        self.setStyleSheet(f"background: #111; border: 1px solid #333; border-radius: 8px;")
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        
        # Thumbnail
        self.lbl_img = QLabel()
        self.lbl_img.setFixedSize(130, 100)
        self.lbl_img.setAlignment(Qt.AlignCenter)
        
        if path.lower().endswith(('.zip', '.rar')):
            self.lbl_img.setText("📦\nARCHIVE")
            self.lbl_img.setStyleSheet("font-size: 24px; color: #ffcc00;")
        else:
            pix = QPixmap(path)
            if not pix.isNull():
                self.lbl_img.setPixmap(pix.scaled(130, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.lbl_img.setText("❌")

        layout.addWidget(self.lbl_img)
        
        # Checkbox + Name
        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")
        
        name_lbl = QLabel(os.path.basename(path))
        name_lbl.setStyleSheet("font-size: 10px; color: #aaa;")
        name_lbl.setWordWrap(True)
        name_lbl.setMaximumHeight(30)
        
        chk_layout = QHBoxLayout()
        chk_layout.addWidget(self.check)
        chk_layout.addWidget(name_lbl)
        layout.addLayout(chk_layout)
        
        # Info
        size_mb = os.path.getsize(path) / (1024 * 1024)
        info_lbl = QLabel(f"{size_mb:.2f} MB")
        info_lbl.setStyleSheet("font-size: 9px; color: #666;")
        layout.addWidget(info_lbl)

    def is_checked(self): return self.check.isChecked()
    def set_checked(self, val): self.check.setChecked(val)

class DuplicateFinderModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Duplicate Finder"
        self._description = "Busca y elimina duplicados por hash o similitud visual."
        self._icon = "👯"
        self.accent_color = "#00ffcc"
        self.view = None
        self.worker = None
        self.duplicate_groups = {} # id -> [DuplicateItem]

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        self.sidebar = self._create_sidebar()
        self.content = self._create_content()
        self.bottom = self._create_bottom_bar()
        
        self.view = StandardToolLayout(
            self.content,
            sidebar_widget=self.sidebar,
            bottom_widget=self.bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(20)
        
        lbl_title = QLabel("⚙ OPCIONES DE ESCANEO")
        lbl_title.setStyleSheet("font-weight: bold; color: #00ffcc;")
        layout.addWidget(lbl_title)
        
        # Mode selector
        layout.addWidget(QLabel("Modo de Comparación:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Hash (Exact Match)", "Visual (Similarity)"])
        # Forzar fondo opaco específico para este combo por si el global falla
        self.combo_mode.setStyleSheet("""
            QComboBox {
                color: #ffffff;
                background-color: #000000;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1a1a;
                color: #ffffff;
                selection-background-color: #00ffcc;
                selection-color: #000000;
                border: 1px solid #333;
                outline: 0px;
            }
        """)
        layout.addWidget(self.combo_mode)
        
        # Threshold (for visual)
        self.threshold_container = QWidget()
        thr_layout = QVBoxLayout(self.threshold_container)
        thr_layout.setContentsMargins(0, 0, 0, 0)
        thr_layout.addWidget(QLabel("Tolerancia Visual (menor = más exacto):"))
        self.slider_thr = QSlider(Qt.Horizontal)
        self.slider_thr.setRange(1, 20)
        self.slider_thr.setValue(5)
        thr_layout.addWidget(self.slider_thr)
        self.lbl_thr_val = QLabel("Valor: 5")
        self.slider_thr.valueChanged.connect(lambda v: self.lbl_thr_val.setText(f"Valor: {v}"))
        thr_layout.addWidget(self.lbl_thr_val)
        layout.addWidget(self.threshold_container)
        
        # Folder selection
        self.btn_select = QPushButton("📁 Seleccionar Carpeta")
        self.btn_select.clicked.connect(self.select_folder)
        self.btn_select.setStyleSheet("background: #222; border: 1px solid #444; padding: 10px;")
        layout.addWidget(self.btn_select)
        
        self.lbl_folder = QLabel("Ninguna carpeta seleccionada")
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.lbl_folder)
        
        layout.addStretch()
        
        self.btn_run = QPushButton("🚀 INICIAR ESCANEO")
        self.btn_run.clicked.connect(self.run_scan)
        self.btn_run.setStyleSheet("background: #00ffcc; color: black; font-weight: bold; padding: 12px;")
        layout.addWidget(self.btn_run)
        
        return container

    def _create_content(self):
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: transparent; border: none;")
        
        self.grid_container = QWidget()
        self.grid_layout = QVBoxLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignTop)
        self.grid_layout.setSpacing(20)
        
        self.scroll.setWidget(self.grid_container)
        
        # Placeholder
        self.lbl_empty = QLabel("Selecciona una carpeta e inicia el escaneo para encontrar duplicados.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setStyleSheet("color: #444; font-size: 16px;")
        self.grid_layout.addWidget(self.lbl_empty)
        
        return self.scroll

    def _create_bottom_bar(self):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)
        
        self.progress = QProgressBar()
        self.progress.setStyleSheet("QProgressBar { height: 10px; }")
        self.progress.hide()
        layout.addWidget(self.progress, 1)
        
        self.btn_auto_select = QPushButton("🧹 Borrar todo menos el primero")
        self.btn_auto_select.clicked.connect(self.auto_select_duplicates)
        self.btn_auto_select.setStyleSheet("background: #333; color: white; padding: 10px 20px; border-radius: 4px;")
        self.btn_auto_select.setEnabled(False)
        layout.addWidget(self.btn_auto_select)
        
        self.btn_delete = QPushButton("🔥 ELIMINAR SELECCIONADOS")
        self.btn_delete.clicked.connect(self.delete_files)
        self.btn_delete.setStyleSheet("background: #ff5555; color: white; font-weight: bold; padding: 10px 20px; border-radius: 4px;")
        self.btn_delete.setEnabled(False)
        layout.addWidget(self.btn_delete)
        
        return container

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Seleccionar Carpeta")
        if folder:
            self.folder_path = folder
            self.lbl_folder.setText(folder)

    def run_scan(self):
        if not hasattr(self, 'folder_path'):
            QMessageBox.warning(self.view, "Error", "Por favor selecciona una carpeta primero.")
            return
            
        self.btn_run.setEnabled(False)
        self.progress.show()
        self.progress.setValue(0)
        
        mode = "hash" if self.combo_mode.currentIndex() == 0 else "visual"
        self.worker = DeduplicationWorker(self.folder_path, mode, self.slider_thr.value())
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.start()

    def update_progress(self, curr, total, msg):
        self.progress.setMaximum(total)
        self.progress.setValue(curr)

    def on_scan_finished(self, results):
        self.btn_run.setEnabled(True)
        self.progress.hide()
        
        # Clear grid
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            
        self.duplicate_groups = {}
        
        if not results:
            self.grid_layout.addWidget(QLabel("No se encontraron duplicados."))
            self.btn_auto_select.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        for g_id, paths in results.items():
            row_frame = QFrame()
            row_frame.setStyleSheet("background: #0a0a0a; border-radius: 10px; border: 1px solid #222;")
            row_layout = QVBoxLayout(row_frame)
            
            # Group Header
            header = QLabel(f"Grupo: {g_id[:8]}... ({len(paths)} archivos)")
            header.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; padding: 5px;")
            row_layout.addWidget(header)
            
            # Row Items
            items_container = QWidget()
            items_layout = QHBoxLayout(items_container)
            items_layout.setAlignment(Qt.AlignLeft)
            
            group_items = []
            for p in paths:
                item = DuplicateItem(p)
                items_layout.addWidget(item)
                group_items.append(item)
            
            row_layout.addWidget(items_container)
            self.grid_layout.addWidget(row_frame)
            self.duplicate_groups[g_id] = group_items

        self.btn_auto_select.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def auto_select_duplicates(self):
        """Implementa 'Borrar todo menos el primero'."""
        for items in self.duplicate_groups.values():
            for i, item in enumerate(items):
                item.set_checked(i > 0) # Marca todos menos el primero

    def delete_files(self):
        to_delete = []
        full_groups_deleted = 0
        
        for g_id, items in self.duplicate_groups.items():
            all_marked = True
            group_to_del = []
            for item in items:
                if item.is_checked():
                    group_to_del.append(item.path)
                else:
                    all_marked = False
            
            if all_marked: full_groups_deleted += 1
            to_delete.extend(group_to_del)

        if not to_delete:
            QMessageBox.information(self.view, "Información", "No hay archivos seleccionados.")
            return

        # Double Check Safety
        confirm_msg = f"¿Estás seguro de eliminar {len(to_delete)} archivos permanentemente?\n\n"
        if full_groups_deleted > 0:
            confirm_msg += f"⚠ ADVERTENCIA: Vas a eliminar TODAS las copias de {full_groups_deleted} grupos. Te quedarás sin archivos originales en esos casos."
        
        res = QMessageBox.critical(self.view, "CONFIRMACIÓN DE BORRADO", confirm_msg, 
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        
        if res == QMessageBox.Yes:
            deleted_count = 0
            for path in to_delete:
                try:
                    os.remove(path)
                    deleted_count += 1
                except Exception as e:
                    print(f"Error borrando {path}: {e}")
            
            QMessageBox.information(self.view, "Proceso Completado", f"Se eliminaron {deleted_count} archivos correctamente.")
            self.run_scan() # Refresh
