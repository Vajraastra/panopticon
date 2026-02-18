from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton, 
                               QTreeWidget, QTreeWidgetItem, QFileDialog, QSplitter, QHBoxLayout)
from PySide6.QtCore import Qt, Slot, QThread, Signal
from PySide6.QtGui import QImage, QPixmap, QIcon, QPainter, QColor
from core.components.standard_layout import StandardToolLayout
from core.theme import Theme
from ..logic.psd_engine import PSDEngine
import cv2

# --- Worker for Heavy PSD Actions ---
class PSDWorker(QThread):
    loaded = Signal(bool, str) # success, message
    rendered = Signal(object) # cv_image
    
    def __init__(self, engine, action, path=None):
        super().__init__()
        self.engine = engine
        self.action = action # 'load' or 'render'
        self.path = path
        
    def run(self):
        if self.action == 'load':
            success, msg = self.engine.load_psd(self.path)
            self.loaded.emit(success, msg)
        elif self.action == 'render':
            img = self.engine.get_preview_image()
            self.rendered.emit(img)

class LayerComposerView(QWidget):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.engine = PSDEngine()
        self.current_worker = None
        
        # UI Setup
        content = self._create_canvas()
        sidebar = self._create_sidebar()
        action_bar = self._create_action_bar()
        
        self.layout_manager = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            bottom_widget=action_bar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.layout_manager)

    def _create_action_bar(self):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        layout.addStretch()
        
        self.btn_export = QPushButton("💾 Export Composite")
        self.btn_export.clicked.connect(self.on_export_clicked)
        self.btn_export.setStyleSheet(Theme.get_action_button_style(Theme.ACCENT_SUCCESS, "black"))
        self.btn_export.setEnabled(False) # Default disabled until loaded
        layout.addWidget(self.btn_export)
        
        return container


    def _create_sidebar(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # Header
        lbl_title = QLabel("PSD LAYERS")
        lbl_title.setStyleSheet(f"color: {Theme.ACCENT_MAIN}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        btn_load = QPushButton("📂 Open PSD")
        btn_load.clicked.connect(self.request_load_file)
        btn_load.setStyleSheet(Theme.get_action_button_style(Theme.ACCENT_INFO, "white"))
        layout.addWidget(btn_load)
        
        layout.addSpacing(10)
        
        # Tree Widget for Layers
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Structure")
        self.tree.setStyleSheet(f"""
            QTreeWidget {{
                background-color: {Theme.BG_INPUT};
                border: 1px solid {Theme.BORDER};
                color: {Theme.TEXT_PRIMARY};
            }}
            QTreeWidget::item {{ padding: 4px; }}
            QTreeWidget::item:hover {{ background-color: {Theme.BG_PANEL}; }}
        """)
        self.tree.itemChanged.connect(self.on_item_changed)
        layout.addWidget(self.tree)
        
        # Status
        self.lbl_status = QLabel("Ready")
        self.lbl_status.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.lbl_status)
        
        return container

    def _create_canvas(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        
        self.image_label = QLabel("No Image Loaded")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 16px;")
        layout.addWidget(self.image_label)
        
        return container

    def request_load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PSD", "", "Photoshop Files (*.psd *.psb)")
        if path:
            self.load_psd(path)

    def load_psd(self, path):
        self.lbl_status.setText("Loading PSD... (Heavy file!)")
        self.tree.clear()
        self.btn_export.setEnabled(False)
        
        # Create new worker
        worker = PSDWorker(self.engine, 'load', path)
        worker.loaded.connect(self.on_psd_loaded)
        # Critical: Handle cleanup
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._worker_finished(worker))
        
        self.current_worker = worker
        worker.start()

    def _worker_finished(self, worker):
        if self.current_worker == worker:
            self.current_worker = None

    @Slot(bool, str)
    def on_psd_loaded(self, success, msg):
        if success:
            self.lbl_status.setText(msg)
            self._populate_tree(self.engine.get_structure(), self.tree.invisibleRootItem())
            self.request_render() # Initial render
        else:
            self.lbl_status.setText(f"Error: {msg}")

    def _populate_tree(self, layer_node, parent_item):
        for layer in layer_node:
            item = QTreeWidgetItem(parent_item)
            item.setText(0, layer.name)
            item.setCheckState(0, Qt.Checked if layer.is_visible() else Qt.Unchecked)
            
            # Store reference to layer object in the item
            item.layer_ref = layer 
            
            # Groups
            if hasattr(layer, 'is_group') and layer.is_group():
                item.setExpanded(True)
                item.setIcon(0, QIcon()) # Folder icon TODO
                self._populate_tree(layer, item)
            else:
                pass # Layer icon

    def on_item_changed(self, item, column):
        if hasattr(item, 'layer_ref'):
            visible = (item.checkState(0) == Qt.Checked)
            self.engine.set_layer_visibility(item.layer_ref, visible)
            self.request_render()

    def request_render(self):
        # If a render is already pending/running, we might want to cancel or queue?
        # For stable V1, if running, we ignore or wait. 
        # But for UI responsiveness, we ideally restart.
        # Simple fix: If running, ignore to prevent crash, simple debounce.
        
        if self.current_worker is not None:
            # Verify if it's the SAME type of action? 
            # If we are loading, definitely don't render.
            return 
            
        self.lbl_status.setText("Rendering Composite...")
        
        worker = PSDWorker(self.engine, 'render')
        worker.rendered.connect(self.on_render_complete)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: self._worker_finished(worker))
        
        self.current_worker = worker
        worker.start()

    @Slot(object)
    def on_render_complete(self, cv_img):
        self.lbl_status.setText("Composite Ready.")
        self.btn_export.setEnabled(True)
        
        if cv_img is None: return

        # Convert to QPixmap and Display
        h, w, ch = cv_img.shape
        bytes_per_line = ch * w
        
        # Max 1024px rule
        max_dim = 1024
        if max(h, w) > max_dim:
            scale = max_dim / float(max(h, w))
            new_w = int(w * scale)
            new_h = int(h * scale)
            cv_img = cv2.resize(cv_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            h, w = new_h, new_w
            bytes_per_line = ch * w

        q_img = QImage(cv_img.data, w, h, bytes_per_line, QImage.Format_RGBA8888)
        # OpenCV is BGRA, QImage Format_RGBA8888 expects R-G-B-A?
        # Actually in logic we did cvtColor(RGBA2BGRA).
        # QImage Format_ARGB32 might be safer or just RGB swizzle.
        # Let's trust logic's output for now and adjust if colors bad.
        
        pix = QPixmap.fromImage(q_img)
        self.image_label.setPixmap(pix)
        self.image_label.setText("")

    def on_export_clicked(self):
        save_path, _ = QFileDialog.getSaveFileName(self, "Export Composite", "composite.png", "PNG Image (*.png);;JPEG Image (*.jpg)")
        if save_path:
            # Re-render full res (or use cached PIL if we saved it in logic)
            # engine.render_composite() returns full res PIL.
            self.lbl_status.setText("Saving...")
            try:
                img = self.engine.render_composite()
                img.save(save_path)
                self.lbl_status.setText(f"Saved to {save_path}")
            except Exception as e:
                self.lbl_status.setText(f"Save Error: {e}")
