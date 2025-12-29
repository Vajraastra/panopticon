from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QDialog, QFrame, QSizePolicy, QSpinBox, QGridLayout, QLineEdit, QScrollArea, QCompleter)
from PySide6.QtCore import Qt, QTimer, Signal, QEvent
from PySide6.QtGui import QPixmap, QKeySequence, QAction
from modules.librarian.logic.tagging_ui import FlowLayout, TagChip
from modules.librarian.logic.db_manager import DatabaseManager
import os

class FullscreenHelper(QDialog):
    """Borderless fullscreen window for immersive viewing."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_viewer = parent
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.showFullScreen()
        self.setStyleSheet("background-color: black;")
        
        # Use Grid Layout to allow overlapping (Overlay)
        self.layout = QGridLayout(self)
        self.layout.setContentsMargins(0,0,0,0)
        
        # Image Area (Underneath)
        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignCenter)
        self.lbl_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.layout.addWidget(self.lbl_image, 0, 0, -1, -1) # Span all corners
        
        # Navigation Overlay (Floating on top)
        # We wrap it in a container that aligns to Bottom Center
        self.nav_container = QWidget(self)
        self.nav_container.setStyleSheet("background-color: rgba(0, 0, 0, 150); border-radius: 20px;")
        
        nav_layout = QHBoxLayout(self.nav_container)
        nav_layout.setContentsMargins(15, 5, 15, 5)
        nav_layout.setSpacing(15)
        
        btn_prev = QPushButton("◀")
        btn_next = QPushButton("▶")
        btn_close = QPushButton("✕")
        
        for btn in (btn_prev, btn_next, btn_close):
            btn.setFixedSize(50, 50)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus) # Prevent stealing arrow keys
            btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(255, 255, 255, 30);
                    color: white;
                    border: 2px solid rgba(255,255,255,50);
                    border-radius: 25px;
                    font-size: 20px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: rgba(255, 255, 255, 80);
                    border: 2px solid white;
                }
            """)
            nav_layout.addWidget(btn)
            
        # Add to grid, aligned to bottom center
        self.layout.addWidget(self.nav_container, 0, 0, Qt.AlignBottom | Qt.AlignHCenter)
        # Add margin to float it up slightly
        self.layout.setContentsMargins(0, 0, 0, 30)

        # Connect signals
        btn_prev.clicked.connect(self.prev_image_click)
        btn_next.clicked.connect(self.next_image_click)
        btn_close.clicked.connect(self.close)
        
        # Action Exit
        self.action_exit = QAction(self)
        self.action_exit.setShortcut(QKeySequence(Qt.Key_Escape))
        self.action_exit.triggered.connect(self.close)
        self.addAction(self.action_exit)
        
    def closeEvent(self, event):
        # Notify parent to stop slideshow if running
        if self.parent_viewer:
            self.parent_viewer.on_fullscreen_closed()
        super().closeEvent(event)

    def set_image(self, path):
        pix = QPixmap(path)
        if not pix.isNull():
            # Scale to screen size
            screen_size = self.screen().size()
            self.lbl_image.setPixmap(pix.scaled(screen_size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.prev_image_click()
        elif event.key() == Qt.Key_Right:
            self.next_image_click()
        elif event.key() == Qt.Key_Escape:
            self.close()
        # Don't call super() for arrows to avoid focus navigation
        else:
            super().keyPressEvent(event)

    def prev_image_click(self):
        if self.parent_viewer: self.parent_viewer.prev_image()
        
    def next_image_click(self):
        if self.parent_viewer: self.parent_viewer.next_image()

class AdvancedViewer(QDialog):
    def __init__(self, paths, start_index=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Panopticon Viewer")
        self.resize(1100, 800)
        self.setWindowState(Qt.WindowMaximized) # Start maximized for better view
        self.setStyleSheet("background-color: #121212; color: #ddd;")
        
        self.paths = paths
        self.current_idx = start_index
        self.fs_window = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_image)
        self.db = DatabaseManager()
        
        # Main Layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Left: Image Area (75%)
        self.img_container = QFrame()
        self.img_container.setStyleSheet("background-color: black;")
        layout.addWidget(self.img_container, 75)
        
        img_layout = QVBoxLayout(self.img_container)
        img_layout.setContentsMargins(0,0,0,0)
        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignCenter)
        self.lbl_image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        img_layout.addWidget(self.lbl_image)
        
        # Right: Data & Controls (25%)
        self.data_panel = QFrame()
        self.data_panel.setFixedWidth(320)
        self.data_panel.setStyleSheet("background-color: #1a1a1a; border-left: 1px solid #333;")
        layout.addWidget(self.data_panel)
        
        data_layout = QVBoxLayout(self.data_panel)
        data_layout.setContentsMargins(20, 20, 20, 20)
        
        # Metadata Section
        lbl_meta_title = QLabel("📝 Image Details")
        lbl_meta_title.setStyleSheet("color: #00ffcc; font-size: 18px; font-weight: bold; margin-bottom: 10px;")
        data_layout.addWidget(lbl_meta_title)
        
        self.lbl_filename = QLabel("Filename: ...")
        self.lbl_filename.setWordWrap(True)
        self.lbl_filename.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        data_layout.addWidget(self.lbl_filename)
        
        self.lbl_path = QLabel("Path: ...")
        self.lbl_path.setWordWrap(True)
        self.lbl_path.setStyleSheet("color: #aaa; font-size: 11px; margin-bottom: 10px;")
        data_layout.addWidget(self.lbl_path)
        
        # Stats Table
        grid_stats = QGridLayout()
        grid_stats.addWidget(QLabel("Resolution:"), 0, 0)
        self.lbl_res = QLabel("...")
        self.lbl_res.setStyleSheet("color: #ddd; font-weight: bold;")
        grid_stats.addWidget(self.lbl_res, 0, 1)

        # Rating Row
        grid_stats.addWidget(QLabel("Rating:"), 1, 0)
        self.lbl_rating_stars = QLabel("None")
        self.lbl_rating_stars.setStyleSheet("color: #ffcc00; font-weight: bold; font-size: 14px;")
        grid_stats.addWidget(self.lbl_rating_stars, 1, 1)
        
        self.lbl_filesize = QLabel("Size: ...")
        self.lbl_filesize.setStyleSheet("color: #aaa;")
        grid_stats.addWidget(self.lbl_filesize, 2, 1)
        
        data_layout.addLayout(grid_stats)
        
        # Create separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("background-color: #333; margin-top: 10px; margin-bottom: 10px;")
        data_layout.addWidget(line)
        
        # --- Tags Section ---
        lbl_tags = QLabel("🏷️ Tags")
        lbl_tags.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 14px;")
        data_layout.addWidget(lbl_tags)
        
        # Tag Input Area
        input_layout = QHBoxLayout()
        self.input_tag = QLineEdit()
        self.input_tag.setPlaceholderText("Add tag...")
        self.input_tag.setStyleSheet("""
            QLineEdit { background-color: #111; color: white; border: 1px solid #444; padding: 5px; border-radius: 4px; }
            QLineEdit:focus { border: 1px solid #00ffcc; }
        """)
        self.input_tag.returnPressed.connect(self.add_tag)
        
        btn_add_tag = QPushButton("+")
        btn_add_tag.setFixedSize(30, 30)
        btn_add_tag.setStyleSheet("background-color: #224433; color: #00ffcc; font-weight: bold; border-radius: 4px;")
        btn_add_tag.clicked.connect(self.add_tag)
        
        input_layout.addWidget(self.input_tag)
        input_layout.addWidget(btn_add_tag)
        data_layout.addLayout(input_layout)
        
        # Tags Flow Layout (Scrollable Area)
        tags_scroll = QScrollArea()
        tags_scroll.setWidgetResizable(True)
        tags_scroll.setStyleSheet("background-color: transparent; border: none;")
        tags_scroll.setFixedHeight(120) # Limit height
        
        self.tags_container = QWidget()
        self.tags_layout = FlowLayout(self.tags_container) # Needs import
        self.tags_layout.setContentsMargins(0, 5, 0, 5)
        
        tags_scroll.setWidget(self.tags_container)
        data_layout.addWidget(tags_scroll)

        data_layout.addStretch()

        # Navigation Buttons (Big)
        nav_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀")
        self.btn_prev.clicked.connect(self.prev_image)
        self.btn_next = QPushButton("▶")
        self.btn_next.clicked.connect(self.next_image)
        
        for btn in [self.btn_prev, self.btn_next]:
            btn.setFixedSize(60, 40)
            btn.setFocusPolicy(Qt.NoFocus) # Fix keyboard nav
            btn.setStyleSheet("""
                QPushButton { background-color: #333; color: white; border: 1px solid #555; border-radius: 5px; font-weight: bold; font-size: 16px; }
                QPushButton:hover { background-color: #00ffcc; color: black; border: 1px solid #00ffcc; }
            """)
            
        nav_layout.addWidget(self.btn_prev)
        nav_layout.addWidget(self.btn_next)
        data_layout.addLayout(nav_layout)
        
        data_layout.addSpacing(20)
        
        # Fullscreen Controls
        lbl_fs = QLabel("🖥️ Presentation Modes")
        lbl_fs.setStyleSheet("color: #eee; font-weight: bold; margin-top: 10px; border-bottom: 1px solid #444; padding-bottom: 5px;")
        data_layout.addWidget(lbl_fs)
        
        self.btn_fullscreen = QPushButton("Enter Fullscreen")
        self.btn_fullscreen.setFocusPolicy(Qt.NoFocus)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        self.btn_fullscreen.setStyleSheet("background-color: #222; color: #ccc; padding: 10px; border: 1px solid #444; border-radius: 5px;")
        data_layout.addWidget(self.btn_fullscreen)
        
        # Timer Section
        data_layout.addSpacing(10)
        
        self.btn_slideshow = QPushButton("Start Slideshow ⏳")
        self.btn_slideshow.setFocusPolicy(Qt.NoFocus)
        self.btn_slideshow.clicked.connect(self.start_slideshow)
        self.btn_slideshow.setObjectName("btn_slideshow")
        self.btn_slideshow.setCheckable(True)
        self.btn_slideshow.setStyleSheet("""
            QPushButton { background-color: #224433; color: white; padding: 10px; border-radius: 5px; font-weight: bold; }
            QPushButton:checked { background-color: #00ffcc; color: black; }
        """)
        data_layout.addWidget(self.btn_slideshow)
        
        timer_layout = QHBoxLayout()
        timer_layout.addWidget(QLabel("Timer (seconds):"))
        self.spin_timer = QSpinBox()
        self.spin_timer.setRange(1, 60)
        self.spin_timer.setValue(5)
        self.spin_timer.setFocusPolicy(Qt.ClickFocus) # Keep focus for editing but default to Click
        self.spin_timer.setStyleSheet("background-color: #111; color: white; padding: 5px; border: 1px solid #444;")
        timer_layout.addWidget(self.spin_timer)
        data_layout.addLayout(timer_layout)
        
        # Initial Display
        self.load_current_image()
        
        # Setup Autocomplete
        all_tags = self.db.get_all_tags()
        self.completer = QCompleter(all_tags)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.input_tag.setCompleter(self.completer)
        
    def closeEvent(self, event):
        self.timer.stop() # Ensure timer stops on exit
        if self.fs_window: self.fs_window.close()
        super().closeEvent(event)

    def on_fullscreen_closed(self):
        """Called by FullscreenHelper when it closes."""
        self.fs_window = None
        # Always stop slideshow when exiting fullscreen
        if self.btn_slideshow.isChecked():
            self.btn_slideshow.setChecked(False)
            self.start_slideshow() # Toggling off calls logic

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.prev_image()
        elif event.key() == Qt.Key_Right:
            self.next_image()
        elif event.key() == Qt.Key_Escape:
            self.close()

    def add_tag(self):
        text = self.input_tag.text().strip()
        if not text: return
        
        # Handle commas
        new_tags = [t.strip() for t in text.split(',') if t.strip()]
        
        path = self.paths[self.current_idx]
        tags_added = False
        
        for tag in new_tags:
            if self.db.add_tag_to_file(path, tag):
                tags_added = True
        
        if tags_added:
            self.load_tags(path)
            self.input_tag.clear()
            
    def remove_tag(self, tag_name):
        path = self.paths[self.current_idx]
        if self.db.remove_tag_from_file(path, tag_name):
            self.load_tags(path)

    def load_tags(self, path):
        # clear layout
        while self.tags_layout.count():
            item = self.tags_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        tags = self.db.get_tags_for_file(path)
        for i, t in enumerate(tags):
            chip = TagChip(t, i)
            chip.removed.connect(self.remove_tag)
            self.tags_layout.addWidget(chip)

    def load_current_image(self):
        if not self.paths: return
        
        path = self.paths[self.current_idx]
        
        # Load Tags
        self.load_tags(path)
        
        # Load Rating
        rating = self.db.get_file_rating(path)
        self.lbl_rating_stars.setText("⭐" * rating if rating > 0 else "None")
        
        # Load Pixmap
        pix = QPixmap(path)
        if not pix.isNull():
            # Scale to fit container (approx size)
            self.lbl_image.setPixmap(pix.scaled(700, 650, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.lbl_res.setText(f"Resolution: {pix.width()} x {pix.height()}")
        else:
            self.lbl_image.setText("Image Load Error")
            
        # Update Metadata
        self.lbl_filename.setText(os.path.basename(path))
        self.lbl_path.setText(path)
        try:
             size_mb = os.path.getsize(path) / (1024*1024)
             self.lbl_filesize.setText(f"Size: {size_mb:.2f} MB")
        except:
             self.lbl_filesize.setText("Size: Unknown")
             
        # Update Fullscreen if active
        if self.fs_window and self.fs_window.isVisible():
            self.fs_window.set_image(path)

    def next_image(self):
        self.current_idx = (self.current_idx + 1) % len(self.paths)
        self.load_current_image()

    def prev_image(self):
        self.current_idx = (self.current_idx - 1) % len(self.paths)
        self.load_current_image()

    def toggle_fullscreen(self):
        if not self.fs_window:
            self.fs_window = FullscreenHelper(self)
            self.fs_window.set_image(self.paths[self.current_idx])
            # We need to capture Close event to stop timer?
        else:
            self.fs_window.showFullScreen()
            self.fs_window.set_image(self.paths[self.current_idx])

    def start_slideshow(self):
        if self.btn_slideshow.isChecked():
            # Start
            self.btn_slideshow.setText("Stop Slideshow ⏹️")
            self.toggle_fullscreen() # Auto enter FS
            interval = self.spin_timer.value() * 1000
            self.timer.start(interval)
        else:
            # Stop
            self.btn_slideshow.setText("Start Slideshow ⏳")
            self.timer.stop()
