import os
import logging
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QPushButton,
                               QHBoxLayout, QFileDialog, QMessageBox, QProgressBar,
                               QFrame, QScrollArea, QSlider, QComboBox, QCheckBox)
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from .logic.deduplicator import Deduplicator


class DeduplicationWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(dict)

    def __init__(self, folder, mode, threshold=5):
        super().__init__()
        self.folder = folder
        self.mode = mode
        self.threshold = threshold
        self.engine = Deduplicator()

    def run(self):
        if self.mode == "hash":
            res = self.engine.find_duplicates_by_hash(self.folder, self.progress.emit)
        else:
            res = self.engine.find_duplicates_visual(self.folder, self.threshold, self.progress.emit)
        self.finished.emit(res)

    def stop(self):
        self.engine.stop()


class DuplicateItem(QFrame):
    """Miniatura con checkbox y metadatos."""
    def __init__(self, path, bg_color="#111", border_color="#333", parent=None):
        super().__init__(parent)
        self.path = path
        self.setFixedSize(140, 180)
        self.setStyleSheet(f"background: {bg_color}; border: 1px solid {border_color}; border-radius: 8px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)

        self.lbl_img = QLabel()
        self.lbl_img.setFixedSize(130, 100)
        self.lbl_img.setAlignment(Qt.AlignCenter)

        if path.lower().endswith(('.zip', '.rar')):
            self.lbl_img.setText("📦\nARCHIVE")
            self.lbl_img.setStyleSheet("font-size: 24px; color: #ffcc00;")
        else:
            pix = QPixmap(path)
            if not pix.isNull():
                self.lbl_img.setPixmap(pix.scaled(130, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.lbl_img.setText("❌")

        layout.addWidget(self.lbl_img)

        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")

        name_lbl = QLabel(os.path.basename(path))
        name_lbl.setStyleSheet("font-size: 10px; color: #aaa;")
        name_lbl.setWordWrap(True)
        name_lbl.setMaximumHeight(30)

        chk_layout = QHBoxLayout()
        chk_layout.addWidget(self.check)
        chk_layout.addWidget(name_lbl)
        layout.addLayout(chk_layout)

        size_mb = os.path.getsize(path) / (1024 * 1024)
        info_lbl = QLabel(f"{size_mb:.2f} MB")
        info_lbl.setStyleSheet("font-size: 9px; color: #666;")
        layout.addWidget(info_lbl)

    def is_checked(self): return self.check.isChecked()
    def set_checked(self, val): self.check.setChecked(val)


class DuplicateFinderModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Duplicate Finder"
        self._description = "Find and remove duplicate images by hash or visual similarity."
        self._icon = "👯"
        self.accent_color = "#00ffcc"
        self.view = None
        self.worker = None
        self.duplicate_groups = {}

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        sidebar = self._create_sidebar()
        content = self._create_content()
        bottom  = self._create_bottom_bar()

        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            bottom_widget=bottom,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_sidebar(self):
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(20)

        lbl_title = QLabel(self.tr("dup.title", "⚙ SCAN OPTIONS"))
        lbl_title.setStyleSheet(f"font-weight: bold; color: {self.accent_color};")
        layout.addWidget(lbl_title)

        layout.addWidget(QLabel(self.tr("dup.mode_label", "Comparison Mode:")))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems([
            self.tr("dup.mode_hash",   "Hash (Exact Match)"),
            self.tr("dup.mode_visual", "Visual (Similarity)")
        ])
        self.combo_mode.setStyleSheet("""
            QComboBox { color: #ffffff; background-color: #000000; }
            QComboBox QAbstractItemView {
                background-color: #1a1a1a; color: #ffffff;
                selection-background-color: #00ffcc; selection-color: #000000;
                border: 1px solid #333; outline: 0px;
            }
        """)
        layout.addWidget(self.combo_mode)

        self.threshold_container = QWidget()
        thr_layout = QVBoxLayout(self.threshold_container)
        thr_layout.setContentsMargins(0, 0, 0, 0)
        thr_layout.addWidget(QLabel(self.tr("dup.threshold", "Visual Tolerance (lower = more exact):")))

        self.slider_thr = QSlider(Qt.Horizontal)
        self.slider_thr.setRange(1, 20)
        self.slider_thr.setValue(5)
        thr_layout.addWidget(self.slider_thr)

        thr_tpl = self.tr("dup.value", "Value: {value}")
        self.lbl_thr_val = QLabel(thr_tpl.format(value=5))
        self.lbl_thr_val.setStyleSheet(f"color: {text_dim};")
        self.slider_thr.valueChanged.connect(
            lambda v: self.lbl_thr_val.setText(thr_tpl.format(value=v))
        )
        thr_layout.addWidget(self.lbl_thr_val)
        layout.addWidget(self.threshold_container)

        self.btn_select = QPushButton(self.tr("dup.select_folder", "📁 Select Folder"))
        self.btn_select.setCursor(Qt.PointingHandCursor)
        self.btn_select.clicked.connect(self.select_folder)
        self.btn_select.setStyleSheet("background: #222; border: 1px solid #444; padding: 10px;")
        layout.addWidget(self.btn_select)

        self.lbl_folder = QLabel(self.tr("dup.no_folder", "No folder selected"))
        self.lbl_folder.setWordWrap(True)
        self.lbl_folder.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        layout.addWidget(self.lbl_folder)

        layout.addStretch()

        self.btn_run = QPushButton(self.tr("dup.run_scan", "🚀 START SCAN"))
        self.btn_run.setCursor(Qt.PointingHandCursor)
        self.btn_run.clicked.connect(self.run_scan)
        self.btn_run.setStyleSheet(
            f"background: {self.accent_color}; color: black; font-weight: bold; padding: 12px;"
        )
        layout.addWidget(self.btn_run)

        return container

    def _create_content(self):
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim') if theme else Theme.TEXT_DIM

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("background: transparent; border: none;")

        self.grid_container = QWidget()
        self.grid_layout = QVBoxLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignTop)
        self.grid_layout.setSpacing(20)

        self.scroll.setWidget(self.grid_container)

        self.lbl_empty = QLabel(
            self.tr("dup.placeholder", "Select a folder and start the scan to find duplicates.")
        )
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setStyleSheet(f"color: {text_dim}; font-size: 16px;")
        self.grid_layout.addWidget(self.lbl_empty)

        return self.scroll

    def _create_bottom_bar(self):
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 5, 10, 5)

        self.progress = QProgressBar()
        self.progress.setStyleSheet("QProgressBar { height: 10px; }")
        self.progress.hide()
        layout.addWidget(self.progress, 1)

        self.btn_auto_select = QPushButton(self.tr("dup.auto_select", "🧹 Delete all but first"))
        self.btn_auto_select.setCursor(Qt.PointingHandCursor)
        self.btn_auto_select.clicked.connect(self.auto_select_duplicates)
        self.btn_auto_select.setStyleSheet(
            "background: #333; color: white; padding: 10px 20px; border-radius: 4px;"
        )
        self.btn_auto_select.setEnabled(False)
        layout.addWidget(self.btn_auto_select)

        self.btn_delete = QPushButton(self.tr("dup.delete", "🔥 DELETE SELECTED"))
        self.btn_delete.setCursor(Qt.PointingHandCursor)
        self.btn_delete.clicked.connect(self.delete_files)
        self.btn_delete.setStyleSheet(
            "background: #ff5555; color: white; font-weight: bold; padding: 10px 20px; border-radius: 4px;"
        )
        self.btn_delete.setEnabled(False)
        layout.addWidget(self.btn_delete)

        return container

    # ------------------------------------------------------------------ #

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self.view, self.tr("dup.select_folder", "Select Folder")
        )
        if folder:
            self.folder_path = folder
            self.lbl_folder.setText(folder)

    def run_scan(self):
        if not hasattr(self, 'folder_path'):
            QMessageBox.warning(
                self.view,
                self.tr("common.error", "Error"),
                self.tr("dup.no_folder_warn", "Please select a folder first.")
            )
            return

        self.btn_run.setEnabled(False)
        self.progress.show()
        self.progress.setValue(0)

        mode = "hash" if self.combo_mode.currentIndex() == 0 else "visual"
        self.worker = DeduplicationWorker(self.folder_path, mode, self.slider_thr.value())
        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.start()

    def update_progress(self, curr, total, msg):
        self.progress.setMaximum(total)
        self.progress.setValue(curr)

    def on_scan_finished(self, results):
        theme    = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg_main  = theme.get_color('bg_main')  if theme else Theme.BG_MAIN
        bg_panel = theme.get_color('bg_panel') if theme else Theme.BG_PANEL
        border   = theme.get_color('border')   if theme else Theme.BORDER
        text_dim = theme.get_color('text_dim') if theme else Theme.TEXT_DIM

        self.btn_run.setEnabled(True)
        self.progress.hide()

        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.duplicate_groups = {}

        if not results:
            lbl = QLabel(self.tr("dup.no_results", "No duplicates found."))
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color: {text_dim}; font-size: 16px;")
            self.grid_layout.addWidget(lbl)
            self.btn_auto_select.setEnabled(False)
            self.btn_delete.setEnabled(False)
            return

        group_tpl = self.tr("dup.group_header", "Group: {id}... ({count} files)")
        for g_id, paths in results.items():
            row_frame = QFrame()
            row_frame.setStyleSheet(
                f"background: {bg_main}; border-radius: 10px; border: 1px solid {border};"
            )
            row_layout = QVBoxLayout(row_frame)

            header = QLabel(group_tpl.format(id=g_id[:8], count=len(paths)))
            header.setStyleSheet(f"color: {text_dim}; font-size: 10px; font-weight: bold; padding: 5px;")
            row_layout.addWidget(header)

            items_container = QWidget()
            items_layout = QHBoxLayout(items_container)
            items_layout.setAlignment(Qt.AlignLeft)

            group_items = []
            for p in paths:
                item = DuplicateItem(p, bg_color=bg_panel, border_color=border)
                items_layout.addWidget(item)
                group_items.append(item)

            row_layout.addWidget(items_container)
            self.grid_layout.addWidget(row_frame)
            self.duplicate_groups[g_id] = group_items

        self.btn_auto_select.setEnabled(True)
        self.btn_delete.setEnabled(True)

    def auto_select_duplicates(self):
        for items in self.duplicate_groups.values():
            for i, item in enumerate(items):
                item.set_checked(i > 0)

    def delete_files(self):
        to_delete = []
        full_groups_deleted = 0

        for g_id, items in self.duplicate_groups.items():
            all_marked = True
            group_to_del = []
            for item in items:
                if item.is_checked():
                    group_to_del.append(item.path)
                else:
                    all_marked = False
            if all_marked:
                full_groups_deleted += 1
            to_delete.extend(group_to_del)

        if not to_delete:
            QMessageBox.information(
                self.view,
                self.tr("common.info", "Information"),
                self.tr("dup.no_selection", "No files selected.")
            )
            return

        confirm_msg = self.tr(
            "dup.confirm_delete", "Are you sure you want to permanently delete {count} files?\n\n"
        ).format(count=len(to_delete))

        if full_groups_deleted > 0:
            confirm_msg += self.tr(
                "dup.confirm_all_warn",
                "⚠ WARNING: You are deleting ALL copies of {count} groups. "
                "You will have no originals left in those cases."
            ).format(count=full_groups_deleted)

        res = QMessageBox.warning(
            self.view,
            self.tr("dup.confirm_title", "DELETION CONFIRMATION"),
            confirm_msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if res == QMessageBox.Yes:
            deleted_count = 0
            for path in to_delete:
                try:
                    os.remove(path)
                    deleted_count += 1
                except Exception as e:
                    logging.warning(f"[DuplicateFinder] Error deleting {path}: {e}")

            QMessageBox.information(
                self.view,
                self.tr("dup.done_title", "Process Complete"),
                self.tr("dup.done_msg", "Deleted {count} files successfully.").format(
                    count=deleted_count
                )
            )
            self.run_scan()

    def run_headless(self, params: dict, input_data) -> None:
        pass
