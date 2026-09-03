"""
Vista nativa del módulo Cherry-DL — perfiles del índice (A2, CF12 = UI nativa).

Reemplaza al lanzador de la GUI qasync, que sobrevive como botón transitorio
(`legacy_gui.py`) hasta que A3 traiga la vista de descarga.

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
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.components.standard_layout import StandardToolLayout
from core.paths import CachePaths
from ..logic import profiles as profiles_logic
from ..logic.runtime import CherryRuntime
from .legacy_gui import LOG_NAME, LegacyGuiLauncher

log = logging.getLogger(__name__)

# Columnas de la tabla (índices simbólicos: nunca comparar por el texto de la
# cabecera, que está traducido).
COL_NAME, COL_SITE, COL_SOURCES, COL_CHECKED, COL_FOLDER = range(5)

class CherryView(StandardToolLayout):
    """Lista de perfiles de cherry-dl, en vivo sobre `index.db`."""

    # Puentes loop → GUI (se emiten desde el hilo del runtime).
    _profiles_ready = Signal(object)   # list[dict]
    _detail_ready   = Signal(object)   # dict | None
    _load_failed    = Signal(str)

    def __init__(self, context: dict | None = None, runtime: CherryRuntime | None = None) -> None:
        self.context = context or {}
        self._theme = self.context.get("theme_manager")
        self._lm = self.context.get("locale_manager")
        self._event_bus = self.context.get("event_bus")

        # El runtime lo inyecta el módulo (es suyo: lo para en `on_unload`).
        self._runtime = runtime or CherryRuntime()
        self._legacy = LegacyGuiLauncher()

        self._profiles: list[dict] = []
        self._selected_id: int | None = None
        self._started = False

        self._table = self._build_table()
        super().__init__(
            self._table,
            sidebar_widget=self._build_sidebar(),
            bottom_widget=self._build_action_bar(),
            theme_manager=self._theme,
            event_bus=self._event_bus,
        )

        self._profiles_ready.connect(self._on_profiles_ready)
        self._detail_ready.connect(self._on_detail_ready)
        self._load_failed.connect(self._on_load_failed)
        self._legacy.state_changed.connect(self._on_legacy_state)

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

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels([
            self.tr("cherry.col_name", "Profile"),
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
        table.setColumnWidth(COL_NAME, 220)

        table.itemSelectionChanged.connect(self._on_selection_changed)
        table.doubleClicked.connect(self._open_folder)
        return table

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)

        lay.addWidget(self._section_label(self.tr("cherry.filters", "FILTERS")))

        self._search = QLineEdit()
        self._search.setPlaceholderText(self.tr("cherry.search_ph", "Search name or folder…"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filters)
        lay.addWidget(self._search)

        self._site_combo = QComboBox()
        self._site_combo.currentIndexChanged.connect(self._apply_filters)
        lay.addWidget(self._site_combo)

        self._only_missing = QPushButton(self.tr("cherry.only_missing", "Missing folders only"))
        self._only_missing.setCheckable(True)
        self._only_missing.toggled.connect(self._apply_filters)
        lay.addWidget(self._only_missing)

        self._count_label = QLabel()
        self._count_label.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._count_label)

        lay.addSpacing(6)
        lay.addWidget(self._divider())

        lay.addWidget(self._section_label(self.tr("cherry.detail", "SELECTED PROFILE")))
        self._detail_label = QLabel(self.tr("cherry.detail_empty", "No profile selected."))
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._detail_label.setStyleSheet(
            f"color: {self._color('text_secondary', '#ccc')}; font-size: 11px;")
        lay.addWidget(self._detail_label)

        lay.addStretch(1)

        lay.addWidget(self._divider())
        lay.addWidget(self._section_label(self.tr("cherry.legacy", "CLASSIC GUI")))
        self._legacy_note = QLabel(self.tr(
            "cherry.legacy_note",
            "Downloading still happens in the classic cherry-dl window; the native "
            "download view lands next. Profiles live in ~/.cherry-dl and are shared "
            "with the standalone.",
        ))
        self._legacy_note.setWordWrap(True)
        self._legacy_note.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._legacy_note)

        self._btn_legacy = QPushButton(self.tr("cherry.launch", "Launch Cherry-DL"))
        self._btn_legacy.clicked.connect(self._launch_legacy)
        lay.addWidget(self._btn_legacy)
        return panel

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(15, 10, 15, 10)
        lay.setSpacing(10)

        self._status_label = QLabel(self.tr("cherry.status_loading", "Reading index…"))
        self._status_label.setStyleSheet(
            f"color: {self._color('text_dim', '#888')}; font-size: 12px;")
        lay.addWidget(self._status_label)
        lay.addStretch(1)

        self._btn_open = QPushButton(self.tr("cherry.open_folder", "Open folder"))
        self._btn_open.clicked.connect(self._open_folder)
        lay.addWidget(self._btn_open)

        self._btn_refresh = QPushButton(self.tr("cherry.refresh", "Refresh"))
        self._btn_refresh.clicked.connect(self.refresh)
        lay.addWidget(self._btn_refresh)
        return bar

    # ── Ciclo de vida ─────────────────────────────────────────────────────
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            self.refresh()

    def shutdown(self) -> None:
        """La llama el módulo al descargarse: para el loop y su hilo."""
        self._runtime.stop()

    # ── Carga de datos ────────────────────────────────────────────────────
    def refresh(self) -> None:
        if not profiles_logic.index_exists():
            self._profiles = []
            self._rebuild_site_combo()
            self._apply_filters()
            self._set_status(self.tr(
                "cherry.no_index",
                "No index found at ~/.cherry-dl/index.db — create a profile in the classic GUI first.",
            ))
            return

        if not self._runtime.start():
            self._set_status(self.tr("cherry.runtime_failed", "Could not start the async runtime."))
            return

        self._btn_refresh.setEnabled(False)
        self._set_status(self.tr("cherry.status_loading", "Reading index…"))
        try:
            self._runtime.submit(self._load_profiles())
        except RuntimeError as exc:
            self._btn_refresh.setEnabled(True)
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
        self._btn_refresh.setEnabled(True)
        self._rebuild_site_combo()
        self._apply_filters()
        missing = sum(1 for r in rows if not r.get("folder_exists"))
        if missing:
            msg = self.tr("cherry.status_loaded_missing",
                          "{n} profiles · {m} with a missing folder")
            self._set_status(msg.format(n=len(rows), m=missing))
        else:
            msg = self.tr("cherry.status_loaded", "{n} profiles")
            self._set_status(msg.format(n=len(rows)))

    def _on_detail_ready(self, detail) -> None:
        if detail is None:
            self._detail_label.setText(self.tr("cherry.detail_gone", "Profile no longer in the index."))
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
        lines.append("")
        lines.append(self.tr("cherry.detail_sources", "Sources ({n}):").format(n=len(urls)))
        for u in urls:
            mark = "" if u["enabled"] else "✕ "
            target = u["url"] or f"{u['site']}:{u['artist_id'] or '?'}"
            synced = u["last_synced"] or "—"
            lines.append(
                f"{mark}{target}<br><span style='opacity:0.7'>"
                f"{u['file_count']} · {synced}</span>"
            )
        self._detail_label.setText("<br>".join(lines))

    def _on_load_failed(self, message: str) -> None:
        self._btn_refresh.setEnabled(True)
        self._set_status(self.tr("cherry.status_error", "Error: {msg}").format(msg=message))

    def _on_legacy_state(self, running: bool, key: str, value) -> None:
        self._btn_legacy.setEnabled(not running)
        default = {
            "cherry.status_running": "Running in its own window…",
            "cherry.status_idle": "Not running",
            "cherry.status_exit_error": "Exited with error (code {code}) — see logs/{log}",
            "cherry.status_launch_failed": "Failed to start — see logs/{log}",
        }[key]
        self._set_status(self.tr(key, default).format(code=value, log=LOG_NAME))
        if not running:
            self.refresh()  # la GUI clásica pudo crear o sincronizar perfiles

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
            name = QTableWidgetItem(p["display_name"])
            name.setData(Qt.UserRole, p["id"])
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
        shown = self.tr("cherry.showing", "Showing {n} of {total}")
        self._count_label.setText(shown.format(n=len(rows), total=len(self._profiles)))
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

    def _current_profile(self) -> dict | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, COL_NAME)
        if item is None:
            return None
        pid = item.data(Qt.UserRole)
        return next((p for p in self._profiles if p["id"] == pid), None)

    def _on_selection_changed(self) -> None:
        profile = self._current_profile()
        self._update_actions()
        if profile is None or profile["id"] == self._selected_id:
            return
        self._selected_id = profile["id"]
        self._detail_label.setText(self.tr("cherry.detail_loading", "Loading…"))
        if self._runtime.is_running:
            try:
                self._runtime.submit(self._load_detail(profile["id"]))
            except RuntimeError as exc:
                self._load_failed.emit(str(exc))

    def _update_actions(self) -> None:
        profile = self._current_profile()
        self._btn_open.setEnabled(bool(profile and profile.get("folder_exists")))

    # ── Acciones ──────────────────────────────────────────────────────────
    def _open_folder(self) -> None:
        profile = self._current_profile()
        if profile and not CachePaths.open_folder(Path(profile["folder_path"])):
            self._set_status(self.tr("cherry.folder_missing", "Folder not found on disk"))

    def _launch_legacy(self) -> None:
        self._legacy.launch()

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)
