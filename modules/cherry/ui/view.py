"""
Vista nativa del módulo Cherry-DL — perfiles del índice (CF12 = UI nativa).

**No usa `StandardToolLayout`, y es el primer módulo que se sale** (David,
2026-09-03). Ese layout reserva el centro para una imagen y deja los controles
en una columna de 320 px; aquí el centro es una tabla de 46 filas y una consola
de descarga, y no hay nada que centrar. El resultado era una barra lateral que
pedía 1014 px de alto —scroll permanente— y 535 px de ancho comprimidos a 320.

En su lugar, tres zonas que siguen la función:

  - **Arriba**: volver, alta, los filtros —que son de la tabla, y por eso viven
    a dos centímetros de ella— y las acciones que no son de ningún proyecto.
  - **Centro, en un splitter**: la tabla a la izquierda con toda la altura, y a
    la derecha un inspector que solo habla de la fila seleccionada (sus
    fuentes, sus tipos de archivo, sus opciones y sus acciones). 573 px de alto
    en vez de 1014.
  - **Abajo**: la consola, siempre presente y plegable. Antes compartía sitio
    con la tabla en un `QStackedWidget`, así que una descarga de horas obligaba
    a elegir entre ver el progreso y usar la app.

Lo único que aportaba el layout estándar además de la disposición era el botón
"← Dashboard"; está repuesto en la barra superior, porque sin él el módulo sería
un callejón sin salida.

Todas las lecturas de `index.db` son asíncronas, así que el flujo es siempre el
mismo triángulo: la GUI encola una corrutina en `CherryRuntime`, la corrutina
lee en el hilo del loop y devuelve el resultado emitiendo una señal de esta
misma vista. Como la vista vive en el hilo GUI, Qt entrega esas señales en cola
— por eso se pueden emitir desde el loop sin tocar un solo widget desde allí.

El runtime NO arranca al construir la vista: `main.py` llama a `get_view()` de
los 13 módulos al iniciar (deuda de lazy-init anotada en TASKS), y eso dejaría
un hilo con un event loop vivo para siempre aunque nadie abra el módulo. Arranca
en el primer `showEvent`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import library_probe
from core.paths import CachePaths
from ..logic import ext_filter as ext_logic
from ..logic import profile_writes as writes
from ..logic import profiles as profiles_logic
from ..logic.download_bridge import DownloadBridge
from ..logic.download_queue import DownloadQueue
from ..logic.runtime import CherryRuntime
from .download_panel import DownloadPanel
from .duplicates_dialog import DuplicatesDialog
from .profile_dialogs import AddUrlDialog, NewProfileDialog
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

# Columnas de la tabla (índices simbólicos: nunca comparar por el texto de la
# cabecera, que está traducido).
COL_NAME, COL_SITE, COL_SOURCES, COL_CHECKED, COL_FOLDER = range(5)
SOURCE_DATA_ROLE = int(Qt.ItemDataRole.UserRole) + 1
NAME_SORT_ROLE = SOURCE_DATA_ROLE + 1


class _ProfileNameItem(QTableWidgetItem):
    """Muestra marcadores sin dejar que alteren el orden por nombre."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        left = str(self.data(NAME_SORT_ROLE) or "").casefold()
        right = str(other.data(NAME_SORT_ROLE) or "").casefold()
        return left < right


class CherryView(QWidget):
    """Lista de perfiles de cherry-dl, en vivo sobre `index.db`."""

    # Puentes loop → GUI (se emiten desde el hilo del runtime).
    _profiles_ready = Signal(object)   # list[dict]
    _detail_ready   = Signal(object)   # dict | None
    _details_ready  = Signal(object, bool, bool)  # perfiles, scan_only, batch
    _profile_created = Signal(int)          # id nuevo; se escanea sin descargar
    _url_added       = Signal(int)          # perfil al que se le añadió
    _url_removed     = Signal(int, int)     # perfil, pendientes retirados
    _filter_saved    = Signal(int, str)     # perfil, ext_filter guardado
    _profile_deleted = Signal(int)          # perfil que ya no está en el índice
    _archive_reactivated = Signal(int, int) # perfil, filas devueltas a la cola
    _archives_finished = Signal(str, object, str)
    _archives_progress = Signal(str)
    _archive_batch_profile = Signal(int, int, int, str)
    _load_failed    = Signal(str)

    def __init__(self, context: dict | None = None, runtime: CherryRuntime | None = None) -> None:
        self.context = context or {}
        self._theme = self.context.get("theme_manager")
        self._lm = self.context.get("locale_manager")
        self._event_bus = self.context.get("event_bus")

        # El runtime lo inyecta el módulo (es suyo: lo para en `on_unload`).
        self._runtime = runtime or CherryRuntime()
        self._bridge = DownloadBridge(self._runtime)
        self._queue = DownloadQueue(self._bridge)

        self._profiles: list[dict] = []
        self._selected_id: int | None = None
        self._active_profile_id: int | None = None
        # Último filtro conocido por perfil, con la edición de la UI ya
        # aplicada. La descarga lo prefiere sobre lo que traiga la lectura
        # de `index.db`: guardar es una corrutina, y entre marcar una
        # casilla y pulsar Descargar puede no haber hecho commit todavía.
        self._filter_cache: dict[int, str] = {}
        self._started = False
        self._auth_boxes: dict[int, QMessageBox] = {}
        self._archive_check_busy = False
        self._maintenance_busy = False
        self._maintenance_future = None
        self._preparing_download = False

        super().__init__()

        # Tres zonas en vez de la sidebar de 320 px del layout estándar: la
        # tabla manda, el inspector solo habla del proyecto seleccionado y la
        # consola vive abajo sin tapar nada. El porqué, en el docstring.
        self._table = self._build_table()
        self._panel = DownloadPanel(self._theme, self._lm)
        self._panel.bind(self._bridge, self._queue)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())
        root.addWidget(self._hline())

        # Splitter y no anchos fijos: 46 proyectos con nombres largos y una
        # pantalla ancha no piden el mismo reparto que 5 y un portátil.
        self._split_main = QSplitter(Qt.Horizontal)
        self._split_main.addWidget(self._table)
        self._split_main.addWidget(self._build_inspector())
        self._split_main.setStretchFactor(0, 3)
        self._split_main.setStretchFactor(1, 1)
        self._split_main.setChildrenCollapsible(False)
        # El inspector pide ~530 px por el grid de tipos y las rutas largas.
        # Con solo el stretch nacería comprimido en una ventana ancha.
        self._split_main.setSizes([1000, 540])

        self._split_console = QSplitter(Qt.Vertical)
        self._split_console.addWidget(self._split_main)
        self._split_console.addWidget(self._build_console())
        self._split_console.setStretchFactor(0, 3)
        self._split_console.setStretchFactor(1, 2)
        self._split_console.setChildrenCollapsible(False)
        # La consola nace con espacio suficiente para leer el historial, pero
        # el splitter sigue dejando que cada usuario elija el reparto.
        self._split_console.setSizes([620, 380])
        root.addWidget(self._split_console, 1)

        # Después de `super()`: antes, `self` todavía no es un QObject y no
        # puede ser padre de nada. Leer el índice tarda decenas de milisegundos,
        # así que sin este destello actualizar repinta el mismo texto y parece
        # que el botón no hiciera nada.
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(900)
        self._flash_timer.timeout.connect(self._unflash_status)

        self._profiles_ready.connect(self._on_profiles_ready)
        self._detail_ready.connect(self._on_detail_ready)
        self._details_ready.connect(self._on_details_ready)
        self._profile_created.connect(self._on_profile_created)
        self._url_added.connect(self._on_url_added)
        self._url_removed.connect(self._on_url_removed)
        self._filter_saved.connect(self._on_filter_saved)
        self._profile_deleted.connect(self._on_profile_deleted)
        self._archive_reactivated.connect(self._on_archive_reactivated)
        self._archives_finished.connect(self._on_archives_finished)
        self._archives_progress.connect(self._on_archives_progress)
        self._archive_batch_profile.connect(self._on_archive_batch_profile)
        self._load_failed.connect(self._on_load_failed)
        self._bridge.auth_required.connect(self._on_auth_required)
        self._bridge.auth_aborted.connect(self._on_auth_aborted)
        self._queue.profile_finished.connect(self._on_profile_finished)
        self._queue.active_profile_changed.connect(self._on_active_profile_changed)
        self._queue.queue_finished.connect(self._on_queue_finished)
        self._queue.queue_cancelled.connect(self._on_queue_cancelled)

        self._update_actions()

    # ── i18n / tema ───────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    # ── Construcción ──────────────────────────────────────────────────────
    def _section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {self._color('accent_main', '#00ffcc')}; "
            "font-size: 11px; font-weight: bold; letter-spacing: 1px;"
        )
        return lbl

    def _divider(self) -> QFrame:
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet(f"background-color: {self._color('border', '#333')};")
        return div

    _hline = _divider

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels([
            self.tr("cherry.col_name", "Name"),
            self.tr("cherry.col_site", "Site"),
            self.tr("cherry.col_sources", "Sources"),
            self.tr("cherry.col_checked", "Last checked"),
            self.tr("cherry.col_folder", "Folder"),
        ])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.setWordWrap(False)

        header = table.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        header.setSectionResizeMode(COL_SITE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_SOURCES, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_CHECKED, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_FOLDER, QHeaderView.Stretch)
        header.setSortIndicator(COL_NAME, Qt.AscendingOrder)
        header.setSortIndicatorShown(True)
        table.setColumnWidth(COL_NAME, 220)

        table.itemSelectionChanged.connect(self._on_selection_changed)
        table.doubleClicked.connect(self._open_folder)
        table.sortItems(COL_NAME, Qt.AscendingOrder)
        return table

    # ── Zona 1: barra superior ────────────────────────────────────────────
    def _build_topbar(self) -> QWidget:
        """Volver, alta, filtros de la tabla y acciones que no son de un proyecto.

        Los filtros viven aquí y no en un panel lateral porque son de la tabla:
        escribir en el buscador cambia lo que se ve dos centímetros más abajo.
        """
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)

        # `StandardToolLayout` traía este botón de serie; al salirse del patrón
        # hay que reponerlo o el módulo se convierte en un callejón sin salida.
        self._btn_back = QPushButton(f"← {self.tr('settings.back', 'Dashboard')}")
        self._btn_back.setToolTip(self.tr(
            "settings.back_tip",
            "Back to the dashboard. Nothing in progress is cancelled and the tool "
            "keeps its state."))
        self._btn_back.setCursor(Qt.PointingHandCursor)
        self._btn_back.setStyleSheet(
            "border: none; background: transparent; font-weight: bold; "
            f"font-size: 14px; color: {self._color('accent_main', '#00ffcc')};")
        self._btn_back.clicked.connect(self._go_back)
        lay.addWidget(self._btn_back)

        lay.addWidget(self._vline())

        self._btn_new = QPushButton(self.tr("cherry.new_profile", "+ New project"))
        self._btn_new.setToolTip(self.tr(
            "cherry.new_profile_tip",
            "Registers a project: name, target folder and its first source. "
            "Downloads nothing yet."))
        self._btn_new.clicked.connect(self._new_profile)
        lay.addWidget(self._btn_new)

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("cherry.search_ph", "Search name or folder…"))
        self._search.setToolTip(self.tr(
            "cherry.search_ph_tip",
            "Filters the list by project name or by its folder on disk. "
            "Does not search inside files."))
        self._search.setClearButtonEnabled(True)
        self._search.setMaximumWidth(280)
        self._search.textChanged.connect(self._apply_filters)
        lay.addWidget(self._search)

        self._site_combo = QComboBox()
        self._site_combo.setToolTip(self.tr(
            "cherry.site_filter_tip",
            "Shows only the projects from one site (Patreon, Pixiv…)."))
        self._site_combo.setMinimumWidth(130)
        self._site_combo.currentIndexChanged.connect(self._apply_filters)
        lay.addWidget(self._site_combo)

        self._only_missing = QPushButton(self.tr("cherry.only_missing", "Missing folders only"))
        self._only_missing.setToolTip(self.tr(
            "cherry.only_missing_tip",
            "Leaves only the projects whose folder is gone from disk — typical after "
            "moving files or unplugging a drive."))
        self._only_missing.setCheckable(True)
        self._only_missing.toggled.connect(self._apply_filters)
        lay.addWidget(self._only_missing)

        self._count_label = QLabel()
        self._count_label.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._count_label)

        lay.addStretch(1)

        self._btn_all = QPushButton()
        self._btn_all.setToolTip(self.tr(
            "cherry.download_all_tip",
            "Downloads EVERY listed project in global source passes: paid official "
            "sites first, then free official sites and repositories. It honours each "
            "project's filter and can be cancelled at any point."))
        self._btn_all.clicked.connect(self._start_all_visible)
        lay.addWidget(self._btn_all)

        self._btn_archives_all = QPushButton(self.tr(
            "cherry.archives_all", "Maintain visible"))
        self._btn_archives_all.setToolTip(self.tr(
            "cherry.archives_all_tip",
            "Runs the same organize, extract, hash-deduplicate and compact operation "
            "for every visible project, in the table order."))
        self._btn_archives_all.clicked.connect(self._process_archives_batch)
        lay.addWidget(self._btn_archives_all)

        self._btn_dups = QPushButton(self.tr("cherry.dups_open", "Find duplicate projects"))
        self._btn_dups.setToolTip(self.tr(
            "cherry.dups_open_tip",
            "Looks for projects pointing at the same source or with near-identical names. "
            "It only reports: it deletes and merges nothing."))
        self._btn_dups.clicked.connect(self._open_duplicates)
        lay.addWidget(self._btn_dups)

        self._btn_settings = QPushButton(self.tr("cherry.settings_open", "Settings…"))
        self._btn_settings.setToolTip(self.tr(
            "cherry.settings_open_tip",
            "Download folder, parallel workers and timeout. The network throttles "
            "deliberately stay in config.toml."))
        self._btn_settings.clicked.connect(self._open_settings)
        lay.addWidget(self._btn_settings)

        self._btn_refresh = QPushButton(self.tr("cherry.refresh", "Refresh"))
        self._btn_refresh.setToolTip(self.tr(
            "cherry.refresh_tip",
            "Re-reads the index from disk. Useful if another program touched the projects."))
        self._btn_refresh.clicked.connect(self.refresh)
        lay.addWidget(self._btn_refresh)

        return bar

    # ── Zona 2: inspector del proyecto seleccionado ───────────────────────
    def _build_inspector(self) -> QWidget:
        """Todo lo que pertenece a la fila seleccionada, y nada más.

        Va dentro de un scroll porque los tipos de archivo y las fuentes crecen,
        pero ya no arrastra los filtros ni el mantenimiento: eso era lo que
        empujaba la barra lateral a 1014 px de alto.
        """
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        lay.addWidget(self._section_label(self.tr("cherry.detail", "SELECTED PROFILE")))
        self._detail_label = QLabel(self.tr("cherry.detail_empty", "No profile selected."))
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail_label.setStyleSheet(
            f"color: {self._color('text_secondary', '#ccc')}; font-size: 11px;")
        lay.addWidget(self._detail_label)

        # Las fuentes van en una lista con casillas, no en el HTML del detalle:
        # desmarcar una es la forma que da el motor de "quitarla" sin perder su
        # `last_synced`.
        self._sources = QListWidget()
        self._sources.setMaximumHeight(140)
        self._sources.itemChanged.connect(self._on_source_toggled)
        self._sources.currentItemChanged.connect(lambda *_: self._update_actions())
        lay.addWidget(self._sources)

        source_actions = QHBoxLayout()
        self._btn_add_url = QPushButton(self.tr("cherry.add_url", "+ Add source"))
        self._btn_add_url.setToolTip(self.tr(
            "cherry.add_url_tip",
            "Adds another source URL to the selected project. One project can gather "
            "several sources into a single folder."))
        self._btn_add_url.clicked.connect(self._add_url)
        source_actions.addWidget(self._btn_add_url)

        self._btn_remove_url = QPushButton(self.tr(
            "cherry.remove_url", "Remove selected source",
        ))
        self._btn_remove_url.setToolTip(self.tr(
            "cherry.remove_url_tip",
            "Removes the selected source and its queued downloads. Files already "
            "downloaded remain in the project.",
        ))
        self._btn_remove_url.clicked.connect(self._remove_url)
        source_actions.addWidget(self._btn_remove_url)
        lay.addLayout(source_actions)

        lay.addWidget(self._divider())
        lay.addWidget(self._build_ext_filter())

        lay.addWidget(self._divider())
        lay.addWidget(self._section_label(self.tr("cherry.dl_options", "DOWNLOAD OPTIONS")))

        workers_row = QHBoxLayout()
        workers_row.addWidget(QLabel(self.tr("cherry.dl_workers_label", "Workers")))
        self._workers = QSpinBox()
        # El tope real lo pone el template de cada sitio: el motor recorta este
        # número y avisa con `workers_resolved`. Aquí solo se pide.
        self._workers.setRange(1, 16)
        self._workers.setValue(4)
        self._workers.setToolTip(self.tr(
            "cherry.dl_workers_tip",
            "How many files download at once. Each site has its own cap and the engine "
            "trims this number if you go over."))
        workers_row.addWidget(self._workers)
        workers_row.addStretch(1)
        lay.addLayout(workers_row)

        self._force_full = QCheckBox(self.tr("cherry.dl_rescan", "Full rescan (ignore last sync)"))
        self._force_full.setToolTip(self.tr(
            "cherry.dl_rescan_tip",
            "Goes through the whole project instead of resuming from the last sync. Much "
            "slower; use it when you suspect something old is missing."))
        lay.addWidget(self._force_full)

        lay.addSpacing(4)
        actions = QGridLayout()
        actions.setSpacing(6)
        self._btn_scan = QPushButton(self.tr("cherry.scan", "Scan"))
        self._btn_scan.setToolTip(self.tr(
            "cherry.scan_tip", "Look for new files and queue them, without downloading."))
        self._btn_scan.clicked.connect(lambda: self._start_selected(scan_only=True))
        actions.addWidget(self._btn_scan, 0, 0)

        self._btn_download = QPushButton(self.tr("cherry.download", "Download"))
        self._btn_download.setToolTip(self.tr(
            "cherry.download_tip",
            "Downloads the new files of the selected project. When it finishes it tells "
            "the library to index them."))
        self._btn_download.clicked.connect(lambda: self._start_selected(scan_only=False))
        actions.addWidget(self._btn_download, 0, 1)

        self._btn_check_archive = QPushButton()
        self._btn_check_archive.setToolTip(self.tr(
            "cherry.archive_check_tip",
            "Rechecks up to 25 Pawchive originals that its post page says have not "
            "been archived yet. They stay outside the active queue meanwhile.",
        ))
        self._btn_check_archive.clicked.connect(self._check_awaiting_archive)
        actions.addWidget(self._btn_check_archive, 1, 0, 1, 2)

        self._btn_open = QPushButton(self.tr("cherry.open_folder", "Open folder"))
        self._btn_open.setToolTip(self.tr(
            "cherry.open_folder_tip",
            "Opens the project's download folder in the file explorer."))
        self._btn_open.clicked.connect(self._open_folder)
        actions.addWidget(self._btn_open, 2, 0)

        self._btn_delete = QPushButton(self.tr("cherry.delete_profile", "Remove project"))
        self._btn_delete.setToolTip(self.tr(
            "cherry.delete_profile_tip",
            "Removes it from the index. The downloaded folder is left untouched.",
        ))
        self._btn_delete.clicked.connect(self._delete_profile)
        actions.addWidget(self._btn_delete, 2, 1)
        self._btn_archives = QPushButton(self.tr(
            "cherry.archives_process", "Organize and extract ZIP/RAR"))
        self._btn_archives.setToolTip(self.tr(
            "cherry.archives_process_tip",
            "Only this project: organizes files by type, extracts ZIP/RAR from Comprimidos, "
            "keeps one copy per hash and deletes verified archives while retaining their "
            "download history. Unavailable during downloads."))
        self._btn_archives.clicked.connect(self._process_archives)
        actions.addWidget(self._btn_archives, 3, 0, 1, 2)
        lay.addLayout(actions)

        lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(300)
        return scroll

    # ── Zona 3: consola de descarga, siempre visible ──────────────────────
    def _build_console(self) -> QWidget:
        """La consola deja de tapar la tabla: vive abajo y se pliega.

        Antes compartía sitio con la lista en un `QStackedWidget`, así que una
        descarga de horas obligaba a elegir entre ver el progreso y usar la app.
        Plegada ocupa una línea; el estado sigue leyéndose en la cabecera.
        """
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QWidget()
        head_lay = QHBoxLayout(head)
        head_lay.setContentsMargins(12, 6, 12, 6)
        head_lay.setSpacing(8)

        self._btn_console = QPushButton("▼")
        self._btn_console.setCheckable(True)
        self._btn_console.setChecked(True)
        self._btn_console.setFixedWidth(28)
        self._btn_console.setToolTip(self.tr("cherry.console_toggle", "Collapse the console"))
        self._btn_console.toggled.connect(self._toggle_console)
        head_lay.addWidget(self._btn_console)

        self._status_label = QLabel(self.tr("cherry.status_loading", "Reading index…"))
        self._status_dim = self._color("text_dim", "#888")
        self._status_label.setStyleSheet(f"color: {self._status_dim}; font-size: 12px;")
        head_lay.addWidget(self._status_label)
        head_lay.addStretch(1)

        self._btn_cancel = QPushButton(self.tr("cherry.cancel", "Cancel"))
        self._btn_cancel.setToolTip(self.tr(
            "cherry.cancel_tip",
            "Stops the running download and the rest of the queue. Whatever already "
            "downloaded is kept."))
        self._btn_cancel.clicked.connect(self._cancel)
        head_lay.addWidget(self._btn_cancel)

        lay.addWidget(head)
        lay.addWidget(self._panel, 1)
        return box

    def _toggle_console(self, expanded: bool) -> None:
        self._panel.setVisible(expanded)
        self._btn_console.setText("▼" if expanded else "▲")
        self._btn_console.setToolTip(
            self.tr("cherry.console_toggle", "Collapse the console") if expanded
            else self.tr("cherry.console_expand", "Expand the console"))

    def _go_back(self) -> None:
        if self._event_bus:
            self._event_bus.publish("navigate", "dashboard")
        else:
            log.warning("cherry: botón Atrás sin EventBus")

    def _vline(self) -> QFrame:
        line = QFrame()
        line.setFixedWidth(1)
        line.setStyleSheet(f"background-color: {self._color('border', '#333')};")
        return line

    def _build_ext_filter(self) -> QWidget:
        """Casillas de tipos + campo libre, del perfil seleccionado.

        Los grupos y sus extensiones los define el motor (`EXT_GROUPS`); aquí
        solo se traduce la etiqueta. Sin nada marcado y con el campo vacío el
        motor entiende "todo", que es lo que espera quien nunca abre esto.
        """
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(self._section_label(self.tr("cherry.ext_title", "FILE TYPES")))
        self._ext_hint = QLabel(self.tr(
            "cherry.ext_hint_none", "Select a project to choose its file types."))
        self._ext_hint.setWordWrap(True)
        self._ext_hint.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._ext_hint)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setSpacing(4)
        self._ext_checks: dict[str, QCheckBox] = {}
        for i, (gid, label, exts) in enumerate(ext_logic.groups()):
            key, default = ext_logic.GROUP_KEYS.get(gid, ("", label))
            chk = QCheckBox(self.tr(key, default) if key else label)
            chk.setToolTip(", ".join(f".{e}" for e in exts))
            chk.toggled.connect(self._on_ext_changed)
            self._ext_checks[gid] = chk
            grid.addWidget(chk, i // 2, i % 2)
        lay.addLayout(grid)

        self._ext_custom = QLineEdit()
        self._ext_custom.setPlaceholderText(self.tr("cherry.ext_custom_ph", "psd, clip…"))
        self._ext_custom.setToolTip(self.tr(
            "cherry.ext_custom_tip",
            "Extra extensions on top of the boxes above, comma separated.",
        ))
        # `editingFinished` y no `textChanged`: cada tecla sería una escritura
        # en `index.db` (que además re-escribe el .cherry-meta.json del perfil).
        self._ext_custom.editingFinished.connect(self._on_ext_changed)
        lay.addWidget(self._ext_custom)

        self._btn_ext_all = QPushButton(
            self.tr("cherry.ext_apply_all", "Apply to all listed"))
        self._btn_ext_all.setToolTip(self.tr(
            "cherry.ext_apply_all_tip",
            "Copy these types to every project currently listed.",
        ))
        self._btn_ext_all.clicked.connect(self._apply_ext_to_visible)
        lay.addWidget(self._btn_ext_all)

        self._set_ext_enabled(False)
        return box

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            self.refresh()

    def shutdown(self) -> None:
        """La llama el módulo al descargarse: para el loop y su hilo."""
        if self._maintenance_future is not None:
            self._maintenance_future.cancel()
        if self._queue.is_running:
            self._queue.cancel()
        self._runtime.stop()

    # ── Carga de datos ────────────────────────────────────────────────────
    def refresh(self) -> None:
        if not profiles_logic.index_exists():
            self._profiles = []
            self._rebuild_site_combo()
            self._apply_filters()
            self._set_status(self.tr(
                "cherry.no_index",
                "No index found at ~/.cherry-dl/index.db — create a project with New project.",
            ))
            return

        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return

        self._busy_refresh(True)
        self._set_status(self.tr("cherry.status_loading", "Reading index…"))
        try:
            self._runtime.submit(self._load_profiles())
        except RuntimeError as exc:
            self._busy_refresh(False)
            self._load_failed.emit(str(exc))

    async def _load_profiles(self) -> None:
        try:
            rows = await profiles_logic.load_profiles()
        except Exception as exc:
            log.exception("cherry: fallo leyendo index.db")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._profiles_ready.emit(rows)

    async def _load_detail(self, profile_id: int) -> None:
        try:
            detail = await profiles_logic.load_profile_detail(profile_id)
        except Exception as exc:
            log.exception("cherry: fallo leyendo el perfil %s", profile_id)
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._detail_ready.emit(detail)

    # ── Slots (hilo GUI) ──────────────────────────────────────────────────
    def _on_profiles_ready(self, rows: list) -> None:
        self._profiles = rows
        self._busy_refresh(False)
        self._rebuild_site_combo()
        self._apply_filters()
        # La hora es la prueba de que la lectura ocurrió: si el índice no
        # cambió, el resto del texto sería idéntico al de hace un segundo.
        stamp = datetime.now().strftime("%H:%M:%S")
        missing = sum(1 for r in rows if not r.get("folder_exists"))
        if missing:
            msg = self.tr("cherry.status_loaded_missing",
                          "{n} profiles · {m} with a missing folder · {t}")
            self._set_status(msg.format(n=len(rows), m=missing, t=stamp), flash=True)
        else:
            msg = self.tr("cherry.status_loaded", "{n} profiles · {t}")
            self._set_status(msg.format(n=len(rows), t=stamp), flash=True)

    def _on_detail_ready(self, detail) -> None:
        if detail is None:
            self._detail_label.setText(self.tr("cherry.detail_gone", "Profile no longer in the index."))
            self._sources.clear()
            self._clear_ext_filter()
            return
        if detail["id"] != self._selected_id:
            return  # llegó tarde: la selección ya cambió

        lines = [f"<b>{detail['display_name']}</b>"]
        folder = detail["folder_path"]
        if not detail.get("folder_exists"):
            warn = self._color("accent_warning", "#ff5555")
            lines.append(f"<span style='color:{warn}'>⚠ {folder}</span>")
        else:
            lines.append(folder)
        created = detail.get("created_at") or "—"
        lines.append(self.tr("cherry.detail_created", "Created: {d}").format(d=created))

        urls = detail.get("urls", [])
        waiting = int(detail.get("awaiting_archive_count", 0))
        if waiting:
            warn = self._color("accent_warning", "#ffb347")
            lines.append(self.tr(
                "cherry.archive_detail",
                "<span style='color:{color}'>◷ Pawchive: {n} originals announced "
                "but not archived yet</span>",
            ).format(color=warn, n=waiting))
        profile = self._current_profile()
        if profile is not None:
            profile["awaiting_archive_count"] = waiting
        lines.append(self.tr("cherry.detail_sources", "Sources ({n}):").format(n=len(urls)))
        self._detail_label.setText("<br>".join(lines))
        self._populate_sources(urls)
        # `list_profiles` no trae `ext_filter` — solo `get_profile`. Por eso el
        # bloque de tipos se puebla aquí y no al pintar la tabla.
        stored = detail.get("ext_filter", "")
        self._filter_cache[detail["id"]] = stored
        self._load_ext_filter(stored)

    def _on_load_failed(self, message: str) -> None:
        self._preparing_download = False
        self._busy_refresh(False)
        self._update_actions()
        self._set_status(
            self.tr("cherry.status_error", "Error: {msg}").format(msg=message), flash=True)

    # ── Tabla y filtros ───────────────────────────────────────────────────
    def _rebuild_site_combo(self) -> None:
        sites = sorted({p["primary_site"] for p in self._profiles})
        previous = self._site_combo.currentData()
        self._site_combo.blockSignals(True)
        self._site_combo.clear()
        # El item "todos" no lleva dato: así ningún sitio real puede chocar
        # con un centinela.
        self._site_combo.addItem(self.tr("cherry.all_sites", "All sites"), None)
        for site in sites:
            self._site_combo.addItem(site, site)
        idx = self._site_combo.findData(previous) if previous else 0
        self._site_combo.setCurrentIndex(max(idx, 0))
        self._site_combo.blockSignals(False)

    def _visible_profiles(self) -> list[dict]:
        needle = self._search.text().strip().lower()
        site = self._site_combo.currentData()
        only_missing = self._only_missing.isChecked()
        result = []
        for p in self._profiles:
            if only_missing and p.get("folder_exists"):
                continue
            if site and p["primary_site"] != site:
                continue
            if needle and needle not in p["display_name"].lower() \
                    and needle not in p["folder_path"].lower():
                continue
            result.append(p)
        return result

    def _apply_filters(self) -> None:
        rows = self._visible_profiles()
        table = self._table
        # El orden se restaura al final: con el sorting activo, insertar filas
        # las reordena a medias y los índices dejan de corresponder.
        table.setSortingEnabled(False)
        table.clearContents()
        table.setRowCount(len(rows))
        warn = self._color("accent_warning", "#ff5555")

        for row, p in enumerate(rows):
            waiting = int(p.get("awaiting_archive_count", 0))
            name = _ProfileNameItem(
                f"◷ {p['display_name']} ({waiting})" if waiting else p["display_name"]
            )
            name.setData(Qt.UserRole, p["id"])
            name.setData(NAME_SORT_ROLE, p["display_name"])
            if waiting:
                name.setForeground(QBrush(QColor(warn)))
                name.setToolTip(self.tr(
                    "cherry.archive_marker_tip",
                    "{n} Pawchive originals are announced but not archived yet.",
                ).format(n=waiting))
            table.setItem(row, COL_NAME, name)
            table.setItem(row, COL_SITE, QTableWidgetItem(p["primary_site"]))

            sources = QTableWidgetItem()
            # EditRole numérico: si no, "10 fuentes" ordena antes que "9".
            sources.setData(Qt.EditRole, int(p["url_count"]))
            sources.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, COL_SOURCES, sources)

            # ISO recortado a minutos: se lee bien y ordena como texto.
            checked = (p["last_checked"] or "")[:16]
            table.setItem(row, COL_CHECKED, QTableWidgetItem(
                checked or self.tr("cherry.never", "never")))

            folder = QTableWidgetItem(p["folder_path"])
            if not p.get("folder_exists"):
                folder.setText("⚠ " + p["folder_path"])
                folder.setForeground(QBrush(QColor(warn)))
                folder.setToolTip(self.tr("cherry.folder_missing", "Folder not found on disk"))
            table.setItem(row, COL_FOLDER, folder)

        table.setSortingEnabled(True)
        self._apply_active_profile_style()
        shown = self.tr("cherry.showing", "Showing {n} of {total}")
        self._count_label.setText(shown.format(n=len(rows), total=len(self._profiles)))
        self._btn_all.setText(
            self.tr("cherry.download_all", "Download all visible ({n})").format(n=len(rows)))
        self._restore_selection()
        self._update_actions()

    def _restore_selection(self) -> None:
        """Vuelve a marcar el perfil seleccionado si sigue visible tras filtrar."""
        if self._selected_id is None:
            return
        for row in range(self._table.rowCount()):
            item = self._table.item(row, COL_NAME)
            if item is not None and item.data(Qt.UserRole) == self._selected_id:
                self._table.selectRow(row)
                return
        self._selected_id = None
        self._detail_label.setText(self.tr("cherry.detail_empty", "No profile selected."))
        self._sources.clear()

    def _current_profile(self) -> dict | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, COL_NAME)
        if item is None:
            return None
        pid = item.data(Qt.UserRole)
        return next((p for p in self._profiles if p["id"] == pid), None)

    def _profiles_in_table_order(self) -> list[dict]:
        """Perfiles visibles en el mismo orden que el usuario ve en la tabla."""
        by_id = {p["id"]: p for p in self._profiles}
        ordered = []
        for row in range(self._table.rowCount()):
            item = self._table.item(row, COL_NAME)
            if item is not None and item.data(Qt.UserRole) in by_id:
                ordered.append(by_id[item.data(Qt.UserRole)])
        return ordered

    def _on_active_profile_changed(self, profile_id: object) -> None:
        self._active_profile_id = int(profile_id) if profile_id is not None else None
        self._apply_active_profile_style()

    def _apply_active_profile_style(self) -> None:
        """Resalta el trabajo actual sin introducir colores ajenos al tema."""
        active = QColor(self._color("accent_main", "#00ffcc"))
        active.setAlpha(45)
        active_brush = QBrush(active)
        empty_brush = QBrush()
        for row in range(self._table.rowCount()):
            name = self._table.item(row, COL_NAME)
            if name is None:
                continue
            is_active = name.data(Qt.UserRole) == self._active_profile_id
            for column in range(self._table.columnCount()):
                item = self._table.item(row, column)
                if item is not None:
                    item.setBackground(active_brush if is_active else empty_brush)
            font = name.font()
            font.setBold(is_active)
            name.setFont(font)

    def _on_selection_changed(self) -> None:
        profile = self._current_profile()
        self._update_actions()
        if profile is None:
            # Sin selección no hay filtro que editar: dejarlo pintado sería
            # enseñar el del perfil anterior y guardarlo sobre el siguiente.
            self._clear_ext_filter()
            return
        if profile["id"] == self._selected_id:
            return
        self._selected_id = profile["id"]
        self._detail_label.setText(self.tr("cherry.detail_loading", "Loading…"))
        # Lo que haya en cache se pinta ya; el detalle lo confirmará al llegar.
        self._load_ext_filter(self._filter_cache.get(profile["id"], ""))
        if self._runtime.is_running:
            try:
                self._runtime.submit(self._load_detail(profile["id"]))
            except RuntimeError as exc:
                self._load_failed.emit(str(exc))

    def _update_actions(self) -> None:
        profile = self._current_profile()
        running = (self._queue.is_running or self._bridge.is_busy
                   or self._maintenance_busy or self._preparing_download)
        self._btn_archives.setEnabled(
            bool(profile and profile.get("folder_exists")) and not running
            and not self._archive_check_busy)
        self._btn_dups.setEnabled(not running)
        self._btn_open.setEnabled(bool(profile and profile.get("folder_exists")))
        self._btn_scan.setEnabled(bool(profile) and not running)
        self._btn_download.setEnabled(bool(profile) and not running)
        waiting = int(profile.get("awaiting_archive_count", 0)) if profile else 0
        sample = min(25, waiting)
        self._btn_check_archive.setText(self.tr(
            "cherry.archive_check", "Check Pawchive waiting ({sample} of {total})",
        ).format(sample=sample, total=waiting))
        self._btn_check_archive.setEnabled(
            bool(profile and waiting) and not running and not self._archive_check_busy
        )
        self._btn_all.setEnabled(bool(self._profiles) and not running)
        self._btn_archives_all.setEnabled(bool(self._profiles) and not running)
        self._btn_cancel.setEnabled(
            self._queue.is_running or self._bridge.is_busy or self._maintenance_busy)
        self._btn_new.setEnabled(not running)
        self._btn_delete.setEnabled(bool(profile) and not running)
        self._btn_add_url.setEnabled(bool(profile) and not running)
        self._btn_remove_url.setEnabled(
            bool(profile and self._sources.currentItem()) and not running)
        # Durante una descarga el filtro se congela: el motor ya lo leyó al
        # arrancar y editarlo a media sync solo mentiría sobre lo que baja.
        self._set_ext_enabled(bool(profile) and not running)

    # ── Altas en el índice ────────────────────────────────────────────────
    def _populate_sources(self, urls: list) -> None:
        """Rellena la lista sin que `itemChanged` la confunda con un clic."""
        self._sources.blockSignals(True)
        self._sources.clear()
        for u in urls:
            item = QListWidgetItem(u["url"] or f"{u['site']}:{u['artist_id'] or '?'}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if u["enabled"] else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, u["id"])
            item.setData(SOURCE_DATA_ROLE, dict(u))
            tip = self.tr(
                "cherry.source_tip", "{n} files · last sync {t}"
            ).format(n=u["file_count"], t=u["last_synced"] or "—")
            if u.get("auto_disabled"):
                tip += "\n" + self.tr(
                    "cherry.source_auto_disabled",
                    "Automatically disabled: {reason}. Enable it manually or run a full rescan.",
                ).format(reason=u.get("auto_disabled_reason") or "unavailable")
            item.setToolTip(tip)
            self._sources.addItem(item)
        self._sources.blockSignals(False)

    def _reload_detail(self) -> None:
        if self._selected_id is None or not self._runtime.is_running:
            return
        try:
            self._runtime.submit(self._load_detail(self._selected_id))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    def _new_profile(self) -> None:
        dialog = NewProfileDialog(self._runtime, self._theme, self._lm, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        self._set_status(self.tr("cherry.creating", "Creating the project…"))
        try:
            self._runtime.submit(self._create_async(
                dialog.display_name(), dialog.url()))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _create_async(self, name: str, url: str) -> None:
        try:
            profile_id = await writes.create_profile(name, url)
        except Exception as exc:
            log.exception("cherry: no se pudo crear el perfil")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._profile_created.emit(profile_id)

    def _on_profile_created(self, profile_id: int) -> None:
        # `refresh()` vuelve a leer el índice y `_restore_selection` deja
        # marcado el perfil nuevo, que ya es el seleccionado.
        self._selected_id = profile_id
        self._set_status(self.tr("cherry.created", "Project created."))
        self.refresh()
        self._start([profile_id], scan_only=True)

    def _add_url(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self._set_status(self.tr("cherry.no_selection", "Select a profile first."))
            return
        dialog = AddUrlDialog(
            profile["display_name"], self._runtime, self._theme, self._lm, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        try:
            self._runtime.submit(self._add_url_async(profile["id"], dialog.url()))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _add_url_async(self, profile_id: int, url: str) -> None:
        try:
            await writes.add_url(profile_id, url)
        except Exception as exc:
            log.exception("cherry: no se pudo añadir la url")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._url_added.emit(profile_id)

    def _on_url_added(self, profile_id: int) -> None:
        if profile_id < 0:
            # Vino de activar/desactivar una fuente: la casilla ya refleja el
            # cambio, pero `url_count` de la tabla cuenta solo las activas.
            self._set_status(self.tr("cherry.source_updated", "Source updated."))
            self.refresh()
            return
        self._set_status(self.tr("cherry.url_added", "Source added."))
        self.refresh()      # cambió url_count
        self._reload_detail()

    def _on_source_toggled(self, item: QListWidgetItem) -> None:
        url_id = item.data(Qt.ItemDataRole.UserRole)
        if url_id is None or not self._runtime.start():
            return
        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            self._runtime.submit(self._set_enabled_async(int(url_id), enabled))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _set_enabled_async(self, url_id: int, enabled: bool) -> None:
        try:
            await writes.set_url_enabled(url_id, enabled)
        except Exception as exc:
            log.exception("cherry: no se pudo cambiar el estado de la fuente")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            # La casilla ya se pintó marcada: hay que devolverla a lo que dice
            # el índice, no a lo que el usuario creyó.
            if self._selected_id is not None:
                await self._load_detail(self._selected_id)
            return
        self._url_added.emit(-1)  # solo para refrescar la tabla y el estado

    def _remove_url(self) -> None:
        profile = self._current_profile()
        item = self._sources.currentItem()
        if profile is None or item is None:
            self._set_status(self.tr(
                "cherry.no_source_selection", "Select a source first."))
            return
        source = item.data(SOURCE_DATA_ROLE) or {}
        url_id = item.data(Qt.ItemDataRole.UserRole)
        if url_id is None:
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("cherry.remove_url_title", "Remove source"))
        box.setText(self.tr(
            "cherry.remove_url_body", "Remove this source from “{name}”?",
        ).format(name=profile["display_name"]))
        box.setInformativeText(self.tr(
            "cherry.remove_url_detail",
            "{url}\n\nIts queued downloads and sync state will be removed. "
            "Files already downloaded and catalogued stay untouched.",
        ).format(url=source.get("url") or item.text()))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if not self._runtime.start():
            self._set_status(self.tr(
                "cherry.runtime_failed", "Could not start the async runtime."))
            return
        self._set_status(self.tr(
            "cherry.removing_source", "Removing source…"))
        try:
            self._runtime.submit(self._remove_url_async(
                profile["id"], Path(profile["folder_path"]), int(url_id),
            ))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _remove_url_async(
        self, profile_id: int, folder: Path, url_id: int,
    ) -> None:
        try:
            pending = await writes.remove_url(profile_id, folder, url_id)
        except Exception as exc:
            log.exception("cherry: no se pudo borrar la fuente")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._url_removed.emit(profile_id, pending)

    def _on_url_removed(self, profile_id: int, pending: int) -> None:
        self._set_status(self.tr(
            "cherry.source_removed",
            "Source removed · {n} queued downloads discarded.",
        ).format(n=pending), flash=True)
        self.refresh()
        if self._selected_id == profile_id:
            self._reload_detail()

    # ── Borrar, ajustes y duplicados (A5) ─────────────────────────────────
    def _delete_profile(self) -> None:
        """Quita el proyecto del índice. La carpeta en disco no se toca.

        Se avisa de las dos cosas que importan y que no son obvias: que los
        archivos se quedan (así que esto no destruye una descarga de meses), y
        que lo que sí se pierde es el `last_synced` de cada fuente — volver a
        darlo de alta funciona, pero su primer escaneo recorrerá el historial
        entero otra vez.
        """
        profile = self._current_profile()
        if profile is None:
            self._set_status(self.tr("cherry.no_selection", "Select a profile first."))
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("cherry.delete_title", "Remove project"))
        box.setText(self.tr(
            "cherry.delete_body", "Remove “{name}” from the index?").format(
                name=profile["display_name"]))
        box.setInformativeText(self.tr(
            "cherry.delete_detail",
            "The folder and everything downloaded into it stay where they are:\n{folder}\n\n"
            "What is lost is the sync state of its sources, so adding it again would "
            "re-scan the whole history from scratch. To just stop downloading, untick "
            "its sources instead.",
        ).format(folder=profile.get("folder_path", "?")))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        try:
            self._runtime.submit(self._delete_async(profile["id"]))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _delete_async(self, profile_id: int) -> None:
        try:
            await writes.delete_profile(profile_id)
        except Exception as exc:
            log.exception("cherry: no se pudo borrar el perfil")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._profile_deleted.emit(profile_id)

    def _on_profile_deleted(self, profile_id: int) -> None:
        if self._selected_id == profile_id:
            self._selected_id = None
            self._clear_ext_filter()
            self._sources.clear()
            self._detail_label.setText(
                self.tr("cherry.detail_empty", "No profile selected."))
        self._filter_cache.pop(profile_id, None)
        self._set_status(self.tr("cherry.deleted", "Project removed."), flash=True)
        self.refresh()

    def _process_archives(self) -> None:
        profile = self._current_profile()
        if (not profile or self._maintenance_busy or self._preparing_download
                or self._queue.is_running or self._bridge.is_busy
                or self._archive_check_busy):
            return
        if not self._runtime.start():
            return
        self._maintenance_busy = True
        self._update_actions()
        self._btn_console.setChecked(True)
        folder = str(profile["folder_path"])
        self._panel.append_log(self.tr(
            "cherry.archives_start", "Organizing project: {folder}").format(folder=folder))
        try:
            self._maintenance_future = self._runtime.submit(self._archives_async(
                folder, profile.get("display_name") or "artist"))
        except Exception as exc:
            self._on_archives_finished(folder, None, str(exc))

    def _process_archives_batch(self) -> None:
        profiles = [
            profile for profile in self._profiles_in_table_order()
            if profile.get("folder_exists")
        ]
        if (not profiles or self._maintenance_busy or self._preparing_download
                or self._queue.is_running or self._bridge.is_busy
                or self._archive_check_busy):
            return
        answer = QMessageBox.question(
            self,
            self.tr("cherry.archives_batch_title", "Maintain visible projects"),
            self.tr(
                "cherry.archives_batch_body",
                "{n} visible projects will be processed in table order. ZIP/RAR files "
                "will be extracted, exact duplicates will keep the lowest number and "
                "each file counter will be compacted. Every project keeps a recovery "
                "plan and catalog backup. Continue?",
            ).format(n=len(profiles)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes or not self._runtime.start():
            return
        self._maintenance_busy = True
        self._on_active_profile_changed(profiles[0]["id"])
        self._update_actions()
        self._btn_console.setChecked(True)
        try:
            self._maintenance_future = self._runtime.submit(
                self._archives_batch_async(profiles)
            )
        except Exception as exc:
            self._on_archives_finished("", None, str(exc))

    async def _archives_batch_async(self, profiles: list[dict]) -> None:
        import asyncio
        from ..logic.archive_batch import process_profiles
        result, error = None, ""
        try:
            result = await process_profiles(
                profiles,
                on_profile=self._archive_batch_profile.emit,
                on_file=self._archives_progress.emit,
            )
        except asyncio.CancelledError:
            error = self.tr("cherry.archives_cancelled", "Cancelled. Run the action again to resume.")
        except Exception as exc:
            error = str(exc)
        finally:
            self._archives_finished.emit("", result, error)

    async def _archives_async(self, folder: str, artist: str) -> None:
        import asyncio
        from third_party.cherry_dl.archives import process_project
        result, error = None, ""
        try:
            result = await process_project(Path(folder), artist, self._archives_progress.emit)
        except asyncio.CancelledError:
            error = self.tr("cherry.archives_cancelled", "Cancelled. Run the action again to resume.")
        except Exception as exc:
            error = str(exc)
        finally:
            self._archives_finished.emit(folder, result, error)

    def _on_archives_progress(self, name: str) -> None:
        text = self.tr("cherry.archives_working", "Processing: {name}").format(name=name)
        self._panel.set_phase(text)
        self._panel.append_log(text)
        self._set_status(text)

    def _on_archive_batch_profile(
        self, position: int, total: int, profile_id: int, name: str,
    ) -> None:
        self._on_active_profile_changed(profile_id)
        text = self.tr(
            "cherry.archives_batch_progress", "Maintenance {position}/{total}: {name}",
        ).format(position=position, total=total, name=name)
        self._panel.set_phase(text)
        self._panel.append_log(text)
        self._set_status(text)

    def _on_archives_finished(self, folder: str, result, error: str) -> None:
        self._maintenance_busy = False
        self._maintenance_future = None
        self._on_active_profile_changed(None)
        if result is not None:
            text = self.tr(
                "cherry.archives_done",
                "Archives: {p} processed, {a} files added, {d} archive duplicates; "
                "maintenance: {r} exact copies removed, {c} renumbered, {u} recovered; "
                "{o} organized, {e} errors.").format(
                    p=result.processed, a=result.added, d=result.duplicates,
                    r=result.duplicates_removed, c=result.compacted,
                    u=result.registered, o=result.organized, e=len(result.errors))
            self._panel.append_log(text, "ok" if not result.errors else "warn")
            for detail in result.errors:
                self._panel.append_log(detail, "warn")
        else:
            text = error
            self._panel.append_log(error, "warn")
        self._panel.set_phase(text)
        self._set_status(text)
        for changed in getattr(result, "folders", []) if result is not None else []:
            self._announce_folder({"folder_path": changed})
        if folder:
            self._announce_folder({"folder_path": folder})
        self._update_actions()
        self._reload_detail()

    def _open_settings(self) -> None:
        """Ajustes del motor. Al guardar cambia la carpeta donde NACEN los
        proyectos nuevos, así que la vista previa de carpeta del alta ya no
        diría lo mismo; nada que refrescar en la tabla."""
        dialog = SettingsDialog(self._theme, self._lm, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._panel.refresh_pawchive_quota()
            self._set_status(self.tr("cherry.settings_saved", "Settings saved."), flash=True)

    def _open_duplicates(self) -> None:
        if self._maintenance_busy or self._preparing_download:
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        dialog = DuplicatesDialog(self._runtime, self._theme, self._lm, self)
        dialog.folders_changed.connect(self._on_merge_folders)
        dialog.exec()

    def _on_merge_folders(self, folders) -> None:
        """Una fusión movió archivos entre dos carpetas: avisar y refrescar.

        Se avisa de **las dos**: la que recibe ganó archivos y la que se vacía
        los perdió, y el Librarian tiene que enterarse de ambas cosas o se queda
        con filas apuntando a rutas que ya no existen. Se reutiliza el mismo
        filtro que la descarga —solo lo que cuelga de una carpeta vigilada—,
        porque re-indexar lo de fuera metería archivos en la biblioteca sin que
        nadie los haya dado de alta.
        """
        if self._event_bus is not None:
            for folder in folders:
                if not library_probe.is_watched(folder):
                    log.info("cherry: %s no está bajo una carpeta vigilada; no se avisa", folder)
                    continue
                self._event_bus.publish("library_changed", str(folder))
                log.info("cherry: library_changed → %s (fusión)", folder)
        # Un proyecto desapareció del índice: la tabla ya no dice la verdad.
        self.refresh()

    # ── Filtro de tipos por perfil ────────────────────────────────────────
    def _set_ext_enabled(self, on: bool) -> None:
        for chk in self._ext_checks.values():
            chk.setEnabled(on)
        self._ext_custom.setEnabled(on)
        self._btn_ext_all.setEnabled(on)

    def _load_ext_filter(self, stored: str) -> None:
        """Refleja lo guardado en las casillas SIN disparar un guardado.

        Sin bloquear las señales, repintar el filtro del perfil recién
        seleccionado lo re-escribiría en la BD en el acto — y un valor legacy
        se convertiría al formato nuevo por el mero hecho de mirarlo.
        """
        group_ids, custom = ext_logic.decode(stored)
        for gid, chk in self._ext_checks.items():
            chk.blockSignals(True)
            chk.setChecked(gid in group_ids)
            chk.blockSignals(False)
        self._ext_custom.blockSignals(True)
        self._ext_custom.setText(custom)
        self._ext_custom.blockSignals(False)
        self._set_ext_enabled(True)
        self._update_ext_hint()

    def _clear_ext_filter(self) -> None:
        """Sin perfil seleccionado no hay filtro que editar: vaciar y apagar."""
        self._load_ext_filter("")
        self._set_ext_enabled(False)
        self._ext_hint.setText(self.tr(
            "cherry.ext_hint_none", "Select a project to choose its file types."))

    def _ext_selection(self) -> tuple[list[str], str]:
        groups = [gid for gid, chk in self._ext_checks.items() if chk.isChecked()]
        return groups, self._ext_custom.text()

    def _update_ext_hint(self) -> None:
        groups, custom = self._ext_selection()
        n = len(ext_logic.effective(ext_logic.encode(groups, custom)))
        if not n:
            self._ext_hint.setText(self.tr(
                "cherry.ext_hint_all", "Nothing selected: every file type is downloaded."))
        else:
            self._ext_hint.setText(self.tr(
                "cherry.ext_hint_n", "{n} extensions will be downloaded.").format(n=n))

    def _on_ext_changed(self) -> None:
        """Una casilla o el campo extra cambiaron: se guarda en el perfil."""
        profile = self._current_profile()
        if profile is None:
            return
        groups, custom = self._ext_selection()
        # El cache se actualiza YA, en el hilo GUI: si el usuario pulsa
        # Descargar antes de que el guardado haga commit, la descarga usa lo
        # que ve en pantalla y no lo que aún hay en disco.
        self._filter_cache[profile["id"]] = ext_logic.encode(groups, custom)
        self._update_ext_hint()
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        try:
            self._runtime.submit(self._save_ext_async(profile["id"], groups, custom))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _save_ext_async(self, profile_id: int, groups: list, custom: str) -> None:
        try:
            stored = await ext_logic.save(profile_id, groups, custom)
        except Exception as exc:
            log.exception("cherry: no se pudo guardar el filtro de tipos")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._filter_saved.emit(profile_id, stored)

    def _on_filter_saved(self, profile_id: int, stored: str) -> None:
        if profile_id < 0:  # vino del lote: el cache ya se escribió al lanzarlo
            self._set_status(
                self.tr("cherry.ext_applied_all", "File types applied to the listed projects."),
                flash=True)
            return
        self._filter_cache[profile_id] = stored
        if profile_id == self._selected_id:
            self._set_status(self.tr("cherry.ext_saved", "File types saved."), flash=True)

    def _apply_ext_to_visible(self) -> None:
        """Copia el filtro actual a todos los perfiles de la lista visible.

        Existe porque el filtro es del perfil: sin esto, dejar 46 proyectos en
        "solo imágenes" sería marcar casillas 46 veces. Respeta el filtro de la
        tabla — se aplica a lo que se ve, no a todo el índice — y por eso el
        diálogo dice cuántos son.
        """
        rows = self._visible_profiles()
        if not rows:
            return
        groups, custom = self._ext_selection()
        n_ext = len(ext_logic.effective(ext_logic.encode(groups, custom)))
        body = (
            self.tr("cherry.ext_apply_all_body",
                    "{n} listed projects will download only these {e} extensions. "
                    "Their current selection is replaced.")
            if n_ext else
            self.tr("cherry.ext_apply_all_body_none",
                    "{n} listed projects will go back to downloading every file type. "
                    "Their current selection is replaced.")
        )
        reply = QMessageBox.question(
            self,
            self.tr("cherry.ext_apply_all_title", "Apply file types"),
            body.format(n=len(rows), e=n_ext),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return
        ids = [p["id"] for p in rows]
        encoded = ext_logic.encode(groups, custom)
        for pid in ids:
            self._filter_cache[pid] = encoded
        try:
            self._runtime.submit(self._save_ext_many_async(ids, groups, custom))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))

    async def _save_ext_many_async(self, ids: list, groups: list, custom: str) -> None:
        """Uno por uno: `update_profile_ext_filter` abre su propia conexión y
        además re-escribe el meta de cada carpeta. En serie tarda, pero no deja
        el índice a medias si uno falla."""
        failed = 0
        for pid in ids:
            try:
                await ext_logic.save(pid, groups, custom)
            except Exception:
                log.exception("cherry: no se pudo guardar el filtro del perfil %s", pid)
                failed += 1
        if failed:
            self._load_failed.emit(
                self.tr("cherry.ext_apply_all_failed", "{n} projects could not be saved.")
                .format(n=failed))
            return
        self._filter_saved.emit(-1, "")

    # ── Descarga (A3) ─────────────────────────────────────────────────────
    def _start_selected(self, scan_only: bool) -> None:
        profile = self._current_profile()
        if profile is None:
            self._set_status(self.tr("cherry.no_selection", "Select a profile first."))
            return
        self._start([profile["id"]], scan_only)

    def _check_awaiting_archive(self) -> None:
        profile = self._current_profile()
        if profile is None or self._archive_check_busy:
            return
        self._archive_check_busy = True
        self._update_actions()
        self._set_status(self.tr(
            "cherry.archive_preparing", "Preparing a Pawchive archive check…",
        ))
        try:
            self._runtime.submit(self._reactivate_archive_async(
                profile["id"], Path(profile["folder_path"]),
            ))
        except RuntimeError as exc:
            self._archive_check_busy = False
            self._load_failed.emit(str(exc))

    async def _reactivate_archive_async(self, profile_id: int, folder: Path) -> None:
        try:
            from third_party.cherry_dl.catalog import reactivate_awaiting_archive
            count = await reactivate_awaiting_archive(folder, limit=25)
        except Exception:
            log.exception("cherry: fallo preparando revisión de Pawchive")
            count = -1
        self._archive_reactivated.emit(profile_id, count)

    def _on_archive_reactivated(self, profile_id: int, count: int) -> None:
        self._archive_check_busy = False
        if count < 0:
            self._set_status(self.tr(
                "cherry.archive_prepare_failed",
                "Could not prepare the Pawchive archive check.",
            ), flash=True)
            self._update_actions()
            return
        if count == 0:
            self._set_status(self.tr(
                "cherry.archive_none_due", "No Pawchive originals to check.",
            ), flash=True)
            self.refresh()
            return
        profile = next((p for p in self._profiles if p["id"] == profile_id), None)
        if profile is not None:
            profile["awaiting_archive_count"] = max(
                0, int(profile.get("awaiting_archive_count", 0)) - count,
            )
        self._set_status(self.tr(
            "cherry.archive_checking", "Checking {n} Pawchive originals…",
        ).format(n=count), flash=True)
        self._start([profile_id], scan_only=False)

    def _start_all_visible(self) -> None:
        # El batch conserva el orden que muestra la tabla, incluido el elegido
        # al pulsar cualquier cabecera.
        rows = self._profiles_in_table_order()
        if not rows:
            return
        if len(rows) > 1:
            reply = QMessageBox.question(
                self,
                self.tr("cherry.batch_title", "Download several profiles"),
                self.tr(
                    "cherry.batch_body",
                    "{n} profiles will be synced in global passes: paid official sites "
                    "first, then free official sites and repositories. It can take hours "
                    "and can be cancelled at any point.",
                ).format(n=len(rows)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._start([p["id"] for p in rows], scan_only=False, batch=True)

    def _start(
        self, profile_ids: list[int], scan_only: bool, batch: bool = False,
    ) -> None:
        if (self._queue.is_running or self._bridge.is_busy
                or self._maintenance_busy or self._preparing_download):
            self._set_status(self.tr("cherry.dl_busy", "A download is already running."))
            return
        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return

        self._preparing_download = True
        self._panel.reset()
        self._panel.set_phase(self.tr("cherry.dl_preparing", "Loading profile sources…"))
        self._btn_console.setChecked(True)  # si estaba plegada, ahora interesa
        self._set_status(self.tr("cherry.dl_preparing", "Loading profile sources…"))
        try:
            self._runtime.submit(self._load_details(profile_ids, scan_only, batch))
        except RuntimeError as exc:
            self._load_failed.emit(str(exc))
        self._update_actions()

    async def _load_details(
        self, profile_ids: list[int], scan_only: bool, batch: bool,
    ) -> None:
        """`run_profile_download` exige la clave `urls`, que la tabla no trae."""
        try:
            rows = await profiles_logic.load_profile_details(profile_ids)
        except Exception as exc:
            log.exception("cherry: fallo cargando los perfiles a descargar")
            self._load_failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._details_ready.emit(rows, scan_only, batch)

    def _on_details_ready(
        self, rows: list, scan_only: bool, batch: bool,
    ) -> None:
        self._preparing_download = False
        if self._maintenance_busy:
            self._update_actions()
            return
        if not rows:
            self._set_status(self.tr("cherry.dl_nothing", "Nothing to download."))
            self._update_actions()
            return
        started = self._queue.start(
            rows,
            params_for=self._params_for_profile,
            workers=self._workers.value(),
            force_full=self._force_full.isChecked(),
            scan_only=scan_only,
            validate_sources=batch or self._force_full.isChecked(),
        )
        if not started:
            self._set_status(self.tr("cherry.dl_busy", "A download is already running."))
        else:
            self._set_status(self.tr("cherry.dl_running", "Downloading…"))
        self._update_actions()

    def _params_for_profile(self, profile: dict) -> dict:
        """Lo que cambia de un perfil a otro: hoy, su filtro de tipos.

        El cache manda sobre lo leído de `index.db` para que una casilla recién
        marcada cuente aunque su guardado todavía no haya hecho commit.
        """
        stored = self._filter_cache.get(profile["id"], profile.get("ext_filter", ""))
        return {"ext_filter": ext_logic.effective(stored)}

    def _cancel(self) -> None:
        if self._maintenance_future is not None:
            self._maintenance_future.cancel()
            self._btn_cancel.setEnabled(False)
            self._panel.set_phase(self.tr("cherry.dl_cancelling", "Cancelling…"))
            return
        if self._queue.cancel() or self._bridge.cancel():
            self._panel.set_phase(self.tr("cherry.dl_cancelling", "Cancelling…"))
            self._set_status(self.tr("cherry.dl_cancelling", "Cancelling…"))
        self._update_actions()

    def _on_profile_finished(self, profile: dict, summary) -> None:
        name = profile.get("display_name", "?")
        if summary is None:
            self._panel.append_log(
                self.tr("cherry.dl_profile_failed", "✗ {name} did not finish").format(name=name),
                "error")
            # Fallo o cancelación NO significa carpeta intacta: el motor
            # cataloga cada archivo al cerrarlo, así que lo que alcanzó a bajar
            # ya está en disco y hay que indexarlo igual.
            self._announce_folder(profile)
            return
        self._panel.append_log(self.tr(
            "cherry.dl_profile_done",
            "✓ {name} · ↓{d} ⏭{s} ✗{e} ⏸{f} ◷{a}",
        ).format(name=name, d=summary.downloaded, s=summary.skipped,
                 e=summary.errors, f=summary.deferred,
                 a=getattr(summary, "awaiting_archive", 0)), "ok")
        if summary.downloaded:
            self._announce_folder(profile)
        if (getattr(summary, "awaiting_archive", 0)
                or profile.get("awaiting_archive_count")):
            self.refresh()

    def _announce_folder(self, profile: dict) -> None:
        """Avisa por EventBus de que la carpeta del perfil tiene archivos nuevos (A4).

        El Librarian escucha `library_changed`, junta las carpetas de una
        ráfaga y lanza UN re-index dirigido a ellas — no un escaneo de las
        294K filas. Se publica por perfil y no al final del lote para que un
        lote de horas vaya indexando según avanza; el debounce del Librarian
        se encarga de que dos perfiles seguidos no sean dos escaneos.

        Solo se publica si la carpeta cuelga de una vigilada. El Librarian
        re-indexa lo que le manden **sin comprobar si le pertenece**, así que
        avisar de una carpeta de fuera metería sus archivos en la biblioteca
        sin que nadie la haya dado de alta. Hoy `G:/images/cherry-dl` sí está
        vigilada, así que el destino por defecto entra solo; un `download_dir`
        movido a otro sitio simplemente no dispara nada.
        """
        if self._event_bus is None:
            return
        folder = profile.get("folder_path")
        if not folder:
            return
        if not library_probe.is_watched(folder):
            log.info("cherry: %s no está bajo una carpeta vigilada; no se avisa", folder)
            return
        self._event_bus.publish("library_changed", str(folder))
        log.info("cherry: library_changed → %s", folder)

    def _on_queue_finished(self, totals) -> None:
        text = self.tr(
            "cherry.dl_queue_done",
            "Done · {n} profiles · ↓{d} ⏭{s} ✗{e} ⏸{f} ◷{a}"
        ).format(n=totals.done, d=totals.downloaded, s=totals.skipped,
                 e=totals.errors, f=totals.deferred,
                 a=getattr(totals, "awaiting_archive", 0))
        self._panel.set_phase(text)
        self._panel.append_log(text, "ok")
        for name, message in totals.failed:
            self._panel.append_log(f"✗ {name}: {message}", "error")
        self._update_actions()
        # `last_checked` y los contadores por fuente cambiaron: la tabla ya no
        # dice la verdad.
        self.refresh()

    def _on_queue_cancelled(self, totals) -> None:
        text = self.tr(
            "cherry.dl_queue_cancelled", "Cancelled · {n} profiles done · ↓{d}"
        ).format(n=totals.done, d=totals.downloaded)
        self._panel.set_phase(text)
        self._panel.append_log(text, "warn")
        self._update_actions()
        self.refresh()

    # ── Autenticación interactiva ─────────────────────────────────────────
    def _on_auth_required(self, site: str, token: int) -> None:
        """La descarga está detenida esperando esta respuesta (`asyncio.Future`).

        `exec()` abre un bucle de eventos anidado, así que las señales del
        puente se siguen entregando mientras el diálogo está abierto — incluida
        `auth_aborted`, que lo cierra si la descarga muere entretanto.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("cherry.auth_title", "Session required"))
        if site == "pixiv":
            body = self.tr(
                "cherry.auth_pixiv_body",
                "Pixiv needs an active session with explicit content enabled. "
                "Your browser will open the login or display settings as needed; "
                "the scan resumes by itself afterwards.\n\nContinue?",
            )
        else:
            body = self.tr(
                "cherry.auth_body",
                "{site} needs an active session. Without one, only free public posts "
                "would be downloaded.\n\nLog in now? Your real browser will open so you "
                "can sign in; the download resumes by itself afterwards.",
            ).format(site=site.capitalize())
        box.setText(body)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        box.setProperty("aborted", False)
        self._auth_boxes[token] = box

        result = box.exec()
        self._auth_boxes.pop(token, None)
        if box.property("aborted"):
            return  # la descarga ya murió; el Future no existe
        if result == QMessageBox.StandardButton.Yes:
            self._panel.set_phase(self.tr("cherry.auth_waiting", "Waiting for the browser login…"))
            self._bridge.run_site_login(site, token)
        else:
            self._bridge.resolve_auth_result(token, False)

    def _on_auth_aborted(self, token: int) -> None:
        box = self._auth_boxes.pop(token, None)
        if box is not None:
            box.setProperty("aborted", True)
            box.done(int(QMessageBox.StandardButton.Cancel))

    # ── Acciones ──────────────────────────────────────────────────────────
    def _open_folder(self) -> None:
        profile = self._current_profile()
        if profile and not CachePaths.open_folder(Path(profile["folder_path"])):
            self._set_status(self.tr("cherry.folder_missing", "Folder not found on disk"))

    def _busy_refresh(self, busy: bool) -> None:
        self._btn_refresh.setEnabled(not busy)
        self._btn_refresh.setText(
            self.tr("cherry.refreshing", "Refreshing…") if busy
            else self.tr("cherry.refresh", "Refresh"))

    def _set_status(self, text: str, flash: bool = False) -> None:
        """`flash` tiñe el estado con el acento un instante: es la única señal
        visible cuando el texto nuevo coincide con el viejo."""
        self._status_label.setText(text)
        if not flash:
            return
        accent = self._color("accent_success", "#00ff66")
        self._status_label.setStyleSheet(f"color: {accent}; font-size: 12px; font-weight: bold;")
        self._flash_timer.start()

    def _unflash_status(self) -> None:
        self._status_label.setStyleSheet(f"color: {self._status_dim}; font-size: 12px;")
