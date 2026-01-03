from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QGridLayout, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QTextEdit, QSizePolicy, QSplitter, QApplication)
from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QPixmap, QIcon, QDragEnterEvent, QDropEvent, QImage, QKeyEvent
from core.base_module import BaseModule
from modules.librarian.module import ClickableThumbnail
from modules.workshop.logic.stripper import modify_metadata, get_export_path
from modules.workshop.logic.parser import UniversalParser
from modules.workshop.logic.watermarker import process_image as watermark_image, get_export_path as watermark_export_path
import os

class ResponsiveImageLabel(QLabel):
    """A QLabel that automatically scales its pixmap to fit its size and handles drops."""
    dropped_files = Signal(list)

    def __init__(self, text="Drop images or folders here\n(or click 'Open')"):
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #444; 
                color: #888; 
                background-color: #0a0a0a;
                font-size: 16px;
                border-radius: 10px;
            }
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(300, 300)
        self.setAcceptDrops(True)
        self._pixmap = None

    def set_image(self, pixmap):
        self._pixmap = pixmap
        self.update_pixmap()

    def update_pixmap(self):
        if self._pixmap and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            super().setPixmap(scaled)
        else:
            self.setPixmap(QPixmap()) 

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_pixmap()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.setStyleSheet("""
                QLabel {
                    border: 2px dashed #00ffcc; 
                    color: #00ffcc; 
                    background-color: #111;
                    font-size: 16px;
                    border-radius: 10px;
                }
            """)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #444; 
                color: #888; 
                background-color: #0a0a0a;
                font-size: 16px;
                border-radius: 10px;
            }
        """)

    def dropEvent(self, event):
        self.dragLeaveEvent(None)
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        self.dropped_files.emit(files)

class WorkshopModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.queue_paths = []
        self.selected_paths = set()
        self.export_dir = os.path.abspath("exports")
        self.view = None
        
        # Metadata Reader State
        self.reader_image_list = []
        self.reader_current_index = -1
        
        # Watermarker State
        self.watermark_path = None
        self.logo_path = None
        self.watermark_queue = []

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
            header.addStretch()
            layout.addLayout(header)

            # --- Tool Selection Panel (Center) ---
            tool_select_layout = QHBoxLayout()
            tool_select_layout.setContentsMargins(0, 20, 0, 20)
            tool_select_layout.addStretch()
            
            lbl_select = QLabel("SELECT A TOOL:")
            lbl_select.setStyleSheet("color: #888; font-weight: bold; font-size: 12px; margin-right: 15px;")
            tool_select_layout.addWidget(lbl_select)
            
            # Metadata Modifier Tool Button
            self.btn_tool_stripper = QPushButton("🛡️ Metadata Modifier")
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
            
            tool_select_layout.addSpacing(15)
            
            # Metadata Reader Tool Button
            self.btn_tool_reader = QPushButton("📋 Metadata Reader")
            self.btn_tool_reader.setCheckable(True)
            self.btn_tool_reader.clicked.connect(self.switch_to_reader)
            self.btn_tool_reader.setFixedSize(200, 60)
            self.btn_tool_reader.setStyleSheet("""
                QPushButton { 
                    background-color: #222; 
                    color: #bd93f9; 
                    text-align: center; 
                    padding: 10px; 
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 14px;
                    border: 2px solid #333;
                }
                QPushButton:checked { 
                    background-color: #221133; 
                    border: 2px solid #bd93f9; 
                }
                QPushButton:hover:!checked { 
                    background-color: #2a2a2a; 
                    border: 2px solid #555;
                }
            """)
            tool_select_layout.addWidget(self.btn_tool_reader)
            
            tool_select_layout.addSpacing(15)
            
            # Watermarker Tool Button
            self.btn_tool_watermarker = QPushButton("🖼️ Watermarker")
            self.btn_tool_watermarker.setCheckable(True)
            self.btn_tool_watermarker.clicked.connect(self.switch_to_watermarker)
            self.btn_tool_watermarker.setFixedSize(200, 60)
            self.btn_tool_watermarker.setStyleSheet("""
                QPushButton { 
                    background-color: #222; 
                    color: #50fa7b; 
                    text-align: center; 
                    padding: 10px; 
                    border-radius: 8px; 
                    font-weight: bold; 
                    font-size: 14px;
                    border: 2px solid #333;
                }
                QPushButton:checked { 
                    background-color: #1a3322; 
                    border: 2px solid #50fa7b; 
                }
                QPushButton:hover:!checked { 
                    background-color: #2a2a2a; 
                    border: 2px solid #555;
                }
            """)
            tool_select_layout.addWidget(self.btn_tool_watermarker)
            
            tool_select_layout.addStretch()
            layout.addLayout(tool_select_layout)

            # --- Metadata Stripper Content Area ---
            self.stripper_panel = QWidget()
            stripper_layout = QVBoxLayout(self.stripper_panel)
            stripper_layout.setContentsMargins(0, 0, 0, 0)
            
            # Stripper Controls
            stripper_controls = QHBoxLayout()
            
            # Left: Controls Panel
            input_panel = QFrame()
            input_panel.setFixedWidth(280)
            input_panel.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 15px;")
            input_layout = QVBoxLayout(input_panel)
            input_layout.setSpacing(10)
            
            lbl_input = QLabel("📥 1. ADD TO QUEUE")
            lbl_input.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 11px; margin-bottom: 5px;")
            input_layout.addWidget(lbl_input)

            btn_add_layout = QHBoxLayout()
            self.btn_add_files = QPushButton("🖼️ Images")
            self.btn_add_files.clicked.connect(self.add_images_dialog)
            self.btn_add_files.setStyleSheet("background-color: #333; color: white; padding: 10px; border-radius: 5px;")
            
            self.btn_add_folder = QPushButton("📂 Folder")
            self.btn_add_folder.clicked.connect(self.add_folder_dialog)
            self.btn_add_folder.setStyleSheet("background-color: #333; color: white; padding: 10px; border-radius: 5px;")
            
            btn_add_layout.addWidget(self.btn_add_files)
            btn_add_layout.addWidget(self.btn_add_folder)
            input_layout.addLayout(btn_add_layout)

            input_layout.addSpacing(15)
            
            # --- New Metadata Input ---
            lbl_meta = QLabel("✍️ 2. NEW METADATA / PROMPT")
            lbl_meta.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 11px;")
            input_layout.addWidget(lbl_meta)
            
            self.txt_modifier_meta = QTextEdit()
            self.txt_modifier_meta.setPlaceholderText("Leave empty to STRIP all metadata...\n\nOr type new metadata to inject (e.g. AI Prompts)")
            self.txt_modifier_meta.setStyleSheet("""
                QTextEdit {
                    background-color: #111;
                    color: #bd93f9;
                    border: 1px solid #333;
                    border-radius: 5px;
                    font-family: Consolas;
                    font-size: 11px;
                }
            """)
            self.txt_modifier_meta.setFixedHeight(150)
            input_layout.addWidget(self.txt_modifier_meta)
            
            input_layout.addSpacing(15)

            # Export Settings
            lbl_set = QLabel("📁 3. EXPORT SETTINGS")
            lbl_set.setStyleSheet("color: #00ffcc; font-weight: bold; font-size: 11px;")
            input_layout.addWidget(lbl_set)

            self.lbl_export_path = QLabel(self.export_dir)
            self.lbl_export_path.setWordWrap(True)
            self.lbl_export_path.setStyleSheet("color: #888; font-size: 10px; background: #222; padding: 8px; border-radius: 5px; border: 1px solid #333;")
            input_layout.addWidget(self.lbl_export_path)

            self.btn_change_export = QPushButton("📁 Change Target Folder")
            self.btn_change_export.clicked.connect(self.change_export_dir)
            self.btn_change_export.setStyleSheet("background-color: #333; color: white; padding: 8px; font-size: 11px;")
            input_layout.addWidget(self.btn_change_export)
            
            input_layout.addStretch()
            
            stripper_controls.addWidget(input_panel)
            
            # Right: Queue Grid
            queue_container = QFrame()
            queue_container.setStyleSheet("background-color: #111; border-radius: 10px;")
            queue_layout = QVBoxLayout(queue_container)
            
            # Header for queue with buttons
            queue_header = QHBoxLayout()
            lbl_queue = QLabel("PROCESSING QUEUE (Drop files here)")
            lbl_queue.setStyleSheet("color: #888; font-weight: bold; font-size: 10px;")
            queue_header.addWidget(lbl_queue)
            queue_header.addStretch()
            
            self.btn_clean_selected = QPushButton("🧹 Remove Selected")
            self.btn_clean_selected.setEnabled(False)
            self.btn_clean_selected.clicked.connect(self.remove_selected)
            self.btn_clean_selected.setFixedWidth(130)
            self.btn_clean_selected.setStyleSheet("""
                QPushButton { background-color: #222; color: #ff5555; font-size: 10px; border: 1px solid #444; border-radius: 4px; padding: 2px; }
                QPushButton:hover { background-color: #333; border: 1px solid #ff5555; }
                QPushButton:disabled { color: #444; border-color: #222; }
            """)
            queue_header.addWidget(self.btn_clean_selected)
            
            self.btn_clear = QPushButton("🗑️ Clear All")
            self.btn_clear.clicked.connect(self.clear_queue)
            self.btn_clear.setFixedWidth(80)
            self.btn_clear.setStyleSheet("""
                QPushButton { background-color: #222; color: #aaa; font-size: 10px; border: 1px solid #444; border-radius: 4px; padding: 2px; }
                QPushButton:hover { background-color: #333; color: white; }
            """)
            queue_header.addWidget(self.btn_clear)
            
            queue_layout.addLayout(queue_header)
            
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

            # --- Metadata Reader Content Area (initially hidden) ---
            self.reader_panel = QWidget()
            reader_layout = QVBoxLayout(self.reader_panel)
            reader_layout.setContentsMargins(0, 0, 0, 0)
            
            self.reader_splitter = QSplitter(Qt.Horizontal)
            
            # Left Panel: Image and Carousel
            reader_left = QWidget()
            reader_left_layout = QVBoxLayout(reader_left)
            
            # Action Buttons
            btn_reader_layout = QHBoxLayout()
            self.btn_reader_open = QPushButton("📂 Open Image(s)")
            self.btn_reader_open.clicked.connect(self.reader_open_images)
            self.btn_reader_open.setStyleSheet("""
                QPushButton { background-color: #bd93f9; color: black; font-weight: bold; padding: 10px; border-radius: 5px; }
                QPushButton:hover { background-color: #a37df0; }
            """)
            
            self.btn_reader_folder = QPushButton("📁 Open Folder")
            self.btn_reader_folder.clicked.connect(self.reader_open_folder)
            self.btn_reader_folder.setStyleSheet("""
                QPushButton { background-color: #333; color: white; padding: 10px; border-radius: 5px; border: 1px solid #444; }
                QPushButton:hover { background-color: #444; }
            """)
            
            btn_reader_layout.addWidget(self.btn_reader_open)
            btn_reader_layout.addWidget(self.btn_reader_folder)
            reader_left_layout.addLayout(btn_reader_layout)
            
            # Carousel
            carousel_layout = QHBoxLayout()
            self.btn_reader_prev = QPushButton("◀ Previous")
            self.btn_reader_prev.clicked.connect(self.reader_prev)
            self.btn_reader_prev.setStyleSheet("padding: 8px; font-weight: bold; border-radius: 5px; background-color: #222; color: white;")
            
            self.reader_index_label = QLabel("0 / 0")
            self.reader_index_label.setAlignment(Qt.AlignCenter)
            self.reader_index_label.setStyleSheet("color: #bd93f9; font-size: 15px; font-weight: bold; min-width: 100px;")
            
            self.btn_reader_next = QPushButton("Next ▶")
            self.btn_reader_next.clicked.connect(self.reader_next)
            self.btn_reader_next.setStyleSheet("padding: 8px; font-weight: bold; border-radius: 5px; background-color: #222; color: white;")
            
            carousel_layout.addWidget(self.btn_reader_prev)
            carousel_layout.addWidget(self.reader_index_label)
            carousel_layout.addWidget(self.btn_reader_next)
            reader_left_layout.addLayout(carousel_layout)
            
            self.reader_stats_label = QLabel("ℹ File Info: -")
            self.reader_stats_label.setStyleSheet("color: #888; font-size: 11px;")
            reader_left_layout.addWidget(self.reader_stats_label)
            
            self.reader_image_label = ResponsiveImageLabel()
            self.reader_image_label.dropped_files.connect(self.reader_handle_dropped)
            reader_left_layout.addWidget(self.reader_image_label, 1)
            
            self.reader_splitter.addWidget(reader_left)
            
            # Right Panel: Metadata
            reader_right = QWidget()
            reader_right_layout = QVBoxLayout(reader_right)
            
            lbl_pos = QLabel("✨ Positive Prompt:")
            lbl_pos.setStyleSheet("font-weight: bold; color: #eee;")
            reader_right_layout.addWidget(lbl_pos)
            
            self.reader_pos_prompt = QTextEdit()
            self.reader_pos_prompt.setPlaceholderText("Positive prompt...")
            self.reader_pos_prompt.setStyleSheet("background: #1a1a1a; color: #aaffaa; border: 1px solid #333; border-radius: 8px;")
            reader_right_layout.addWidget(self.reader_pos_prompt, 2)
            
            lbl_neg = QLabel("🚫 Negative Prompt:")
            lbl_neg.setStyleSheet("font-weight: bold; color: #eee;")
            reader_right_layout.addWidget(lbl_neg)
            
            self.reader_neg_prompt = QTextEdit()
            self.reader_neg_prompt.setPlaceholderText("Negative prompt...")
            self.reader_neg_prompt.setStyleSheet("background: #1a1a1a; color: #faa; border: 1px solid #333; border-radius: 8px;")
            reader_right_layout.addWidget(self.reader_neg_prompt, 1)
            
            lbl_tech = QLabel("⚙️ Technical Details:")
            lbl_tech.setStyleSheet("font-weight: bold; color: #eee;")
            reader_right_layout.addWidget(lbl_tech)
            
            self.reader_meta_info = QTextEdit()
            self.reader_meta_info.setReadOnly(True)
            self.reader_meta_info.setStyleSheet("background: #111; color: #bbb; border: 1px solid #222; border-radius: 8px; font-family: Consolas;")
            reader_right_layout.addWidget(self.reader_meta_info, 2)
            
            self.reader_splitter.addWidget(reader_right)
            self.reader_splitter.setStretchFactor(0, 3)
            self.reader_splitter.setStretchFactor(1, 2)
            
            reader_layout.addWidget(self.reader_splitter)
            
            self.reader_panel.setVisible(False)
            layout.addWidget(self.reader_panel)
            
            # Injecting Key Events for carousel
            def reader_key_event(event):
                if self.reader_panel.isVisible():
                    if event.key() == Qt.Key_Left:
                        self.reader_prev()
                    elif event.key() == Qt.Key_Right:
                        self.reader_next()
            self.view.keyPressEvent = reader_key_event

            # --- Watermarker Content Area (initially hidden) ---
            self.watermarker_panel = QWidget()
            watermarker_layout = QVBoxLayout(self.watermarker_panel)
            watermarker_layout.setContentsMargins(0, 0, 0, 0)
            
            watermarker_controls = QHBoxLayout()
            
            # Left: Controls Panel (Scrollable)
            wm_control_scroll = QScrollArea()
            wm_control_scroll.setFixedWidth(310)
            wm_control_scroll.setWidgetResizable(True)
            wm_control_scroll.setStyleSheet("border: none; background: transparent;")
            
            wm_control_container = QFrame()
            wm_control_container.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 10px;")
            wm_control_layout = QVBoxLayout(wm_control_container)
            wm_control_layout.setContentsMargins(15, 15, 15, 15)
            wm_control_layout.setSpacing(10)
            
            # 1. Asset Loading
            lbl_assets = QLabel("📥 1. LOAD ASSETS")
            lbl_assets.setStyleSheet("color: #50fa7b; font-weight: bold; font-size: 11px;")
            wm_control_layout.addWidget(lbl_assets)
            
            self.btn_load_watermark = QPushButton("🎨 Load Watermark Image")
            self.btn_load_watermark.clicked.connect(self.load_watermark_asset)
            self.btn_load_watermark.setStyleSheet("background-color: #333; color: white; padding: 8px; border-radius: 5px;")
            wm_control_layout.addWidget(self.btn_load_watermark)
            
            self.lbl_watermark_status = QLabel("No watermark loaded")
            self.lbl_watermark_status.setStyleSheet("color: #888; font-size: 10px; padding: 2px;")
            self.lbl_watermark_status.setWordWrap(True)
            wm_control_layout.addWidget(self.lbl_watermark_status)
            
            self.btn_load_logo = QPushButton("🏷️ Load Logo Image")
            self.btn_load_logo.clicked.connect(self.load_logo_asset)
            self.btn_load_logo.setStyleSheet("background-color: #333; color: white; padding: 8px; border-radius: 5px;")
            wm_control_layout.addWidget(self.btn_load_logo)
            
            self.lbl_logo_status = QLabel("No logo loaded (optional)")
            self.lbl_logo_status.setStyleSheet("color: #888; font-size: 10px; padding: 2px;")
            self.lbl_logo_status.setWordWrap(True)
            wm_control_layout.addWidget(self.lbl_logo_status)
            
            wm_control_layout.addSpacing(5)
            
            # 2. Watermark Settings
            lbl_wm_settings = QLabel("🎨 2. WATERMARK SETTINGS")
            lbl_wm_settings.setStyleSheet("color: #50fa7b; font-weight: bold; font-size: 11px;")
            wm_control_layout.addWidget(lbl_wm_settings)
            
            # Angle dropdown
            from PySide6.QtWidgets import QComboBox
            lbl_angle = QLabel("Rotation Angle:")
            lbl_angle.setStyleSheet("color: #aaa; font-size: 10px;")
            wm_control_layout.addWidget(lbl_angle)
            
            self.combo_wm_angle = QComboBox()
            self.combo_wm_angle.addItems(["0°", "45°", "90°", "135°", "180°", "225°", "270°", "315°"])
            self.combo_wm_angle.setStyleSheet("background-color: #222; color: white; padding: 5px; border: 1px solid #444;")
            wm_control_layout.addWidget(self.combo_wm_angle)
            
            # Scale slider
            lbl_scale = QLabel("Scale: 100%")
            lbl_scale.setStyleSheet("color: #aaa; font-size: 10px;")
            wm_control_layout.addWidget(lbl_scale)
            self.lbl_wm_scale = lbl_scale
            
            from PySide6.QtWidgets import QSlider
            self.slider_wm_scale = QSlider(Qt.Horizontal)
            self.slider_wm_scale.setMinimum(10)
            self.slider_wm_scale.setMaximum(200)
            self.slider_wm_scale.setValue(100)
            self.slider_wm_scale.valueChanged.connect(lambda v: self.lbl_wm_scale.setText(f"Scale: {v}%"))
            self.slider_wm_scale.setStyleSheet("QSlider::groove:horizontal { background: #333; height: 6px; border-radius: 3px; } QSlider::handle:horizontal { background: #50fa7b; width: 14px; margin: -4px 0; border-radius: 7px; }")
            wm_control_layout.addWidget(self.slider_wm_scale)
            
            # Opacity slider
            lbl_opacity = QLabel("Opacity: 30%")
            lbl_opacity.setStyleSheet("color: #aaa; font-size: 10px;")
            wm_control_layout.addWidget(lbl_opacity)
            self.lbl_wm_opacity = lbl_opacity
            
            self.slider_wm_opacity = QSlider(Qt.Horizontal)
            self.slider_wm_opacity.setMinimum(0)
            self.slider_wm_opacity.setMaximum(100)
            self.slider_wm_opacity.setValue(30)
            self.slider_wm_opacity.valueChanged.connect(lambda v: self.lbl_wm_opacity.setText(f"Opacity: {v}%"))
            self.slider_wm_opacity.setStyleSheet("QSlider::groove:horizontal { background: #333; height: 6px; border-radius: 3px; } QSlider::handle:horizontal { background: #50fa7b; width: 14px; margin: -4px 0; border-radius: 7px; }")
            wm_control_layout.addWidget(self.slider_wm_opacity)
            
            wm_control_layout.addSpacing(5)
            
            # 3. Logo Settings
            lbl_logo_settings = QLabel("🏷️ 3. LOGO SETTINGS")
            lbl_logo_settings.setStyleSheet("color: #50fa7b; font-weight: bold; font-size: 11px;")
            wm_control_layout.addWidget(lbl_logo_settings)
            
            # Position dropdown
            lbl_position = QLabel("Position:")
            lbl_position.setStyleSheet("color: #aaa; font-size: 10px;")
            wm_control_layout.addWidget(lbl_position)
            
            self.combo_logo_position = QComboBox()
            self.combo_logo_position.addItems(["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left"])
            self.combo_logo_position.setStyleSheet("background-color: #222; color: white; padding: 5px; border: 1px solid #444;")
            wm_control_layout.addWidget(self.combo_logo_position)
            
            # Size slider
            lbl_size = QLabel("Size: 150px")
            lbl_size.setStyleSheet("color: #aaa; font-size: 10px;")
            wm_control_layout.addWidget(lbl_size)
            self.lbl_logo_size = lbl_size
            
            self.slider_logo_size = QSlider(Qt.Horizontal)
            self.slider_logo_size.setMinimum(50)
            self.slider_logo_size.setMaximum(500)
            self.slider_logo_size.setValue(150)
            self.slider_logo_size.valueChanged.connect(lambda v: self.lbl_logo_size.setText(f"Size: {v}px"))
            self.slider_logo_size.setStyleSheet("QSlider::groove:horizontal { background: #333; height: 6px; border-radius: 3px; } QSlider::handle:horizontal { background: #50fa7b; width: 14px; margin: -4px 0; border-radius: 7px; }")
            wm_control_layout.addWidget(self.slider_logo_size)
            
            wm_control_layout.addSpacing(10)
            
            # Preview button
            self.btn_wm_preview = QPushButton("👁️ Generate Preview")
            self.btn_wm_preview.clicked.connect(self.generate_watermark_preview)
            self.btn_wm_preview.setEnabled(False)
            self.btn_wm_preview.setStyleSheet("""
                QPushButton { background-color: #00ffcc; color: black; font-weight: bold; padding: 12px; border-radius: 5px; }
                QPushButton:hover { background-color: #00ccaa; }
                QPushButton:disabled { background-color: #222; color: #444; border: 1px solid #333; }
            """)
            wm_control_layout.addWidget(self.btn_wm_preview)
            
            wm_control_layout.addStretch()
            
            wm_control_scroll.setWidget(wm_control_container)
            watermarker_controls.addWidget(wm_control_scroll)
            
            # Right: Preview and Queue
            wm_right_panel = QWidget()
            wm_right_layout = QVBoxLayout(wm_right_panel)
            
            # Preview area
            preview_frame = QFrame()
            preview_frame.setStyleSheet("background-color: #111; border: 1px solid #333; border-radius: 8px;")
            preview_layout = QVBoxLayout(preview_frame)
            
            lbl_preview_title = QLabel("PREVIEW")
            lbl_preview_title.setStyleSheet("color: #888; font-weight: bold; font-size: 10px; padding: 5px;")
            lbl_preview_title.setAlignment(Qt.AlignCenter)
            preview_layout.addWidget(lbl_preview_title)
            
            self.wm_preview_label = QLabel()
            self.wm_preview_label.setAlignment(Qt.AlignCenter)
            self.wm_preview_label.setMinimumHeight(300)
            self.wm_preview_label.setStyleSheet("color: #666; padding: 20px;")
            self.wm_preview_label.setText("Load assets and click 'Generate Preview'")
            preview_layout.addWidget(self.wm_preview_label, 1)
            
            wm_right_layout.addWidget(preview_frame, 1)
            
            # Batch queue area
            queue_frame = QFrame()
            queue_frame.setStyleSheet("background-color: #111; border: 1px solid #333; border-radius: 8px; margin-top: 10px;")
            queue_layout = QVBoxLayout(queue_frame)
            
            queue_header = QHBoxLayout()
            lbl_queue_title = QLabel("BATCH QUEUE (Drop images here)")
            lbl_queue_title.setStyleSheet("color: #888; font-weight: bold; font-size: 10px;")
            queue_header.addWidget(lbl_queue_title)
            queue_header.addStretch()
            
            self.btn_wm_add_images = QPushButton("➕ Add Images")
            self.btn_wm_add_images.clicked.connect(self.wm_add_images)
            self.btn_wm_add_images.setStyleSheet("background-color: #222; color: #aaa; font-size: 10px; border: 1px solid #444; border-radius: 4px; padding: 2px;")
            self.btn_wm_add_images.setFixedWidth(100)
            queue_header.addWidget(self.btn_wm_add_images)
            
            self.btn_wm_clear = QPushButton("🗑️ Clear")
            self.btn_wm_clear.clicked.connect(self.wm_clear_queue)
            self.btn_wm_clear.setStyleSheet("background-color: #222; color: #aaa; font-size: 10px; border: 1px solid #444; border-radius: 4px; padding: 2px;")
            self.btn_wm_clear.setFixedWidth(60)
            queue_header.addWidget(self.btn_wm_clear)
            
            queue_layout.addLayout(queue_header)
            
            self.wm_queue_scroll = QScrollArea()
            self.wm_queue_scroll.setWidgetResizable(True)
            self.wm_queue_scroll.setStyleSheet("border: none; background: transparent;")
            self.wm_queue_scroll.setMinimumHeight(150)
            
            self.wm_queue_grid_widget = QWidget()
            self.wm_queue_grid_layout = QGridLayout(self.wm_queue_grid_widget)
            self.wm_queue_grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.wm_queue_scroll.setWidget(self.wm_queue_grid_widget)
            
            queue_layout.addWidget(self.wm_queue_scroll)
            
            wm_right_layout.addWidget(queue_frame)
            
            watermarker_controls.addWidget(wm_right_panel)
            watermarker_layout.addLayout(watermarker_controls)
            
            self.watermarker_panel.setVisible(False)
            layout.addWidget(self.watermarker_panel)

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
            self.btn_tool_reader.setChecked(False)
            self.btn_tool_watermarker.setChecked(False)
            self.stripper_panel.setVisible(True)
            self.dummy_panel.setVisible(False)
            self.reader_panel.setVisible(False)
            self.watermarker_panel.setVisible(False)
            self.btn_process.setEnabled(len(self.queue_paths) > 0)
        else:
            self.btn_tool_stripper.setChecked(True)
    
    def switch_to_dummy(self):
        """Switch to Dummy Creator tool."""
        if self.btn_tool_dummy.isChecked():
            self.btn_tool_stripper.setChecked(False)
            self.btn_tool_reader.setChecked(False)
            self.btn_tool_watermarker.setChecked(False)
            self.stripper_panel.setVisible(False)
            self.dummy_panel.setVisible(True)
            self.reader_panel.setVisible(False)
            self.watermarker_panel.setVisible(False)
            self.btn_process.setEnabled(False) # Dummy has its own button
        else:
            self.btn_tool_dummy.setChecked(True)

    def switch_to_reader(self):
        """Switch to Metadata Reader tool."""
        if self.btn_tool_reader.isChecked():
            self.btn_tool_stripper.setChecked(False)
            self.btn_tool_dummy.setChecked(False)
            self.btn_tool_watermarker.setChecked(False)
            self.stripper_panel.setVisible(False)
            self.dummy_panel.setVisible(False)
            self.reader_panel.setVisible(True)
            self.watermarker_panel.setVisible(False)
            self.btn_process.setEnabled(False)
        else:
            self.btn_tool_reader.setChecked(True)

    # --- Metadata Reader Logic ---
    def reader_open_images(self):
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images", "", "Images (*.png *.jpg *.webp)")
        if files:
            self.reader_load_list(files)

    def reader_open_folder(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder")
        if folder:
            self.reader_handle_dropped([folder])

    def reader_handle_dropped(self, paths):
        all_images = []
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        for path in paths:
            if os.path.isdir(path):
                for root, _, files in os.walk(path):
                    for f in files:
                        if f.lower().endswith(extensions):
                            all_images.append(os.path.join(root, f))
            else:
                if path.lower().endswith(extensions):
                    all_images.append(path)
        
        if all_images:
            self.reader_load_list(sorted(all_images))

    def reader_load_list(self, files):
        self.reader_image_list = files
        self.reader_current_index = 0
        self.reader_display_current()

    def reader_prev(self):
        if self.reader_image_list and self.reader_current_index > 0:
            self.reader_current_index -= 1
            self.reader_display_current()

    def reader_next(self):
        if self.reader_image_list and self.reader_current_index < len(self.reader_image_list) - 1:
            self.reader_current_index += 1
            self.reader_display_current()

    def reader_display_current(self):
        if 0 <= self.reader_current_index < len(self.reader_image_list):
            path = self.reader_image_list[self.reader_current_index]
            self.reader_index_label.setText(f"🖼️ {self.reader_current_index + 1} / {len(self.reader_image_list)}")
            self.reader_load_data(path)
            
            self.btn_reader_prev.setEnabled(self.reader_current_index > 0)
            self.btn_reader_next.setEnabled(self.reader_current_index < len(self.reader_image_list) - 1)

    def reader_load_data(self, path):
        pixmap = QPixmap(path)
        self.reader_image_label.set_image(pixmap)
        
        result = UniversalParser.parse_image(path)
        
        s = result.get("stats", {})
        fname = os.path.basename(path)
        stats_text = f"📄 {fname} | 📐 {s.get('format', '-')} | 💾 {s.get('size', '-')} | 📅 {s.get('created', '-')}"
        self.reader_stats_label.setText(stats_text)
        
        if "error" in result:
            self.reader_pos_prompt.setPlainText(f"Error: {result['error']}")
            self.reader_neg_prompt.clear()
            self.reader_meta_info.clear()
        else:
            self.reader_pos_prompt.setPlainText(result.get("positive", ""))
            self.reader_neg_prompt.setPlainText(result.get("negative", ""))
            
            tech_info = f"GENERATION TOOL: {result.get('tool', 'Unknown')}\n"
            tech_info += "-------------------\n"
            tech_info += f"Model: {result.get('model', '-')}\n"
            tech_info += f"VAE: {result.get('vae', '-')}\n"
            
            loras = result.get('loras', [])
            if loras:
                tech_info += f"LoRAs: {', '.join(loras)}\n"
            
            tech_info += f"Seed: {result.get('seed', '-')}\n"
            tech_info += f"Sampler: {result.get('sampler', '-')} | Steps: {result.get('steps', '-')} | CFG: {result.get('cfg', '-')}\n"
            tech_info += "-------------------\n\n"
            
            raw_str = tech_info
            for k, v in result.get("raw", {}).items():
                raw_str += f"[{k}]:\n{v}\n\n"
            self.reader_meta_info.setPlainText(raw_str)

    def load_images(self, paths, tool="modifier"):
        """Integration hook from Librarian/Gallery."""
        if tool == "reader":
            self.btn_tool_reader.setChecked(True)
            self.switch_to_reader()
            self.reader_handle_dropped(paths)
        else:
            # Default to modifier (stripper/changer)
            self.btn_tool_stripper.setChecked(True)
            self.switch_to_stripper()
            self.handle_dropped_files(paths)

    def load_paths(self, paths):
        """Compatibility alias."""
        self.load_images(paths, tool="reader")

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
        """Processes the images in the queue based on the active tool."""
        # 1. Determine active tool
        if self.btn_tool_stripper.isChecked():
            self._process_modifier_batch()
        elif self.btn_tool_watermarker.isChecked():
            self._process_watermark_batch()
        else:
            QMessageBox.information(self.view, "Processing", "Please select a supported tool in the Workshop.")

    def _process_modifier_batch(self):
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
        new_meta = self.txt_modifier_meta.toPlainText().strip()
        
        for path in self.queue_paths:
            dest = get_export_path(path, export_dir=self.export_dir)
            success, _ = modify_metadata(path, dest, metadata_text=new_meta)
            if success:
                success_count += 1
            
            self.progress.setValue(self.progress.value() + 1)
            QApplication.instance().processEvents()

        self.progress.setVisible(False)
        self.btn_process.setEnabled(True)
        self.btn_clear.setEnabled(True)
        
        QMessageBox.information(self.view, "Processing Complete", 
                                f"Exported {success_count} of {count} images to:\n{self.export_dir}")

    def _process_watermark_batch(self):
        if not self.watermark_queue:
            QMessageBox.warning(self.view, "No Images", "The Watermarker queue is empty. Drop some images first!")
            return
        
        if not self.watermark_path:
            QMessageBox.warning(self.view, "No Watermark", "Please load a watermark image first.")
            return

        count = len(self.watermark_queue)
        self.progress.setMaximum(count)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_process.setEnabled(False)
        
        # Get parameters
        angle_text = self.combo_wm_angle.currentText().replace("°", "")
        angle = int(angle_text)
        scale = self.slider_wm_scale.value() / 100.0
        opacity = self.slider_wm_opacity.value() / 100.0
        
        logo_pos_map = {
            "Top-Right": "top-right",
            "Top-Left": "top-left",
            "Bottom-Right": "bottom-right",
            "Bottom-Left": "bottom-left"
        }
        logo_pos = logo_pos_map.get(self.combo_logo_position.currentText(), "top-right")
        logo_size = self.slider_logo_size.value()

        success_count = 0
        for path in self.watermark_queue:
            dest = watermark_export_path(path, export_dir=os.path.join(self.export_dir, "watermarked"))
            success, _ = watermark_image(
                path, dest, 
                watermark_path=self.watermark_path, 
                logo_path=self.logo_path,
                wm_angle=angle,
                wm_scale=scale,
                wm_opacity=opacity,
                logo_position=logo_pos,
                logo_size=logo_size
            )
            if success:
                success_count += 1
            
            self.progress.setValue(self.progress.value() + 1)
            QApplication.instance().processEvents()

        self.progress.setVisible(False)
        self.btn_process.setEnabled(True)
        
        QMessageBox.information(self.view, "Watermarking Complete", 
                                f"Successfully processed {success_count} of {count} images.\nResults saved in: {self.export_dir}/watermarked")

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

    def switch_to_watermarker(self):
        """Switch to Watermarker tool."""
        if self.btn_tool_watermarker.isChecked():
            self.btn_tool_stripper.setChecked(False)
            self.btn_tool_dummy.setChecked(False)
            self.btn_tool_reader.setChecked(False)
            self.stripper_panel.setVisible(False)
            self.dummy_panel.setVisible(False)
            self.reader_panel.setVisible(False)
            self.watermarker_panel.setVisible(True)
            self.btn_process.setEnabled(len(self.watermark_queue) > 0)
        else:
            self.btn_tool_watermarker.setChecked(True)

    # --- Watermarker Logic ---
    def load_watermark_asset(self):
        """Load watermark image."""
        file, _ = QFileDialog.getOpenFileName(self.view, "Select Watermark Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if file:
            self.watermark_path = file
            self.lbl_watermark_status.setText(f"✅ {os.path.basename(file)}")
            self.lbl_watermark_status.setStyleSheet("color: #50fa7b; font-size: 10px; padding: 5px;")
            self._update_preview_button()
    
    def load_logo_asset(self):
        """Load logo image."""
        file, _ = QFileDialog.getOpenFileName(self.view, "Select Logo Image", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if file:
            self.logo_path = file
            self.lbl_logo_status.setText(f"✅ {os.path.basename(file)}")
            self.lbl_logo_status.setStyleSheet("color: #50fa7b; font-size: 10px; padding: 5px;")
            self._update_preview_button()
    
    def _update_preview_button(self):
        """Enable preview button if watermark is loaded."""
        self.btn_wm_preview.setEnabled(self.watermark_path is not None)
    
    def generate_watermark_preview(self):
        """Generate and display preview with current settings."""
        if not self.watermark_path:
            return
        
        try:
            # Create a demo image for preview
            from PIL import Image
            demo_image = Image.new('RGBA', (800, 600), (255, 255, 255, 255))
            
            # Get parameters
            angle_text = self.combo_wm_angle.currentText().replace("°", "")
            angle = int(angle_text)
            scale = self.slider_wm_scale.value() / 100.0
            opacity = self.slider_wm_opacity.value() / 100.0
            
            # Apply watermark
            from modules.workshop.logic.watermarker import apply_watermark_pattern, apply_logo
            result = apply_watermark_pattern(demo_image, self.watermark_path, angle, scale, opacity)
            
            # Apply logo if loaded
            if self.logo_path:
                position_map = {
                    "Top-Right": "top-right",
                    "Top-Left": "top-left",
                    "Bottom-Right": "bottom-right",
                    "Bottom-Left": "bottom-left"
                }
                position = position_map.get(self.combo_logo_position.currentText(), "top-right")
                size = self.slider_logo_size.value()
                result = apply_logo(result, self.logo_path, position, size)
            
            # Convert to QPixmap and display
            result_rgb = result.convert('RGB')
            result_rgb.save("temp_preview.jpg", quality=90)
            
            pixmap = QPixmap("temp_preview.jpg")
            scaled_pixmap = pixmap.scaled(
                self.wm_preview_label.width() - 40, 
                self.wm_preview_label.height() - 40,
                Qt.KeepAspectRatio, 
                Qt.SmoothTransformation
            )
            self.wm_preview_label.setPixmap(scaled_pixmap)
            
            # Cleanup
            if os.path.exists("temp_preview.jpg"):
                os.remove("temp_preview.jpg")
                
        except Exception as e:
            QMessageBox.warning(self.view, "Preview Error", f"Failed to generate preview:\n{str(e)}")
    
    def wm_add_images(self):
        """Add images to watermark queue."""
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images to Watermark", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            for f in files:
                if f not in self.watermark_queue:
                    self.watermark_queue.append(f)
            self._update_wm_queue_grid()
    
    def wm_clear_queue(self):
        """Clear watermark queue."""
        self.watermark_queue.clear()
        self._update_wm_queue_grid()
    
    def _update_wm_queue_grid(self):
        """Update watermark queue grid display."""
        # Clear existing
        while self.wm_queue_grid_layout.count():
            item = self.wm_queue_grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # Add thumbnails
        cols = 6
        for i, path in enumerate(self.watermark_queue):
            row = i // cols
            col = i % cols
            
            thumb = QLabel()
            thumb.setFixedSize(80, 80)
            thumb.setStyleSheet("border: 1px solid #333; border-radius: 4px;")
            thumb.setAlignment(Qt.AlignCenter)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(78, 78, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            self.wm_queue_grid_layout.addWidget(thumb, row, col)
            
        self.btn_process.setEnabled(len(self.watermark_queue) > 0)
