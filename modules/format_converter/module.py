"""
Format Converter Module
Converts images between formats (PNG/JPEG → WebP) preserving metadata.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QProgressBar, QComboBox, QSlider, QFileDialog,
    QMessageBox, QCheckBox, QTextEdit
)
from PySide6.QtCore import Qt, Signal, QThread, QObject
from pathlib import Path

from core.base_module import BaseModule
from core.theme import Theme
from core.components.standard_layout import StandardToolLayout
from .logic.converter import (
    convert_batch, scan_folder_for_conversion,
    verify_batch_conversion, BatchConversionReport
)

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
# Combo order: index 0 = PNG, 1 = WebP, 2 = JPEG
FORMAT_MAP = {0: "PNG", 1: "WEBP", 2: "JPEG"}
FORMAT_EXTS = {"PNG": {".jpg", ".jpeg", ".webp"}, "WEBP": {".png", ".jpg", ".jpeg"}, "JPEG": {".png", ".webp"}}


class DropZoneWidget(QFrame):
    """Zona de drag & drop que acepta carpetas e imágenes sueltas."""
    items_dropped = Signal(list)  # list of Path

    def __init__(self, accent_color, border_color, text_color, label_text, parent=None):
        super().__init__(parent)
        self.accent_color = accent_color
        self.border_color = border_color
        self.setAcceptDrops(True)
        self.setMinimumHeight(60)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(8, 8, 8, 8)

        self.lbl = QLabel(label_text)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet(
            f"color: {text_color}; font-size: 11px; background: transparent; border: none;"
        )
        layout.addWidget(self.lbl)
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet(f"""
            QFrame {{
                border: 2px dashed {self.border_color};
                border-radius: 8px;
                background: transparent;
            }}
        """)

    def _set_hover_style(self):
        self.setStyleSheet(f"""
            QFrame {{
                border: 2px solid {self.accent_color};
                border-radius: 8px;
                background: rgba(139, 233, 253, 15);
            }}
        """)

    def update_label(self, text):
        self.lbl.setText(text)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hover_style()

    def dragLeaveEvent(self, event):
        self._set_idle_style()

    def dropEvent(self, event):
        self._set_idle_style()
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        if paths:
            self.items_dropped.emit(paths)
        event.acceptProposedAction()


class ConversionWorker(QObject):
    """Worker thread para conversión batch."""
    progress = Signal(int, int, str)  # current, total, filename
    finished = Signal(object)          # BatchConversionReport
    error = Signal(str)

    def __init__(self, files, output_dir, target_format, quality, preserve_metadata):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.target_format = target_format
        self.quality = quality
        self.preserve_metadata = preserve_metadata

    def run(self):
        try:
            report = convert_batch(
                self.files,
                output_dir=self.output_dir,
                target_format=self.target_format,
                quality=self.quality,
                preserve_metadata=self.preserve_metadata,
                progress_callback=lambda c, t, f: self.progress.emit(c, t, f)
            )
            self.finished.emit(report)
        except Exception as e:
            self.error.emit(str(e))


class FormatConverterModule(BaseModule):
    def __init__(self):
        super().__init__()
        self._name = "Format Converter"
        self._description = "Convert images between formats preserving AI metadata."
        self._icon = "🔄"
        self.accent_color = "#8be9fd"

        self.view = None
        self.source_folder = None
        self.manual_files = set()   # individually added/dropped files
        self.files_to_convert = []
        self.worker_thread = None

    def get_view(self) -> QWidget:
        if self.view:
            return self.view

        content = self._create_content()
        sidebar = self._create_sidebar()

        self.view = StandardToolLayout(
            content,
            sidebar_widget=sidebar,
            theme_manager=self.context.get('theme_manager'),
            event_bus=self.context.get('event_bus')
        )
        return self.view

    def _create_content(self) -> QWidget:
        theme = self.context.get('theme_manager') if hasattr(self, 'context') else None
        bg_panel = theme.get_color('bg_panel')      if theme else Theme.BG_PANEL
        bg_input = theme.get_color('bg_input')      if theme else Theme.BG_INPUT
        border   = theme.get_color('border')         if theme else Theme.BORDER
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        header = QLabel(self.tr("fc.title", "FORMAT CONVERTER"))
        header.setStyleSheet(f"color: {self.accent_color}; font-size: 32px; font-weight: bold;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)

        subtitle = QLabel(self.tr("fc.subtitle", "Batch convert images while preserving AI metadata"))
        subtitle.setStyleSheet(f"color: {text_dim}; font-size: 14px;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(20)

        stats_frame = QFrame()
        stats_frame.setStyleSheet(f"""
            QFrame {{
                background: {bg_panel};
                border-radius: 10px;
                padding: 15px;
            }}
        """)
        stats_layout = QHBoxLayout(stats_frame)

        stat1 = self._create_stat_widget("📁", "0", self.tr("fc.files", "Files"), text_dim)
        self.lbl_file_count = stat1.findChild(QLabel, "value")
        stats_layout.addWidget(stat1)

        stat2 = self._create_stat_widget("📊", "0 MB", self.tr("fc.original", "Original"), text_dim)
        self.lbl_size_before = stat2.findChild(QLabel, "value")
        stats_layout.addWidget(stat2)

        stat3 = self._create_stat_widget("✨", "0 MB", self.tr("fc.new", "Converted"), text_dim)
        self.lbl_size_after = stat3.findChild(QLabel, "value")
        stats_layout.addWidget(stat3)

        stat4 = self._create_stat_widget("💾", "0%", self.tr("fc.saved", "Saved"), text_dim)
        self.lbl_savings = stat4.findChild(QLabel, "value")
        stats_layout.addWidget(stat4)

        layout.addWidget(stats_frame)

        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {bg_input};
                border: 1px solid {border};
                border-radius: 5px;
                text-align: center;
                color: white;
            }}
            QProgressBar::chunk {{
                background: {self.accent_color};
                border-radius: 4px;
            }}
        """)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.lbl_current_file = QLabel("")
        self.lbl_current_file.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        self.lbl_current_file.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_current_file)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(f"""
            QTextEdit {{
                background: {bg_input};
                color: {text_sec};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }}
        """)
        self.txt_log.setPlaceholderText(self.tr("fc.log_placeholder", "Conversion log will appear here..."))
        layout.addWidget(self.txt_log, 1)

        return container

    def _create_stat_widget(self, icon: str, value: str, label: str, text_dim: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(4)

        lbl_icon = QLabel(icon)
        lbl_icon.setStyleSheet("font-size: 28px;")
        lbl_icon.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_icon)

        lbl_value = QLabel(value)
        lbl_value.setObjectName("value")
        lbl_value.setStyleSheet(f"color: {self.accent_color}; font-size: 20px; font-weight: bold;")
        lbl_value.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_value)

        lbl_label = QLabel(label)
        lbl_label.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        lbl_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_label)

        return widget

    def _create_sidebar(self) -> QWidget:
        theme = self.context.get('theme_manager') if hasattr(self, 'context') else None
        text_dim = theme.get_color('text_dim')       if theme else Theme.TEXT_DIM
        text_sec = theme.get_color('text_secondary') if theme else Theme.TEXT_SECONDARY
        bg_input = theme.get_color('bg_input')       if theme else Theme.BG_INPUT
        border   = theme.get_color('border')         if theme else Theme.BORDER

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)

        lbl_title = QLabel(self.tr("fc.title", "🔄 FORMAT CONVERTER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)

        # --- FUENTE ---
        layout.addWidget(self._section_label(self.tr("fc.source", "📂 1. SOURCE"), text_sec))

        self.btn_select_folder = QPushButton(self.tr("fc.select_folder", "📁 Select Folder"))
        self.btn_select_folder.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000"))
        self.btn_select_folder.setFixedHeight(36)
        self.btn_select_folder.clicked.connect(self.select_source_folder)
        layout.addWidget(self.btn_select_folder)

        # Blue area: Add Files button, right below Select Folder
        self.btn_add_files = QPushButton(self.tr("fc.add_files", "📄 Add Files"))
        self.btn_add_files.setStyleSheet(Theme.get_button_style(self.accent_color))
        self.btn_add_files.setFixedHeight(36)
        self.btn_add_files.clicked.connect(self._add_manual_files)
        layout.addWidget(self.btn_add_files)

        self.lbl_folder = QLabel(self.tr("fc.no_folder", "No folder selected"))
        self.lbl_folder.setStyleSheet(f"color: {text_dim}; font-size: 10px;")
        self.lbl_folder.setWordWrap(True)
        layout.addWidget(self.lbl_folder)

        self.chk_recursive = QCheckBox(self.tr("fc.recursive", "Include subfolders"))
        self.chk_recursive.setChecked(True)
        self.chk_recursive.setStyleSheet(f"color: {text_sec};")
        self.chk_recursive.stateChanged.connect(self._refresh_queue)
        layout.addWidget(self.chk_recursive)

        layout.addSpacing(10)

        # --- FORMATO ---
        layout.addWidget(self._section_label(self.tr("fc.format", "🎯 2. FORMAT"), text_sec))

        layout.addWidget(QLabel(self.tr("fc.target_format", "Target Format:")))
        self.combo_format = QComboBox()
        self.combo_format.addItems(["PNG (Standard)", "WebP (Legacy)", "JPEG (Lossy)"])
        self.combo_format.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_format.currentIndexChanged.connect(self.on_format_changed)
        layout.addWidget(self.combo_format)

        self.quality_container = QWidget()
        quality_layout = QVBoxLayout(self.quality_container)
        quality_layout.setContentsMargins(0, 0, 0, 0)

        quality_tpl = self.tr("fc.quality", "Quality: {quality}%")
        self.lbl_quality = QLabel(quality_tpl.format(quality=90))
        self.lbl_quality.setStyleSheet(f"color: {text_sec};")
        quality_layout.addWidget(self.lbl_quality)

        self.slider_quality = QSlider(Qt.Horizontal)
        self.slider_quality.setRange(50, 100)
        self.slider_quality.setValue(90)
        self.slider_quality.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {bg_input};
                height: 8px;
                border-radius: 4px;
            }}
            QSlider::handle:horizontal {{
                background: {self.accent_color};
                width: 16px;
                margin: -4px 0;
                border-radius: 8px;
            }}
        """)
        self.slider_quality.valueChanged.connect(
            lambda v: self.lbl_quality.setText(quality_tpl.format(quality=v))
        )
        quality_layout.addWidget(self.slider_quality)

        self.lbl_webp_warning = QLabel(
            self.tr("fc.webp_warning", "⚠️ WebP metadata is Internal-Only (Not visible to external tools)")
        )
        self.lbl_webp_warning.setStyleSheet("color: #ffaa00; font-size: 10px; font-style: italic;")
        self.lbl_webp_warning.setWordWrap(True)
        self.lbl_webp_warning.setVisible(False)
        layout.addWidget(self.lbl_webp_warning)

        layout.addWidget(self.quality_container)

        layout.addSpacing(10)

        # --- OPCIONES ---
        layout.addWidget(self._section_label(self.tr("fc.options", "⚙️ 3. OPTIONS"), text_sec))

        self.chk_preserve_meta = QCheckBox(self.tr("fc.preserve_meta", "Preserve AI metadata"))
        self.chk_preserve_meta.setChecked(True)
        self.chk_preserve_meta.setStyleSheet(f"color: {text_sec};")
        layout.addWidget(self.chk_preserve_meta)

        self.chk_verify = QCheckBox(self.tr("fc.verify", "Verify metadata after"))
        self.chk_verify.setChecked(True)
        self.chk_verify.setStyleSheet(f"color: {text_sec};")
        layout.addWidget(self.chk_verify)

        # Green area: drop zone fills all remaining space above the action button
        self.drop_zone = DropZoneWidget(
            accent_color=self.accent_color,
            border_color=border,
            text_color=text_dim,
            label_text=self.tr("fc.drop_zone", "📂 Drop folders or files here")
        )
        self.drop_zone.items_dropped.connect(self._on_items_dropped)
        layout.addWidget(self.drop_zone, stretch=1)

        # Small clear button, hidden until manual files exist
        self.btn_clear = QPushButton(self.tr("fc.clear_queue", "✕ Clear manual files"))
        self.btn_clear.setStyleSheet(
            f"color: {text_dim}; border: none; background: transparent; font-size: 10px;"
        )
        self.btn_clear.setVisible(False)
        self.btn_clear.clicked.connect(self._clear_manual_files)
        layout.addWidget(self.btn_clear, alignment=Qt.AlignCenter)

        layout.addSpacing(4)

        self.btn_convert = QPushButton(self.tr("fc.convert", "🚀 START CONVERSION"))
        self.btn_convert.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000"))
        self.btn_convert.setFixedHeight(48)
        self.btn_convert.clicked.connect(self.start_conversion)
        self.btn_convert.setEnabled(False)
        layout.addWidget(self.btn_convert)

        return container

    def _section_label(self, text: str, color: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 12px;")
        return lbl

    # ------------------------------------------------------------------ #
    #  Source selection                                                    #
    # ------------------------------------------------------------------ #

    def select_source_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self.view,
            self.tr("fc.select_folder", "Select Source Folder")
        )
        if folder:
            self.source_folder = Path(folder)
            self.lbl_folder.setText(folder)
            self._refresh_queue()

    def _add_manual_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self.view,
            self.tr("fc.add_files_dialog", "Select Images to Add"),
            "",
            "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if files:
            for f in files:
                self.manual_files.add(Path(f))
            self._refresh_queue()

    def _on_items_dropped(self, paths: list):
        for path in paths:
            if path.is_dir():
                self.source_folder = path
                self.lbl_folder.setText(str(path))
            elif path.suffix.lower() in IMAGE_EXTENSIONS:
                self.manual_files.add(path)
        self._refresh_queue()

    def _clear_manual_files(self):
        self.manual_files.clear()
        self._refresh_queue()

    # ------------------------------------------------------------------ #
    #  Queue management                                                    #
    # ------------------------------------------------------------------ #

    def _refresh_queue(self):
        """Rebuild files_to_convert from folder scan + manual files."""
        target_format = FORMAT_MAP.get(self.combo_format.currentIndex(), "PNG")

        folder_files = []
        if self.source_folder:
            folder_files = scan_folder_for_conversion(
                self.source_folder,
                target_format=target_format,
                recursive=self.chk_recursive.isChecked()
            )

        valid_exts = FORMAT_EXTS.get(target_format, set())
        filtered_manual = {f for f in self.manual_files if f.suffix.lower() in valid_exts}

        self.files_to_convert = sorted(set(folder_files) | filtered_manual)
        count = len(self.files_to_convert)
        self.lbl_file_count.setText(str(count))
        self.btn_convert.setEnabled(count > 0)

        has_manual = bool(filtered_manual)
        self.btn_clear.setVisible(has_manual)
        if count > 0:
            self.drop_zone.update_label(
                self.tr("fc.queue_count", "{count} files in queue").format(count=count)
            )
        else:
            self.drop_zone.update_label(self.tr("fc.drop_zone", "📂 Drop folders or files here"))

        self.log(self.tr("fc.found", "Found {count} files to convert to {format}").format(
            count=count, format=target_format
        ))

    def on_format_changed(self, index):
        self.quality_container.setVisible(index != 0)
        self.lbl_webp_warning.setVisible(index == 1)
        self._refresh_queue()

    def log(self, message: str):
        self.txt_log.append(message)

    # ------------------------------------------------------------------ #
    #  Conversion                                                          #
    # ------------------------------------------------------------------ #

    def start_conversion(self):
        if not self.files_to_convert:
            return

        output_dir = QFileDialog.getExistingDirectory(
            self.view,
            self.tr("fc.select_output", "Select Output Folder"),
            str(self.source_folder) if self.source_folder else ""
        )
        if not output_dir:
            return

        target_format = FORMAT_MAP.get(self.combo_format.currentIndex(), "PNG")

        self.btn_convert.setEnabled(False)
        self.btn_select_folder.setEnabled(False)
        self.btn_add_files.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(self.files_to_convert))
        self.progress_bar.setValue(0)

        sep = "=" * 40
        self.log(f"\n{sep}")
        self.log(self.tr("fc.log.start", "Starting conversion: {count} files → {format}").format(
            count=len(self.files_to_convert), format=target_format
        ))
        self.log(self.tr("fc.log.output", "Output: {path}").format(path=output_dir))
        self.log(f"{sep}\n")

        self.worker = ConversionWorker(
            self.files_to_convert,
            output_dir,
            target_format,
            self.slider_quality.value(),
            self.chk_preserve_meta.isChecked()
        )
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_conversion_finished)
        self.worker.error.connect(self.on_conversion_error)

        self.worker_thread.start()

    def on_progress(self, current, total, filename):
        self.progress_bar.setValue(current)
        self.lbl_current_file.setText(
            self.tr("fc.converting", "Converting: {filename}").format(filename=filename)
        )

    def on_conversion_finished(self, report: BatchConversionReport):
        self.worker_thread.quit()
        self.worker_thread.wait()

        self.lbl_size_before.setText(f"{report.total_original_bytes / 1024 / 1024:.1f} MB")
        self.lbl_size_after.setText(f"{report.total_new_bytes / 1024 / 1024:.1f} MB")
        self.lbl_savings.setText(f"{report.compression_ratio:.1f}%")

        sep = "=" * 40
        self.log(f"\n{sep}")
        self.log(self.tr("fc.log.complete", "CONVERSION COMPLETE"))
        self.log(f"{sep}")
        self.log(report.get_summary())

        if report.failed_files:
            self.log(self.tr("fc.log.failed", "Failed files:"))
            for path, error in report.failed_files[:10]:
                self.log(f"  ❌ {Path(path).name}: {error}")
            if len(report.failed_files) > 10:
                self.log(self.tr("fc.log.more", "... and {count} more").format(
                    count=len(report.failed_files) - 10
                ))

        if self.chk_verify.isChecked() and report.results:
            self.log(self.tr("fc.log.verifying", "Verifying metadata integrity..."))
            try:
                verify_report = verify_batch_conversion(report)
                verified_count = verify_report.ok_count + verify_report.repaired_count
                self.log(self.tr("fc.log.verified", "  Verified: {ok}/{total}").format(
                    ok=verified_count, total=verify_report.total_files
                ))
                self.log(self.tr("fc.log.integrity", "  Integrity: {pct}%").format(
                    pct=f"{verify_report.avg_integrity:.1f}"
                ))
            except Exception as e:
                self.log(self.tr("fc.log.verify_error", "  Verification error: {error}").format(error=e))

        self.btn_convert.setEnabled(True)
        self.btn_select_folder.setEnabled(True)
        self.btn_add_files.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_current_file.setText("")

        QMessageBox.information(
            self.view,
            self.tr("fc.complete", "Conversion Complete"),
            self.tr("fc.complete_msg", "Converted {count} files.\nSaved {saved} MB ({ratio}%)").format(
                count=report.converted_count,
                saved=f"{report.total_saved_bytes / 1024 / 1024:.1f}",
                ratio=f"{report.compression_ratio:.1f}"
            )
        )

    def on_conversion_error(self, error):
        self.worker_thread.quit()
        self.worker_thread.wait()

        self.log(f"\n❌ ERROR: {error}")

        self.btn_convert.setEnabled(True)
        self.btn_select_folder.setEnabled(True)
        self.btn_add_files.setEnabled(True)
        self.progress_bar.setVisible(False)

        QMessageBox.critical(
            self.view,
            self.tr("fc.error", "Error"),
            self.tr("fc.error_msg", "Conversion failed: {error}").format(error=error)
        )
