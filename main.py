
import sys
import os
import logging
from core.native_threads import native_threads_ready
import traceback
from types import SimpleNamespace
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                             QFrame, QGridLayout, QComboBox, QMessageBox, QScrollArea,
                             QSizePolicy, QProgressBar)
from PySide6.QtCore import Qt, QSize, QTimer, Slot
from PySide6.QtGui import QIcon, QFont
from shiboken6 import isValid

# Core Imports
from core.mod_loader import ModuleLoader
from core.module_handoff import image_set_consumers
from core.module_window import ModuleWindow
from core.workshop_window import WorkshopWindow, WORKSHOP_TOOLS
from core.theme_manager import ThemeManager
from core.locale_manager import LocaleManager
from core.event_bus import EventBus
from core.logging_config import setup_logging
from core.library_service import (get_library_service, close_library_services,
    request_close_library_services, library_services_shutdown_ready)
from core.library_startup import create_library_window

# Configuración central de logging (consola + logs/panopticon.log rotativo).
setup_logging()
log = logging.getLogger(__name__)

class MainWindow(QMainWindow):
    """
    Ventana principal de Panopticon.
    Actúa como el orquestador central, gestionando la inyección de dependencias,
    la navegación global y el cargador de módulos.
    """
    def __init__(self):
        super().__init__()
        
        # 1. Inicialización de Servicios Centrales (Core Services)
        # Estos servicios se pasan a cada módulo para permitir comunicación y consistencia funcional.
        self.locale_manager = LocaleManager()
        self.tr = self.locale_manager.tr # Atajo para traducciones
        self.theme_manager = ThemeManager()
        self.event_bus = EventBus()
        
        # Suscripción a eventos de navegación global
        self.event_bus.subscribe("navigate", self.on_navigate)
        # Live theme preview: refresh global QSS whenever the theme changes
        self.theme_manager.theme_changed.connect(
            lambda: QApplication.instance().setStyleSheet(self.theme_manager.get_stylesheet())
        )
        
        # 2. Configuración de la Ventana
        self.setWindowTitle(f"{self.tr('app.title', 'Panopticon')} - {self.tr('app.subtitle')}")
        self.resize(1280, 800) # Tamaño base razonable
        self.setStyleSheet(self.theme_manager.get_stylesheet())
        
        # 3. Contexto de Inyección de Dependencias
        # Este diccionario se entrega a los módulos cargados para que accedan a los servicios.
        self.context = {
            "theme_manager": self.theme_manager,
            "locale_manager": self.locale_manager,
            "event_bus": self.event_bus,
            "data_service": get_library_service(),
            "main_window": self
        }

        # 4. UI Initialization
        self.module_windows = {}
        self._workshop_window = None
        self._workshop_card = False
        self._producer_recovery_dialog = None
        self._shutting_down = False
        self._shutdown_finished = False
        self._shutdown_retry_scheduled = False
        self.module_grid_count = 0
        self.loaded_modules = {}
        self.root_stack = None
        self.init_ui()
        self._cherry_scan_widgets = []
        self._cherry_scan_status = ('', '', None)
        self._cherry_scan_active = False
        self._cherry_scan_error = None
        self._cherry_batch_deferred = False
        self._add_cherry_scan_status(self)
        
        # 5. Load Modules
        self.loader = ModuleLoader()
        self.load_available_modules()
        from core.cherry_ingest import CherryIngestor
        self._cherry_ingestor = CherryIngestor(self.context['data_service'], self.event_bus, self, autostart=False)
        self.context['cherry_ingestor'] = self._cherry_ingestor
        self._cherry_ingestor.scan_started.connect(self._cherry_scan_started, Qt.QueuedConnection)
        self._cherry_ingestor.scan_progress.connect(self._cherry_scan_progress, Qt.QueuedConnection)
        self._cherry_ingestor.scan_finished.connect(self._cherry_scan_finished, Qt.QueuedConnection)
        self._cherry_ingestor.batch_deferred.connect(self._cherry_batch_deferral, Qt.QueuedConnection)
        self._cherry_ingestor.failed.connect(self._cherry_ingest_failed, Qt.QueuedConnection)
        self._cherry_ingestor.start()

    def _add_cherry_scan_status(self, window):
        label = QLabel()
        label.setObjectName('cherryScanStatus')
        label.setTextFormat(Qt.PlainText)
        bar = QProgressBar()
        bar.setObjectName('cherryScanProgress')
        bar.setFixedWidth(100)
        status = window.statusBar()
        status.addPermanentWidget(label)
        status.addPermanentWidget(bar)
        self._cherry_scan_widgets.append((label, bar))
        self._paint_cherry_scan_status(label, bar)

    def _paint_cherry_scan_status(self, label, bar):
        message, detail, progress = self._cherry_scan_status
        if self._cherry_batch_deferred:
            message = self.tr('tool.cherry.ingest_batch_deferred', 'Cherry · Indexing after the batch')
            detail = self.tr('tool.cherry.ingest_batch_deferred_tip',
                'Pending folders stay in the queue and are indexed when the download batch ends.')
            progress = None
        label.setText(message)
        label.setToolTip(detail)
        label.setVisible(bool(message))
        if progress is None:
            bar.hide()
            return
        percent, tip = progress
        if percent is None:
            bar.setRange(0, 0)
            tip = self.tr('tool.cherry.ingest_scan_progress_pending_tip', 'Calculating totals for each catalog')
        else:
            bar.setRange(0, 100)
            bar.setValue(percent)
        bar.setToolTip(tip)
        bar.show()

    def _set_cherry_scan_status(self, message, detail, *, progress=None):
        self._cherry_scan_status = (message, detail, progress)
        self._refresh_cherry_scan_status()

    @Slot(bool)
    def _cherry_batch_deferral(self, deferred):
        self._cherry_batch_deferred = deferred
        self._refresh_cherry_scan_status()

    def _refresh_cherry_scan_status(self):
        for label, bar in self._cherry_scan_widgets:
            self._paint_cherry_scan_status(label, bar)

    @Slot(str)
    def _cherry_scan_started(self, folder):
        self._cherry_scan_active = True
        self._cherry_scan_error = None
        self._set_cherry_scan_status(self.tr('tool.cherry.ingest_scan_started',
            'Cherry · Scanning {folder}…').format(folder=os.path.basename(folder) or folder), folder,
            progress=(None, ''))

    @Slot(str, int, int, str, int, int)
    def _cherry_scan_progress(self, folder, accepted, conflicts, catalog, processed, total):
        scope = catalog or folder
        name = self.fontMetrics().elidedText(os.path.basename(scope) or scope, Qt.ElideRight, 180)
        message = self.tr('tool.cherry.ingest_scan_progress',
            'Cherry · Scanning {folder}… · {accepted} without conflicts · {conflicts} to review').format(
                folder=name, accepted=accepted, conflicts=conflicts)
        if total < 0:
            progress = (None, '')
        else:
            percent = 100 if total == 0 else (processed * 100) // total
            tip = self.tr('tool.cherry.ingest_scan_progress_tip',
                'Current catalog progress · {processed} of {total} entries processed').format(
                    processed=processed, total=total)
            progress = (percent, f'{tip}\n{scope}')
        self._set_cherry_scan_status(message, scope, progress=progress)

    @Slot(str, str, int, int)
    def _cherry_scan_finished(self, folder, outcome, accepted, conflicts):
        titles = {
            'complete': ('tool.cherry.ingest_scan_complete', 'Scan complete'),
            'failed': ('tool.cherry.ingest_scan_failed', 'Scan incomplete'),
            'cancelled': ('tool.cherry.ingest_scan_cancelled', 'Scan canceled'),
        }
        title = self.tr(*titles[outcome])
        message = self.tr('tool.cherry.ingest_scan_result',
            'Cherry · {title} · {accepted} without conflicts · {conflicts} to review')
        detail = f'{folder}\n{self._cherry_scan_error}' if self._cherry_scan_error else folder
        self._cherry_scan_active = False
        self._set_cherry_scan_status(message.format(title=title, accepted=accepted, conflicts=conflicts), detail)

    @Slot(str)
    def _cherry_ingest_failed(self, error):
        if self._cherry_scan_active:
            self._cherry_scan_error = error
            return
        self._set_cherry_scan_status(self.tr('tool.cherry.ingest_scan_unavailable', 'Cherry · Scan unavailable'), error)

    def init_ui(self):
        """Initialize the main application layout."""
        # Root Container (Stacked Widget for Dashboard vs Settings)
        self.root_stack = QStackedWidget()
        self.setCentralWidget(self.root_stack)
        
        # --- PAGE 0: DASHBOARD ---
        self.create_dashboard_page()
        self.root_stack.addWidget(self.dashboard_page) # Index 0
        
        # --- PAGE 1: SETTINGS ---
        self.create_settings_page()
        self.root_stack.addWidget(self.settings_page) # Index 1

    # Etapas del flujo de trabajo (UI_DESIGN.md). Clave = nombre interno del módulo.
    DASHBOARD_STAGES = (
        ("obtain",  "Obtain",  ("Cherry-DL",)),
        ("library", "Library", ("Gallery", "Librarian", "Duplicate Finder")),
        ("curate",  "Curate",  ("Pickler", "Character Recognizer")),
        ("workshop","Workshop",("Workshop", "Metadata Hub")),
        ("dataset", "Dataset", ("Dataset Tagger",)),
    )

    def create_dashboard_page(self):
        from core.components.ui_roles import section, hint, link
        from core.components.tag_widgets import FlowLayout
        tm = self.theme_manager
        self.dashboard_page = QWidget()
        self.dashboard_page.setStyleSheet(f"background-color: {tm.get_color('bg_main')};")

        layout = QVBoxLayout(self.dashboard_page)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # 1. Cabecera compacta: título a la izquierda, enlaces a la derecha.
        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(40, 24, 40, 8)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        lbl_title = QLabel(self.tr('dashboard.welcome', 'PANOPTICON'))
        lbl_title.setStyleSheet(f"color: {tm.get_color('text_primary')}; font-size: 32px; font-weight: bold; letter-spacing: 3px; background: transparent;")
        lbl_subtitle = QLabel(self.tr('app.subtitle', 'Modular Image Organizer'))
        lbl_subtitle.setStyleSheet(f"color: {tm.get_color('accent_main')}; font-size: 14px; letter-spacing: 1px; background: transparent;")
        titles.addWidget(lbl_title)
        titles.addWidget(lbl_subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch()

        btn_recovery = link(QPushButton(self.tr('dashboard.recovery_link', 'Recovery center')))
        btn_recovery.setCursor(Qt.PointingHandCursor)
        btn_recovery.setToolTip(self.tr('dashboard.recovery_tip',
            'Interrupted operations from every tool, in one place. Nothing changes without your confirmation.'))
        btn_recovery.clicked.connect(self.open_recovery_center)
        header_layout.addWidget(btn_recovery)

        btn_settings = QPushButton(f"⚙ {self.tr('settings.title', 'Settings')}")
        btn_settings.setCursor(Qt.PointingHandCursor)
        btn_settings.setToolTip(self.tr('dashboard.settings_tip', 'Language and theme.'))
        btn_settings.clicked.connect(lambda: self.root_stack.setCurrentIndex(1))
        header_layout.addWidget(btn_settings)
        layout.addWidget(header)

        # 2. Herramientas agrupadas por etapa; cada fila hace wrap con el ancho.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        self.modules_grid_widget = QWidget()
        self.modules_grid_widget.setStyleSheet("background: transparent;")
        self.modules_stages_layout = QVBoxLayout(self.modules_grid_widget)
        self.modules_stages_layout.setContentsMargins(40, 8, 40, 24)
        self.modules_stages_layout.setSpacing(6)
        self._stage_rows = {}
        for key, default, _names in self.DASHBOARD_STAGES + (("other", "Other", ()),):
            label = section(self.tr(f'dashboard.stage.{key}', default))
            row_widget = QWidget()
            row = FlowLayout(row_widget, margin=0, h_spacing=16, v_spacing=16)
            self.modules_stages_layout.addWidget(label)
            self.modules_stages_layout.addWidget(row_widget)
            label.setVisible(False)  # se muestra al recibir la primera tarjeta
            self._stage_rows[key] = (label, row)
        self.modules_stages_layout.addStretch()
        scroll.setWidget(self.modules_grid_widget)
        layout.addWidget(scroll, 1)

    def _stage_for(self, module_name):
        for key, _default, names in self.DASHBOARD_STAGES:
            if module_name in names:
                return key
        return "other"

    def create_settings_page(self):
        self.settings_page = QWidget()
        self.settings_page.setStyleSheet(f"background-color: {self.theme_manager.get_color('bg_main')};")

        outer_layout = QVBoxLayout(self.settings_page)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        layout.setSpacing(24)
        layout.setContentsMargins(60, 40, 60, 40)

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)

        # Title
        lbl = QLabel(self.tr("settings.title", "Settings"))
        lbl.setStyleSheet(f"color: {self.theme_manager.get_color('text_primary')}; font-size: 32px; font-weight: bold;")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        # ── LANGUAGE ────────────────────────────────────────────────────────────
        lbl_lang_sec = QLabel(self.tr("settings.language_section", "LANGUAGE"))
        lbl_lang_sec.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(lbl_lang_sec)

        lang_box = QFrame()
        lang_box.setStyleSheet(f"background: {self.theme_manager.get_color('bg_panel')}; border-radius: 10px;")
        lb_layout = QHBoxLayout(lang_box)
        lb_layout.setContentsMargins(20, 14, 20, 14)

        lbl_lang = QLabel(self.tr("settings.language", "Language:"))
        lbl_lang.setStyleSheet(f"color: {self.theme_manager.get_color('text_primary')}; font-size: 16px; border: none;")
        lb_layout.addWidget(lbl_lang)
        lb_layout.addStretch()

        self.combo_lang = QComboBox()
        self.combo_lang.addItems(["English", "Español"])
        self.combo_lang.setFixedSize(200, 40)
        self.combo_lang.setCurrentIndex(1 if self.locale_manager.get_locale() == "es" else 0)
        self.combo_lang.setStyleSheet(f"""
            QComboBox {{
                background-color: {self.theme_manager.get_color('bg_input')};
                color: {self.theme_manager.get_color('text_primary')};
                border: 1px solid {self.theme_manager.get_color('border')};
                border-radius: 5px; padding: 5px; font-size: 16px;
            }}
            QComboBox::drop-down {{ border: none; }}
        """)
        lb_layout.addWidget(self.combo_lang)
        layout.addWidget(lang_box)

        # ── THEME ────────────────────────────────────────────────────────────────
        lbl_theme_sec = QLabel(self.tr("settings.theme_section", "THEME"))
        lbl_theme_sec.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; font-size: 11px; font-weight: bold; letter-spacing: 2px;")
        layout.addWidget(lbl_theme_sec)

        themes_frame = QFrame()
        themes_frame.setStyleSheet(f"background: {self.theme_manager.get_color('bg_panel')}; border-radius: 10px;")
        tf_layout = QVBoxLayout(themes_frame)
        tf_layout.setContentsMargins(20, 18, 20, 18)
        tf_layout.setSpacing(12)

        self._theme_cards = {}
        self._theme_card_dots = {}
        self._pending_theme = self.theme_manager.current_theme

        all_themes = ["cyberpunk", "midnight", "forest", "slate", "light",
                      "sepia", "cosmic", "grape", "ocean", "aurora"]

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, key in enumerate(all_themes):
            card = self._make_theme_card(key)
            grid.addWidget(card, i // 5, i % 5)

        tf_layout.addLayout(grid)
        layout.addWidget(themes_frame)

        # ── SAVE & BACK ──────────────────────────────────────────────────────────
        btn_save = QPushButton(self.tr("settings.save_restart", "Save and restart"))
        btn_save.setFixedSize(250, 50)
        btn_save.setCursor(Qt.PointingHandCursor)
        btn_save.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme_manager.get_color('accent_main')};
                color: {self.theme_manager.get_color('bg_main')}; border-radius: 25px; font-weight: bold; font-size: 16px;
            }}
            QPushButton:hover {{ background-color: {self.theme_manager.get_color('text_primary')}; }}
        """)
        btn_save.clicked.connect(self.apply_settings)
        layout.addWidget(btn_save, alignment=Qt.AlignHCenter)

        btn_back = QPushButton(self.tr("settings.back", "Back to Dashboard"))
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setStyleSheet(f"color: {self.theme_manager.get_color('text_dim')}; background: transparent; text-decoration: underline; border: none;")
        btn_back.clicked.connect(lambda: self.root_stack.setCurrentIndex(0))
        layout.addWidget(btn_back, alignment=Qt.AlignHCenter)

    def _make_theme_card(self, key):
        """Build a clickable theme preview card with icon + swatches."""
        from core.theme_manager import ThemeManager as TM
        colors = TM.THEMES[key]
        name = TM.THEME_NAMES.get(key, key.capitalize())
        icon = TM.THEME_ICONS.get(key, "🎨")
        is_selected = (key == self._pending_theme)

        card = QFrame()
        card.setFixedSize(150, 110)
        card.setCursor(Qt.PointingHandCursor)
        self._theme_cards[key] = card
        self._apply_theme_card_style(card, key, is_selected)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(1, 1, 1, 1)
        card_layout.setSpacing(0)

        # ── Preview area ──
        preview = QWidget()
        preview.setStyleSheet(f"background: {colors['bg_main']}; border-radius: 6px 6px 0 0; border: none;")
        prev_layout = QVBoxLayout(preview)
        prev_layout.setContentsMargins(0, 8, 0, 6)
        prev_layout.setSpacing(6)
        prev_layout.setAlignment(Qt.AlignCenter)

        # Big icon
        lbl_icon = QLabel(icon)
        lbl_icon.setAlignment(Qt.AlignCenter)
        lbl_icon.setStyleSheet("font-size: 28px; background: transparent; border: none;")
        prev_layout.addWidget(lbl_icon)

        # Three color swatches row: bg_panel · accent_main · accent_warning
        swatches_row = QWidget()
        swatches_row.setStyleSheet("background: transparent; border: none;")
        sw_layout = QHBoxLayout(swatches_row)
        sw_layout.setContentsMargins(0, 0, 0, 0)
        sw_layout.setSpacing(6)
        sw_layout.setAlignment(Qt.AlignCenter)
        for swatch_key in ('bg_panel', 'accent_main', 'accent_warning'):
            swatch = QFrame()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(f"background: {colors[swatch_key]}; border-radius: 7px; border: none;")
            sw_layout.addWidget(swatch)
        prev_layout.addWidget(swatches_row)

        card_layout.addWidget(preview, 1)

        # ── Name bar ──
        name_bar = QWidget()
        name_bar.setFixedHeight(30)
        name_bar.setStyleSheet(f"background: {colors['bg_panel']}; border-radius: 0 0 6px 6px; border: none;")
        nb_layout = QHBoxLayout(name_bar)
        nb_layout.setContentsMargins(8, 0, 8, 0)

        lbl = QLabel(name)
        lbl.setStyleSheet(f"color: {colors['text_primary']}; font-size: 11px; font-weight: bold; background: transparent; border: none;")
        nb_layout.addWidget(lbl)
        nb_layout.addStretch()

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {colors['accent_main']}; font-size: 8px; background: transparent; border: none;")
        dot.setVisible(is_selected)
        nb_layout.addWidget(dot)
        self._theme_card_dots[key] = dot

        card_layout.addWidget(name_bar)

        def on_click(e, k=key):
            self._select_theme(k)
        card.mousePressEvent = on_click
        return card

    def _apply_theme_card_style(self, card, key, is_selected):
        from core.theme_manager import ThemeManager as TM
        colors = TM.THEMES[key]
        border_color = colors['accent_main'] if is_selected else colors['border']
        border_width = 3 if is_selected else 1
        card.setStyleSheet(f"""
            QFrame {{
                background: {colors['bg_main']};
                border: {border_width}px solid {border_color};
                border-radius: 8px;
            }}
        """)

    def _select_theme(self, key):
        # Apply and save theme, then rebuild the settings page in-place so
        # all inline styles (backgrounds, borders, labels) reflect the new colors.
        self.theme_manager.set_theme(key)
        self._rebuild_settings_page()

    def _rebuild_settings_page(self):
        """Replace the settings page widget in the stack without touching other pages."""
        old = self.root_stack.widget(1)
        self.root_stack.removeWidget(old)
        old.deleteLater()
        self.create_settings_page()
        self.root_stack.insertWidget(1, self.settings_page)
        self.root_stack.setCurrentIndex(1)

    def apply_settings(self):
        index = self.combo_lang.currentIndex()
        code = "es" if index == 1 else "en"
        self.locale_manager.set_locale(code)
        # Theme is already applied and saved via live preview (_select_theme)
        QMessageBox.information(self, self.tr("settings.restart_required", "Restart Required"),
                                self.tr("settings.restart_msg", "Please restart Panopticon to apply all changes."))

    def load_available_modules(self):
        """
        Descubre y carga dinámicamente todos los submódulos presentes en la carpeta /modules.
        Utiliza el ModuleLoader para inicializar cada componente con el contexto inyectado.
        """
        self.loaded_modules = {}
        for name in self.loader.discover_modules():
            try:
                # Se inyecta el contexto (servicios core) durante la carga
                module = self.loader.load_module(name, self.context)
                if module:
                    self.add_module_card(module)
                    self.loaded_modules[module.name] = module
            except Exception as e:
                log.error("Error loading module %s: %s\n%s", name, e, traceback.format_exc())
                
        # Configuración de conexiones entre módulos (Inter-module wiring)
        self.setup_integrations()

    def add_module_card(self, module):
        """Tarjeta de una herramienta dentro de la fila de su etapa."""
        opener = module
        if module.name in WORKSHOP_TOOLS:
            # Una sola tarjeta «Taller» para Cropper, Optimizer y Watermarker (U5).
            if self._workshop_card:
                return
            self._workshop_card = True
            module = SimpleNamespace(name="Workshop", icon="🛠️",
                description="Crop, optimize and watermark in one window that shares the image set.")
        tm = self.theme_manager
        card = QFrame()
        card.setFixedSize(230, 120)
        card.setCursor(Qt.PointingHandCursor)
        accent = getattr(module, "accent_color", tm.get_color('accent_main'))
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {tm.get_color('bg_panel')};
                border: 1px solid {tm.get_color('border')};
                border-radius: 12px;
            }}
            QFrame:hover {{
                background-color: {tm.get_color('bg_sidebar')};
                border: 2px solid {accent};
            }}
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        head = QHBoxLayout()
        lbl_icon = QLabel(str(getattr(module, "icon", "📦")))
        lbl_icon.setStyleSheet("font-size: 26px; background: transparent; border: none;")
        title_key = f"tool.{module.name.lower().replace(' ', '_')}.name"
        lbl_title = QLabel(self.tr(title_key, module.name))
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {tm.get_color('text_primary')}; background: transparent; border: none;")
        head.addWidget(lbl_icon)
        head.addWidget(lbl_title, 1)
        layout.addLayout(head)

        # Descripción completa con ajuste de línea (antes se cortaba a 57 caracteres).
        desc_key = f"tool.{module.name.lower().replace(' ', '_')}.desc"
        lbl_desc = QLabel(self.tr(desc_key, getattr(module, "description", "")))
        lbl_desc.setWordWrap(True)
        lbl_desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lbl_desc.setStyleSheet(f"font-size: 12px; color: {tm.get_color('text_secondary')}; background: transparent; border: none;")
        layout.addWidget(lbl_desc, 1)

        card.setToolTip(self.tr(
            "dashboard.open_module_tip",
            "Open this tool in its own window. Click again to return to the same session."
        ))
        card.mousePressEvent = (lambda e: self.open_workshop()) if opener is not module             else (lambda e: self.switch_to_module(module))

        # Orden dentro de la etapa = orden del flujo en DASHBOARD_STAGES, no el de descubrimiento.
        stage = self._stage_for(module.name)
        label, row = self._stage_rows[stage]
        names = next((n for k, _d, n in self.DASHBOARD_STAGES if k == stage), ())
        card.setProperty("stage_order", names.index(module.name) if module.name in names else len(names))
        cards = [row.takeAt(0).widget() for _ in range(row.count())] + [card]
        for widget in sorted(cards, key=lambda w: w.property("stage_order")):
            row.addWidget(widget)
        label.setVisible(True)
        self.module_grid_count += 1

    def switch_to_module(self, module, *args):
        if self._shutting_down:
            return
        window = self.module_windows.get(module.name)
        if module.name in WORKSHOP_TOOLS:
            if self.open_workshop(module) is None:
                return
        elif window is None:
            try:
                view = module.get_view()
                if view is None:
                    raise RuntimeError("Module returned no view")
                window = ModuleWindow(self, module, view)
                self._add_cherry_scan_status(window)
                self.module_windows[module.name] = window
            except Exception:
                log.exception("Failed to open module %s", module.name)
                QMessageBox.warning(self, module.name, self.tr(
                    "dashboard.module_open_error", "Could not open this tool. See the log for details."
                ))
                return
        if module.name not in WORKSHOP_TOOLS:
            window.show_session()
        # Pass args if supported
        if args:
            if hasattr(module, "load_image_set"):
                module.load_image_set(*args)
            elif hasattr(module, "load_images"):
                module.load_images(*args)
            elif hasattr(module, "load_custom_view"):
                module.load_custom_view(*args)

    def open_workshop(self, module=None):
        """Abre el Taller (lo crea la primera vez) y, si se indica, la pestaña de ``module``."""
        if self._shutting_down:
            return None
        window = self._workshop_window
        if window is None:
            tools = [self.loaded_modules[name] for name in WORKSHOP_TOOLS if name in self.loaded_modules]
            window = WorkshopWindow(self, tools)
            if not window.tools:
                window.deleteLater()
                QMessageBox.warning(self, self.tr('tool.workshop.name', 'Workshop'), self.tr(
                    "dashboard.module_open_error", "Could not open this tool. See the log for details."))
                return None
            self._add_cherry_scan_status(window)
            self._workshop_window = window
            for tool in window.tools:
                self.module_windows[tool.name] = window
        window.show_session(module.name if module is not None else None)
        return window

    def setup_integrations(self):
        # «Enviar a…» genérico (core/components/send_to.py): todo origen con
        # set_send_targets + request_open_module recibe todos los consumidores.
        wired = self.__dict__.setdefault('_send_wired', set())
        for source in self.loaded_modules.values():
            if not (hasattr(source, 'set_send_targets') and hasattr(source, 'request_open_module')):
                continue
            targets = image_set_consumers(self.loaded_modules, source)
            source.set_send_targets([
                (name, self.tr(f"tool.{name.lower().replace(' ', '_')}.name", name), module.icon)
                for name, module in targets.items()
            ])
            if id(source) not in wired:
                source.request_open_module.connect(self.open_image_set)
                wired.add(id(source))

        gallery = self.get_module("Gallery")
        duplicate_finder = self.get_module("Duplicate Finder")

        if duplicate_finder and gallery:
            duplicate_finder.request_open_gallery.connect(
                lambda paths, title: self.switch_to_module(gallery, paths)
            )

    def get_module(self, partial_name):
        for name, mod in self.loaded_modules.items():
            if partial_name.lower() in name.lower():
                return mod
        return None

    def open_image_set(self, name, paths):
        target = image_set_consumers(self.loaded_modules).get(name)
        if target is not None and paths:
            self.switch_to_module(target, paths)

    open_cherry_image_set = open_image_set  # nombre histórico, lo usan las pruebas

    def on_navigate(self, target):
        if target == "dashboard":
            self.root_stack.setCurrentIndex(0)
            if self.isMinimized():
                self.showNormal()
            self.show()
            self.raise_()
            self.activateWindow()

    def recovery_entries(self):
        """Entradas del core + las que declara cada módulo (sin importarlos)."""
        from core.components.recovery_center import RecoveryEntry
        entries = [RecoveryEntry(
            "producer_journals",
            self.tr('tool.producer_recovery.title', 'Recovery journals'),
            self.tr('tool.producer_recovery.open_tip',
                'Review one producer journal and explicitly reconcile verified Native evidence using the open library.'),
            self.open_producer_recovery)]
        for module in self.loaded_modules.values():
            try:
                declared = module.recovery_entries()
            except Exception:
                log.exception("recovery_entries failed for %s", module.name)
                continue
            owner = self.tr(f"tool.{module.name.lower().replace(' ', '_')}.name", module.name)
            for key, title, description, opener in declared:
                def open_entry(module=module, opener=opener):
                    self.switch_to_module(module)  # crea la vista y trae su ventana
                    if module.name in self.module_windows:
                        opener()
                entries.append(RecoveryEntry(key, title, description, open_entry, owner))
        return entries

    def open_recovery_center(self):
        if self._shutting_down:
            return
        from core.components.recovery_center import RecoveryCenterDialog
        dialog = getattr(self, '_recovery_center', None)
        if dialog is not None and isValid(dialog):
            dialog.close()
            dialog.deleteLater()
        # Se reconstruye cada vez: la lista sigue a los módulos cargados.
        self._recovery_center = RecoveryCenterDialog(self.recovery_entries(), self.tr, self)
        self._recovery_center.show()
        self._recovery_center.raise_()

    def open_producer_recovery(self):
        if self._shutting_down:
            return
        dialog = self._producer_recovery_dialog
        if dialog is not None and isValid(dialog):
            if not dialog._closed:
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            return
        from core.components.producer_recovery import ProducerRecoveryDialog
        dialog = ProducerRecoveryDialog(self.context['data_service'], self.tr, self)
        self._producer_recovery_dialog = dialog
        dialog.show()

    def closeEvent(self, event):
        """Cierre ordenado: cada módulo suelta lo suyo antes de que Qt destruya
        la ventana. Sin esto, un módulo con hilos propios (p.ej. el event loop
        asyncio de Cherry-DL) muere por terminación del proceso, no por parada
        limpia."""
        if self._shutdown_finished:
            event.accept()
            return
        if not self._shutting_down:
            for name, module in self.loaded_modules.items():
                prepare_close = getattr(module, 'prepare_close', None)
                if prepare_close is None:
                    continue
                event.ignore()
                if not prepare_close(self.close):
                    window = self.module_windows.get(name)
                    if window is not None:
                        window.show_session()
                    return
            self._shutting_down = True
            dialog = self._producer_recovery_dialog
            if dialog is not None and isValid(dialog):
                dialog.request_shutdown()
                dialog.close()
            for name, module in self.loaded_modules.items():
                try:
                    module.on_unload()
                except Exception as e:
                    log.error("Error unloading module %s: %s", name, e)
            for window in self.module_windows.values():
                window.hide()
        for module in self.loaded_modules.values():
            ready = getattr(module, 'shutdown_ready', None)
            if ready is not None and not ready():
                event.ignore()
                self.statusBar().showMessage(self.tr('app.shutdown_wait', 'Finishing accepted work before closing…'))
                if not self._shutdown_retry_scheduled:
                    self._shutdown_retry_scheduled = True
                    QTimer.singleShot(50, self._retry_close)
                return
        if not native_threads_ready():
            event.ignore()
            self.statusBar().showMessage(self.tr('app.shutdown_wait', 'Finishing accepted work before closing…'))
            if not self._shutdown_retry_scheduled:
                self._shutdown_retry_scheduled = True
                QTimer.singleShot(50, self._retry_close)
            return
        ingestor = getattr(self, '_cherry_ingestor', None)
        if ingestor is not None:
            ingestor.request_shutdown()
            if not ingestor.shutdown_ready():
                event.ignore()
                self.statusBar().showMessage(self.tr('app.shutdown_wait', 'Finishing accepted work before closing…'))
                if not self._shutdown_retry_scheduled:
                    self._shutdown_retry_scheduled = True
                    QTimer.singleShot(50, self._retry_close)
                return
        request_close_library_services(permanent=True)
        if not library_services_shutdown_ready():
            event.ignore()
            self.statusBar().showMessage(self.tr('app.shutdown_wait', 'Finishing accepted work before closing…'))
            if not self._shutdown_retry_scheduled:
                self._shutdown_retry_scheduled = True
                QTimer.singleShot(50, self._retry_close)
            return
        close_library_services(permanent=True)
        self._shutdown_finished = True
        super().closeEvent(event)

    def _retry_close(self):
        self._shutdown_retry_scheduled = False
        if not self._shutdown_finished:
            self.close()

if __name__ == "__main__":
    # Solo al ejecutar la app, no al importar `main` (las pruebas lo importan y no deben
    # tocar el crash.log real ni cambiar los manejadores de excepciones y de Qt).
    from core.crash_diagnostics import install_crash_diagnostics
    from core.paths import ProjectPaths
    install_crash_diagnostics(ProjectPaths.root() / "logs")
    # Diálogos nativos del OS (selector de archivos, etc.): enrutar al portal del
    # escritorio. Sin esto, Qt cae a su propio diálogo (feo y estilizado por la QSS
    # global). SOLO Linux: en Windows/macOS este theme no existe y fuerza el estilo
    # nativo. setdefault respeta cualquier override que el usuario ya tenga.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
    app = QApplication(sys.argv)
    app.aboutToQuit.connect(lambda: close_library_services(permanent=True))
    # Estilo Fusion en TODAS las plataformas: apariencia idéntica Linux/Windows y,
    # en Windows, evita el motor de QSS del estilo nativo, que provoca un access
    # violation al construir la página de ajustes (reproducido en PySide6 6.8/6.9/6.11).
    app.setStyle("Fusion")
    # Habilitar fallback de fuente emoji en Linux (Noto Color Emoji / Noto Emoji)
    _f = app.font()
    _f.setFamilies([_f.family(), "Noto Color Emoji", "Noto Emoji", "Segoe UI Emoji", "Apple Color Emoji"])
    app.setFont(_f)
    window = create_library_window(MainWindow, LocaleManager().tr,
        lambda title, text: QMessageBox.critical(None, title, text))
    if window is None:
        close_library_services(permanent=True)
        sys.exit(1)
    window.showMaximized() # Forzar maximizado al final para evitar re-dimensiones inesperadas
    sys.exit(app.exec())
