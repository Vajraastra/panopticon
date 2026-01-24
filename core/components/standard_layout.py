from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QScrollArea, 
                               QFrame, QPushButton, QSplitter, QSizePolicy)
from PySide6.QtCore import Qt
from core.locale_manager import LocaleManager

class StandardToolLayout(QWidget):
    """
    Standard 3-Panel Layout:
    [ Sidebar (Fixed 320px) ] [ Workspace (Stretch) ]
    |  [ Back Button      ] | |                     |
    |  [ Settings (Scroll)] | | [ Content Area    ] |
    |  [ File Browser     ] | |                     |
    |_______________________| |_____________________|
                              | [ Bottom Bar      ] |
                              |_____________________|
    """
    def __init__(self, content_widget: QWidget, sidebar_widget: QWidget = None, bottom_widget: QWidget = None, theme_manager=None, event_bus=None):
        super().__init__()
        self.theme = theme_manager
        self.event_bus = event_bus
        
        # 1. Main Layout (Horizontal)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        # --- LEFT SIDEBAR CONTAINER ---
        self.left_container = QFrame(self)
        self.left_container.setFixedWidth(320)
        self.left_container.setObjectName("sidebar_container")
        
        # Apply Theme to Left Container
        # We rely on the stylesheet ID selector #sidebar_container for base style
        border_col = self.theme.get_color('border') if self.theme else "#333"
        pass
        
        self.left_layout = QVBoxLayout(self.left_container)
        self.left_layout.setContentsMargins(0, 0, 0, 0)
        self.left_layout.setSpacing(0)
        
        # 1a. Back Button Header
        header = QFrame()
        header.setFixedHeight(50)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(15, 0, 0, 0)
        
        self.btn_back = QPushButton(f"← {LocaleManager().tr('settings.back', 'Dashboard')}")
        self.btn_back.setCursor(Qt.PointingHandCursor)
        accent_col = self.theme.get_color('accent_main') if self.theme else "#00ffcc"
        self.btn_back.setStyleSheet(f"border: none; background: transparent; font-weight: bold; font-size: 14px; color: {accent_col}; text-align: left;")
        self.btn_back.clicked.connect(self._go_back)
        
        header_layout.addWidget(self.btn_back)
        self.left_layout.addWidget(header)
        
        # Divider
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {border_col};")
        self.left_layout.addWidget(div)
        
        # 1b. Splitter for Settings vs Future File Browser
        # Note: We currently only have 'sidebar_widget' (Settings). 
        # Future: We can accept a separate 'file_browser_widget'.
        # For now, we wrap settings in a ScrollArea.
        
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        if sidebar_widget:
            self.settings_scroll.setWidget(sidebar_widget)
        else:
            self.settings_scroll.setWidget(QWidget())
        self.settings_scroll.setFrameShape(QFrame.NoFrame)
        self.settings_scroll.setStyleSheet("background: transparent;") # Allow container bg to show
        
        self.left_layout.addWidget(self.settings_scroll, stretch=1)
        
        # Placeholder for File Browser (Bottom Left)
        # self.file_browser_frame = QFrame()
        # self.left_layout.addWidget(self.file_browser_frame, stretch=0)
        
        self.layout.addWidget(self.left_container)
        
        # --- RIGHT WORKSPACE CONTAINER ---
        self.right_container = QFrame(self)
        self.right_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Default bg for workspace to ensure visibility if content is transp
        # self.right_container.setStyleSheet("background-color: #121212;") 
        
        self.right_layout = QVBoxLayout(self.right_container)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_layout.setSpacing(0)
        
        # 2a. Content
        if content_widget:
            # Check if content needs a layout wrapper? Usually it's a widget.
            # We explicitly allow it to expand.
            content_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.right_layout.addWidget(content_widget, stretch=1)
        else:
            # Fallback for debugging
            lbl_err = QPushButton("ERROR: NO CONTENT WIDGET")
            self.right_layout.addWidget(lbl_err)
            
        # 2b. Bottom Bar
        if bottom_widget:
            self.bottom_frame = QFrame()
            # Style it clearly
            bg_panel = self.theme.get_color('bg_panel') if self.theme else "#2d2d2d"
            self.bottom_frame.setStyleSheet(f"background-color: {bg_panel}; border-top: 1px solid {border_col};")
            self.bottom_frame.setMinimumHeight(60)
            
            bf_layout = QVBoxLayout(self.bottom_frame)
            bf_layout.setContentsMargins(15, 10, 15, 10)
            bf_layout.addWidget(bottom_widget)
            
            self.right_layout.addWidget(self.bottom_frame, stretch=0)
            
        self.layout.addWidget(self.right_container, stretch=1)
        
    def _go_back(self):
        if self.event_bus:
            self.event_bus.publish("navigate", "dashboard")
        else:
            print("[WARN] Back button clicked but no EventBus connected.")

    def update_theme(self):
        # TODO: Implement dynamic theme updates if needed
        pass
