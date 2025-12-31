from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QGridLayout, QFrame, QFileDialog, QMessageBox, QProgressBar)
from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QPixmap, QIcon, QDragEnterEvent, QDropEvent
from core.base_module import BaseModule
from modules.librarian.module import ClickableThumbnail
from modules.workshop.logic.stripper import strip_metadata, get_export_path
import os

class WorkshopModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.queue_paths = []
        self.selected_paths = set()
        self.export_dir = os.path.abspath("exports")
        self.view = None

    @property
    def name(self):
        return "The Workshop"

    @property
    def description(self):
        return "Batch processing, metadata stripping, and image transformations."

    @property
    def icon(self):
        return "🛠️"

    def get_view(self):
        if not self.view:
            self.view = QWidget()
            self.view.setAcceptDrops(True)
            # Inject drop events into the module logic
            self.view.dragEnterEvent = self.dragEnterEvent
            self.view.dropEvent = self.dropEvent
            
            layout = QVBoxLayout(self.view)
            layout.setContentsMargins(20, 20, 20, 20)

            # --- Header ---
            header = QHBoxLayout()
            lbl_title = QLabel("🛠️ The Workshop")
            lbl_title.setStyleSheet("font-size: 24px; font-weight: bold; color: #00ffcc;")
            header.addWidget(lbl_title)
            
            self.btn_clear = QPushButton("🗑️ Clear Queue")
            self.btn_clear.clicked.connect(self.clear_queue)
            self.btn_clear.setStyleSheet("background-color: #444; color: white; padding: 5px 15px; border-radius: 5px;")
            
            self.btn_clean_selected = QPushButton("🧹 Clean Selected")
            self.btn_clean_selected.setEnabled(False)
            self.btn_clean_selected.clicked.connect(self.remove_selected)
            self.btn_clean_selected.setStyleSheet("""
                QPushButton { background-color: #442222; color: #ff5555; padding: 5px 15px; border-radius: 5px; border: 1px solid #ff5555; }
                QPushButton:disabled { color: #555; border-color: #333; }
            """)
            
            header.addStretch()
            header.addWidget(self.btn_clean_selected)
            header.addWidget(self.btn_clear)
            layout.addLayout(header)

            # --- Tool Selection Panel (Center) ---
            tool_select_layout = QHBoxLayout()
            tool_select_layout.setContentsMargins(0, 20, 0, 20)
            tool_select_layout.addStretch()
            
            lbl_select = QLabel("SELECT A TOOL:")
            lbl_select.setStyleSheet("color: #888; font-weight: bold; font-size: 12px; margin-right: 15px;")
            tool_select_layout.addWidget(lbl_select)
            
            # Metadata Stripper Tool Button
            self.btn_tool_stripper = QPushButton("🛡️ Metadata Stripper")
            self.btn_tool_stripper.setCheckable(True)
            self.btn_tool_stripper.setChecked(True)
            self.btn_tool_stripper.clicked.connect(self.switch_to_stripper)
            self.btn_tool_stripper.setFixedSize(200, 60)
            self.btn_tool_stripper.setStyleSheet("""
                QPushButton { 
                    background-color: #222; 
                    color: #00ffcc; 
                    text-align: center; 
                    padding: 10px; 
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 14px;
                    border: 2px solid #333;
                }
                QPushButton:checked { 
                    background-color: #224433; 
                    border: 2px solid #00ffcc; 
                }
                QPushButton:hover:!checked { 
                    background-color: #2a2a2a; 
                    border: 2px solid #555;
                }
            """)
            tool_select_layout.addWidget(self.btn_tool_stripper)
            
            tool_select_layout.addSpacing(15)
            
            # Dummy Creator Tool Button
            self.btn_tool_dummy = QPushButton("🎭 Dummy Creator")
            self.btn_tool_dummy.setCheckable(True)
            self.btn_tool_dummy.clicked.connect(self.switch_to_dummy)
            self.btn_tool_dummy.setFixedSize(200, 60)
            self.btn_tool_dummy.setStyleSheet("""
                QPushButton { 
                    background-color: #222; 
                    color: #f1fa8c; 
                    text-align: center; 
                    padding: 10px; 
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 14px;
                    border: 2px solid #333;
                }
                QPushButton:checked { 
                    background-color: #332211; 
                    border: 2px solid #f1fa8c; 
                }
                QPushButton:hover:!checked { 
                    background-color: #2a2a2a; 
                    border: 2px solid #555;
                }
            """)
            tool_select_layout.addWidget(self.btn_tool_dummy)
            
            tool_select_layout.addStretch()
            layout.addLayout(tool_select_layout)

            # --- Metadata Stripper Content Area ---
            self.stripper_panel = QWidget()
            stripper_layout = QVBoxLayout(self.stripper_panel)
            stripper_layout.setContentsMargins(0, 0, 0, 0)
            
            # Stripper Controls
            stripper_controls = QHBoxLayout()
            
            # Left: Input Buttons
            input_panel = QFrame()
            input_panel.setFixedWidth(250)
            input_panel.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 15px;")
            input_layout = QVBoxLayout(input_panel)
            
            lbl_input = QLabel("📥 ADD TO QUEUE")
            lbl_input.setStyleSheet("color: #888; font-weight: bold; font-size: 10px; margin-bottom: 5px;")
            input_layout.addWidget(lbl_input)

            self.btn_add_files = QPushButton("🖼️ Add Images...")
            self.btn_add_files.clicked.connect(self.add_images_dialog)
            self.btn_add_files.setStyleSheet("background-color: #333; color: white; padding: 10px; border-radius: 5px; text-align: left;")
            input_layout.addWidget(self.btn_add_files)

            self.btn_add_folder = QPushButton("📂 Add Folder...")
            self.btn_add_folder.clicked.connect(self.add_folder_dialog)
            self.btn_add_folder.setStyleSheet("background-color: #333; color: white; padding: 10px; border-radius: 5px; text-align: left;")
            input_layout.addWidget(self.btn_add_folder)

            input_layout.addSpacing(20)
            
            # Export Settings
            lbl_set = QLabel("⚙️ SETTINGS")
            lbl_set.setStyleSheet("color: #888; font-weight: bold; font-size: 10px; margin-bottom: 5px;")
            input_layout.addWidget(lbl_set)

            lbl_export_info = QLabel("Export Folder:")
            lbl_export_info.setStyleSheet("color: #666; font-size: 11px;")
            input_layout.addWidget(lbl_export_info)

            self.lbl_export_path = QLabel(self.export_dir)
            self.lbl_export_path.setWordWrap(True)
            self.lbl_export_path.setStyleSheet("color: #00ffcc; font-size: 10px; background: #222; padding: 5px; border-radius: 3px;")
            input_layout.addWidget(self.lbl_export_path)

            self.btn_change_export = QPushButton("📁 Change Folder")
            self.btn_change_export.clicked.connect(self.change_export_dir)
            self.btn_change_export.setStyleSheet("background-color: #333; color: white; padding: 5px; font-size: 11px; margin-top: 5px;")
            input_layout.addWidget(self.btn_change_export)
            
            input_layout.addStretch()
            
            stripper_controls.addWidget(input_panel)
            
            # Right: Queue Grid
            queue_container = QFrame()
            queue_container.setStyleSheet("background-color: #111; border-radius: 10px;")
            queue_layout = QVBoxLayout(queue_container)
            
            lbl_queue = QLabel("PROCESSING QUEUE (Drop files here)")
            lbl_queue.setStyleSheet("color: #888; font-weight: bold; font-size: 10px;")
            queue_layout.addWidget(lbl_queue)
            
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setStyleSheet("border: none; background: transparent;")
            
            self.grid_widget = QWidget()
            self.grid_layout = QGridLayout(self.grid_widget)
            self.grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.scroll.setWidget(self.grid_widget)
            
            queue_layout.addWidget(self.scroll)
            stripper_controls.addWidget(queue_container)
            
            stripper_layout.addLayout(stripper_controls)
            
            # Add stripper panel to main layout
            layout.addWidget(self.stripper_panel)
            
            # --- Dummy Creator Content Area (initially hidden) ---
            self.dummy_panel = QWidget()
            dummy_layout = QVBoxLayout(self.dummy_panel)
            dummy_layout.setContentsMargins(50, 50, 50, 50)
            
            lbl_dummy_info = QLabel("🎭 Dummy Creator")
            lbl_dummy_info.setAlignment(Qt.AlignCenter)
            lbl_dummy_info.setStyleSheet("font-size: 24px; font-weight: bold; color: #f1fa8c; margin-bottom: 20px;")
            dummy_layout.addWidget(lbl_dummy_info)
            
            lbl_dummy_desc = QLabel(
                "Archive your scraped collections while preserving scraper state.\n\n"
                "• Moves originals to 'originals/' subfolder\n"
                "• Creates tiny dummy files (32x32 gray images, 1-byte for others)\n"
                "• Incremental processing (only handles new files on re-runs)\n"
                "• No manifest needed - self-detecting system"
            )
            lbl_dummy_desc.setAlignment(Qt.AlignCenter)
            lbl_dummy_desc.setWordWrap(True)
            lbl_dummy_desc.setStyleSheet("font-size: 13px; color: #aaa; line-height: 1.6;")
            dummy_layout.addWidget(lbl_dummy_desc)
            
            dummy_layout.addSpacing(30)
            
            btn_run_dummy = QPushButton("📂 Select Folder to Dummify")
            btn_run_dummy.setFixedSize(300, 50)
            btn_run_dummy.clicked.connect(self.open_dummy_creator_dialog)
            btn_run_dummy.setStyleSheet("""
                QPushButton {
                    background-color: #332211;
                    color: #f1fa8c;
                    font-size: 16px;
                    font-weight: bold;
                    border: 2px solid #f1fa8c;
                    border-radius: 8px;
                    padding: 10px;
                }
                QPushButton:hover {
                    background-color: #443322;
                }
            """)
            dummy_layout.addWidget(btn_run_dummy, alignment=Qt.AlignCenter)
            dummy_layout.addStretch()
            
            self.dummy_panel.setVisible(False)  # Initially hidden
            layout.addWidget(self.dummy_panel)

            # --- Bottom Bar ---
            footer = QHBoxLayout()
            
            self.progress = QProgressBar()
            self.progress.setVisible(False)
            self.progress.setStyleSheet("height: 10px;")
            footer.addWidget(self.progress)
            
            self.btn_process = QPushButton("🚀 START PROCESSING")
            self.btn_process.clicked.connect(self.process_queue)
            self.btn_process.setMinimumHeight(50)
            self.btn_process.setEnabled(False)
            self.btn_process.setStyleSheet("""
                QPushButton { background-color: #00ffcc; color: black; font-weight: bold; font-size: 16px; border-radius: 10px; padding: 10px 40px; }
                QPushButton:hover { background-color: #00ccaa; }
                QPushButton:disabled { background-color: #333; color: #666; }
            """)
            footer.addStretch()
            footer.addWidget(self.btn_process)
            
            layout.addLayout(footer)

        return self.view

    def switch_to_stripper(self):
        """Switch to Metadata Stripper tool."""
        if self.btn_tool_stripper.isChecked():
            self.btn_tool_dummy.setChecked(False)
            self.stripper_panel.setVisible(True)
            self.dummy_panel.setVisible(False)
        else:
            # Prevent unchecking (radio behavior)
            self.btn_tool_stripper.setChecked(True)
    
    def switch_to_dummy(self):
        """Switch to Dummy Creator tool."""
        if self.btn_tool_dummy.isChecked():
            self.btn_tool_stripper.setChecked(False)
            self.stripper_panel.setVisible(False)
            self.dummy_panel.setVisible(True)
        else:
            # Prevent unchecking (radio behavior)
            self.btn_tool_dummy.setChecked(True)

    # --- Input Handlers ---
    def change_export_dir(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Select Export Folder", self.export_dir)
        if folder:
            self.export_dir = folder
            self.lbl_export_path.setText(self.export_dir)

    def add_images_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.add_to_queue(files)

    def add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder")
        if folder:
            paths = []
            extensions = ('.png', '.jpg', '.jpeg', '.webp')
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(extensions):
                        paths.append(os.path.join(root, f))
            if paths:
                self.add_to_queue(paths)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = []
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isdir(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(extensions):
                            paths.append(os.path.join(root, f))
            elif p.lower().endswith(extensions):
                paths.append(p)
        
        if paths:
            self.add_to_queue(paths)

    # --- Queue Logic ---
    def add_to_queue(self, paths):
        # Avoid duplicates in queue
        existing = set(self.queue_paths)
        new_paths = [p for p in paths if p not in existing]
        self.queue_paths.extend(new_paths)
        self.refresh_queue_grid()

    def load_images(self, paths):
        """Standard entry point for other modules to send images here."""
        self.add_to_queue(paths)

    def clear_queue(self):
        self.queue_paths = []
        self.selected_paths = set()
        self.refresh_queue_grid()

    def toggle_selection(self, path):
        if path in self.selected_paths:
            self.selected_paths.remove(path)
        else:
            self.selected_paths.add(path)
        
        # Update UI button state
        self.btn_clean_selected.setEnabled(len(self.selected_paths) > 0)
        # We need to find the specific widget to update its visual state?
        # Simpler: refresh_queue_grid but it's expensive.
        # Let's find child widgets.
        for i in range(self.grid_layout.count()):
            widget = self.grid_layout.itemAt(i).widget()
            if hasattr(widget, 'path') and widget.path == path:
                widget.setSelected(path in self.selected_paths)

    def remove_selected(self):
        self.queue_paths = [p for p in self.queue_paths if p not in self.selected_paths]
        self.selected_paths = set()
        self.btn_clean_selected.setEnabled(False)
        self.refresh_queue_grid()

    def refresh_queue_grid(self):
        # Clean current grid
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        cols = 6
        for i, path in enumerate(self.queue_paths):
            row = i // cols
            col = i % cols
            thumb = ClickableThumbnail(path)
            thumb.setFixedSize(120, 120)
            
            # Selection visual state
            thumb.setSelected(path in self.selected_paths)
            
            # Click toggles selection instead of opening preview (Workshop mode)
            thumb.clicked.connect(self.toggle_selection)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(120, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            self.grid_layout.addWidget(thumb, row, col)
            
        self.btn_process.setEnabled(len(self.queue_paths) > 0)

    def process_queue(self):
        if not self.queue_paths:
            return
            
        count = len(self.queue_paths)
        self.progress.setMaximum(count)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_process.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.btn_clean_selected.setEnabled(False)

        success_count = 0
        for path in self.queue_paths:
            dest = get_export_path(path, export_dir=self.export_dir)
            success, _ = strip_metadata(path, dest)
            if success:
                success_count += 1
            
            self.progress.setValue(self.progress.value() + 1)
            # Give UI a chance to update
            from PySide6.QtWidgets import QApplication
            QApplication.instance().processEvents()

        self.progress.setVisible(False)
        self.btn_process.setEnabled(True)
        self.btn_clear.setEnabled(True)
        
        QMessageBox.information(self.view, "Processing Complete", 
                                f"Exported {success_count} of {count} images to:\n{self.export_dir}")

    def open_dummy_creator_dialog(self):
        """Opens the Dummy Creator dialog for folder selection and processing."""
        from PySide6.QtWidgets import QDialog, QLabel, QTextEdit, QApplication
        from modules.workshop.logic.dummy_manager import process_folder, get_folder_stats
        
        # Select folder
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder to Dummify")
        if not folder:
            return
        
        # Preview stats
        stats = get_folder_stats(folder)
        if not stats:
            QMessageBox.warning(self.view, "Invalid Path", "Could not access folder.")
            return
        
        # Show preview dialog
        preview_msg = (
            f"📊 Folder Analysis\n\n"
            f"Total Files: {stats['total_files']}\n"
            f"Already Dummies: {stats['dummies']}\n"
            f"Originals to Process: {stats['originals']}\n\n"
            f"Action: Move {stats['originals']} files to 'originals/' subfolder and create dummy placeholders.\n\n"
            f"⚠️ This operation cannot be easily undone. Continue?"
        )
        
        reply = QMessageBox.question(
            self.view, 
            "Dummy Creator - Confirm",
            preview_msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Progress dialog
        progress_dialog = QDialog(self.view)
        progress_dialog.setWindowTitle("Dummy Creator - Processing")
        progress_dialog.setModal(True)
        progress_dialog.resize(500, 300)
        
        layout = QVBoxLayout(progress_dialog)
        
        lbl_title = QLabel("🎭 Creating Dummies...")
        lbl_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #f1fa8c;")
        layout.addWidget(lbl_title)
        
        log_box = QTextEdit()
        log_box.setReadOnly(True)
        log_box.setStyleSheet("background: #111; color: #ccc; font-family: Consolas; font-size: 11px;")
        layout.addWidget(log_box)
        
        progress_bar = QProgressBar()
        layout.addWidget(progress_bar)
        
        btn_close = QPushButton("Close")
        btn_close.setEnabled(False)
        btn_close.clicked.connect(progress_dialog.accept)
        layout.addWidget(btn_close)
        
        progress_dialog.show()
        
        # Progress callback
        def on_progress(current, total, filename):
            progress_bar.setMaximum(total)
            progress_bar.setValue(current)
            log_box.append(f"[{current}/{total}] {filename}")
            QApplication.instance().processEvents()
        
        # Execute
        try:
            log_box.append(f"➤ Processing: {folder}\n")
            final_stats = process_folder(folder, progress_callback=on_progress)
            
            # Summary
            space_saved_mb = final_stats['space_saved_bytes'] / (1024 * 1024)
            summary = (
                f"\n✅ DONE!\n\n"
                f"Processed: {final_stats['processed']}\n"
                f"Skipped (already dummies): {final_stats['skipped_dummies']}\n"
                f"Skipped (already in originals/): {final_stats['skipped_originals']}\n"
                f"Errors: {final_stats['errors']}\n"
                f"Space Saved: {space_saved_mb:.2f} MB\n"
            )
            log_box.append(summary)
            
        except Exception as e:
            log_box.append(f"\n❌ ERROR: {str(e)}\n")
        
        btn_close.setEnabled(True)
        progress_dialog.exec()
