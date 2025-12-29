from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QListWidget, QFileDialog, QMessageBox, QProgressBar, QFrame,
                               QLineEdit, QDialog, QCompleter)
from PySide6.QtGui import QPixmap, QIcon, QMouseEvent
from PySide6.QtCore import Qt, QSize, Signal
from core.base_module import BaseModule
from .logic.db_manager import DatabaseManager
from .logic.indexer import IndexerWorker
from .logic.tagging_ui import FlowLayout, TagChip
import os

class ClickableThumbnail(QLabel):
    clicked = Signal(str)
    selection_changed = Signal(bool)
    rating_changed = Signal(str, int)
    
    def __init__(self, path, parent=None, auto_load=True, rating=0):
        super().__init__(parent)
        # Ensure path matches DB/Loader format (forward slashes)
        self.path = os.path.normpath(path).replace('\\', '/')
        self.selected = False
        self.rating = rating
        self.setFixedSize(100, 100)
        self.setStyleSheet("border: 1px solid #555; border-radius: 5px; background-color: black;")
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        
        # Rating Label (Overlay)
        self.lbl_rating = QLabel(self)
        self.lbl_rating.setFixedSize(30, 20)
        self.lbl_rating.setAlignment(Qt.AlignCenter)
        self.lbl_rating.setStyleSheet("""
            background-color: rgba(0, 0, 0, 150); 
            color: #ffcc00; 
            font-weight: bold; 
            font-size: 10px; 
            border-bottom-left-radius: 5px;
        """)
        # Position in top-right
        self.lbl_rating.move(70, 0)
        self.update_rating_display()
        
        if auto_load:
            pix = QPixmap(path)
            if not pix.isNull():
                self.setPixmap(pix.scaled(100, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.setText("❌")
        else:
            self.setText("⏳") # Placeholder

    def update_rating_display(self):
        if self.rating > 0:
            self.lbl_rating.setText(f"⭐{self.rating}")
            self.lbl_rating.show()
        else:
            self.lbl_rating.hide()

    def setRating(self, rating):
        self.rating = rating
        self.update_rating_display()

    def cycleRating(self):
        self.rating = (self.rating + 1) % 6 # 0,1,2,3,4,5 then back to 0
        self.update_rating_display()
        self.rating_changed.emit(self.path, self.rating)

    def setSelected(self, selected):
        self.selected = selected
        if self.selected:
            self.setStyleSheet("border: 3px solid #00ffcc; border-radius: 5px; background-color: #111;")
        else:
            self.setStyleSheet("border: 1px solid #555; border-radius: 5px; background-color: black;")
        self.selection_changed.emit(self.selected)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ControlModifier:
                self.cycleRating()
            else:
                self.clicked.emit(self.path)

class LibrarianModule(BaseModule):
    # Signals for Integration
    request_open_gallery = Signal(list, str) # paths, title_context
    request_open_metadata = Signal(list)     # paths
    request_open_workshop = Signal(list)     # paths

    def __init__(self):
        super().__init__()
        self.view = None
        # Initialize DB Manager (using default 'panopticon.db' in the root)
        self.db = DatabaseManager()
        self.indexer_thread = None

    @property
    def name(self):
        return "The Librarian"

    @property
    def description(self):
        return "Image Database & Tag Manager"

    @property
    def icon(self):
        return "📚"

    def get_view(self) -> QWidget:
        if self.view: return self.view
        
        self.view = QWidget()
        layout = QVBoxLayout(self.view)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Header
        title = QLabel("📚 Library Manager")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #00ffcc; margin-bottom: 10px;")
        layout.addWidget(title)
        
        # Watched Folders Section
        lbl_folders = QLabel("📂 Watched Folders for Indexing:")
        lbl_folders.setStyleSheet("font-size: 16px; font-weight: bold; color: #eee;")
        layout.addWidget(lbl_folders)
        
        self.folder_list = QListWidget()
        self.folder_list.setStyleSheet("""
            QListWidget {
                background-color: #111;
                border: 1px solid #333;
                border-radius: 8px;
                color: #ddd;
                font-size: 14px;
                padding: 5px;
            }
            QListWidget::item {
                padding: 8px;
            }
            QListWidget::item:selected {
                background-color: #333;
                color: #00ffcc;
            }
        """)
        self.folder_list.itemClicked.connect(self.on_folder_selected)
        layout.addWidget(self.folder_list)
        
        # Button Bar
        btn_layout = QHBoxLayout()
        
        self.btn_add = QPushButton("➕ Add Folder")
        self.btn_add.clicked.connect(self.add_folder)
        self.btn_add.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #00ffcc;
                border: 1px solid #00ffcc;
                border-radius: 5px;
                padding: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00ffcc;
                color: black;
            }
        """)
        
        self.btn_remove = QPushButton("❌ Remove Selected")
        self.btn_remove.clicked.connect(self.remove_folder)
        self.btn_remove.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #ff5555;
                border: 1px solid #ff5555;
                border-radius: 5px;
                padding: 10px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ff5555;
                color: white;
            }
        """)
        
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_remove)
        layout.addLayout(btn_layout)
        
        # --- Preview Section ---
        layout.addSpacing(15)
        self.preview_frame = QFrame()
        self.preview_frame.setStyleSheet("background-color: #1a1a1a; border-radius: 10px; border: 1px solid #333;")
        self.preview_frame.setFixedHeight(120)
        self.preview_frame.hide() # Hidden by default until selection
        
        # Layout for images only
        self.preview_layout_images = QHBoxLayout(self.preview_frame)
        self.preview_layout_images.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.preview_frame)
        
        # Folder Stats Label (Between Preview and Scan)
        self.lbl_folder_stats = QLabel("")
        self.lbl_folder_stats.setStyleSheet("color: #00ffcc; font-size: 14px; font-weight: bold; margin-top: 5px; margin-bottom: 5px;")
        self.lbl_folder_stats.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_folder_stats)

        # Indexer Controls
        layout.addSpacing(5)
        idx_layout = QHBoxLayout()
        
        self.btn_scan = QPushButton("🚀 Scan & Index Library")
        self.btn_scan.clicked.connect(self.toggle_scan)
        self.btn_scan.setStyleSheet("""
            QPushButton {
                background-color: #00ba88;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 15px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00e6a8;
            }
            QPushButton:disabled {
                background-color: #444;
                color: #888;
            }
        """)

        self.btn_sync = QPushButton("🧹 Sync & Clean")
        self.btn_sync.clicked.connect(lambda: self.toggle_scan(sync_only=True))
        self.btn_sync.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #00ffcc;
                border: 1px solid #00ffcc;
                border-radius: 8px;
                padding: 15px;
                font-size: 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00ffcc;
                color: black;
            }
            QPushButton:disabled {
                background-color: #444;
                color: #888;
            }
        """)
        idx_layout.addWidget(self.btn_scan)
        idx_layout.addWidget(self.btn_sync)
        layout.addLayout(idx_layout)
        
        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #444;
                border-radius: 5px;
                background-color: #111;
                text-align: center;
                color: white;
            }
            QProgressBar::chunk {
                background-color: #00ffcc;
                width: 10px; 
            }
        """)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #888; font-size: 12px; margin-top: 5px;")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_status)

        # Trigger initial sync (Intelligent)
        if self.db.get_watched_folders():
            # Small delay to let the UI render
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, lambda: self.toggle_scan(auto=True))
        
        # --- Tag Explorer ---
        layout.addSpacing(15)
        
        # Divider
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("background-color: #333;")
        layout.addWidget(line)
        
        lbl_search = QLabel("🔭 Tag Explorer")
        lbl_search.setStyleSheet("font-size: 16px; font-weight: bold; color: #eee; margin-top: 10px;")
        layout.addWidget(lbl_search)
        
        # Tag Display Area (Flow Layout)
        self.tag_container = QWidget()
        self.tag_flow_layout = FlowLayout(self.tag_container)
        layout.addWidget(self.tag_container)
        
        self.active_tags = [] # Keep track of current tags
        
        # Input Area
        search_layout = QHBoxLayout()
        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText("Type a tag and hit Enter (or separate with commas)...")
        self.input_search.returnPressed.connect(self.add_tag_from_input)
        self.input_search.setStyleSheet("""
            QLineEdit {
                background-color: #222;
                border: 1px solid #444;
                border-radius: 5px;
                padding: 8px;
                color: white;
            }
            QLineEdit:focus {
                border: 1px solid #00ffcc;
            }
        """)
        search_layout.addWidget(self.input_search)
        
        self.btn_search = QPushButton("Search")
        self.btn_search.setStyleSheet("""
            QPushButton {
                background-color: #333;
                border: 1px solid #555;
                color: white;
                padding: 8px 15px;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #555;
            }
        """)
        self.btn_search.clicked.connect(self.mock_search)
        search_layout.addWidget(self.btn_search)
        layout.addLayout(search_layout)
        
        self.btn_to_viewer = QPushButton("📤 Send to Viewer")
        self.btn_to_viewer.setEnabled(False) 
        self.btn_to_viewer.clicked.connect(self.send_to_viewer)
        self.btn_to_viewer.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #aaa;
                border: 1px solid #444;
                border-radius: 5px;
                padding: 10px;
                margin-top: 5px;
            }
            QPushButton:hover { background-color: #333; }
        """)
        
        self.btn_to_workshop = QPushButton("🛠️ Send to Workshop")
        self.btn_to_workshop.setEnabled(False) 
        self.btn_to_workshop.clicked.connect(self.send_to_workshop)
        self.btn_to_workshop.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #aaa;
                border: 1px solid #444;
                border-radius: 5px;
                padding: 10px;
                margin-top: 5px;
            }
            QPushButton:hover { background-color: #333; }
        """)

        self.btn_to_metadata = QPushButton("📋 Send to Metadata Reader")
        self.btn_to_metadata.setEnabled(False) 
        self.btn_to_metadata.clicked.connect(self.send_to_metadata)
        self.btn_to_metadata.setStyleSheet("""
            QPushButton {
                background-color: #222;
                color: #aaa;
                border: 1px solid #444;
                border-radius: 5px;
                padding: 10px;
                margin-top: 5px;
            }
            QPushButton:hover { background-color: #333; }
        """)
        
        btns_export_layout = QHBoxLayout()
        btns_export_layout.addWidget(self.btn_to_viewer)
        btns_export_layout.addWidget(self.btn_to_workshop)
        btns_export_layout.addWidget(self.btn_to_metadata)
        layout.addLayout(btns_export_layout)

        layout.addStretch()
        
        # DB Stats Footer
        self.lbl_stats = QLabel("Loading...")
        self.lbl_stats.setStyleSheet("color: #888; font-size: 13px; border-top: 1px solid #333; padding-top: 10px;")
        layout.addWidget(self.lbl_stats)
        
        # Initial Load
        self.refresh_ui()
        self.update_global_stats()
        
        # Setup Autocomplete for Tags
        self.setup_completer()
        
        return self.view

    def setup_completer(self):
        """Initializes the QCompleter for the tag input."""
        all_tags = self.db.get_all_tags()
        self.completer = QCompleter(all_tags)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains) # Allow matching 'lee' in 'Zoe Lee'
        self.input_search.setCompleter(self.completer)

    def add_tag_from_input(self):
        text = self.input_search.text().strip()
        if not text: return
        
        # Handle commas
        new_tags = [t.strip() for t in text.split(',') if t.strip()]
        
        tags_added = False
        for tag in new_tags:
            if tag not in self.active_tags:
                self.active_tags.append(tag)
                self.add_chip_to_ui(tag)
                tags_added = True
        
        self.input_search.clear()
        
        if tags_added:
            self.perform_search()

    def add_chip_to_ui(self, tag_text):
        # Index determines color cycling
        idx = len(self.active_tags)
        chip = TagChip(tag_text, idx)
        chip.removed.connect(self.remove_tag)
        self.tag_flow_layout.addWidget(chip)
        
    def remove_tag(self, tag_text):
        if tag_text in self.active_tags:
            self.active_tags.remove(tag_text)
            self.perform_search() # Update search on remove

    def perform_search(self):
        """Executes search based on active_tags and updates UI."""
        
        # If no tags, reset to default state
        if not self.active_tags:
            self.preview_frame.hide()
            self.lbl_folder_stats.clear()
            self.update_global_stats() # Reset footer to global
            return

        # Perform Search
        count, preview_paths = self.db.search_by_terms(self.active_tags, limit=5)
        
        # Update Preview UI
        self.preview_frame.show()
        # Ensure we clear any folder selection highlight since we are now in "Search Mode"
        self.folder_list.clearSelection() 
        
        tags_display = ", ".join(self.active_tags[:3])
        if len(self.active_tags) > 3: tags_display += "..."
        
        self.lbl_folder_stats.setText(f"📊 Search: [{tags_display}] | 👁️ Matching Images: {count}")
        
        # Clear previous thumbnails
        while self.preview_layout_images.count():
            child = self.preview_layout_images.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
                
        if count == 0:
            lbl = QLabel("No matches found.")
            lbl.setStyleSheet("color: #666; font-style: italic;")
            self.preview_layout_images.addWidget(lbl)
        else:
            # Add new thumbnails (Clickable)
            for p in preview_paths:
                thumb = ClickableThumbnail(p)
                thumb.clicked.connect(self.on_thumbnail_clicked)
                self.preview_layout_images.addWidget(thumb)
            
        self.preview_layout_images.addStretch()
        
        # Update Footer Stats with Search Info
        folders = self.db.get_watched_folders()
        self.lbl_stats.setText(f"📊 SEARCH MODE | Criteria: {len(self.active_tags)} tags | Found: {count} | Watched Folders: {len(folders)}")

        # Enable export buttons if results found
        has_results = count > 0
        self.btn_to_viewer.setEnabled(has_results)
        self.btn_to_workshop.setEnabled(has_results)
        self.btn_to_metadata.setEnabled(has_results)

    def on_thumbnail_clicked(self, path):
        """Opens the clicked thumbnail in a larger dialog."""
        dlg = QDialog(self.view)
        dlg.setWindowTitle("Image Preview")
        dlg.resize(800, 600)
        
        vbox = QVBoxLayout(dlg)
        lbl_img = QLabel()
        lbl_img.setAlignment(Qt.AlignCenter)
        
        pix = QPixmap(path)
        if not pix.isNull():
            lbl_img.setPixmap(pix.scaled(750, 550, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            lbl_img.setText("Error loading image")
            
        vbox.addWidget(lbl_img)
        dlg.exec()

    def on_folder_selected(self, item):
        """Displays preview for the selected folder."""
        folder_path = item.text()
        
        # Get count
        count = self.db.get_folder_count(folder_path)
        
        # Get thumbnails
        preview_paths = self.db.get_folder_preview(folder_path, limit=5)
        
        # Update UI
        self.preview_frame.show()
        # Update the standalone stats label instead of the title
        self.lbl_folder_stats.setText(f"📁 {(os.path.basename(folder_path))} | 👁️ Images Found: {count}")
        
        # Clear previous thumbnails
        while self.preview_layout_images.count():
            child = self.preview_layout_images.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
                
        # Add new thumbnails (Clickable)
        for p in preview_paths:
            thumb = ClickableThumbnail(p)
            thumb.clicked.connect(self.on_thumbnail_clicked)
            self.preview_layout_images.addWidget(thumb)
            
        self.preview_layout_images.addStretch()

        # Enable export buttons
        has_results = count > 0
        self.btn_to_viewer.setEnabled(has_results)
        self.btn_to_workshop.setEnabled(has_results)
        self.btn_to_metadata.setEnabled(has_results)

    def mock_search(self):
        """Placeholder for search functionality."""
        if not self.active_tags:
             QMessageBox.information(self.view, "Search", "Please add some tags first!")
             return
             
        tags_str = ", ".join(self.active_tags)
        QMessageBox.information(self.view, "Search", f"Searching for tags: [{tags_str}] is not yet implemented.\nThis will filter the database and update the preview box.")

    def update_global_stats(self):
        if self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT count(*) FROM files")
            count = cursor.fetchone()[0]
            folders = self.db.get_watched_folders()
            self.lbl_stats.setText(f"📊 Global Status | Indexed Files: {count} | Watched Folders: {len(folders)}")

    def refresh_ui(self):
        # specific reload logic
        self.folder_list.clear()
        folders = self.db.get_watched_folders()
        self.folder_list.addItems(folders)
        self.preview_frame.hide() # Reset preview on refresh
        self.lbl_folder_stats.clear() # Clear stats

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder to Index")
        if folder:
            if self.db.add_watched_folder(folder):
                self.refresh_ui()
                self.update_global_stats()
            else:
                QMessageBox.warning(self.view, "Error", "Could not add folder (maybe it's already there?)")

    def remove_folder(self):
        selected = self.folder_list.currentItem()
        if not selected: return
        
        path = selected.text()
        confirm = QMessageBox.question(self.view, "Remove Folder", 
                                       f"Stop watching '{path}' and remove its images from the database?\n(Files on disk will NOT be deleted)",
                                       QMessageBox.Yes | QMessageBox.No)
        
        if confirm == QMessageBox.Yes:
            if self.db.remove_watched_folder(path):
                # Trigger VACUUM to reclaim disk space immediately
                self.db.vacuum_database()
                self.refresh_ui()
                self.update_global_stats()

    def toggle_scan(self, sync_only=False, auto=False):
        if self.indexer_thread and self.indexer_thread.isRunning():
            # Stop logic
            self.indexer_thread.stop()
            self.btn_scan.setText("Stopping...")
            if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
            self.btn_scan.setEnabled(False)
        else:
            # Start logic
            folders = self.db.get_watched_folders()
            if not folders:
                if not auto:
                    QMessageBox.warning(self.view, "No Folders", "Please add at least one folder to index.")
                return
            
            # Update UI state
            if not auto:
                self.btn_scan.setText("🛑 Stop Scanning")
                self.btn_scan.setStyleSheet("background-color: #ff5555; color: white; border-radius: 8px; padding: 15px; font-weight: bold;")
                if hasattr(self, 'btn_sync'): self.btn_sync.setEnabled(False)
                self.progress_bar.setVisible(True)
                self.progress_bar.setValue(0)
            else:
                self.lbl_status.setText("🔍 Running Background Sync...")
            
            # Pass sync_only as deep_clean to trigger Full Audit + Vacuum
            self.indexer_thread = IndexerWorker(self.db, folders, deep_clean=sync_only)
            self.indexer_thread.progress_signal.connect(self.update_progress_text)
            self.indexer_thread.count_signal.connect(self.update_progress_bar)
            self.indexer_thread.finished_signal.connect(self.scan_finished)
            self.indexer_thread.start()

    def update_progress_text(self, text):
        self.lbl_status.setText(text)

    def update_progress_bar(self, current, total):
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def scan_finished(self):
        self.btn_scan.setText("🚀 Scan & Index Library")
        self.btn_scan.setStyleSheet("background-color: #00ba88; color: white; border-radius: 8px; padding: 15px; font-weight: bold;")
        self.btn_scan.setEnabled(True)
        if hasattr(self, 'btn_sync'):
            self.btn_sync.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.refresh_ui()
        self.update_global_stats()
        QMessageBox.information(self.view, "Done", "Library Scan Complete!")

    def send_to_viewer(self):
        tags = self.active_tags
        if not tags: return
        
        # Get ALL matching files, not just preview
        count, paths = self.db.search_by_terms(tags, limit=9999)
        if paths:
             self.request_open_gallery.emit(paths, f"Search: {', '.join(tags)}")
             
    def send_to_metadata(self):
        # We send the currently filtered results
        # OR do we send selected files? 
        # User said: "Send to Metadata Reader" similar to drag and drop multiple images
        
        # Strategy: If folder selected, send folder contents. If Search Mode, send search results.
        
        paths_to_send = []
        
        # Check if in search mode
        if self.active_tags:
             _, paths = self.db.search_by_terms(self.active_tags, limit=100) # Limit to 100 for safety?
             paths_to_send = paths
        
        # Check if folder selected (and no active tags potentially) or just prioritizing folder
        elif self.folder_list.currentItem():
             folder_path = self.folder_list.currentItem().text()
             paths_to_send = self.db.get_folder_preview(folder_path, limit=100) # Re-using preview logic for now, or get all
             # Actually get_folder_preview only returns 5. We might need a `get_folder_files` logic.
             # Let's simple use the search logic with folder path if we had it, but for now let's rely on what we have.
             # We can query DB for all files in folder.
             
             cursor = self.db.conn.cursor()
             # We need to handle windows paths...
             cursor.execute("SELECT path FROM files WHERE path LIKE ?", (f"{folder_path}%",))
             paths_to_send = [r[0] for r in cursor.fetchall()]

        if paths_to_send:
             self.request_open_metadata.emit(paths_to_send)
        else:
             QMessageBox.warning(self.view, "No Files", "No files selected or found to send.")

    def send_to_workshop(self):
        paths_to_send = []
        if self.active_tags:
             _, paths = self.db.search_by_terms(self.active_tags, limit=500)
             paths_to_send = paths
        elif self.folder_list.currentItem():
             folder_path = self.folder_list.currentItem().text()
             cursor = self.db.conn.cursor()
             cursor.execute("SELECT path FROM files WHERE path LIKE ?", (f"{folder_path}%",))
             paths_to_send = [r[0] for r in cursor.fetchall()]

        if paths_to_send:
             self.request_open_workshop.emit(paths_to_send)
        else:
             QMessageBox.warning(self.view, "No Files", "No files selected or found to send.")
