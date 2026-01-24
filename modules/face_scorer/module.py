from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QTextEdit, QSlider, QApplication, QStackedWidget, QGridLayout)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QImage
from core.base_module import BaseModule
from core.theme import Theme
import os
import math
from modules.face_scorer.logic.face_scorer import score_batch, sort_files_by_score

class FaceScorerModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.view = None
        self.fs_image_paths = []
        self.fs_results = []
        self.last_dir = os.path.expanduser("~")
        
        # Pagination
        self.current_page = 0
        self.page_size = 100
        self.total_pages = 0
        
    @property
    def name(self):
        return "Face Scorer"

    @property
    def description(self):
        return "Califica imágenes por la claridad del rostro para curar datasets óptimos."

    @property
    def icon(self):
        return "🎯"

    @property
    def accent_color(self):
        return "#ff5555"

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        content = self._create_content()
        sidebar = self._create_sidebar()
        
        from core.components.standard_layout import StandardToolLayout
        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)

        lbl_title = QLabel("🎯 FACE SCORER")
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        lbl_desc = QLabel("Analiza la calidad de las imágenes basándose en la confianza del rostro detectado y su nitidez.")
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(lbl_desc)
        
        layout.addSpacing(10)
        
        self.btn_load = QPushButton("📂 Cargar Carpeta")
        self.btn_load.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_load.setFixedHeight(40)
        self.btn_load.clicked.connect(self.load_folder_dialog)
        layout.addWidget(self.btn_load)
        
        self.btn_analyze = QPushButton("🔍 Analizar y Auto-Clasificar")
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_analyze.setFixedHeight(40)
        self.btn_analyze.clicked.connect(self.run_analysis)
        layout.addWidget(self.btn_analyze)
        
        layout.addSpacing(10)
        
        # Threshold Slider
        lbl_threshold = QLabel("Umbral de Puntuación Mínima:")
        lbl_threshold.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(lbl_threshold)
        
        threshold_row = QHBoxLayout()
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 100)
        self.threshold_slider.setValue(50)
        self.threshold_slider.valueChanged.connect(self.update_threshold_label)
        threshold_row.addWidget(self.threshold_slider)
        
        self.lbl_threshold_val = QLabel("50")
        self.lbl_threshold_val.setFixedWidth(35)
        self.lbl_threshold_val.setStyleSheet(f"color: {self.accent_color}; font-weight: bold;")
        threshold_row.addWidget(self.lbl_threshold_val)
        layout.addLayout(threshold_row)
        
        layout.addStretch()
        
        note = QLabel("Las imágenes se clasificarán en subcarpetas (100%/, 90%...) según su puntuación.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
        layout.addWidget(note)
        
        return container

    def _create_content(self) -> QWidget:
        self.content_stack = QStackedWidget()
        
        # 1. Dropzone Page
        self.dropzone = QFrame()
        self.dropzone.setStyleSheet(f"border: 2px dashed {Theme.BORDER}; border-radius: 20px; background: {Theme.BG_PANEL};")
        dz_layout = QVBoxLayout(self.dropzone)
        dz_layout.setAlignment(Qt.AlignCenter)
        
        lbl_dz = QLabel("📥 Arrastra una carpeta aquí\no usa el botón 'Cargar Carpeta'")
        lbl_dz.setAlignment(Qt.AlignCenter)
        lbl_dz.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 18px; font-weight: bold;")
        dz_layout.addWidget(lbl_dz)
        self.content_stack.addWidget(self.dropzone) # Index 0
        
        # 2. Grid Page (Selection)
        self.grid_page = QWidget()
        grid_vbox = QVBoxLayout(self.grid_page)
        
        # Pagination Controls
        pag_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀ Anterior")
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next = QPushButton("Siguiente ▶")
        self.btn_next.clicked.connect(self.next_page)
        self.lbl_page_info = QLabel("Página 1 de 1")
        self.lbl_page_info.setStyleSheet(f"color: {self.accent_color}; font-weight: bold;")
        
        pag_layout.addWidget(self.btn_prev)
        pag_layout.addStretch()
        pag_layout.addWidget(self.lbl_page_info)
        pag_layout.addStretch()
        pag_layout.addWidget(self.btn_next)
        grid_vbox.addLayout(pag_layout)
        
        self.scroll_grid = QScrollArea()
        self.scroll_grid.setWidgetResizable(True)
        self.scroll_grid.setStyleSheet("border: none; background: transparent;")
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setSpacing(10)
        self.scroll_grid.setWidget(self.grid_container)
        grid_vbox.addWidget(self.scroll_grid)
        
        self.content_stack.addWidget(self.grid_page) # Index 1
        
        # 3. Results Page
        self.results_page = QWidget()
        res_vbox = QVBoxLayout(self.results_page)
        
        self.scroll_res = QScrollArea()
        self.scroll_res.setWidgetResizable(True)
        self.scroll_res.setStyleSheet("border: none; background: transparent;")
        self.res_container = QWidget()
        self.res_layout = QVBoxLayout(self.res_container)
        self.scroll_res.setWidget(self.res_container)
        res_vbox.addWidget(self.scroll_res)
        
        # Folder Buttons Area
        self.folders_widget = QWidget()
        self.folders_layout = QHBoxLayout(self.folders_widget)
        self.folders_layout.setAlignment(Qt.AlignLeft)
        res_vbox.addWidget(self.folders_widget)
        
        self.content_stack.addWidget(self.results_page) # Index 2
        
        # Main Layout
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.content_stack)
        
        # Footer / Progress
        self.lbl_status = QLabel("Listo. Carga una carpeta para comenzar.")
        self.lbl_status.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; padding: 5px;")
        layout.addWidget(self.lbl_status)
        
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setStyleSheet("height: 8px;")
        layout.addWidget(self.progress)
        
        # Drag and Drop
        container.setAcceptDrops(True)
        container.dragEnterEvent = self.dragEnterEvent
        container.dropEvent = self.dropEvent
        
        return container

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        folders = [p for p in paths if os.path.isdir(p)]
        if folders:
            self.load_folder(folders[0])
        else:
            # Maybe it's a list of files?
            extensions = ('.png', '.jpg', '.jpeg', '.webp')
            files = [p for p in paths if p.lower().endswith(extensions)]
            if files:
                self.fs_image_paths = files
                self.process_loaded_files()

    def load_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(None, "Seleccionar carpeta con imágenes", self.last_dir)
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder):
        self.last_dir = folder
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        self.fs_image_paths = []
        
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(extensions):
                    self.fs_image_paths.append(os.path.join(root, f))
        
        self.process_loaded_files()

    def process_loaded_files(self):
        count = len(self.fs_image_paths)
        if count == 0:
            QMessageBox.warning(None, "Sin imágenes", "No se encontraron imágenes en la ruta seleccionada.")
            return
            
        self.total_pages = math.ceil(count / self.page_size)
        self.current_page = 0
        self.refresh_grid()
        
        self.content_stack.setCurrentIndex(1)
        self.lbl_status.setText(f"Cargadas {count} imágenes. Lista para analizar.")
        self.btn_analyze.setEnabled(True)

    def refresh_grid(self):
        # Clear existing
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.fs_image_paths))
        current_batch = self.fs_image_paths[start:end]
        
        cols = 5
        for i, path in enumerate(current_batch):
            row = i // cols
            col = i % cols
            
            thumb = QLabel()
            thumb.setFixedSize(140, 140)
            thumb.setStyleSheet(f"border: 1px solid {Theme.BORDER}; border-radius: 8px; background: #000;")
            thumb.setAlignment(Qt.AlignCenter)
            
            # Lazy load pixmap
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(130, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            self.grid_layout.addWidget(thumb, row, col)
            
        self.lbl_page_info.setText(f"Página {self.current_page + 1} de {self.total_pages}")
        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled(self.current_page < self.total_pages - 1)

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.refresh_grid()

    def next_page(self):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.refresh_grid()

    def run_analysis(self):
        if not self.fs_image_paths: return
        
        self.progress.setRange(0, len(self.fs_image_paths))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_analyze.setEnabled(False)
        self.btn_load.setEnabled(False)
        
        def progress_cb(current, total, path):
            self.progress.setValue(current)
            self.lbl_status.setText(f"Analizando: {os.path.basename(path)} ({current}/{total})")
            QApplication.instance().processEvents()
        
        try:
            # We need to use base folder for moving
            base_folder = os.path.dirname(self.fs_image_paths[0])
            results = score_batch(self.fs_image_paths, progress_cb)
            
            threshold = self.threshold_slider.value()
            
            self.lbl_status.setText("Ordenando archivos...")
            QApplication.instance().processEvents()
            
            # Note: sort_files_by_score MOVES files, so we should keep track of where they went
            stats = sort_files_by_score(results, threshold, base_folder=base_folder, move_files=True)
            self._display_results_visual(results, stats, base_folder)
            
        except Exception as e:
            QMessageBox.critical(None, "Error", f"El análisis falló: {str(e)}")
        finally:
            self.progress.setVisible(False)
            self.btn_analyze.setEnabled(True)
            self.btn_load.setEnabled(True)
            self.fs_image_paths = [] 

    def _display_results_visual(self, results, stats, base_folder):
        # Clear results layout
        while self.res_layout.count():
            item = self.res_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            
        self.content_stack.setCurrentIndex(2)
        
        # Summary Header
        summary = QLabel(f"🎯 Análisis Completado: {stats['total_moved']} imágenes organizadas.")
        summary.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {self.accent_color}; margin: 10px;")
        self.res_layout.addWidget(summary)
        
        # Categories
        buckets = sorted(stats["moved_counts"].items(), key=lambda x: int(x[0].replace('%', '')), reverse=True)
        
        for bucket, count in buckets:
            cat_frame = QFrame()
            cat_frame.setStyleSheet(f"background: {Theme.BG_PANEL}; border-radius: 10px; margin-bottom: 20px;")
            cat_layout = QVBoxLayout(cat_frame)
            
            title = QLabel(f"📂 Clase: {bucket} ({count} imágenes)")
            title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {Theme.TEXT_PRIMARY};")
            cat_layout.addWidget(title)
            
            # Grid for this bucket
            bucket_grid_widget = QWidget()
            # Limited preview of images from results that match this bucket score
            bucket_grid = QGridLayout(bucket_grid_widget)
            bucket_grid.setSpacing(5)
            
            threshold_low = int(bucket.replace('%', ''))
            threshold_high = threshold_low + 10
            
            # Find matching results (max 15 for preview)
            matches = [r for r in results if threshold_low <= r['composite_score'] < threshold_high]
            if threshold_low == 100:
                 matches = [r for r in results if r['composite_score'] == 100]
            
            for i, r in enumerate(matches[:15]): 
                # Note: original file was moved to bucket subfolder
                new_path = os.path.join(base_folder, bucket, os.path.basename(r['path']))
                
                thumb = QLabel()
                thumb.setFixedSize(80, 80)
                thumb.setStyleSheet("border: 1px solid #444; border-radius: 4px; background: #000;")
                
                pix = QPixmap(new_path if os.path.exists(new_path) else r['path'])
                if not pix.isNull():
                    thumb.setPixmap(pix.scaled(70, 70, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                bucket_grid.addWidget(thumb, i // 8, i % 8)
            
            cat_layout.addWidget(bucket_grid_widget)
            self.res_layout.addWidget(cat_frame)
            
        if not buckets:
            no_res = QLabel("No se encontraron imágenes que superen el umbral.")
            no_res.setAlignment(Qt.AlignCenter)
            self.res_layout.addWidget(no_res)
            
        self.res_layout.addStretch()
        
        # Setup Folder Buttons
        self._setup_folder_buttons(buckets, base_folder)

    def _setup_folder_buttons(self, buckets, base_folder):
        while self.folders_layout.count():
            item = self.folders_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        
        btn_root = QPushButton("📂 Abrir Carpeta Raíz")
        btn_root.setStyleSheet(Theme.get_button_style("#444"))
        btn_root.clicked.connect(lambda: os.startfile(base_folder))
        self.folders_layout.addWidget(btn_root)
        
        for bucket, _ in buckets:
            p = os.path.join(base_folder, bucket)
            btn = QPushButton(f"Abrir {bucket}")
            btn.setStyleSheet(Theme.get_button_style(self.accent_color))
            btn.clicked.connect(lambda checked=False, path=p: os.startfile(path))
            self.folders_layout.addWidget(btn)

    def update_threshold_label(self, val):
        self.lbl_threshold_val.setText(str(val))

    def on_load(self, context):
        super().on_load(context)
        
    def load_image_set(self, paths: list):
        if paths:
            self.fs_image_paths = paths
            self.process_loaded_files()
