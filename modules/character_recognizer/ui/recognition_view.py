from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, 
                               QHBoxLayout, QFrame, QStackedLayout, QComboBox, QLineEdit, QFileDialog, QGridLayout, QListWidget)
from PySide6.QtCore import Qt, Signal, Slot, QFileInfo
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QFont
from ..logic.profile_db import ProfileDB
from modules.librarian.logic.db_manager import DatabaseManager
from core.components.standard_layout import StandardToolLayout
from core.theme import Theme
import cv2
import os

class CharacterRecognitionView(QWidget):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.profile_db = ProfileDB()
        
        # UI Components
        content = self._create_content()
        sidebar = self._create_sidebar()
        
        # Standard Layout
        self.layout_manager = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.layout_manager)
        
        # Init Data
        self.refresh_profiles()
        
    def _create_content(self):
        # --- Viewport Container (Stacking) ---
        self.viewport = QWidget()
        self.stack_layout = QGridLayout(self.viewport) # Grid allows overlapping
        self.stack_layout.setContentsMargins(0, 0, 0, 0)
        
        # 1. Image Layer (Background)
        self.image_label = QLabel("Drop images here")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet(f"background-color: {Theme.BG_MAIN}; color: {Theme.TEXT_DIM}; font-size: 16px;")
        
        # 2. Overlay Layer (Centered Menu)
        self.action_bar_container = QFrame()
        self.action_bar_container.setObjectName("OverlayMenu")
        self.action_bar_container.setStyleSheet("""
            #OverlayMenu {
                background-color: rgba(20, 20, 20, 230); 
                border-radius: 8px; 
                border: 1px solid #444;
            }
            QLabel { color: #EEE; }
        """)
        
        # Shadow effect
        try:
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(15)
            shadow.setColor(QColor(0, 0, 0, 180))
            shadow.setOffset(0, 4)
            self.action_bar_container.setGraphicsEffect(shadow)
        except: pass
        
        self.action_bar_layout = QStackedLayout(self.action_bar_container)
        
        # Page 1: Prediction
        self.page_predict = QWidget()
        layout_predict = QVBoxLayout(self.page_predict) 
        layout_predict.setContentsMargins(8, 8, 8, 8)
        layout_predict.setSpacing(5)
        
        self.lbl_prediction = QLabel("Unknown")
        self.lbl_prediction.setAlignment(Qt.AlignCenter)
        self.lbl_prediction.setStyleSheet("font-size: 13px; font-weight: bold;")
        
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(5)
        
        self.btn_reject = QPushButton("Manual (Esc)")
        self.btn_confirm = QPushButton("Yes (Enter)")
        
        # Compact Button Styles
        btn_style = """
            QPushButton {
                color: white;
                padding: 4px 12px;
                border-radius: 4px;
                font-size: 12px;
                font-weight: bold;
            }
        """
        self.btn_confirm.setStyleSheet("background-color: #2E7D32;" + btn_style)
        self.btn_reject.setStyleSheet("background-color: #C62828;" + btn_style)
        
        btn_layout.addWidget(self.btn_reject)
        btn_layout.addWidget(self.btn_confirm)
        
        layout_predict.addWidget(self.lbl_prediction)
        layout_predict.addLayout(btn_layout)
        
        # Page 2: Manual
        self.page_manual = QWidget()
        layout_manual = QVBoxLayout(self.page_manual)
        layout_manual.setContentsMargins(8, 8, 8, 8)
        layout_manual.setSpacing(5)
        
        self.combo_names = QComboBox()
        self.combo_names.setEditable(True)
        self.combo_names.setPlaceholderText("Name...")
        self.combo_names.setStyleSheet("padding: 4px; font-size: 12px;")
        
        btn_manual_layout = QHBoxLayout()
        btn_manual_layout.setSpacing(5)
        
        btn_save = QPushButton("Save")
        btn_save.setStyleSheet("background-color: #1565C0;" + btn_style)
        btn_cancel = QPushButton("Back")
        btn_cancel.setStyleSheet("background-color: #555;" + btn_style)

        btn_manual_layout.addWidget(btn_cancel)
        btn_manual_layout.addWidget(btn_save)
        
        layout_manual.addWidget(self.combo_names)
        layout_manual.addLayout(btn_manual_layout)
        
        self.action_bar_layout.addWidget(self.page_predict)
        self.action_bar_layout.addWidget(self.page_manual)
        
        # Add to Grid (Overlapping)
        self.stack_layout.addWidget(self.image_label, 0, 0)
        self.stack_layout.addWidget(self.action_bar_container, 0, 0, Qt.AlignmentFlag.AlignCenter)
        
        # Connections
        self.btn_confirm.clicked.connect(self.on_confirm)
        self.btn_reject.clicked.connect(self.on_reject_clicked)
        btn_save.clicked.connect(self.on_manual_save)
        btn_cancel.clicked.connect(lambda: self.action_bar_layout.setCurrentIndex(0))
        
        self.action_bar_container.hide() # Hide initially
        
        # Enable Drag & Drop
        self.viewport.setAcceptDrops(True)
        # Note: We need to redirect Viewport's drop events to Self logic or bind them here
        # Easiest way: Let the method names match (dropEvent) but bind them to the viewport instance?
        # Or just keep drag logic on 'self' which is the wrapper?
        # NO, 'self' is the wrapper now. StandardLayout puts content in center.
        # So we should enable drop on 'self.viewport' (the content widget).
        
        # Monkey patch or properly subclass?
        # Let's just bind the methods for now to keep it simple
        self.viewport.dragEnterEvent = self.dragEnterEvent
        self.viewport.dropEvent = self.dropEvent
        
        return self.viewport
        
    def _create_sidebar(self):
        """Returns the widget to be placed in the sidebar."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # --- Header ---
        title_lbl = QLabel("RECOGNIZER")
        title_lbl.setStyleSheet(f"color: {Theme.ACCENT_MAIN}; font-weight: bold; font-size: 14px; letter-spacing: 1px;") 
        layout.addWidget(title_lbl)
        
        desc_lbl = QLabel("Auto-tag characters using facial recognition.")
        desc_lbl.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)
        
        layout.addSpacing(10)
        
        # --- File Info Section ---
        lbl_meta = QLabel("FILE INFO")
        lbl_meta.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_meta)
        
        # HIGH CONTRAST: White text for data, keep consolas for tech feel
        meta_style = f"color: {Theme.TEXT_PRIMARY}; font-family: 'Consolas', 'Monaco', monospace; font-size: 11px;"
        
        self.lbl_filename = QLabel("No File")
        self.lbl_filename.setStyleSheet(f"color: {Theme.TEXT_PRIMARY}; font-size: 12px; font-weight: bold;")
        self.lbl_filename.setWordWrap(True)
        layout.addWidget(self.lbl_filename)
        
        self.lbl_dimensions = QLabel("--- x ---")
        self.lbl_dimensions.setStyleSheet(meta_style)
        layout.addWidget(self.lbl_dimensions)
        
        self.lbl_filesize = QLabel("--- KB")
        self.lbl_filesize.setStyleSheet(meta_style)
        layout.addWidget(self.lbl_filesize)
        
        layout.addSpacing(10)
        
        # --- Existing Tags ---
        lbl_tags = QLabel("CURRENT TAGS")
        lbl_tags.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_tags)
        
        self.list_tags = QListWidget()
        self.list_tags.setStyleSheet(f"""
            QListWidget {{
                background-color: {Theme.BG_INPUT};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
                color: {Theme.TEXT_PRIMARY};
                font-size: 11px;
            }}
            QListWidget::item {{ margin: 2px; }}
        """)
        self.list_tags.setFixedHeight(100)
        layout.addWidget(self.list_tags)

        layout.addSpacing(10)
        
        # --- Controls ---
        lbl_source = QLabel("SOURCE")
        lbl_source.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_source)
        
        btn_load = QPushButton("📂 Load Folder")
        btn_load.clicked.connect(self.on_load_folder_clicked)
        # Custom Style to fix Overflow (Reduced Padding directly)
        btn_load.setStyleSheet(f"""
            QPushButton {{
                background-color: {Theme.ACCENT_INFO};
                color: white;
                border-radius: 6px;
                font-size: 12px;
                font-weight: bold;
                padding: 6px 4px; /* Tighter padding */
            }}
            QPushButton:hover {{ background-color: #a070e0; }}
        """)
        layout.addWidget(btn_load)
        
        layout.addSpacing(10)
        
        # --- Status ---
        lbl_status_header = QLabel("STATUS")
        lbl_status_header.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 11px;")
        layout.addWidget(lbl_status_header)
        
        self.lbl_status = QLabel("Idle")
        # Brighten status text slightly
        self.lbl_status.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 11px;") 
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        
        layout.addStretch()
        return container
        
    # Remove old get_sidebar_widget since we renamed/refactored logic


    def refresh_profiles(self):
        """Loads names into ComboBox."""
        self.combo_names.clear()
        profiles = self.profile_db.get_all_profiles()
        names = sorted([p[0] for p in profiles])
        self.combo_names.addItems(names)

    @Slot(str, object, object, str, object, float)
    def on_image_processed(self, path, cv_img, embedding, suggestion, bbox, confidence):
        print(f"DEBUG: UI received processed image: {path}")
        try:
            self.current_path = path
            self.current_embedding = embedding
            self.current_suggestion = suggestion
            
            # --- Update Metadata Sidebar ---
            file_info = QFileInfo(path)
            self.lbl_filename.setText(file_info.fileName())
            
            # Size
            size_kb = file_info.size() / 1024.0
            if size_kb > 1024:
                self.lbl_filesize.setText(f"{size_kb/1024.0:.2f} MB")
            else:
                self.lbl_filesize.setText(f"{size_kb:.1f} KB")
                
            # Dimensions (Original)
            orig_h, orig_w = cv_img.shape[:2]
            self.lbl_dimensions.setText(f"{orig_w} x {orig_h} px")
            
            # Tags
            db = DatabaseManager()
            tags = db.get_tags_for_file(path)
            self.list_tags.clear()
            if tags:
                self.list_tags.addItems(sorted(tags))
            else:
                self.list_tags.addItem("(No tags)")
            
            # Reset UI State
            self.action_bar_layout.setCurrentIndex(0) # Predict Mode
            
            # --- Smart Visual Scaling ---
            h, w = cv_img.shape[:2]
            max_dim = 1024
            scale = 1.0
            
            if max(h, w) > max_dim:
                scale = max_dim / float(max(h, w))
                new_w = int(w * scale)
                new_h = int(h * scale)
                cv_img = cv2.resize(cv_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            
            # Recalculate dimensions
            height, width, channel = cv_img.shape
            bytes_per_line = cv_img.strides[0]
            
            q_img = QImage(cv_img.data, width, height, bytes_per_line, QImage.Format_RGB888)
            q_img = q_img.rgbSwapped() 
        
            # Draw BBox directly
            painter = QPainter(q_img)
            
            if bbox is not None:
                ox, oy, ow, oh = bbox
                
                # Apply Scale
                x = int(ox * scale)
                y = int(oy * scale)
                w = int(ow * scale)
                h = int(oh * scale)
                
                # Apply Padding (20%)
                pad_w = int(w * 0.1)
                pad_h = int(h * 0.1)
                x = max(0, x - pad_w)
                y = max(0, y - pad_h)
                w = min(width - x, w + 2*pad_w)
                h = min(height - y, h + 2*pad_h)
                
                # Color Logic (Cyberpunk)
                if confidence > 0.6:
                    color = QColor("#50fa7b") # Neon Green
                elif confidence > 0.4:
                    color = QColor("#f1fa8c") # Yellow
                else:
                    color = QColor("#ff5555") # Red/Pink
                    
                pen = QPen(color)
                pen.setWidth(3)
                painter.setPen(pen)
                painter.drawRect(x, y, w, h)
                
                # Draw Tag
                target_name = suggestion if suggestion else "Unknown"
                painter.setBrush(color)
                painter.setPen(Qt.NoPen)
                
                font = QFont("Segoe UI", 12, QFont.Bold)
                painter.setFont(font)
                fm = painter.fontMetrics()
                text_w = fm.horizontalAdvance(target_name) + 10
                text_h = fm.height() + 5
                
                painter.drawRect(x, max(0, y - text_h), text_w, text_h)
                painter.setPen(QColor("black"))
                painter.drawText(x + 5, max(0, y - 5), target_name)
                
            painter.end()
            
            pixmap = QPixmap.fromImage(q_img)
            
            # Scale to fit
            lbl_size = self.image_label.size()
            if lbl_size.width() > 0 and lbl_size.height() > 0:
                 scaled_pixmap = pixmap.scaled(lbl_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                 self.image_label.setPixmap(scaled_pixmap)
            
            self.image_label.repaint()
            
            # Update Status text but keep buttons
            if suggestion:
                self.lbl_prediction.setText(f"{suggestion}?\n({confidence:.2f})")
                
                # Button 1: Confirm (Green)
                self.btn_confirm.setText("Yes")
                self.btn_confirm.setVisible(True)
                self.btn_confirm.setStyleSheet(f"background-color: {Theme.ACCENT_SUCCESS}; color: black; border-radius: 4px; padding: 4px 12px; font-weight: bold;") 
                
                # Button 2: Reject/Edit (Red) - NOW "Edit" to fit
                self.btn_reject.setText("Edit") 
                self.btn_reject.setVisible(True)
                self.btn_reject.setStyleSheet(f"background-color: {Theme.ACCENT_WARNING}; color: white; border-radius: 4px; padding: 4px 12px; font-weight: bold;")

            else:
                self.lbl_prediction.setText("Unknown")
                
                # Button 1: Confirm -> Acts as "Manual Tag" (Purple)
                self.btn_confirm.setText("Identify")
                self.btn_confirm.setVisible(True)
                self.btn_confirm.setStyleSheet(f"background-color: {Theme.ACCENT_INFO}; color: white; border-radius: 4px; padding: 4px 12px; font-weight: bold;")
                
                # Button 2: Reject -> HIDE (Redundant)
                self.btn_reject.setVisible(False) 

            # Helper to ensure size is correct before positioning
            self.action_bar_container.adjustSize()
            
            # --- Overlay Positioning (Manual) ---
            # Remove from layout management to allow manual .move()
            if self.stack_layout.indexOf(self.action_bar_container) != -1:
                 self.stack_layout.removeWidget(self.action_bar_container)
                 self.action_bar_container.setParent(self.viewport) # Ensure it stays in viewport
            
            if bbox:
                # 1. Get Viewport Dimensions
                v_w = self.image_label.width()
                v_h = self.image_label.height()
                
                # 2. Get Pixmap Dimensions (The one actually displayed)
                p_w = pixmap.width()
                p_h = pixmap.height()
                
                # 3. Calculate Scale Factor (Qt.KeepAspectRatio logic)
                scale_w = v_w / p_w
                scale_h = v_h / p_h
                final_scale = min(scale_w, scale_h)
                
                # Dimensions of the drawn image
                disp_w = int(p_w * final_scale)
                disp_h = int(p_h * final_scale)
                
                # 4. Calculate Offsets (Black Bars)
                off_x = (v_w - disp_w) // 2
                off_y = (v_h - disp_h) // 2
                
                # 5. Map Bounding Box to Viewport Coordinates
                # Note: 'bbox' (ox, oy, ow, oh) is relative to the *Original/Resized CV Image* 
                # which corresponds to the *Pixmap* size.
                # So we just scale BBox by `final_scale` and add offsets.
                
                ox, oy, ow, oh = bbox # These are relative to the 'new_w/new_h' image from earlier resize
                
                # Correction: earlier we scaled cv_img to max 1024. 'pixmap' matches that.
                # So 'bbox' is relative to 'pixmap' dimensions? 
                # Wait, 'bbox' passed in is usually original coords? 
                # Let's check logic:
                # Earlier: x = int(ox * scale) -> This 'scale' was for the 1024 resize.
                # So (x,y,w,h) drawn by painter are on the pixmap.
                # We need the bottom of THAT rect.
                
                box_x_pixmap = ox * scale # This matches the painter logic
                box_y_pixmap = oy * scale
                box_w_pixmap = ow * scale
                box_h_pixmap = oh * scale
                
                # Viewport Coords
                box_bottom_y_vp = (box_y_pixmap + box_h_pixmap) * final_scale + off_y
                box_center_x_vp = (box_x_pixmap + box_w_pixmap / 2) * final_scale + off_x
                
                # 6. Position Menu
                menu_w = self.action_bar_container.width()
                menu_h = self.action_bar_container.height()
                
                target_x = int(box_center_x_vp - (menu_w / 2))
                target_y = int(box_bottom_y_vp + 15) # 15px margin below chin
                
                # 7. Clamp to Viewport (Don't fall off screen)
                # Keep fully within viewport if possible
                target_x = max(10, min(target_x, v_w - menu_w - 10))
                
                # If falling off bottom, put ABOVE the head?
                if target_y + menu_h > v_h - 10:
                     # Calculate Top of Box
                     box_top_y_vp = (box_y_pixmap) * final_scale + off_y
                     target_y = int(box_top_y_vp - menu_h - 15)
                
                self.action_bar_container.move(target_x, target_y)
            else:
                # Fallback: Center
                self.action_bar_container.move(
                    (self.viewport.width() - self.action_bar_container.width()) // 2,
                    (self.viewport.height() - self.action_bar_container.height()) // 2
                )

            self.action_bar_container.show()
            self.action_bar_container.raise_() # Ensure on top
                
            # Pause worker
            if self.worker:
                self.worker.pause()
                
        except Exception as e:
            print(f"CRITICAL UI ERROR: {e}")
            import traceback
            traceback.print_exc()
            if self.worker:
                self.worker.pause()

    def on_reject_clicked(self):
        """Switch to Manual Mode."""
        self.action_bar_layout.setCurrentIndex(1)
        self.combo_names.setFocus()
        
    def on_manual_save(self):
        """Save manual entry and proceed."""
        name = self.combo_names.currentText().strip()
        if not name: return 
        
        # Override suggestion
        self.current_suggestion = name
        
        # Save to ProfileDB (Learn!)
        if self.current_embedding is not None:
             print(f"Learning new face for: {name}")
             self.profile_db.add_reference(name, self.current_embedding)
             self.refresh_profiles() # Update list
        
        self.on_confirm() # Proceed as if confirmed
        
    def on_confirm(self):
        print(f"DEBUG: on_confirm called. Suggestion: '{self.current_suggestion}'")
        if not hasattr(self, 'current_path'): return
        
        # If no suggestion (Unknown), switch to Manual Mode
        if not self.current_suggestion:
            print("DEBUG: No suggestion -> Switching to Manual Mode.")
            self.on_reject_clicked()
            return
            
        # ... [Existing on_confirm logic: Save tag to Librarian]
        if self.current_suggestion:
             from modules.librarian.logic.db_manager import DatabaseManager
             central_db = DatabaseManager() 
             success = central_db.add_tag_to_file(self.current_path, self.current_suggestion)
             if success:
                 print(f"Tagged {self.current_path} as {self.current_suggestion}")
                 self.lbl_status.setText(f"Tagged: {self.current_suggestion}")
        
        # Request next
        if self.worker:
            self.worker.request_next()
            
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls: return
        
        paths = []
        for url in urls:
            paths.append(url.toLocalFile())
            
        if paths:
            expanded_paths = []
            for p in paths:
                if os.path.isdir(p):
                    for root, _, files in os.walk(p):
                        for f in files:
                            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                                expanded_paths.append(os.path.join(root, f))
                else:
                    if p.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                       expanded_paths.append(p)
                       
            if expanded_paths:
                print(f"DEBUG: DropEvent found {len(expanded_paths)} valid images.")
                self.load_images(expanded_paths)
            else:
                print("DEBUG: DropEvent found NO valid images after expansion.")



        
    def on_load_folder_clicked(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Process")
        if folder:
             expanded_paths = []
             for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                        expanded_paths.append(os.path.join(root, f))
             if expanded_paths:
                 self.load_images(expanded_paths)
             else:
                 self.lbl_status.setText("No images found in folder.")

    def load_images(self, paths):
        from ..logic.thread_worker import RecognitionWorker
        
        self.lbl_status.setText(f"Inizializing AI Engine...")
        
        # Stop previous worker if exists
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.is_running = False
            self.worker.wait()
            
        self.worker = RecognitionWorker(paths)
        self.worker.image_processed.connect(self.on_image_processed)
        self.worker.finished.connect(self.on_finished)
        self.worker.progress.connect(self.update_progress)
        
        # Start (Paused initially by logic? No, let's start)
        self.worker.paused = True 
        self.worker.request_next()
        self.worker.start()

    def update_progress(self, current, total):
        pass

    def on_finished(self):
        self.lbl_status.setText("Done.")
        self.image_label.clear()
        
        # Show nice completion message
        self.image_label.setText("<h2>✨ Batch Completed! ✨</h2><p>All images processed.</p><p style='color: #888;'>Drop more images to continue.</p>")
        self.image_label.setStyleSheet("QLabel { background-color: #202020; color: white; }") 
        
        self.action_bar_container.hide()
        
        if hasattr(self, 'worker'):
             self.worker.deleteLater()
             self.worker = None
