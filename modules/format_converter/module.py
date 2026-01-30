"""
Format Converter Module
Convierte imágenes entre formatos (PNG/JPEG → WebP) preservando metadata.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFrame, QProgressBar, QComboBox, QSlider, QFileDialog,
    QMessageBox, QSpinBox, QCheckBox, QTextEdit, QSizePolicy
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


class ConversionWorker(QObject):
    """Worker thread para conversión batch."""
    progress = Signal(int, int, str)  # current, total, filename
    finished = Signal(object)  # BatchConversionReport
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
    """
    Módulo Format Converter.
    Convierte imágenes entre formatos optimizando espacio y preservando metadata.
    """
    
    def __init__(self):
        super().__init__()
        self._name = "Format Converter"
        self._description = "Convierte imágenes a WebP/PNG/JPEG preservando metadata."
        self._icon = "🔄"
        self.accent_color = "#8be9fd"  # Cyan
        
        self.view = None
        self.source_folder = None
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
        """Área principal con previsualización y log."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)
        
        # Header
        header = QLabel("FORMAT CONVERTER")
        header.setStyleSheet(f"color: {self.accent_color}; font-size: 32px; font-weight: bold;")
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)
        
        subtitle = QLabel(self.tr("fc.subtitle", "Batch convert images while preserving AI metadata"))
        subtitle.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 14px;")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)
        
        layout.addSpacing(20)
        
        # Stats panel
        stats_frame = QFrame()
        stats_frame.setStyleSheet(f"""
            QFrame {{
                background: {Theme.BG_PANEL};
                border-radius: 10px;
                padding: 15px;
            }}
        """)
        stats_layout = QHBoxLayout(stats_frame)
        
        # File count
        stat1 = self._create_stat_widget("📁", "0", self.tr("fc.files", "Files"))
        self.lbl_file_count = stat1.findChild(QLabel, "value")
        stats_layout.addWidget(stat1)
        
        # Size before
        stat2 = self._create_stat_widget("📊", "0 MB", self.tr("fc.original", "Original"))
        self.lbl_size_before = stat2.findChild(QLabel, "value")
        stats_layout.addWidget(stat2)
        
        # Size after
        stat3 = self._create_stat_widget("✨", "0 MB", self.tr("fc.new", "Converted"))
        self.lbl_size_after = stat3.findChild(QLabel, "value")
        stats_layout.addWidget(stat3)
        
        # Savings
        stat4 = self._create_stat_widget("💾", "0%", self.tr("fc.saved", "Saved"))
        self.lbl_savings = stat4.findChild(QLabel, "value")
        stats_layout.addWidget(stat4)
        
        layout.addWidget(stats_frame)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                background: {Theme.BG_INPUT};
                border: 1px solid {Theme.BORDER};
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
        
        # Current file label
        self.lbl_current_file = QLabel("")
        self.lbl_current_file.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 11px;")
        self.lbl_current_file.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_current_file)
        
        # Log output
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet(f"""
            QTextEdit {{
                background: {Theme.BG_INPUT};
                color: {Theme.TEXT_SECONDARY};
                border: 1px solid {Theme.BORDER};
                border-radius: 8px;
                padding: 10px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }}
        """)
        self.txt_log.setPlaceholderText(self.tr("fc.log_placeholder", "Conversion log will appear here..."))
        layout.addWidget(self.txt_log, 1)
        
        return container
    
    def _create_stat_widget(self, icon: str, value: str, label: str) -> QWidget:
        """Crea un widget de estadística."""
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
        lbl_label.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
        lbl_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_label)
        
        return widget
    
    def _create_sidebar(self) -> QWidget:
        """Panel lateral con controles."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(12)
        
        # Title
        lbl_title = QLabel(self.tr("fc.title", "🔄 FORMAT CONVERTER"))
        lbl_title.setStyleSheet(f"color: {self.accent_color}; font-weight: bold; font-size: 14px;")
        layout.addWidget(lbl_title)
        
        # Section: Source
        layout.addWidget(self._section_label(self.tr("fc.source", "📂 1. SOURCE")))
        
        self.btn_select_folder = QPushButton(self.tr("fc.select_folder", "📁 Select Folder"))
        self.btn_select_folder.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000"))
        self.btn_select_folder.setFixedHeight(36)
        self.btn_select_folder.clicked.connect(self.select_source_folder)
        layout.addWidget(self.btn_select_folder)
        
        self.lbl_folder = QLabel(self.tr("fc.no_folder", "No folder selected"))
        self.lbl_folder.setStyleSheet(f"color: {Theme.TEXT_DIM}; font-size: 10px;")
        self.lbl_folder.setWordWrap(True)
        layout.addWidget(self.lbl_folder)
        
        self.chk_recursive = QCheckBox(self.tr("fc.recursive", "Include subfolders"))
        self.chk_recursive.setChecked(True)
        self.chk_recursive.setStyleSheet(f"color: {Theme.TEXT_SECONDARY};")
        self.chk_recursive.stateChanged.connect(self.rescan_folder)
        layout.addWidget(self.chk_recursive)
        
        layout.addSpacing(10)
        
        # Section: Format
        layout.addWidget(self._section_label(self.tr("fc.format", "🎯 2. FORMAT")))
        
        layout.addWidget(QLabel(self.tr("fc.target_format", "Target Format:")))
        self.combo_format = QComboBox()
        self.combo_format.addItems(["PNG (Standard)", "WebP (Legacy)", "JPEG (Lossy)"])
        self.combo_format.setStyleSheet(Theme.get_input_style(self.accent_color))
        self.combo_format.currentIndexChanged.connect(self.on_format_changed)
        layout.addWidget(self.combo_format)
        
        # Quality slider (for lossy formats)
        self.quality_container = QWidget()
        quality_layout = QVBoxLayout(self.quality_container)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        
        self.lbl_quality = QLabel(self.tr("fc.quality", "Quality: 90%"))
        self.lbl_quality.setStyleSheet(f"color: {Theme.TEXT_SECONDARY};")
        quality_layout.addWidget(self.lbl_quality)
        
        self.slider_quality = QSlider(Qt.Horizontal)
        self.slider_quality.setRange(50, 100)
        self.slider_quality.setValue(90)
        self.slider_quality.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {Theme.BG_INPUT};
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
            lambda v: self.lbl_quality.setText(self.tr("fc.quality", f"Quality: {v}%"))
        )
        quality_layout.addWidget(self.slider_quality)
        
        layout.addWidget(self.quality_container)
        
        layout.addSpacing(10)
        
        # Section: Options
        layout.addWidget(self._section_label(self.tr("fc.options", "⚙️ 3. OPTIONS")))
        
        self.chk_preserve_meta = QCheckBox(self.tr("fc.preserve_meta", "Preserve AI metadata"))
        self.chk_preserve_meta.setChecked(True)
        self.chk_preserve_meta.setStyleSheet(f"color: {Theme.TEXT_SECONDARY};")
        layout.addWidget(self.chk_preserve_meta)
        
        self.chk_verify = QCheckBox(self.tr("fc.verify", "Verify metadata after"))
        self.chk_verify.setChecked(True)
        self.chk_verify.setStyleSheet(f"color: {Theme.TEXT_SECONDARY};")
        layout.addWidget(self.chk_verify)
        
        layout.addStretch()
        
        # Convert button
        self.btn_convert = QPushButton(self.tr("fc.convert", "🚀 START CONVERSION"))
        self.btn_convert.setStyleSheet(Theme.get_action_button_style(self.accent_color, "#000"))
        self.btn_convert.setFixedHeight(48)
        self.btn_convert.clicked.connect(self.start_conversion)
        self.btn_convert.setEnabled(False)
        layout.addWidget(self.btn_convert)
        
        return container
    
    def _section_label(self, text: str) -> QLabel:
        """Crea etiqueta de sección."""
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {Theme.TEXT_SECONDARY}; font-weight: bold; font-size: 12px;")
        return lbl
    
    def select_source_folder(self):
        """Selecciona carpeta fuente."""
        folder = QFileDialog.getExistingDirectory(None, self.tr("fc.select_folder", "Select Source Folder"))
        if folder:
            self.source_folder = Path(folder)
            self.lbl_folder.setText(folder)
            self.rescan_folder()
    
    def rescan_folder(self):
        """Vuelve a escanear la carpeta."""
        if not self.source_folder:
            return
        
        format_map = {0: "WEBP", 1: "PNG", 2: "JPEG"}
        target_format = format_map.get(self.combo_format.currentIndex(), "WEBP")
        
        self.files_to_convert = scan_folder_for_conversion(
            self.source_folder,
            target_format=target_format,
            recursive=self.chk_recursive.isChecked()
        )
        
        count = len(self.files_to_convert)
        self.lbl_file_count.setText(str(count))
        self.btn_convert.setEnabled(count > 0)
        
        self.log(f"Found {count} files to convert")
    
    def on_format_changed(self, index):
        """Actualiza UI según formato seleccionado."""
        # Show quality only for lossy formats
        self.quality_container.setVisible(index != 1)  # PNG is lossless
        self.rescan_folder()
    
    def log(self, message: str):
        """Añade mensaje al log."""
        self.txt_log.append(message)
    
    def start_conversion(self):
        """Inicia la conversión batch."""
        if not self.files_to_convert:
            return
        
        # Get output directory
        output_dir = QFileDialog.getExistingDirectory(
            None, 
            self.tr("fc.select_output", "Select Output Folder"),
            str(self.source_folder)
        )
        if not output_dir:
            return
        
        format_map = {0: "WEBP", 1: "PNG", 2: "JPEG"}
        target_format = format_map.get(self.combo_format.currentIndex(), "WEBP")
        
        # Disable UI
        self.btn_convert.setEnabled(False)
        self.btn_select_folder.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(self.files_to_convert))
        self.progress_bar.setValue(0)
        
        self.log(f"\n{'='*40}")
        self.log(f"Starting conversion: {len(self.files_to_convert)} files → {target_format}")
        self.log(f"Output: {output_dir}")
        self.log(f"{'='*40}\n")
        
        # Create worker
        self.worker = ConversionWorker(
            self.files_to_convert,
            output_dir,
            target_format,
            self.slider_quality.value(),
            self.chk_preserve_meta.isChecked()
        )
        self.worker_thread = QThread()
        self.worker.moveToThread(self.worker_thread)
        
        # Connect signals
        self.worker_thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_conversion_finished)
        self.worker.error.connect(self.on_conversion_error)
        
        self.worker_thread.start()
    
    def on_progress(self, current, total, filename):
        """Actualiza progreso."""
        self.progress_bar.setValue(current)
        self.lbl_current_file.setText(f"Converting: {filename}")
    
    def on_conversion_finished(self, report: BatchConversionReport):
        """Conversión completada."""
        self.worker_thread.quit()
        self.worker_thread.wait()
        
        # Update stats
        self.lbl_size_before.setText(f"{report.total_original_bytes / 1024 / 1024:.1f} MB")
        self.lbl_size_after.setText(f"{report.total_new_bytes / 1024 / 1024:.1f} MB")
        self.lbl_savings.setText(f"{report.compression_ratio:.1f}%")
        
        # Log results
        self.log(f"\n{'='*40}")
        self.log(f"CONVERSION COMPLETE")
        self.log(f"{'='*40}")
        self.log(report.get_summary())
        
        if report.failed_files:
            self.log(f"\nFailed files:")
            for path, error in report.failed_files[:10]:
                self.log(f"  ❌ {Path(path).name}: {error}")
            if len(report.failed_files) > 10:
                self.log(f"  ... and {len(report.failed_files) - 10} more")
        
        # Verify if enabled
        if self.chk_verify.isChecked() and report.results:
            self.log(f"\nVerifying metadata integrity...")
            try:
                verify_report = verify_batch_conversion(report)
                verified_count = verify_report.ok_count + verify_report.repaired_count
                self.log(f"  Verified: {verified_count}/{verify_report.total_files}")
                self.log(f"  Integrity: {verify_report.avg_integrity:.1f}%")
            except Exception as e:
                self.log(f"  Verification error: {e}")
        
        # Re-enable UI
        self.btn_convert.setEnabled(True)
        self.btn_select_folder.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.lbl_current_file.setText("")
        
        QMessageBox.information(
            None,
            self.tr("fc.complete", "Conversion Complete"),
            self.tr("fc.complete_msg", "Converted {count} files.\nSaved {saved:.1f} MB ({ratio:.1f}%)").format(
                count=report.converted_count,
                saved=report.total_saved_bytes / 1024 / 1024,
                ratio=report.compression_ratio
            )
        )
    
    def on_conversion_error(self, error):
        """Error durante conversión."""
        self.worker_thread.quit()
        self.worker_thread.wait()
        
        self.log(f"\n❌ ERROR: {error}")
        
        self.btn_convert.setEnabled(True)
        self.btn_select_folder.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        QMessageBox.critical(None, "Error", f"Conversion failed: {error}")
