"""
Proyectos que quizá son el mismo artista, y qué hacer con ellos (A5).

Muestra lo que encuentra `logic/duplicates.py` y deja hacer dos cosas con un par:

  - **Son distintos** — el motor guarda la exclusión y el par no vuelve a salir.
  - **Fusionar** — uno absorbe al otro. Mueve archivos entre carpetas del disco
    y **no tiene deshacer**; la orquestación y sus salvaguardas están en
    `logic/merge.py`.

La confirmación de la fusión enseña los números ANTES de tocar nada, que es la
diferencia con el original: allí la pregunta de qué hacer con los archivos
repetidos llega a mitad de la operación, cuando ya se movieron y la respuesta ya
no cambia nada reversible.

Que la lista salga vacía es el resultado normal y bueno. Lo que sí merece
atención es un par de tipo "misma fuente": ese no debería existir nunca, porque
significa que dos proyectos están bajando al mismo artista del mismo sitio.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton, QRadioButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..logic import duplicates as dup_logic
from ..logic import merge as merge_logic

log = logging.getLogger(__name__)

COL_A, COL_B, COL_WHY = range(3)


class MergeConfirmDialog(QDialog):
    """Lo que va a pasar, con los números delante, antes de mover un solo archivo.

    Tres cosas que el usuario no puede adivinar y aquí se dicen: en qué dirección
    va la fusión (la decide el motor por antigüedad, no se elige), qué pasa con
    los archivos que el destino ya tiene, y que los que el catálogo no conoce se
    quedan donde están.
    """

    def __init__(self, plan, theme=None, locale=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plan = plan
        self._theme = theme
        self._lm = locale
        self.setWindowTitle(self.tr("cherry.merge_title", "Merge projects"))
        self.setMinimumWidth(560)

        lay = QVBoxLayout(self)

        head = QLabel(self.tr(
            "cherry.merge_direction", "<b>{keep}</b> absorbs <b>{remove}</b>",
        ).format(keep=plan.keep_name, remove=plan.remove_name))
        head.setWordWrap(True)
        head.setStyleSheet(f"font-size: 14px; color: {self._color('text_primary', '#eee')};")
        lay.addWidget(head)

        why = QLabel(self.tr(
            "cherry.merge_why_keep",
            "The older project is the one that stays — cherry-dl decides that, "
            "it is not a choice. Its folder is the one that keeps growing.",
        ))
        why.setWordWrap(True)
        why.setStyleSheet(f"color: {self._color('text_dim', '#888')}; font-size: 11px;")
        lay.addWidget(why)

        paths = QLabel(f"→ {plan.keep_folder}\n← {plan.remove_folder}")
        paths.setWordWrap(True)
        paths.setStyleSheet(
            f"color: {self._color('text_secondary', '#bbb')}; font-size: 11px;")
        paths.setToolTip(self.tr(
            "cherry.merge_paths_tip",
            "Top: the folder that receives the files. Bottom: the one left behind.",
        ))
        lay.addWidget(paths)

        lay.addWidget(self._separator())

        numbers = QFormLayout()
        numbers.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._add_number(numbers, "cherry.merge_n_move", "Files that will move",
                         plan.movable, "accent_success",
                         "cherry.merge_n_move_tip",
                         "Unique to the project being absorbed: nothing in the "
                         "destination has the same content.")
        self._add_number(numbers, "cherry.merge_n_dup", "Already in the destination",
                         plan.duplicated, "text_secondary",
                         "cherry.merge_n_dup_tip",
                         "Same content, byte for byte. They do not move — you "
                         "choose below what happens to them.")
        if plan.missing:
            self._add_number(numbers, "cherry.merge_n_missing", "Listed but not on disk",
                             plan.missing, "text_dim",
                             "cherry.merge_n_missing_tip",
                             "The catalogue mentions them but the file is gone. "
                             "They are skipped.")
        if plan.untracked:
            self._add_number(numbers, "cherry.merge_n_untracked", "Not in the catalogue",
                             plan.untracked, "accent_warning",
                             "cherry.merge_n_untracked_tip",
                             "Files sitting in the folder that cherry-dl never "
                             "registered. Nothing moves or deletes them: they stay "
                             "where they are, even if the folder is emptied.")
        lay.addLayout(numbers)

        lay.addWidget(self._separator())

        orphan_title = QLabel(self.tr(
            "cherry.merge_orphan_q", "What to do with the {n} already in the destination:",
        ).format(n=plan.duplicated))
        orphan_title.setWordWrap(True)
        lay.addWidget(orphan_title)

        self._orphans = QButtonGroup(self)
        for value, key, text, tip_key, tip in (
            (merge_logic.ORPHAN_IGNORE, "cherry.merge_orphan_keep",
             "Leave them where they are",
             "cherry.merge_orphan_keep_tip",
             "Nothing is deleted. The folder stays with its copies — the safe "
             "choice if you want to check the result first."),
            (merge_logic.ORPHAN_RENAME, "cherry.merge_orphan_rename",
             "Rename the folder to orphan_…",
             "cherry.merge_orphan_rename_tip",
             "Marks the leftover folder without deleting anything, so it is easy "
             "to find and remove later."),
            (merge_logic.ORPHAN_DELETE, "cherry.merge_orphan_delete",
             "Delete them permanently",
             "cherry.merge_orphan_delete_tip",
             "Deletes the copies for good — not to the recycle bin. The folder "
             "itself goes too if nothing else is left in it."),
        ):
            radio = QRadioButton(self.tr(key, text))
            radio.setToolTip(self.tr(tip_key, tip) if value == merge_logic.ORPHAN_IGNORE else
                self.tr("cherry.merge_orphan_review_tip", "Review current bytes and curation in Duplicate Finder before retiring a copy."))
            radio.setProperty("orphan_action", value)
            radio.setEnabled(plan.duplicated > 0 and value == merge_logic.ORPHAN_IGNORE)
            if value == merge_logic.ORPHAN_IGNORE:
                radio.setChecked(True)
            self._orphans.addButton(radio)
            lay.addWidget(radio)

        warn = QLabel(self.tr(
            "cherry.merge_warn",
            "⚠ Merging moves files on disk and cannot be undone.",
        ))
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"color: {self._color('accent_warning', '#ffb86c')}; font-size: 11px;")
        lay.addWidget(warn)

        buttons = QDialogButtonBox()
        self._btn_go = buttons.addButton(
            self.tr("cherry.merge_go", "Merge"),
            QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_go.setToolTip(self.tr(
            "cherry.merge_go_tip",
            "Moves the files and merges the two projects into one. No undo.",
        ))
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setText(self.tr("cherry.merge_cancel", "Cancel"))
        cancel.setToolTip(self.tr(
            "cherry.merge_cancel_tip", "Closes without touching anything."))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        # Nada que mover y nada que limpiar: fusionar solo uniría los índices, y
        # eso no es lo que el usuario cree estar pidiendo aquí.
        if not plan.readable:
            self._btn_go.setEnabled(False)
            warn.setText(self.tr(
                "cherry.merge_unreadable",
                "⚠ The catalogue of the project to absorb could not be read. "
                "Nothing will be merged.",
            ))

    # ── helpers ───────────────────────────────────────────────────────────
    def tr(self, key: str, default: str) -> str:
        return self._lm.tr(key, default) if self._lm else default

    def _color(self, key: str, fallback: str) -> str:
        return self._theme.get_color(key) if self._theme else fallback

    def _separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {self._color('border', '#444')};")
        return line

    def _add_number(self, form: QFormLayout, key: str, text: str, value: int,
                    color: str, tip_key: str, tip: str) -> None:
        label = QLabel(f"<b>{value}</b>")
        label.setStyleSheet(f"color: {self._color(color, '#ccc')}; font-size: 13px;")
        caption = QLabel(self.tr(key, text))
        caption.setToolTip(self.tr(tip_key, tip))
        label.setToolTip(caption.toolTip())
        form.addRow(label, caption)

    def orphan_action(self) -> str:
        button = self._orphans.checkedButton()
        if button is None:
            return merge_logic.ORPHAN_IGNORE
        return button.property("orphan_action") or merge_logic.ORPHAN_IGNORE


class DuplicatesDialog(QDialog):
    """Lista de pares sospechosos, sobre el runtime del módulo."""

    _pairs_ready = Signal(object, int)   # list[dict], exclusiones ya guardadas
    _failed      = Signal(str)
    _excluded    = Signal(int, int)
    _planned     = Signal(object, object)  # MergePlan, par que lo originó
    _merged      = Signal(object, object)  # MergeResult, par fusionado
    _progress    = Signal(int, int)

    # Carpetas cuyo contenido cambió tras una fusión, para que la vista avise
    # por EventBus. El diálogo no conoce el bus: quien publica es la vista, que
    # es la que sabe si la carpeta cuelga de una vigilada.
    folders_changed = Signal(object)     # list[str]

    def __init__(self, runtime, theme=None, locale=None, parent: QWidget | None = None, *, data_service=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._data_service = data_service
        self._theme = theme
        self._lm = locale
        self._pairs: list[dict] = []
        self._busy = False   # hay una fusión en marcha: nada de tocar la lista
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
        self._btn_merge = QPushButton(self.tr("cherry.dups_merge", "Merge…"))
        self._btn_merge.setToolTip(self.tr(
            "cherry.dups_merge_tip",
            "Turns the two projects into one: the older keeps its folder and "
            "receives what the other has and it lacks. Shows the numbers before "
            "moving anything — but once confirmed there is no undo.",
        ))
        self._btn_merge.clicked.connect(self._merge_selected)
        row.addWidget(self._btn_merge)
        btn_close = QPushButton(self.tr("cherry.dups_close", "Close"))
        btn_close.setToolTip(self.tr(
            "cherry.dups_close_tip", "Closes the list. Nothing is left running."))
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        lay.addLayout(row)

        self._pairs_ready.connect(self._on_pairs)
        self._failed.connect(self._on_failed)
        self._excluded.connect(self._on_excluded)
        self._planned.connect(self._on_planned)
        self._merged.connect(self._on_merged)
        self._progress.connect(self._on_progress)
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
        ready = self._current_row() >= 0 and not self._busy
        self._btn_not_same.setEnabled(ready)
        self._btn_merge.setEnabled(ready)

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

    # ── fusión ────────────────────────────────────────────────────────────
    def _merge_selected(self) -> None:
        """Primer paso: leer los catálogos para poder enseñar los números.

        La confirmación no se abre hasta tener el plan; preguntar "¿seguro?" sin
        poder decir cuántos archivos se mueven es pedir una firma en blanco.
        """
        row = self._current_row()
        if row < 0 or self._busy or self._runtime is None or not self._runtime.is_running:
            return
        pair = self._pairs[row]
        self._busy = True
        self._update_buttons()
        self._status.setText(self.tr(
            "cherry.merge_planning", "Reading both catalogues…"))
        try:
            self._runtime.submit(self._plan_async(pair))
        except RuntimeError as exc:
            self._busy = False
            self._on_failed(str(exc))

    async def _plan_async(self, pair: dict) -> None:
        try:
            plan = await merge_logic.preview(pair)
        except Exception as exc:
            log.exception("cherry: no se pudo planear la fusión")
            self._failed.emit(f"{type(exc).__name__}: {exc}")
            self._planned.emit(None, pair)
            return
        self._planned.emit(plan, pair)

    def _on_planned(self, plan, pair: dict) -> None:
        """Ya en el hilo GUI: confirmar y, si el usuario acepta, ejecutar."""
        self._busy = False
        self._update_buttons()
        if plan is None:
            return

        dialog = MergeConfirmDialog(plan, self._theme, self._lm, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._status.setText(self.tr(
                "cherry.merge_cancelled", "Merge cancelled. Nothing was touched."))
            return

        action = dialog.orphan_action()
        self._busy = True
        self._update_buttons()
        self._status.setText(self.tr(
            "cherry.merge_running", "Merging {remove} into {keep}…",
        ).format(remove=plan.remove_name, keep=plan.keep_name))
        try:
            self._runtime.submit(self._merge_async(pair, action))
        except RuntimeError as exc:
            self._busy = False
            self._update_buttons()
            self._on_failed(str(exc))

    async def _merge_async(self, pair: dict, orphan_action: str) -> None:
        try:
            result = await merge_logic.merge_pair(
                pair,
                orphan_action=orphan_action,
                data_service=self._data_service,
                on_progress=lambda done, total: self._progress.emit(done, total),
            )
        except Exception as exc:
            log.exception("cherry: la fusión falló")
            self._failed.emit(f"{type(exc).__name__}: {exc}")
            self._merged.emit(None, pair)
            return
        self._merged.emit(result, pair)

    def _on_progress(self, done: int, total: int) -> None:
        self._status.setText(self.tr(
            "cherry.merge_progress", "Moving files… {done} of {total}",
        ).format(done=done, total=total))

    def _on_merged(self, result, pair: dict) -> None:
        self._busy = False
        if result is None:
            self._update_buttons()
            return

        folders = [str(f) for f in result.touched_folders]
        if folders:
            self.folders_changed.emit(folders)
        if result.ok:
            # El par ya no existe: uno de los dos proyectos desapareció.
            for row, item in enumerate(self._pairs):
                if item["id_a"] == pair["id_a"] and item["id_b"] == pair["id_b"]:
                    self._table.removeRow(row)
                    self._pairs.pop(row)
                    break
            self._status.setText(self.tr(
                "cherry.merge_done",
                "✓ Merged into {keep} · {moved} files moved, {dup} were already there.",
            ).format(keep=result.plan.keep_name, moved=result.moved, dup=result.orphaned))
        else:
            # Ver `logic/merge.py`: si mover falla, el índice queda intacto a
            # propósito, así que las dos carpetas siguen siendo proyectos válidos.
            detail = result.errors[0] if result.errors else ""
            self._status.setText(self.tr(
                "cherry.merge_failed",
                "Merge stopped. Completed moves remain; review the reported paths before retrying. {msg}",
            ).format(msg=detail))
        self._update_buttons()
