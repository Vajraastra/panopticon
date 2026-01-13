import os
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFileDialog, QMessageBox, QFrame, QSplitter, QSpinBox,
    QScrollArea, QSizePolicy, QGridLayout
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QImage
from core.base_module import BaseModule

from modules.fashion_studio.logic.cropper_widget import ImageCropperWidget
from modules.fashion_studio.logic.cropper_logic import crop_image

class FashionStudioModule(BaseModule):
    def __init__(self):
        super().__init__()
        self.view = None
        self.image_path = None
        self._updating_inputs = False
        
    @property
    def name(self):
        return "Fashion Studio"

    @property
    def description(self):
        return "High-precision image editing and smart cropping."

    @property
    def icon(self):
        return "👗"

    def get_view(self):
        if not self.view:
            self.view = QWidget()
            self.view.setAcceptDrops(True)
            self.view.dragEnterEvent = self.dragEnterEvent
            self.view.dropEvent = self.dropEvent
            self.build_ui()
        return self.view

    def build_ui(self):
        self.main_layout = QVBoxLayout(self.view)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # --- Header ---
        self.header = QFrame()
        self.header.setFixedHeight(60)
        self.header.setStyleSheet("background-color: #1a1a1a; border-bottom: 1px solid #333;")
        header_layout = QHBoxLayout(self.header)
        
        self.btn_back_to_dash = QPushButton("↩ DASHBOARD")
        self.btn_back_to_dash.setVisible(False)
        self.btn_back_to_dash.clicked.connect(self.switch_to_dashboard)
        self.btn_back_to_dash.setFixedSize(120, 35)
        self.btn_back_to_dash.setStyleSheet("""
            QPushButton { 
                background-color: #333; 
                color: #ff79c6; 
                border-radius: 6px; 
                font-weight: bold; 
                font-size: 11px;
            }
            QPushButton:hover { background-color: #444; }
        """)
        header_layout.addWidget(self.btn_back_to_dash)
        header_layout.addSpacing(10)

        self.lbl_title = QLabel(f"{self.icon} {self.name}")
        self.lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #ff79c6;")
        header_layout.addWidget(self.lbl_title)
        header_layout.addStretch()
        
        self.btn_open = QPushButton("📂 Open Image")
        self.btn_open.setVisible(False) # Only in tools
        self.btn_open.clicked.connect(self.open_image_dialog)
        self.btn_open.setStyleSheet("""
            QPushButton {
                background-color: #333;
                color: white;
                border-radius: 5px;
                padding: 8px 15px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #444; }
        """)
        header_layout.addWidget(self.btn_open)
        
        self.main_layout.addWidget(self.header)

        # --- Dashboard Area ---
        self.dashboard = QWidget()
        dash_layout = QVBoxLayout(self.dashboard)
        dash_layout.setContentsMargins(40, 40, 40, 40)
        dash_layout.setAlignment(Qt.AlignCenter)
        
        lbl_welcome = QLabel("Welcome to the Studio")
        lbl_welcome.setStyleSheet("color: white; font-size: 32px; font-weight: bold; margin-bottom: 40px;")
        lbl_welcome.setAlignment(Qt.AlignCenter)
        dash_layout.addWidget(lbl_welcome)
        
        cards_grid = QGridLayout()
        cards_grid.setSpacing(30)
        cards_grid.setAlignment(Qt.AlignCenter)
        
        # Smart Cropper Card
        cards_grid.addWidget(self.create_tool_card(
            "✂️ Smart Cropper", 
            "Manual high-precision cropping with fixed aspect ratios and pixel-perfect controls.",
            "#ff79c6", self.switch_to_cropper
        ), 0, 0)
        
        # Placeholder for future tools
        cards_grid.addWidget(self.create_tool_card(
            "✨ Coming Soon", 
            "More specialized retouching and fashion tools are on the way.",
            "#888", lambda: None
        ), 0, 1)
        
        dash_layout.addLayout(cards_grid)
        dash_layout.addStretch()
        self.main_layout.addWidget(self.dashboard)

        # --- Cropper Tool Panel (Hidden initially) ---
        self.cropper_panel = QWidget()
        self.cropper_panel.setVisible(False)
        cropper_layout = QVBoxLayout(self.cropper_panel)
        cropper_layout.setContentsMargins(0, 0, 0, 0)
        cropper_layout.setSpacing(0)
        
        self.splitter = QSplitter(Qt.Horizontal)
        
        # Left Panel (Viewport)
        self.viewport = QFrame()
        self.viewport.setStyleSheet("background-color: #050505;")
        viewport_layout = QVBoxLayout(self.viewport)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_welcome_tool = QLabel("Drop an image here or click 'Open Image'")
        self.lbl_welcome_tool.setAlignment(Qt.AlignCenter)
        self.lbl_welcome_tool.setStyleSheet("color: #444; font-size: 18px;")
        viewport_layout.addWidget(self.lbl_welcome_tool)
        
        self.cropper = ImageCropperWidget()
        self.cropper.setVisible(False)
        self.cropper.selection_changed.connect(self.on_selection_changed)
        viewport_layout.addWidget(self.cropper)
        
        self.splitter.addWidget(self.viewport)
        
        # Right Panel (Controls)
        self.controls = QFrame()
        self.controls.setFixedWidth(300)
        self.controls.setStyleSheet("background-color: #111; border-left: 1px solid #333;")
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(20, 20, 20, 20)
        controls_layout.setSpacing(15)
        
        lbl_tools_title = QLabel("CROP SETTINGS")
        lbl_tools_title.setStyleSheet("color: #ff79c6; font-weight: bold; font-size: 11px;")
        controls_layout.addWidget(lbl_tools_title)
        
        self.crop_box = QFrame()
        self.crop_box.setStyleSheet("background: #1a1a1a; border-radius: 10px; padding: 15px; border: 1px solid #333;")
        crop_inner = QVBoxLayout(self.crop_box)
        
        # Aspect Ratio
        lbl_ratio = QLabel("Aspect Ratio (X : Y):")
        lbl_ratio.setStyleSheet("color: #eee; font-size: 12px;")
        crop_inner.addWidget(lbl_ratio)
        
        self.lbl_original_info = QLabel("Original Ratio: -")
        self.lbl_original_info.setStyleSheet("color: #888; font-size: 11px; margin-bottom: 5px;")
        crop_inner.addWidget(self.lbl_original_info)
        
        ratio_layout = QHBoxLayout()
        self.spin_ratio_x = QSpinBox()
        self.spin_ratio_x.setRange(0, 100)
        self.spin_ratio_x.setStyleSheet("background: #222; color: #ff79c6; border: 1px solid #444; padding: 5px;")
        self.spin_ratio_x.valueChanged.connect(self.update_ratio)
        
        lbl_colon = QLabel(":")
        lbl_colon.setStyleSheet("color: white; font-weight: bold;")
        
        self.spin_ratio_y = QSpinBox()
        self.spin_ratio_y.setRange(0, 100)
        self.spin_ratio_y.setStyleSheet("background: #222; color: #ff79c6; border: 1px solid #444; padding: 5px;")
        self.spin_ratio_y.valueChanged.connect(self.update_ratio)
        
        ratio_layout.addWidget(self.spin_ratio_x)
        ratio_layout.addWidget(lbl_colon)
        ratio_layout.addWidget(self.spin_ratio_y)
        crop_inner.addLayout(ratio_layout)
        
        crop_inner.addSpacing(10)
        
        # Dimensions
        lbl_pixels = QLabel("Target Size (Pixels):")
        lbl_pixels.setStyleSheet("color: #eee; font-size: 12px;")
        crop_inner.addWidget(lbl_pixels)
        
        px_layout = QHBoxLayout()
        self.px_w = QSpinBox()
        self.px_w.setRange(1, 15000)
        self.px_w.setStyleSheet("background: #222; color: white; border: 1px solid #444; padding: 5px;")
        self.px_w.valueChanged.connect(self.on_px_input_changed)
        
        lbl_x = QLabel("x")
        lbl_x.setStyleSheet("color: #888;")
        
        self.px_h = QSpinBox()
        self.px_h.setRange(1, 15000)
        self.px_h.setStyleSheet("background: #222; color: white; border: 1px solid #444; padding: 5px;")
        self.px_h.valueChanged.connect(self.on_px_input_changed)
        
        px_layout.addWidget(self.px_w)
        px_layout.addWidget(lbl_x)
        px_layout.addWidget(self.px_h)
        crop_inner.addLayout(px_layout)
        
        controls_layout.addWidget(self.crop_box)
        controls_layout.addStretch()
        
        self.btn_save = QPushButton("💾 CROP & SAVE")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.perform_crop)
        self.btn_save.setFixedHeight(50)
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.setStyleSheet("""
            QPushButton {
                background-color: #ff79c6;
                color: black;
                border-radius: 8px;
                font-size: 15px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #ff92d0; }
            QPushButton:disabled { background-color: #222; color: #555; border: 1px solid #333; }
        """)
        controls_layout.addWidget(self.btn_save)
        
        self.splitter.addWidget(self.controls)
        self.splitter.setStretchFactor(0, 1)
        cropper_layout.addWidget(self.splitter)
        
        self.main_layout.addWidget(self.cropper_panel)

    def create_tool_card(self, title, desc, color, callback):
        """Helper to create tool cards (mirrors Workshop design)."""
        card = QFrame()
        card.setFixedSize(320, 200)
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
        
        layout = QVBoxLayout(card)
        layout.setContentsMargins(25, 25, 25, 25)
        
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(f"color: {color}; font-size: 20px; font-weight: bold; border: none;")
        layout.addWidget(lbl_title)
        
        lbl_desc = QLabel(desc)
        lbl_desc.setStyleSheet("color: #888; font-size: 13px; border: none;")
        lbl_desc.setWordWrap(True)
        lbl_desc.setAlignment(Qt.AlignTop)
        layout.addWidget(lbl_desc, 1)
        
        btn = QPushButton("Open Studio")
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
        layout.addWidget(btn)
        return card

    def switch_to_dashboard(self):
        self.dashboard.setVisible(True)
        self.cropper_panel.setVisible(False)
        self.btn_back_to_dash.setVisible(False)
        self.btn_open.setVisible(False)
        self.lbl_title.setText(f"{self.icon} {self.name}")

    def switch_to_cropper(self):
        self.dashboard.setVisible(False)
        self.cropper_panel.setVisible(True)
        self.btn_back_to_dash.setVisible(True)
        self.btn_open.setVisible(True)
        self.lbl_title.setText("✂️ Smart Cropper")

    def open_image_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self.view, "Select Image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp)"
        )
        if file_path:
            self.load_image(file_path)

    def load_image(self, path):
        self.image_path = path
        pixmap = QPixmap(path)
        if pixmap.isNull(): return
            
        self.lbl_welcome_tool.setVisible(False)
        self.cropper.setVisible(True)
        self.cropper.set_image(pixmap)
        self.btn_save.setEnabled(True)
        
        self.px_w.setRange(1, pixmap.width())
        self.px_h.setRange(1, pixmap.height())
        
        # Calculate Original Ratio
        def gcd(a, b):
            while b: a, b = b, a % b
            return a
        
        common = gcd(pixmap.width(), pixmap.height())
        rx, ry = pixmap.width() // common, pixmap.height() // common
        if rx > 50 or ry > 50:
            ratio_float = pixmap.width() / pixmap.height()
            self.lbl_original_info.setText(f"Original: {pixmap.width()}x{pixmap.height()} ({ratio_float:.2f}:1)")
        else:
            self.lbl_original_info.setText(f"Original: {pixmap.width()}x{pixmap.height()} ({rx}:{ry})")
            
        self.on_selection_changed(self.cropper._selection)

    def update_ratio(self):
        rx, ry = self.spin_ratio_x.value(), self.spin_ratio_y.value()
        self.cropper.set_fixed_aspect_ratio(rx / ry if rx > 0 and ry > 0 else None)

    def on_selection_changed(self, rect_norm):
        if self._updating_inputs or not self.image_path: return
        self._updating_inputs = True
        pixmap = self.cropper.image
        self.px_w.setValue(int(rect_norm.width() * pixmap.width()))
        self.px_h.setValue(int(rect_norm.height() * pixmap.height()))
        self._updating_inputs = False

    def on_px_input_changed(self):
        if self._updating_inputs or not self.image_path: return
        self._updating_inputs = True
        pixmap = self.cropper.image
        new_w_norm = self.px_w.value() / pixmap.width()
        new_h_norm = self.px_h.value() / pixmap.height()
        
        if self.cropper._aspect_ratio:
            self.cropper._selection.setWidth(new_w_norm)
            self.cropper._enforce_ratio()
        else:
            self.cropper._selection.setWidth(new_w_norm)
            self.cropper._selection.setHeight(new_h_norm)
        self.cropper.update()
        self._updating_inputs = False

    def perform_crop(self):
        if not self.image_path: return
        
        # Save in "crop" subfolder of the original image
        original_dir = os.path.dirname(self.image_path)
        export_dir = os.path.join(original_dir, "crop")
        os.makedirs(export_dir, exist_ok=True)
        
        name, ext = os.path.splitext(os.path.basename(self.image_path))
        output_path = os.path.join(export_dir, f"{name}_crop{ext}")
        
        try:
            crop_image(self.image_path, self.cropper._selection, output_path)
            QMessageBox.information(self.view, "Success", f"Saved to:\n{output_path}")
            os.startfile(export_dir)
        except Exception as e:
            QMessageBox.critical(self.view, "Error", f"Crop failed: {str(e)}")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.accept()
        else: event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if os.path.isfile(path):
                if not self.cropper_panel.isVisible():
                    self.switch_to_cropper()
                self.load_image(path)
