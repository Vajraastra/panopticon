from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QSlider, QApplication, QStackedWidget, QGridLayout, QComboBox,
                               QCheckBox, QGroupBox, QTextEdit, QSplitter)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QFont
from core.base_module import BaseModule
from core.theme import Theme
import os
import math
from modules.quality_scorer.logic.quality_scorer import (
    score_image, score_batch, run_full_workflow, 
    get_improvable_count, get_predicted_improvement_summary,
    PROFILES, DEFAULT_PROFILE
)


class QualityScorerModule(BaseModule):
    """
    Módulo Quality Scorer v2.
    Flujo de trabajo:
    1. Scan inicial → score primario
    2. Predicción de mejora potencial
    3. Mejora opcional (si usuario acepta)
    4. Re-scan → scores finales
    5. Catalogación a score/ subfolder
    """
    def __init__(self):
        super().__init__()
        self._name = "Quality Scorer"
        self._description = "Evalúa calidad técnica de imágenes para curación de datasets de IA."
        self._icon = "📊"
        self.accent_color = "#00cc88"
        
        self.view = None
        self.image_paths = []
        self.scan_results = []
        self.last_dir = os.path.expanduser("~")
        self.base_folder = None
        
        # Pagination
        self.current_page = 0
        self.page_size = 100
        self.total_pages = 0
        
        # State
        self.current_profile = DEFAULT_PROFILE

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
        layout.setSpacing(12)

        # Title
        lbl_title = QLabel(self.tr("qs.title", "📊 QUALITY SCORER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        lbl_desc = QLabel(self.tr("qs.desc", "Analyze technical quality for AI dataset curation."))
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(lbl_desc)
        
        layout.addSpacing(5)
        
        # Profile Selector
        lbl_profile = QLabel(self.tr("qs.profile", "Content Profile:"))
        lbl_profile.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 12px;")
        layout.addWidget(lbl_profile)
        
        self.combo_profile = QComboBox()
        for key, profile in PROFILES.items():
            self.combo_profile.addItem(profile["name"], key)
        self.combo_profile.setCurrentIndex(0)
        self.combo_profile.currentIndexChanged.connect(self.on_profile_changed)
        self.combo_profile.setStyleSheet("""
            QComboBox {
                background-color: #222;
                color: #eee;
                border: 1px solid #444;
                border-radius: 6px;
                padding: 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { 
                background-color: #222; 
                color: white; 
                selection-background-color: #00cc88; 
                selection-color: black; 
            }
        """)
        layout.addWidget(self.combo_profile)
        
        layout.addSpacing(10)
        
        # Input Options Group
        input_group = QGroupBox(self.tr("qs.input_title", "📁 Input"))
        input_group.setStyleSheet(f"""
            QGroupBox {{
                color: {Theme.TEXT_SECONDARY};
                font-weight: bold;
                border: 1px solid #333;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }}
        """)
        input_layout = QVBoxLayout(input_group)
        
        self.btn_load_folder = QPushButton(self.tr("qs.load_folder", "📂 Load Folder"))
        self.btn_load_folder.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_load_folder.setFixedHeight(36)
        self.btn_load_folder.clicked.connect(self.load_folder_dialog)
        input_layout.addWidget(self.btn_load_folder)
        
        self.btn_load_image = QPushButton(self.tr("qs.load_image", "🖼️ Load Single Image"))
        self.btn_load_image.setStyleSheet(Theme.get_button_style("#555"))
        self.btn_load_image.setFixedHeight(36)
        self.btn_load_image.clicked.connect(self.load_single_image_dialog)
        input_layout.addWidget(self.btn_load_image)
        
        layout.addWidget(input_group)
        
        layout.addSpacing(10)
        
        # Action Button
        self.btn_scan = QPushButton(self.tr("qs.scan", "🔍 Start Scan"))
        self.btn_scan.setEnabled(False)
        self.btn_scan.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#ffffff"))
        self.btn_scan.setFixedHeight(44)
        self.btn_scan.clicked.connect(self.run_initial_scan)
        layout.addWidget(self.btn_scan)
        
        layout.addSpacing(10)
        
        # Enhancement Options (will be enabled after scan)
        self.enhance_group = QGroupBox(self.tr("qs.enhance_title", "🔧 Enhancement"))
        self.enhance_group.setStyleSheet(f"""
            QGroupBox {{
                color: {Theme.TEXT_SECONDARY};
                font-weight: bold;
                border: 1px solid #333;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }}
        """)
        self.enhance_group.setEnabled(False)
        enhance_layout = QVBoxLayout(self.enhance_group)
        
        self.lbl_improve_info = QLabel(self.tr("qs.improve_info", "Run scan first to see improvement potential."))
        self.lbl_improve_info.setWordWrap(True)
        self.lbl_improve_info.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        enhance_layout.addWidget(self.lbl_improve_info)
        
        self.btn_enhance = QPushButton(self.tr("qs.enhance_btn", "✨ Apply Enhancements & Re-scan"))
        self.btn_enhance.setStyleSheet(Theme.get_button_style("#ffaa00"))
        self.btn_enhance.setFixedHeight(38)
        self.btn_enhance.clicked.connect(self.run_with_enhancements)
        enhance_layout.addWidget(self.btn_enhance)
        
        self.btn_skip = QPushButton(self.tr("qs.skip_btn", "⏭️ Skip Enhancement, Catalog Now"))
        self.btn_skip.setStyleSheet(Theme.get_button_style("#666"))
        self.btn_skip.setFixedHeight(38)
        self.btn_skip.clicked.connect(self.run_catalog_only)
        enhance_layout.addWidget(self.btn_skip)
        
        layout.addWidget(self.enhance_group)
        
        layout.addStretch()
        
        # Note
        note = QLabel(self.tr("qs.note", "Copies saved to 'score/' subfolder. Originals preserved."))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
        layout.addWidget(note)
        
        return container

    def _create_content(self) -> QWidget:
        self.content_stack = QStackedWidget()
        
        # 0. Dropzone Page
        self.dropzone = QFrame()
        self.dropzone.setStyleSheet(f"border: 2px dashed {Theme.BORDER}; border-radius: 20px; background: {Theme.BG_PANEL};")
        dz_layout = QVBoxLayout(self.dropzone)
        dz_layout.setAlignment(Qt.AlignCenter)
        
        lbl_dz = QLabel(self.tr("qs.dropzone", "📥 Drop a folder or image here\nor use the sidebar buttons"))
        lbl_dz.setAlignment(Qt.AlignCenter)
        lbl_dz.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 18px; font-weight: bold;")
        dz_layout.addWidget(lbl_dz)
        self.content_stack.addWidget(self.dropzone)  # Index 0
        
        # 1. Grid Preview Page (after loading)
        self.grid_page = QWidget()
        grid_vbox = QVBoxLayout(self.grid_page)
        
        pag_layout = QHBoxLayout()
        self.btn_prev = QPushButton(self.tr("common.prev", "◀ Previous"))
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next = QPushButton(self.tr("common.next", "Next ▶"))
        self.btn_next.clicked.connect(self.next_page)
        self.lbl_page_info = QLabel("Page 1 of 1")
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
        
        self.content_stack.addWidget(self.grid_page)  # Index 1
        
        # 2. Scan Results Page (after initial scan)
        self.scan_results_page = QWidget()
        scan_vbox = QVBoxLayout(self.scan_results_page)
        
        self.lbl_scan_title = QLabel("📊 Initial Scan Complete")
        self.lbl_scan_title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {self.accent_color}; margin: 10px;")
        scan_vbox.addWidget(self.lbl_scan_title)
        
        self.scan_stats_text = QTextEdit()
        self.scan_stats_text.setReadOnly(True)
        self.scan_stats_text.setStyleSheet(f"""
            QTextEdit {{
                background: {Theme.BG_PANEL};
                color: {Theme.TEXT_PRIMARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }}
        """)
        scan_vbox.addWidget(self.scan_stats_text)
        
        self.content_stack.addWidget(self.scan_results_page)  # Index 2
        
        # 3. Final Results Page (after cataloging)
        self.final_results_page = QWidget()
        final_vbox = QVBoxLayout(self.final_results_page)
        
        self.lbl_final_title = QLabel("✅ Cataloging Complete")
        self.lbl_final_title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {self.accent_color}; margin: 10px;")
        final_vbox.addWidget(self.lbl_final_title)
        
        # Split: Log on left, folder buttons on right
        splitter = QSplitter(Qt.Horizontal)
        
        self.final_log_text = QTextEdit()
        self.final_log_text.setReadOnly(True)
        self.final_log_text.setStyleSheet(f"""
            QTextEdit {{
                background: {Theme.BG_PANEL};
                color: {Theme.TEXT_PRIMARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }}
        """)
        splitter.addWidget(self.final_log_text)
        
        # Folder buttons container
        folders_scroll = QScrollArea()
        folders_scroll.setWidgetResizable(True)
        folders_scroll.setStyleSheet("border: none;")
        self.folders_container = QWidget()
        self.folders_layout = QVBoxLayout(self.folders_container)
        self.folders_layout.setAlignment(Qt.AlignTop)
        folders_scroll.setWidget(self.folders_container)
        splitter.addWidget(folders_scroll)
        
        splitter.setSizes([600, 200])
        final_vbox.addWidget(splitter)
        
        self.content_stack.addWidget(self.final_results_page)  # Index 3
        
        # Main container
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.addWidget(self.content_stack)
        
        # Footer / Progress
        self.lbl_status = QLabel(self.tr("common.status.ready", "Ready."))
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
            extensions = ('.png', '.jpg', '.jpeg', '.webp')
            files = [p for p in paths if p.lower().endswith(extensions)]
            if files:
                self.load_images(files)

    def load_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(None, self.tr("common.select_folder", "Select Folder"), self.last_dir)
        if folder:
            self.load_folder(folder)

    def load_single_image_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            None, 
            self.tr("common.select_image", "Select Image"), 
            self.last_dir,
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.load_images([path])

    def load_folder(self, folder):
        self.last_dir = folder
        self.base_folder = folder
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        self.image_paths = []
        
        for root, _, files in os.walk(folder):
            # Skip score subfolder if it exists
            if "score" in root.split(os.sep):
                continue
            for f in files:
                if f.lower().endswith(extensions):
                    self.image_paths.append(os.path.join(root, f))
        
        self._process_loaded()

    def load_images(self, paths):
        self.image_paths = paths
        if paths:
            self.base_folder = os.path.dirname(paths[0])
            self.last_dir = self.base_folder
        self._process_loaded()

    def _process_loaded(self):
        count = len(self.image_paths)
        if count == 0:
            QMessageBox.warning(None, self.tr("common.error", "Error"), self.tr("qs.msg.no_images", "No images found."))
            return
        
        self.total_pages = math.ceil(count / self.page_size)
        self.current_page = 0
        self.scan_results = []
        self.refresh_grid()
        
        self.content_stack.setCurrentIndex(1)
        self.lbl_status.setText(self.tr("common.status.loaded", "Loaded {count} images.").format(count=count))
        self.btn_scan.setEnabled(True)
        self.enhance_group.setEnabled(False)

    def refresh_grid(self):
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
            
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.image_paths))
        current_batch = self.image_paths[start:end]
        
        cols = 5
        for i, path in enumerate(current_batch):
            row = i // cols
            col = i % cols
            
            thumb = QLabel()
            thumb.setFixedSize(140, 140)
            thumb.setStyleSheet(f"border: 1px solid {Theme.BORDER}; border-radius: 8px; background: #000;")
            thumb.setAlignment(Qt.AlignCenter)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(130, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            self.grid_layout.addWidget(thumb, row, col)
            
        self.lbl_page_info.setText(f"Page {self.current_page + 1} of {self.total_pages}")
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

    def on_profile_changed(self, index):
        self.current_profile = self.combo_profile.currentData()

    def run_initial_scan(self):
        """Step 1: Initial scan to get scores and predict improvements."""
        if not self.image_paths:
            return
        
        self.progress.setRange(0, len(self.image_paths))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_scan.setEnabled(False)
        
        def progress_cb(current, total, path):
            self.progress.setValue(current)
            self.lbl_status.setText(f"Scanning: {os.path.basename(path)} ({current}/{total})")
            QApplication.instance().processEvents()
        
        try:
            self.scan_results = score_batch(self.image_paths, self.current_profile, progress_cb)
            self._show_scan_results()
            
        except Exception as e:
            QMessageBox.critical(None, self.tr("common.error", "Error"), f"Scan failed: {str(e)}")
        finally:
            self.progress.setVisible(False)
            self.btn_scan.setEnabled(True)

    def _show_scan_results(self):
        """Display scan results and enable enhancement options."""
        self.content_stack.setCurrentIndex(2)
        
        # Calculate statistics
        total = len(self.scan_results)
        avg_score = sum(r["composite_score"] for r in self.scan_results) / max(1, total)
        
        # Score distribution
        buckets = {"100%": 0, "90%": 0, "80%": 0, "70%": 0, "60%": 0, "<60%": 0}
        for r in self.scan_results:
            score = r["composite_score"]
            if score >= 100: buckets["100%"] += 1
            elif score >= 90: buckets["90%"] += 1
            elif score >= 80: buckets["80%"] += 1
            elif score >= 70: buckets["70%"] += 1
            elif score >= 60: buckets["60%"] += 1
            else: buckets["<60%"] += 1
        
        # Improvement prediction
        improvement_summary = get_predicted_improvement_summary(self.scan_results)
        
        # Build report
        report = []
        report.append("═" * 50)
        report.append("           📊 INITIAL SCAN RESULTS")
        report.append("═" * 50)
        report.append(f"")
        report.append(f"Total Images: {total}")
        report.append(f"Average Score: {avg_score:.1f}/100")
        report.append(f"Profile: {PROFILES[self.current_profile]['name']}")
        report.append(f"")
        report.append("─" * 50)
        report.append("SCORE DISTRIBUTION:")
        report.append("─" * 50)
        
        for bucket, count in buckets.items():
            bar = "█" * int(count / max(1, total) * 30)
            report.append(f"  {bucket:>6}: {count:>4} {bar}")
        
        report.append("")
        report.append("─" * 50)
        report.append("IMPROVEMENT POTENTIAL:")
        report.append("─" * 50)
        
        if improvement_summary:
            report.append(f"  Images that could improve: {improvement_summary['count']}")
            report.append(f"  Average predicted gain: +{improvement_summary['avg_improvement']:.1f} points")
            report.append("")
            report.append("  → Use 'Apply Enhancements' to improve these images")
            report.append("  → Or 'Skip' to catalog with current scores")
            
            self.lbl_improve_info.setText(
                f"✨ {improvement_summary['count']} images could improve by ~{improvement_summary['avg_improvement']:.1f} points"
            )
        else:
            report.append("  All images are already at optimal quality!")
            report.append("  → Click 'Skip Enhancement' to catalog")
            self.lbl_improve_info.setText("No significant improvements possible.")
        
        report.append("")
        report.append("═" * 50)
        
        self.scan_stats_text.setPlainText("\n".join(report))
        
        # Enable enhancement options
        self.enhance_group.setEnabled(True)
        self.lbl_status.setText("Scan complete. Choose action.")

    def run_with_enhancements(self):
        """Run full workflow WITH enhancements."""
        self._run_catalog(apply_enhancements=True)

    def run_catalog_only(self):
        """Run cataloging WITHOUT enhancements."""
        self._run_catalog(apply_enhancements=False)

    def _run_catalog(self, apply_enhancements=False):
        """Execute cataloging workflow."""
        if not self.scan_results:
            return
        
        action = "Enhancing & Cataloging" if apply_enhancements else "Cataloging"
        
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.enhance_group.setEnabled(False)
        
        def progress_cb(step, curr, total, name):
            pct = int((curr / max(1, total)) * 100)
            self.progress.setValue(pct)
            self.lbl_status.setText(f"{step}: {name} ({curr}/{total})")
            QApplication.instance().processEvents()
        
        try:
            stats = run_full_workflow(
                self.image_paths,
                self.base_folder,
                profile=self.current_profile,
                apply_enhancements=apply_enhancements,
                progress_callback=progress_cb
            )
            
            self._show_final_results(stats)
            
        except Exception as e:
            QMessageBox.critical(None, self.tr("common.error", "Error"), f"Workflow failed: {str(e)}")
            self.enhance_group.setEnabled(True)
        finally:
            self.progress.setVisible(False)

    def _show_final_results(self, stats):
        """Display final results with log and folder buttons."""
        self.content_stack.setCurrentIndex(3)
        
        # Show log
        full_log = "\n".join(stats["log"])
        self.final_log_text.setPlainText(full_log)
        
        # Clear and create folder buttons
        while self.folders_layout.count():
            item = self.folders_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        
        # Title
        lbl = QLabel("📂 Output Folders")
        lbl.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 13px;")
        self.folders_layout.addWidget(lbl)
        
        # Root folder button
        btn_root = QPushButton("📁 Open score/ folder")
        btn_root.setStyleSheet(Theme.get_button_style(self.accent_color))
        btn_root.clicked.connect(lambda: os.startfile(stats["output_folder"]))
        self.folders_layout.addWidget(btn_root)
        
        # Category buttons
        for bucket in ["100%", "90%", "80%", "70%", "60%", "below_60%"]:
            count = len(stats["categories"].get(bucket, []))
            if count > 0:
                folder_path = os.path.join(stats["output_folder"], bucket)
                btn = QPushButton(f"{bucket}: {count} images")
                btn.setStyleSheet(Theme.get_button_style("#555"))
                btn.clicked.connect(lambda checked=False, p=folder_path: os.startfile(p))
                self.folders_layout.addWidget(btn)
        
        self.folders_layout.addStretch()
        
        # Summary
        self.lbl_final_title.setText(f"✅ Cataloging Complete: {stats['total_images']} images processed")
        self.lbl_status.setText(f"Output: {stats['output_folder']}")

    def on_load(self, context):
        super().on_load(context)
        
    def load_image_set(self, paths: list):
        """Standard interface for receiving images from other modules."""
        if paths:
            self.load_images(paths)
