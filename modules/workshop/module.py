from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, 
                               QScrollArea, QGridLayout, QFrame, QFileDialog, QMessageBox, QProgressBar,
                               QTextEdit, QSizePolicy, QSplitter, QApplication, QSpinBox, QCheckBox, QComboBox, QSlider)
from PySide6.QtCore import Qt, Signal, Slot, QSize, QSettings
from PySide6.QtGui import QPixmap, QIcon, QDragEnterEvent, QDropEvent, QImage, QKeyEvent
from core.base_module import BaseModule
from modules.librarian.module import ClickableThumbnail
from modules.workshop.logic.stripper import modify_metadata, get_export_path
from modules.workshop.logic.parser import UniversalParser
from modules.workshop.logic.watermarker import process_image as watermark_image, get_export_path as watermark_export_path
from modules.workshop.logic.optimizer import (optimize_image, analyze_image, 
                                            get_export_path as optimizer_export_path)
import os
import tempfile
from core.theme import Theme
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtCore import QPoint

# ... (Previous imports kept if needed, assuming they're at top)

class ResponsiveImageLabel(QLabel):
    """A QLabel that automatically scales its pixmap to fit its size and handles drops."""
    dropped_files = Signal(list)

    def __init__(self, text="Drop images or folders here\n(or click 'Open')"):
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"""
            QLabel {{
                border: 2px dashed {Theme.BORDER}; 
                color: {Theme.TEXT_DIM}; 
                background-color: {Theme.BG_INPUT};
                font-size: 16px;
                border-radius: 10px;
            }}
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
            self.setStyleSheet(f"""
                QLabel {{
                    border: 2px dashed {Theme.ACCENT_MAIN}; 
                    color: {Theme.ACCENT_MAIN}; 
                    background-color: {Theme.BG_PANEL};
                    font-size: 16px;
                    border-radius: 10px;
                }}
            """)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(f"""
            QLabel {{
                border: 2px dashed {Theme.BORDER}; 
                color: {Theme.TEXT_DIM}; 
                background-color: {Theme.BG_INPUT};
                font-size: 16px;
                border-radius: 10px;
            }}
        """)

class MaskDrawingLabel(QLabel):
    """A QLabel that allows drawing a red mask over an image."""
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.drawing = False
        self.last_point = QPoint()
        self.brush_size = 20
        self.erase_mode = False
        
        self.original_pixmap = None
        self.mask_pixmap = None
        self.scaled_bg = None # Cache for background
        
        self.setStyleSheet(f"border: 2px dashed {Theme.BORDER}; background-color: {Theme.BG_INPUT};")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setMinimumSize(200, 200)

    def set_image(self, pixmap):
        self.original_pixmap = pixmap
        # Create an empty transparent mask of the same size
        self.mask_pixmap = QPixmap(pixmap.size())
        self.mask_pixmap.fill(Qt.transparent)
        self.scaled_bg = None # Reset cache
        self.update_display()

    def update_display(self):
        if not self.original_pixmap:
            return
            
        target_size = self.size()
        if target_size.width() <= 100:
            target_size = QSize(1200, 800)

        # 1. Update/Cache scaled background
        if not self.scaled_bg or self.scaled_bg.size() != target_size:
            # We use FastTransformation for background caching to keep it snappy
            self.scaled_bg = self.original_pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.FastTransformation)
            
        # 2. Draw on cached background
        # Note: We still draw the MASK overlay. To be fast, we draw the mask pixmap scaled.
        display_pix = self.scaled_bg.copy()
        painter = QPainter(display_pix)
        painter.setOpacity(0.5)
        # Use simple drawPixmap with scaling - Qt handles this efficiently
        painter.drawPixmap(display_pix.rect(), self.mask_pixmap)
        painter.end()
        
        super().setPixmap(display_pix)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = True
            self.last_point = self.map_to_pixmap(event.pos())
            self.draw_on_mask(self.last_point)

    def mouseMoveEvent(self, event):
        if (event.buttons() & Qt.LeftButton) and self.drawing:
            current_point = self.map_to_pixmap(event.pos())
            self.draw_on_mask(current_point)
            self.last_point = current_point

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scaled_bg = None # Invalidate cache
        self.update_display()

    def map_to_pixmap(self, pos):
        if not self.pixmap() or self.pixmap().isNull() or not self.original_pixmap:
            return pos
            
        pm_rect = self.pixmap().rect()
        w_w = self.width()
        w_h = self.height()
        pm_w = self.pixmap().width()
        pm_h = self.pixmap().height()
        
        offset_x = (w_w - pm_w) / 2
        offset_y = (w_h - pm_h) / 2
        
        rel_x = (pos.x() - offset_x) / pm_w
        rel_y = (pos.y() - offset_y) / pm_h
        
        final_x = int(rel_x * self.original_pixmap.width())
        final_y = int(rel_y * self.original_pixmap.height())
        
        # Clamp to image bounds
        final_x = max(0, min(final_x, self.original_pixmap.width() - 1))
        final_y = max(0, min(final_y, self.original_pixmap.height() - 1))
        
        return QPoint(final_x, final_y)

    def draw_on_mask(self, point):
        if not self.mask_pixmap:
            return
            
        painter = QPainter(self.mask_pixmap)
        # CRITICAL: Always use the brush size even in eraser mode
        pen = QPen(QColor(255, 0, 0, 200), self.brush_size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        
        if self.erase_mode:
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            # Use transparent color + CompositionMode_Clear to erase
            pen.setColor(Qt.transparent)
            
        painter.setPen(pen)
        painter.drawLine(self.last_point, point)
        painter.end()
        self.update_display()

    def get_mask_as_qimage(self):
        if not self.mask_pixmap:
            return None
        return self.mask_pixmap.toImage()

    def clear_mask(self):
        if self.mask_pixmap:
            self.mask_pixmap.fill(Qt.transparent)
            self.update_display()

class WorkshopModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Panopticon", "Workshop")
        self.queue_paths = []
        self.selected_paths = set()
        self.view = None
        
        # Metadata Reader State
        self.reader_image_list = []
        self.reader_current_index = -1
        
        # Watermarker State
        self.watermark_path = None
        self.logo_path = None
        self.watermark_queue = []
        
        # Paths persistence
        self.last_asset_dir = self.settings.value("last_asset_dir", os.path.expanduser("~"))
        self.last_watermark_dir = self.settings.value("last_watermark_dir", self.last_asset_dir)
        self.last_logo_dir = self.settings.value("last_logo_dir", self.last_asset_dir)
        self.last_batch_dir = self.settings.value("last_batch_dir", self.last_asset_dir)
        self.last_optimizer_dir = self.settings.value("last_optimizer_dir", self.last_asset_dir)
        
        # Optimizer State
        self.optimizer_queue = []
        self.optimizer_analysis_results = {}
        
        # Root export dir
        self.export_dir = self.settings.value("export_dir", os.path.abspath("Workshop_Exports"))
        if not os.path.isabs(self.export_dir):
            self.export_dir = os.path.abspath(self.export_dir)

    @property
    def name(self):
        return "The Workshop"

    @property
    def description(self):
        return "Batch processing, metadata stripping, and image transformations."

    @property
    def icon(self):
        return "🛠️"

    @property
    def accent_color(self):
        return Theme.ACCENT_MAIN

    # ... Properties ...

    def get_view(self):
        if not self.view:
            self.view = QWidget()
            self.view.setAcceptDrops(True)
            self.view.dragEnterEvent = self.dragEnterEvent
            self.view.dropEvent = self.dropEvent
            
            layout = QVBoxLayout(self.view)
            layout.setContentsMargins(20, 20, 20, 20)

            # --- Header ---
            self.header_layout = QHBoxLayout()
            self.btn_back_to_dash = QPushButton("↩ WORKSHOP")
            self.btn_back_to_dash.setVisible(False)
            self.btn_back_to_dash.clicked.connect(self.switch_to_dashboard)
            self.btn_back_to_dash.setFixedSize(120, 35)
            self.btn_back_to_dash.setStyleSheet(Theme.get_button_style(Theme.ACCENT_MAIN))
            
            self.header_layout.addWidget(self.btn_back_to_dash)
            self.header_layout.addSpacing(10)

            self.lbl_title = QLabel("🛠️ The Workshop")
            self.lbl_title.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {Theme.ACCENT_MAIN};")
            self.header_layout.addWidget(self.lbl_title)
            self.header_layout.addStretch()
            layout.addLayout(self.header_layout)

            # --- Dashboard Area ---
            self.workshop_dashboard = QWidget()
            dashboard_layout = QVBoxLayout(self.workshop_dashboard)
            dashboard_layout.setContentsMargins(0, 40, 0, 40)
            dashboard_layout.setAlignment(Qt.AlignCenter)
            
            lbl_dash_welcome = QLabel("What are we building today?")
            lbl_dash_welcome.setStyleSheet("color: white; font-size: 28px; font-weight: bold; margin-bottom: 30px;")
            lbl_dash_welcome.setAlignment(Qt.AlignCenter)
            dashboard_layout.addWidget(lbl_dash_welcome)
            
            self.cards_grid = QGridLayout()
            self.cards_grid.setSpacing(25)
            self.cards_grid.setAlignment(Qt.AlignCenter)
            
            # Tool Cards
            self.cards_grid.addWidget(self.create_tool_card(
                "🛡️ Metadata Modifier", 
                "Clean stripping or custom prompt injection for batch images.",
                "#00ffcc", self.switch_to_stripper
            ), 0, 0)
            
            self.cards_grid.addWidget(self.create_tool_card(
                "🎭 Dummy Creator", 
                "Save disk space by creating 32x32 placeholders for your library.",
                "#f1fa8c", self.switch_to_dummy
            ), 0, 1)
            
            self.cards_grid.addWidget(self.create_tool_card(
                "📋 Metadata Reader", 
                "Deep inspection of AI generation data (SD, ComfyUI, etc).",
                "#bd93f9", self.switch_to_reader
            ), 1, 0)
            
            self.cards_grid.addWidget(self.create_tool_card(
                "🖼️ Watermarker", 
                "Apply patterns and logos with professional transparency.",
                "#50fa7b", self.switch_to_watermarker
            ), 1, 1)

            self.cards_grid.addWidget(self.create_tool_card(
                "⚡ Image Optimizer", 
                "Reduce size significantly without losing perceptible quality.",
                "#ffb86c", self.switch_to_optimizer
            ), 2, 0)

            self.cards_grid.addWidget(self.create_tool_card(
                "🎯 Face Scorer", 
                "Score images by face clarity for optimal dataset curation.",
                "#ff5555", self.switch_to_face_scorer
            ), 2, 1)

            
            dashboard_layout.addLayout(self.cards_grid)
            dashboard_layout.addStretch()
            
            # Settings button at the bottom of splash
            btn_settings_dash = QPushButton("⚙️ Workshop Settings")
            btn_settings_dash.setFixedSize(200, 40)
            btn_settings_dash.clicked.connect(self.switch_to_settings)
            btn_settings_dash.setStyleSheet("""
                QPushButton { background-color: #222; color: #ff79c6; border-radius: 8px; border: 1px solid #333; font-weight: bold; }
                QPushButton:hover { background-color: #333; border-color: #ff79c6; }
            """)
            dashboard_layout.addWidget(btn_settings_dash, 0, Qt.AlignCenter)
            
            layout.addWidget(self.workshop_dashboard)

            # --- Metadata Stripper Content Area ---
            self.stripper_panel = QWidget()
            self.stripper_panel.setVisible(False)
            stripper_outer_layout = QVBoxLayout(self.stripper_panel)
            stripper_outer_layout.setContentsMargins(0, 10, 0, 0)
            
            # Centering Container
            stripper_container = QWidget()
            stripper_container.setMaximumWidth(1400)
            stripper_layout = QVBoxLayout(stripper_container)
            stripper_layout.setContentsMargins(0, 0, 0, 0)
            
            stripper_outer_layout.addWidget(stripper_container, 1, Qt.AlignHCenter)
            
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
            dummy_outer_layout = QVBoxLayout(self.dummy_panel)
            dummy_outer_layout.setContentsMargins(0, 50, 0, 50)
            
            # Centering Container
            dummy_container = QWidget()
            dummy_container.setMaximumWidth(800)  # Narrower for text-heavy panel
            dummy_layout = QVBoxLayout(dummy_container)
            dummy_layout.setContentsMargins(0, 0, 0, 0)
            
            dummy_outer_layout.addWidget(dummy_container, 1, Qt.AlignHCenter)
            
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
            reader_outer_layout = QVBoxLayout(self.reader_panel)
            reader_outer_layout.setContentsMargins(0, 10, 0, 0)
            
            # Centering Container
            reader_container = QWidget()
            reader_container.setMaximumWidth(1400)
            reader_layout = QVBoxLayout(reader_container)
            reader_layout.setContentsMargins(0, 0, 0, 0)
            reader_layout.setSpacing(15)
            
            reader_outer_layout.addWidget(reader_container, 1, Qt.AlignHCenter)
            
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
            
            # Stretch factors: Image=3, Metadata=2
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
            wm_outer_layout = QVBoxLayout(self.watermarker_panel)
            wm_outer_layout.setContentsMargins(0, 10, 0, 0)
            
            # Centering Container
            wm_container = QWidget()
            wm_container.setMaximumWidth(1400)
            watermarker_layout = QVBoxLayout(wm_container)
            watermarker_layout.setContentsMargins(0, 0, 0, 0)
            
            wm_outer_layout.addWidget(wm_container, 1, Qt.AlignHCenter)
            
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

            # --- Image Optimizer Content Area (initially hidden) ---
            self.optimizer_panel = QWidget()
            opt_outer_layout = QVBoxLayout(self.optimizer_panel)
            opt_outer_layout.setContentsMargins(0, 10, 0, 0)
            
            opt_container = QWidget()
            opt_container.setMaximumWidth(1400)
            optimizer_layout = QVBoxLayout(opt_container)
            optimizer_layout.setContentsMargins(0, 0, 0, 0)
            opt_outer_layout.addWidget(opt_container, 1, Qt.AlignHCenter)
            
            optimizer_controls = QHBoxLayout()
            
            # Left: Controls
            opt_left_panel = QFrame()
            opt_left_panel.setFixedWidth(320)
            opt_left_panel.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 15px;")
            opt_left_layout = QVBoxLayout(opt_left_panel)
            opt_left_layout.setSpacing(12)
            
            lbl_opt_input = QLabel("📥 1. INPUT & RESIZE")
            lbl_opt_input.setStyleSheet("color: #ffb86c; font-weight: bold; font-size: 11px;")
            opt_left_layout.addWidget(lbl_opt_input)
            
            btn_opt_add_layout = QHBoxLayout()
            self.btn_opt_add_files = QPushButton("🖼️ Images")
            self.btn_opt_add_files.clicked.connect(self.optimizer_add_images)
            self.btn_opt_add_files.setStyleSheet("background-color: #333; color: white; padding: 8px; border-radius: 5px;")
            
            self.btn_opt_add_folder = QPushButton("📂 Folder")
            self.btn_opt_add_folder.clicked.connect(self.optimizer_add_folder)
            self.btn_opt_add_folder.setStyleSheet("background-color: #333; color: white; padding: 8px; border-radius: 5px;")
            
            btn_opt_add_layout.addWidget(self.btn_opt_add_files)
            btn_opt_add_layout.addWidget(self.btn_opt_add_folder)
            opt_left_layout.addLayout(btn_opt_add_layout)
            
            # Resize Group
            resize_frame = QFrame()
            resize_frame.setStyleSheet("background-color: #111; border-radius: 5px; padding: 8px;")
            resize_layout = QVBoxLayout(resize_frame)
            
            lbl_resize = QLabel("Resize Preset:")
            lbl_resize.setStyleSheet("color: #aaa; font-size: 10px;")
            resize_layout.addWidget(lbl_resize)
            
            self.chk_opt_enable_resize = QCheckBox("Enable Resize")
            self.chk_opt_enable_resize.setChecked(False)
            self.chk_opt_enable_resize.setStyleSheet("color: white; font-weight: bold; margin-bottom: 5px;")
            resize_layout.addWidget(self.chk_opt_enable_resize)
            
            self.combo_opt_resize = QComboBox()
            self.combo_opt_resize.addItems(["Keep Original Size", "Resize (Longest Side)"])
            self.combo_opt_resize.currentIndexChanged.connect(self.optimizer_on_resize_preset_changed)
            self.combo_opt_resize.setStyleSheet("background-color: #222; color: white; padding: 5px;")
            self.combo_opt_resize.setEnabled(False) # Controlled by checkbox
            self.chk_opt_enable_resize.toggled.connect(self.combo_opt_resize.setEnabled)
            resize_layout.addWidget(self.combo_opt_resize)
            
            dim_layout = QHBoxLayout()
            self.spin_opt_max_side = QSpinBox()
            self.spin_opt_max_side.setRange(1, 10000)
            self.spin_opt_max_side.setValue(1024)
            self.spin_opt_max_side.setEnabled(False)
            
            dim_layout.addWidget(QLabel("Target Longest Side:"))
            dim_layout.addWidget(self.spin_opt_max_side)
            resize_layout.addLayout(dim_layout)
            
            self.chk_opt_lock_aspect = QCheckBox("Lock Aspect Ratio")
            self.chk_opt_lock_aspect.setChecked(True)
            self.chk_opt_lock_aspect.setVisible(False) # Now implicit for longest side
            
            opt_left_layout.addWidget(resize_frame)
            
            # 2. Format & Quality
            lbl_opt_format = QLabel("⚙️ 2. FORMAT & OPTIMIZATION")
            lbl_opt_format.setStyleSheet("color: #ffb86c; font-weight: bold; font-size: 11px;")
            opt_left_layout.addWidget(lbl_opt_format)

            lbl_opt_format_hint = QLabel("Select Output Format:")
            lbl_opt_format_hint.setStyleSheet("color: #aaa; font-size: 10px;")
            opt_left_layout.addWidget(lbl_opt_format_hint)
            
            self.combo_opt_format = QComboBox()
            self.combo_opt_format.addItems([
                "Original (Same as source)", 
                "PNG (Lossless / Alpha support)", 
                "JPEG (High compression for photos)", 
                "WebP (Modern / Efficient)"
            ])
            self.combo_opt_format.setStyleSheet("background-color: #222; color: white; padding: 5px;")
            opt_left_layout.addWidget(self.combo_opt_format)
            
            self.btn_opt_analyze = QPushButton("🔍 Analyze & Suggest")
            self.btn_opt_analyze.clicked.connect(self.optimizer_analyze_and_suggest)
            self.btn_opt_analyze.setStyleSheet("background-color: #333; color: #ffb86c; padding: 5px; font-size: 10px;")
            opt_left_layout.addWidget(self.btn_opt_analyze)
            
            self.lbl_opt_suggestion = QLabel("Suggestion: -")
            self.lbl_opt_suggestion.setStyleSheet("color: #888; font-size: 10px; font-style: italic;")
            opt_left_layout.addWidget(self.lbl_opt_suggestion)
            
            # Quality is now handled automatically for optimal results
            self.slider_opt_quality = QSlider(Qt.Horizontal)
            self.slider_opt_quality.setRange(1, 100)
            self.slider_opt_quality.setValue(92)
            self.slider_opt_quality.setVisible(False)
            
            self.chk_opt_preserve_meta = QCheckBox("Preserve AI Metadata (Prompts)")
            self.chk_opt_preserve_meta.setChecked(True)
            self.chk_opt_preserve_meta.setStyleSheet("color: #eee; font-size: 11px;")
            opt_left_layout.addWidget(self.chk_opt_preserve_meta)
            
            opt_left_layout.addStretch()
            
            # 3. Export
            lbl_opt_exp = QLabel("📁 3. EXPORT TARGET")
            lbl_opt_exp.setStyleSheet("color: #ffb86c; font-weight: bold; font-size: 11px;")
            opt_left_layout.addWidget(lbl_opt_exp)
            
            exp_row = QHBoxLayout()
            self.txt_opt_export_path = QTextEdit()
            self.txt_opt_export_path.setMaximumHeight(60)
            self.txt_opt_export_path.setReadOnly(True)
            self.txt_opt_export_path.setPlaceholderText("Select base folder...")
            self.txt_opt_export_path.setStyleSheet("background-color: #0a0a0a; color: #888; border: 1px solid #333; font-size: 10px;")
            
            btn_opt_browse = QPushButton("...")
            btn_opt_browse.setFixedWidth(40)
            btn_opt_browse.clicked.connect(self.optimizer_browse_export)
            btn_opt_browse.setStyleSheet("background-color: #333; color: white; border-radius: 3px;")
            
            exp_row.addWidget(self.txt_opt_export_path)
            exp_row.addWidget(btn_opt_browse)
            opt_left_layout.addLayout(exp_row)
            
            self.txt_opt_export_path.setText(self.last_optimizer_dir)
            
            self.btn_opt_process = QPushButton("⚡ OPTIMIZE BATCH")
            self.btn_opt_process.clicked.connect(self.optimizer_process_batch)
            self.btn_opt_process.setMinimumHeight(45)
            self.btn_opt_process.setStyleSheet("""
                QPushButton { background-color: #ffb86c; color: black; font-weight: bold; border-radius: 5px; }
                QPushButton:hover { background-color: #ffaa55; }
            """)
            opt_left_layout.addWidget(self.btn_opt_process)
            
            optimizer_controls.addWidget(opt_left_panel)
            
            # Right: Queue & Stats
            opt_right_panel = QFrame()
            opt_right_panel.setStyleSheet("background-color: #111; border-radius: 10px;")
            opt_right_layout = QVBoxLayout(opt_right_panel)
            
            opt_queue_header = QHBoxLayout()
            opt_queue_header.addWidget(QLabel("OPTIMIZATION QUEUE"))
            opt_queue_header.addStretch()
            
            self.btn_opt_clear = QPushButton("🗑️ Clear")
            self.btn_opt_clear.clicked.connect(self.optimizer_clear_queue)
            self.btn_opt_clear.setStyleSheet("background-color: #222; color: #888; font-size: 10px; padding: 2px 10px;")
            opt_queue_header.addWidget(self.btn_opt_clear)
            opt_right_layout.addLayout(opt_queue_header)
            
            self.opt_queue_scroll = QScrollArea()
            self.opt_queue_scroll.setWidgetResizable(True)
            self.opt_queue_scroll.setStyleSheet("border: none; background: transparent;")
            self.opt_queue_grid_widget = QWidget()
            self.opt_queue_grid_layout = QGridLayout(self.opt_queue_grid_widget)
            self.opt_queue_grid_layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.opt_queue_scroll.setWidget(self.opt_queue_grid_widget)
            opt_right_layout.addWidget(self.opt_queue_scroll, 1)
            
            # Stats bottom bar
            self.lbl_opt_stats = QLabel("Ready to optimize. Add images to see total savings estimate.")
            self.lbl_opt_stats.setStyleSheet("background-color: #1a1a1a; color: #ffb86c; padding: 10px; border-top: 1px solid #333; border-radius: 0 0 10px 10px;")
            opt_right_layout.addWidget(self.lbl_opt_stats)
            
            optimizer_controls.addWidget(opt_right_panel, 1)
            optimizer_layout.addLayout(optimizer_controls)
            
            self.optimizer_panel.setVisible(False)
            layout.addWidget(self.optimizer_panel)

            # --- Face Scorer Content Area ---
            self.face_scorer_panel = QWidget()
            self.face_scorer_panel.setVisible(False)
            fs_outer_layout = QVBoxLayout(self.face_scorer_panel)
            fs_outer_layout.setContentsMargins(0, 10, 0, 0)
            
            fs_container = QWidget()
            fs_container.setMaximumWidth(1400)
            fs_layout = QVBoxLayout(fs_container)
            fs_layout.setContentsMargins(0, 0, 0, 0)
            fs_outer_layout.addWidget(fs_container, 1, Qt.AlignHCenter)
            
            fs_controls = QHBoxLayout()
            
            # Left: Controls Panel
            fs_left_panel = QFrame()
            fs_left_panel.setFixedWidth(300)
            fs_left_panel.setStyleSheet(f"background-color: {Theme.BG_PANEL}; border: 1px solid {Theme.BORDER}; border-radius: 10px; padding: 15px;")
            fs_left_layout = QVBoxLayout(fs_left_panel)
            fs_left_layout.setSpacing(15)
            
            lbl_fs_title = QLabel("🎯 FACE SCORER")
            lbl_fs_title.setStyleSheet(f"color: #ff5555; font-weight: bold; font-size: 14px;")
            fs_left_layout.addWidget(lbl_fs_title)
            
            lbl_fs_desc = QLabel("Score images by face detection confidence for optimal dataset curation.")
            lbl_fs_desc.setWordWrap(True)
            lbl_fs_desc.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
            fs_left_layout.addWidget(lbl_fs_desc)
            
            fs_left_layout.addSpacing(10)
            
            self.btn_fs_load = QPushButton("📂 Load Folder")
            self.btn_fs_load.setStyleSheet(Theme.get_action_button_style("#ff5555", "#ffffff"))
            self.btn_fs_load.setFixedHeight(40)
            self.btn_fs_load.clicked.connect(self._fs_load_folder)
            fs_left_layout.addWidget(self.btn_fs_load)
            
            self.btn_fs_analyze = QPushButton("🔍 Analyze & Auto-Sort")
            self.btn_fs_analyze.setEnabled(False)
            self.btn_fs_analyze.setStyleSheet(Theme.get_action_button_style("#ff5555", "#ffffff"))
            self.btn_fs_analyze.setFixedHeight(40)
            self.btn_fs_analyze.clicked.connect(self._fs_analyze)
            fs_left_layout.addWidget(self.btn_fs_analyze)
            
            fs_left_layout.addSpacing(10)
            
            # Threshold Slider
            lbl_threshold = QLabel("Minimum Score Threshold:")
            lbl_threshold.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-size: 12px;")
            fs_left_layout.addWidget(lbl_threshold)
            
            threshold_row = QHBoxLayout()
            self.fs_threshold_slider = QSlider(Qt.Horizontal)
            self.fs_threshold_slider.setRange(0, 100)
            self.fs_threshold_slider.setValue(50)
            self.fs_threshold_slider.valueChanged.connect(self._fs_update_threshold_label)
            threshold_row.addWidget(self.fs_threshold_slider)
            
            self.lbl_fs_threshold_val = QLabel("50")
            self.lbl_fs_threshold_val.setFixedWidth(35)
            self.lbl_fs_threshold_val.setStyleSheet("color: #ff5555; font-weight: bold;")
            threshold_row.addWidget(self.lbl_fs_threshold_val)
            fs_left_layout.addLayout(threshold_row)
            
            fs_left_layout.addStretch()
            
            # Auto-sort happens automatically now
            note = QLabel("Images will be automatically sorted into\npercentage folders (100%/, 90%/...)")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
            fs_left_layout.addWidget(note)
            
            fs_controls.addWidget(fs_left_panel)
            
            # Right: Results Grid
            fs_right_panel = QFrame()
            fs_right_panel.setStyleSheet(f"background-color: {Theme.BG_PANEL}; border: 1px solid {Theme.BORDER}; border-radius: 10px;")
            fs_right_layout = QVBoxLayout(fs_right_panel)
            
            # Results Text Area (Stats)
            self.fs_stats_text = QTextEdit()
            self.fs_stats_text.setReadOnly(True)
            self.fs_stats_text.setStyleSheet(f"background-color: #0a0a0a; color: {Theme.TEXT_PRIMARY}; border: none; font-family: Consolas, monospace;")
            fs_right_layout.addWidget(self.fs_stats_text, 1)
            
            # Buttons to open folders
            self.fs_folders_widget = QWidget()
            self.fs_folders_layout = QHBoxLayout(self.fs_folders_widget)
            self.fs_folders_layout.setAlignment(Qt.AlignLeft)
            fs_right_layout.addWidget(self.fs_folders_widget)

            
            # Stats bar
            self.lbl_fs_stats = QLabel("Ready. Load a folder to begin scoring.")
            self.lbl_fs_stats.setStyleSheet("background-color: #1a1a1a; color: #ff5555; padding: 10px; border-top: 1px solid #333;")
            fs_right_layout.addWidget(self.lbl_fs_stats)
            
            fs_controls.addWidget(fs_right_panel, 1)
            fs_layout.addLayout(fs_controls)
            
            layout.addWidget(self.face_scorer_panel)
            
            # Instance variables for face scorer
            self.fs_image_paths = []
            self.fs_results = []
            self.fs_selected_paths = set()
            self.fs_thumbnails = {}  # path -> widget

            # --- Settings Content Area (initially hidden) ---
            self.settings_panel = QWidget()
            settings_layout = QVBoxLayout(self.settings_panel)
            settings_layout.setContentsMargins(0, 0, 0, 0)
            
            settings_frame = QFrame()
            settings_frame.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 25px;")
            settings_inner_layout = QVBoxLayout(settings_frame)
            
            lbl_settings_header = QLabel("⚙️ WORKSHOP CONFIGURATION")
            lbl_settings_header.setStyleSheet("font-size: 18px; font-weight: bold; color: #ff79c6; margin-bottom: 10px;")
            settings_inner_layout.addWidget(lbl_settings_header)
            
            # Export Directory
            lbl_export_title = QLabel("Root Export Directory")
            lbl_export_title.setStyleSheet("color: #eee; font-weight: bold; font-size: 14px;")
            settings_inner_layout.addWidget(lbl_export_title)
            
            lbl_export_desc = QLabel("This folder will house subfolders for each tool (e.g., /watermarker, /metadata_modifier).")
            lbl_export_desc.setStyleSheet("color: #888; font-size: 11px; margin-bottom: 10px;")
            settings_inner_layout.addWidget(lbl_export_desc)
            
            dir_selection_layout = QHBoxLayout()
            self.lbl_current_export_path = QLabel(self.export_dir)
            self.lbl_current_export_path.setStyleSheet("""
                background-color: #111; 
                color: #50fa7b; 
                padding: 12px; 
                border-radius: 6px; 
                border: 1px solid #333;
                font-family: Consolas;
                font-size: 12px;
            """)
            self.lbl_current_export_path.setWordWrap(True)
            
            btn_change_dir = QPushButton("Browse Folder")
            btn_change_dir.clicked.connect(self.browse_export_dir)
            btn_change_dir.setFixedSize(120, 40)
            btn_change_dir.setStyleSheet("""
                QPushButton { background-color: #444; color: white; border-radius: 5px; font-weight: bold; }
                QPushButton:hover { background-color: #555; }
            """)
            
            dir_selection_layout.addWidget(self.lbl_current_export_path, 1)
            dir_selection_layout.addWidget(btn_change_dir)
            settings_inner_layout.addLayout(dir_selection_layout)
            
            settings_inner_layout.addStretch()
            settings_layout.addWidget(settings_frame)
            
            self.settings_panel.setVisible(False)
            layout.addWidget(self.settings_panel)

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


    # --- Metadata Reader Logic ---
    def reader_open_images(self):
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images", self.last_asset_dir, "Images (*.png *.jpg *.webp)")
        if files:
            self.last_asset_dir = os.path.dirname(files[0])
            self.settings.setValue("last_asset_dir", self.last_asset_dir)
            self.reader_load_list(files)

    def reader_open_folder(self):
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder", self.last_asset_dir)
        if folder:
            self.last_asset_dir = folder
            self.settings.setValue("last_asset_dir", self.last_asset_dir)
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
        elif tool == "optimizer":
            self.switch_to_optimizer()
            # Add to optimizer queue
            for p in paths:
                if p not in self.optimizer_queue:
                    self.optimizer_queue.append(p)
            self._update_optimizer_queue_grid()
            self._update_optimizer_stats()
        else:
            # Default to modifier (stripper/changer)
            self.btn_tool_stripper.setChecked(True)
            self.switch_to_stripper()
            self.add_to_queue(paths)

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
        """Dispatches paths to the correct queue based on active panel."""
        if self.optimizer_panel.isVisible():
            for p in paths:
                if p not in self.optimizer_queue:
                    self.optimizer_queue.append(p)
            self._update_optimizer_queue_grid()
            self._update_optimizer_stats()
        elif self.watermarker_panel.isVisible():
            for p in paths:
                if p not in self.watermark_queue:
                    self.watermark_queue.append(p)
            self._update_wm_queue_grid()
        elif self.reader_panel.isVisible():
            self.reader_handle_dropped(paths)
        else:
            # Default to stripper queue
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
        # Determine active tool by checking which panel is visible
        if self.stripper_panel.isVisible():
            self._process_modifier_batch()
        elif self.watermarker_panel.isVisible():
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
        export_path = os.path.join(self.export_dir, "metadata_modifier")
        
        for path in self.queue_paths:
            dest = get_export_path(path, export_dir=export_path)
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
        export_path = os.path.join(self.export_dir, "watermarker")
        
        for path in self.watermark_queue:
            dest = watermark_export_path(path, export_dir=export_path)
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
                                f"Successfully processed {success_count} of {count} images.\nResults saved in: {export_path}")

    def open_dummy_creator_dialog(self):
        """Opens the Dummy Creator dialog for folder selection and processing."""
        from PySide6.QtWidgets import QDialog, QLabel, QTextEdit, QApplication
        from modules.workshop.logic.dummy_manager import process_folder, get_folder_stats
        
        # Select folder
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder to Dummify", self.last_asset_dir)
        if not folder:
            return
        
        self.last_asset_dir = folder
        self.settings.setValue("last_asset_dir", self.last_asset_dir)
        
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

    def switch_to_dashboard(self):
        """Show main workshop dashboard."""
        self.workshop_dashboard.setVisible(True)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(False)
        self.btn_back_to_dash.setVisible(False)
        self.lbl_title.setText("🛠️ The Workshop")

    def create_tool_card(self, title, desc, color, callback):
        """Helper to create decorative tool cards for the dashboard."""
        card = QFrame()
        card.setFixedSize(300, 180)
        card.setStyleSheet(f"""
            QFrame {{
                background-color: #1a1a1a;
                border: 2px solid #333;
                border-radius: 15px;
            }}
            QFrame:hover {{
                border: 2px solid {color};
                background-color: #222;
            }}
        """)
        
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold; border: none; background: transparent;")
        lbl_title.setAlignment(Qt.AlignLeft)
        card_layout.addWidget(lbl_title)
        
        lbl_desc = QLabel(desc)
        lbl_desc.setStyleSheet("color: #888; font-size: 12px; border: none; background: transparent;")
        lbl_desc.setWordWrap(True)
        lbl_desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        card_layout.addWidget(lbl_desc)
        
        btn = QPushButton("Open Tool")
        btn.clicked.connect(callback)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #2a2a2a;
                color: white;
                border-radius: 8px;
                padding: 10px;
                font-weight: bold;
                border: 1px solid #444;
            }}
            QPushButton:hover {{
                background-color: {color};
                color: black;
            }}
        """)
        card_layout.addWidget(btn)
        
        return card

    def switch_to_face_scorer(self):
        """Switch to Face Scorer tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(True)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(False)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("🎯 Face Scorer")
        self.progress.setVisible(False)

    def switch_to_stripper(self):
        """Switch to Metadata Stripper tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(True)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(True)
        self.btn_process.setEnabled(len(self.queue_paths) > 0)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("🛡️ Metadata Modifier")

    def switch_to_optimizer(self):
        """Switch to Image Optimizer tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(True)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(False) # Has its own process button
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("⚡ Image Optimizer")
        self.progress.setVisible(False)
        # Ensure grid is up to date when switching
        if hasattr(self, '_update_optimizer_queue_grid'):
            self._update_optimizer_queue_grid()
    
    def switch_to_dummy(self):
        """Switch to Dummy Creator tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(True)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(False)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("🎭 Dummy Creator")

    def switch_to_reader(self):
        """Switch to Metadata Reader tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(True)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(False)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("📋 Metadata Reader")

    def switch_to_watermarker(self):
        """Switch to Watermarker tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(True)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(False)
        self.btn_process.setVisible(True)
        self.btn_process.setEnabled(len(self.watermark_queue) > 0)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("🖼️ Watermarker")


    def switch_to_settings(self):
        """Switch to Settings tool."""
        self.workshop_dashboard.setVisible(False)
        self.stripper_panel.setVisible(False)
        self.dummy_panel.setVisible(False)
        self.reader_panel.setVisible(False)
        self.watermarker_panel.setVisible(False)
        self.optimizer_panel.setVisible(False)
        self.face_scorer_panel.setVisible(False)
        self.settings_panel.setVisible(True)
        self.btn_process.setVisible(False)
        self.btn_back_to_dash.setVisible(True)
        self.lbl_title.setText("⚙️ Workshop Settings")
    
    def browse_export_dir(self):
        """Browse and set custom root export directory."""
        folder = QFileDialog.getExistingDirectory(self.view, "Select Root Export Directory", self.export_dir)
        if folder:
            self.export_dir = os.path.abspath(folder)
            self.lbl_current_export_path.setText(self.export_dir)
            self.settings.setValue("export_dir", self.export_dir)

    # --- Face Scorer Logic ---
    def _fs_load_folder(self):
        """Load images from a folder for face scoring."""
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder with Images", self.export_dir)
        if not folder:
            return
        
        extensions = ('.png', '.jpg', '.jpeg', '.webp')
        self.fs_image_paths = []
        
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(extensions):
                    self.fs_image_paths.append(os.path.join(root, f))
        
        count = len(self.fs_image_paths)
        self.lbl_fs_stats.setText(f"Loaded {count} images. Ready to score and auto-sort.")
        self.btn_fs_analyze.setEnabled(count > 0)
        
        # Clear previous state
        self.fs_results = []
        if hasattr(self, 'fs_stats_text'):
            self.fs_stats_text.clear()
        
        # Clear folder buttons
        if hasattr(self, 'fs_folders_layout'):
            while self.fs_folders_layout.count():
                item = self.fs_folders_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

    def _fs_analyze(self):
        """Run face detection, score, and IMMEDIATELY sort/move files."""
        if not self.fs_image_paths:
            return
        
        from modules.workshop.logic.face_scorer import score_batch, sort_files_by_score
        
        # 1. Score
        self.progress.setRange(0, len(self.fs_image_paths))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_fs_analyze.setEnabled(False)
        self.lbl_fs_stats.setText("Scoring images...")
        QApplication.instance().processEvents()
        
        def progress_cb(current, total, path):
            self.progress.setValue(current)
            self.lbl_fs_stats.setText(f"Analyzing: {os.path.basename(path)} ({current}/{total})")
            QApplication.instance().processEvents()
        
        try:
            self.fs_results = score_batch(self.fs_image_paths, progress_cb)
        except Exception as e:
            QMessageBox.critical(self.view, "Error", f"Face scoring failed: {str(e)}")
            self.progress.setVisible(False)
            self.btn_fs_analyze.setEnabled(True)
            return
            
        # 2. Sort & Move (Auto process)
        if not self.fs_results:
            return
            
        threshold = self.fs_threshold_slider.value()
        base_folder = os.path.dirname(self.fs_image_paths[0])
        
        self.lbl_fs_stats.setText("Sorting into folders...")
        QApplication.instance().processEvents()
        
        # Move files
        stats = sort_files_by_score(self.fs_results, threshold, base_folder=base_folder, move_files=True)
        
        # 3. Show Stats & Cleanup
        self.progress.setVisible(False)
        self.btn_fs_analyze.setEnabled(True)
        
        # Clear data since files moved
        self.fs_image_paths.clear()
        
        self._fs_show_stats_summary(stats, base_folder)

    def _fs_show_stats_summary(self, stats, base_folder):
        """Display summary stats and folder buttons."""
        moved = stats["moved_counts"] # {"100%": 5, "90%": 2}
        total = stats["total_moved"]
        errors = stats["errors"]
        
        # 1. Text Report
        report = []
        report.append("=== PROCESSING COMPLETE ===")
        report.append(f"Base Folder: {base_folder}")
        report.append(f"Total Moved: {total}")
        report.append(f"Errors: {errors}")
        report.append("-" * 30)
        report.append("DISTRIBUTION:")
        
        sorted_buckets = sorted(moved.items(), key=lambda x: int(x[0].replace('%', '')), reverse=True)
        bucket_count = 0
        for bucket, count in sorted_buckets:
            report.append(f"  [{bucket}] : {count} images")
            bucket_count += 1
            
        if bucket_count == 0:
            report.append("  (No images met the threshold moved)")
            
        self.fs_stats_text.setText("\n".join(report))
        
        # 2. Folder Buttons
        # Clear previous buttons
        while self.fs_folders_layout.count():
            item = self.fs_folders_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # Add 'Open Base Folder'
        btn_base = QPushButton("📂 Open Root")
        btn_base.setStyleSheet(Theme.get_button_style("#444"))
        btn_base.clicked.connect(lambda: os.startfile(base_folder))
        self.fs_folders_layout.addWidget(btn_base)
        
        # Add buttons for each created bucket
        for bucket, _ in sorted_buckets:
            bucket_path = os.path.join(base_folder, bucket)
            btn = QPushButton(f"Open {bucket}")
            btn.setStyleSheet(Theme.get_button_style(Theme.ACCENT_PRIMARY))
            btn.clicked.connect(lambda checked=False, p=bucket_path: os.startfile(p))
            self.fs_folders_layout.addWidget(btn)
            
        self.lbl_fs_stats.setText(f"Done. {total} images organized.")

    def _fs_update_threshold_label(self, val):
        self.lbl_fs_threshold_val.setText(str(val))

    # --- Watermarker Logic ---
    def load_watermark_asset(self):
        """Load watermark image."""
        file, _ = QFileDialog.getOpenFileName(self.view, "Select Watermark Image", self.last_watermark_dir, "Images (*.png *.jpg *.jpeg *.webp *.svg)")
        if file:
            self.watermark_path = file
            self.last_watermark_dir = os.path.dirname(file)
            self.settings.setValue("last_watermark_dir", self.last_watermark_dir)
            
            self.lbl_watermark_status.setText(f"✅ {os.path.basename(file)}")
            self.lbl_watermark_status.setStyleSheet("color: #50fa7b; font-size: 10px; padding: 5px;")
            self._update_preview_button()
    
    def load_logo_asset(self):
        """Load logo image."""
        file, _ = QFileDialog.getOpenFileName(self.view, "Select Logo Image", self.last_logo_dir, "Images (*.png *.jpg *.jpeg *.webp *.svg)")
        if file:
            self.logo_path = file
            self.last_logo_dir = os.path.dirname(file)
            self.settings.setValue("last_logo_dir", self.last_logo_dir)
            
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
            # Try to use the first image from the queue for a real preview
            from PIL import Image
            if self.watermark_queue:
                demo_image = Image.open(self.watermark_queue[0]).convert('RGBA')
                # Resize for preview performance
                demo_image.thumbnail((1024, 1024), Image.LANCZOS)
            else:
                # Fallback to a neutral background
                demo_image = Image.new('RGBA', (800, 600), (40, 40, 40, 255))
            
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
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images to Watermark", self.last_batch_dir, "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.last_batch_dir = os.path.dirname(files[0])
            self.settings.setValue("last_batch_dir", self.last_batch_dir)
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

    # --- Optimizer Logic ---
    def optimizer_add_images(self):
        """Add images to optimizer queue."""
        files, _ = QFileDialog.getOpenFileNames(self.view, "Select Images to Optimize", self.last_optimizer_dir, "Images (*.png *.jpg *.jpeg *.webp)")
        if files:
            self.last_optimizer_dir = os.path.dirname(files[0])
            self.settings.setValue("last_optimizer_dir", self.last_optimizer_dir)
            count_before = len(self.optimizer_queue)
            for f in files:
                if f not in self.optimizer_queue:
                    self.optimizer_queue.append(f)
            
            added = len(self.optimizer_queue) - count_before
            self._update_optimizer_queue_grid()
            self._update_optimizer_stats()
            
            if added > 0:
                QApplication.instance().processEvents() # Ensure grid updates
                # Small status update instead of a blocking message box for simple files
                self.lbl_opt_stats.setText(f"Successfully added {added} images. {self.lbl_opt_stats.text()}")

    def optimizer_add_folder(self):
        """Add all images from a folder to optimizer queue."""
        folder = QFileDialog.getExistingDirectory(self.view, "Select Folder", self.last_optimizer_dir)
        if folder:
            self.last_optimizer_dir = folder
            self.settings.setValue("last_optimizer_dir", self.last_optimizer_dir)
            files = []
            for root, _, filenames in os.walk(folder):
                for f in filenames:
                    if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                        files.append(os.path.join(root, f))
            
            count_before = len(self.optimizer_queue)
            for f in files:
                if f not in self.optimizer_queue:
                    self.optimizer_queue.append(f)
            
            added = len(self.optimizer_queue) - count_before
            self._update_optimizer_queue_grid()
            self._update_optimizer_stats()
            
            if added > 0:
                QMessageBox.information(self.view, "Folder Loaded", f"Successfully added {added} images from folder.")
            else:
                QMessageBox.warning(self.view, "No Images Found", "No valid or new images were found in the selected folder.")

    def optimizer_browse_export(self):
        """Browse for the base export directory for the optimizer."""
        folder = QFileDialog.getExistingDirectory(self.view, "Select Base Export Folder", self.last_optimizer_dir)
        if folder:
            self.txt_opt_export_path.setText(folder)
            self.last_optimizer_dir = folder
            self.settings.setValue("last_optimizer_dir", folder)

    def optimizer_clear_queue(self):
        """Clear optimizer queue."""
        self.optimizer_queue.clear()
        self.optimizer_analysis_results.clear()
        self._update_optimizer_queue_grid()
        self._update_optimizer_stats()
        self.lbl_opt_suggestion.setText("Suggestion: -")

    def optimizer_on_resize_preset_changed(self, index):
        """Handle resize preset changes."""
        preset = self.combo_opt_resize.currentText()
        is_manual = "Longest Side" in preset
        self.spin_opt_max_side.setEnabled(is_manual)
        
        if not is_manual and preset != "Keep Original Size":
            # Just visual feedback, we'll calculate real pixels during processing
            pass

    def optimizer_analyze_and_suggest(self):
        """Analyze the first image (or a representative set) and suggest format."""
        if not self.optimizer_queue:
            QMessageBox.information(self.view, "No Images", "Add some images to analyze first.")
            return
        
        # Analyze first image
        path = self.optimizer_queue[0]
        self.lbl_opt_suggestion.setText("Analyzing...")
        QApplication.instance().processEvents()
        
        result = analyze_image(path)
        if "error" in result:
            self.lbl_opt_suggestion.setText(f"Error: {result['error']}")
        else:
            suggestion = result['suggested_format']
            reason = result['suggestion_reason']
            self.lbl_opt_suggestion.setText(f"Suggestion: {suggestion} ({reason})")
            
            # Optionally auto-select it if user chose "Auto (Suggested)"
            if self.combo_opt_format.currentText() == "Auto (Suggested)":
                # We'll handle this in process_batch
                pass

    def _update_optimizer_queue_grid(self):
        """Update optimizer queue grid display."""
        if not hasattr(self, 'opt_queue_grid_layout'): return

        while self.opt_queue_grid_layout.count():
            item = self.opt_queue_grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        cols = 6
        max_previews = 120 # Reasonable limit to avoid UI lag
        
        for i, path in enumerate(self.optimizer_queue):
            if i >= max_previews:
                more_lbl = QLabel(f"+ {len(self.optimizer_queue) - max_previews} more...")
                more_lbl.setStyleSheet("color: #666; font-style: italic; font-size: 10px;")
                more_lbl.setAlignment(Qt.AlignCenter)
                self.opt_queue_grid_layout.addWidget(more_lbl, i // cols, i % cols)
                break
                
            row = i // cols
            col = i % cols
            
            thumb = QLabel()
            thumb.setFixedSize(100, 100)
            thumb.setStyleSheet("border: 1px solid #333; border-radius: 4px; background: #050505;")
            thumb.setAlignment(Qt.AlignCenter)
            
            pix = QPixmap(path)
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(98, 98, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            
            self.opt_queue_grid_layout.addWidget(thumb, row, col)
            
        self.btn_opt_process.setEnabled(len(self.optimizer_queue) > 0)

    def _update_optimizer_stats(self):
        """Update the stats label with estimated savings."""
        count = len(self.optimizer_queue)
        if count == 0:
            self.lbl_opt_stats.setText("Ready to optimize. Add images to see total savings estimate.")
            return
        
        total_bytes = 0
        for p in self.optimizer_queue:
            try:
                total_bytes += os.path.getsize(p)
            except: pass
        
        size_mb = total_bytes / (1024 * 1024)
        # Estimate 40% savings on average for research-backed estimate
        est_savings = size_mb * 0.4 
        
        self.lbl_opt_stats.setText(f"Queue: {count} images | Total Input: {size_mb:.2f} MB | Est. Savings: ~{est_savings:.2f} MB (40%)")

    def optimizer_process_batch(self):
        """Execute the optimization pipeline for all images in queue."""
        if not self.optimizer_queue:
            return
            
        count = len(self.optimizer_queue)
        self.progress.setMaximum(count)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_opt_process.setEnabled(False)
        self.btn_opt_clear.setEnabled(False)
        
        # 1. Get Settings
        format_mode = self.combo_opt_format.currentText()
        # Assume optimal quality (92 is a great balance for apparent quality)
        quality = 92
        resize_enabled = self.chk_opt_enable_resize.isChecked()
        resize_preset = self.combo_opt_resize.currentText()
        lock_aspect = True # Always lock now
        preserve_meta = self.chk_opt_preserve_meta.isChecked()
        
        base_export = self.txt_opt_export_path.toPlainText().strip()
        if not base_export:
            QMessageBox.warning(self.view, "No Export Path", "Please select an export directory first.")
            return
            
        export_path = os.path.join(base_export, "optimized")
        if not os.path.exists(export_path):
            try:
                os.makedirs(export_path, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self.view, "Error", f"Could not create 'optimized' folder: {str(e)}")
                return
        
        success_count = 0
        total_saved = 0
        
        for path in self.optimizer_queue:
            # Determine target format
            target_format = None
            if "Original" in format_mode:
                target_format = None
            elif "PNG" in format_mode:
                target_format = "PNG"
            elif "JPEG" in format_mode:
                target_format = "JPEG"
            elif "WebP" in format_mode:
                target_format = "WEBP"
            
            # Determine resize
            r_max_side = None
            if resize_enabled:
                if "Longest Side" in resize_preset:
                    r_max_side = self.spin_opt_max_side.value()
                # If "Keep Original Size" but resize enabled (redundant but handled), r_max_side stays None
                # No more percents to handle here

            # Run optimization
            dest = optimizer_export_path(path, export_dir=export_path)
            result = optimize_image(
                path, dest,
                format_override=target_format,
                quality=quality,
                max_side=r_max_side,
                preserve_metadata=preserve_meta
            )
            
            if result["success"]:
                success_count += 1
                total_saved += result["saved_bytes"]
            
            self.progress.setValue(self.progress.value() + 1)
            QApplication.instance().processEvents()



