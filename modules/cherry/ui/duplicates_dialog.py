"""
Proyectos que quizá son el mismo artista — fase 1 (A5).

Muestra lo que encuentra `logic/duplicates.py` y deja hacer una sola cosa con un
par: marcarlo como distinto, que el motor guarda y respeta en adelante.

Fusionar no está aquí a propósito: mueve archivos entre carpetas de disco,
resuelve huérfanos y renumera el destino, no tiene deshacer, y va en su propia
sesión (decisión de David, 2026-09-03).

Que la lista salga vacía es el resultado normal y bueno. Lo que sí merece
atención es un par de tipo "misma fuente": ese no debería existir nunca, porque
significa que dos proyectos están bajando al mismo artista del mismo sitio.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..logic import duplicates as dup_logic

log = logging.getLogger(__name__)

COL_A, COL_B, COL_WHY = range(3)


class DuplicatesDialog(QDialog):
    """Lista de pares sospechosos, sobre el runtime del módulo."""

    _pairs_ready = Signal(object, int)   # list[dict], exclusiones ya guardadas
    _failed      = Signal(str)
    _excluded    = Signal(int, int)

    def __init__(self, runtime, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._theme = theme
        self._lm = locale
        self._pairs: list[dict] = []
        self.setWindowTitle(self.tr("cherry.dups_title", "Possible duplicate projects"))
        self.setMinimumSize(760, 420)

        lay = QVBoxLayout(self)

        intro = QLabel(self.tr(
            "cherry.dups_intro",
            "Two projects sharing a source are the same artist added twice — that one is "
            "a certainty, not a guess. A similar name is only a suspicion. Merging is not "
            "done here: it moves files between folders on disk.",
        ))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(intro)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels([
            self.tr("cherry.dups_col_a", "Project"),
            self.tr("cherry.dups_col_b", "Looks like"),
            self.tr("cherry.dups_col_why", "Why"),
        ])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_A, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_B, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_WHY, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self._table, 1)

        self._status = QLabel(self.tr("cherry.dups_scanning", "Comparing projects…"))
        self._status.setStyleSheet(f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(self._status)

        row = QHBoxLayout()
        row.addStretch(1)
        self._btn_not_same = QPushButton(self.tr("cherry.dups_not_same", "They are different"))
        self._btn_not_same.setToolTip(self.tr(
            "cherry.dups_not_same_tip", "The pair stops showing up, here and in cherry-dl."))
        self._btn_not_same.clicked.connect(self._exclude_selected)
        row.addWidget(self._btn_not_same)
        btn_close = QPushButton(self.tr("cherry.dups_close", "Close"))
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        lay.addLayout(row)

        self._pairs_ready.connect(self._on_pairs)
        self._failed.connect(self._on_failed)
        self._excluded.connect(self._on_excluded)
        self._update_buttons()
        self._scan()

    # ── helpers ───────────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    def _tier_label(self, tier: str) -> str:
        return {
            dup_logic.TIER_SOURCE:   self.tr("cherry.dups_tier_source", "same source"),
            dup_logic.TIER_PROBABLE: self.tr("cherry.dups_tier_probable", "similar name"),
            dup_logic.TIER_POSSIBLE: self.tr("cherry.dups_tier_possible", "vaguely similar"),
        }.get(tier, tier)

    # ── búsqueda ──────────────────────────────────────────────────────────
    def _scan(self) -> None:
        if self._runtime is None or not self._runtime.is_running:
            self._on_failed(self.tr(
                "cherry.runtime_failed", "Could not start the async runtime."))
            return
        try:
            self._runtime.submit(self._scan_async())
        except RuntimeError as exc:
            self._on_failed(str(exc))

    async def _scan_async(self) -> None:
        try:
            pairs = await dup_logic.find_pairs()
            excluded = await dup_logic.excluded_count()
        except Exception as exc:
            log.exception("cherry: la búsqueda de duplicados falló")
            self._failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._pairs_ready.emit(pairs, excluded)

    def _on_pairs(self, pairs: list, excluded: int) -> None:
        self._pairs = list(pairs)
        self._table.setRowCount(len(self._pairs))
        warn = QBrush(QColor(self._color("accent_warning", "#ff5555")))
        for row, pair in enumerate(self._pairs):
            self._table.setItem(row, COL_A, QTableWidgetItem(pair["name_a"]))
            self._table.setItem(row, COL_B, QTableWidgetItem(pair["name_b"]))
            why = QTableWidgetItem(f"{self._tier_label(pair['tier'])} · {pair['reason']}")
            if pair["tier"] == dup_logic.TIER_SOURCE:
                # Lo único que no admite duda: se pinta como lo que es.
                why.setForeground(warn)
                why.setToolTip(self.tr(
                    "cherry.dups_source_tip",
                    "Both projects download the same artist from the same site.",
                ))
            self._table.setItem(row, COL_WHY, why)
        if not self._pairs:
            self._status.setText(self.tr(
                "cherry.dups_none",
                "No suspicious pairs. {n} pairs are already marked as different.",
            ).format(n=excluded))
        else:
            self._status.setText(self.tr(
                "cherry.dups_found",
                "{n} pairs to review · {e} already marked as different.",
            ).format(n=len(self._pairs), e=excluded))
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self._status.setText(self.tr(
            "cherry.dups_failed", "Could not compare: {msg}").format(msg=message))
        self._update_buttons()

    # ── exclusión ─────────────────────────────────────────────────────────
    def _current_row(self) -> int:
        model = self._table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return -1
        index = rows[0].row()
        return index if 0 <= index < len(self._pairs) else -1

    def _update_buttons(self) -> None:
        self._btn_not_same.setEnabled(self._current_row() >= 0)

    def _exclude_selected(self) -> None:
        row = self._current_row()
        if row < 0 or self._runtime is None or not self._runtime.is_running:
            return
        pair = self._pairs[row]
        self._btn_not_same.setEnabled(False)
        try:
            self._runtime.submit(self._exclude_async(pair["id_a"], pair["id_b"]))
        except RuntimeError as exc:
            self._on_failed(str(exc))

    async def _exclude_async(self, id_a: int, id_b: int) -> None:
        try:
            await dup_logic.exclude(id_a, id_b)
        except Exception as exc:
            log.exception("cherry: no se pudo guardar la exclusión")
            self._failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self._excluded.emit(id_a, id_b)

    def _on_excluded(self, id_a: int, id_b: int) -> None:
        """La fila se quita de la tabla en vez de re-buscar: volver a comparar
        los 1.035 pares para tachar uno sería absurdo."""
        for row, pair in enumerate(self._pairs):
            if pair["id_a"] == id_a and pair["id_b"] == id_b:
                self._table.removeRow(row)
                self._pairs.pop(row)
                break
        self._status.setText(self.tr(
            "cherry.dups_excluded", "Marked as different. {n} pairs left to review.",
        ).format(n=len(self._pairs)))
        self._update_buttons()
