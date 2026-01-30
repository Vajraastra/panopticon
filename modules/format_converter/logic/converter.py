"""
Format Converter Logic - Conversión masiva de imágenes a WebP.
Usa el nuevo sistema de metadata centralizado para preservar prompts.
"""
import os
from pathlib import Path
from typing import Callable, Optional, Literal
from dataclasses import dataclass, field
from PIL import Image

from core.paths import CachePaths
from core.metadata import (
    MetadataExtractor, 
    MetadataStamper, 
    MetadataVerifier,
    BatchVerifier,
    BatchVerificationReport
)


@dataclass
class ConversionResult:
    """Resultado de conversión de un archivo."""
    source_path: str
    output_path: str
    success: bool
    original_size: int = 0
    new_size: int = 0
    saved_bytes: int = 0
    saved_percent: float = 0.0
    metadata_preserved: bool = True
    error: Optional[str] = None


@dataclass
class BatchConversionReport:
    """Reporte de conversión batch."""
    total_files: int = 0
    converted_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    
    total_original_bytes: int = 0
    total_new_bytes: int = 0
    total_saved_bytes: int = 0
    
    results: list = field(default_factory=list)
    failed_files: list = field(default_factory=list)
    
    output_dir: str = ""
    
    @property
    def success_rate(self) -> float:
        if self.total_files == 0:
            return 100.0
        return (self.converted_count / self.total_files) * 100
    
    @property
    def compression_ratio(self) -> float:
        if self.total_original_bytes == 0:
            return 0.0
        return (self.total_saved_bytes / self.total_original_bytes) * 100
    
    def get_summary(self) -> str:
        """Genera resumen de texto."""
        lines = [
            f"Total: {self.total_files} files",
            f"Converted: {self.converted_count}",
            f"Failed: {self.failed_count}",
            f"Skipped: {self.skipped_count}",
            "",
            f"Original size: {self.total_original_bytes / 1024 / 1024:.1f} MB",
            f"New size: {self.total_new_bytes / 1024 / 1024:.1f} MB",
            f"Saved: {self.total_saved_bytes / 1024 / 1024:.1f} MB ({self.compression_ratio:.1f}%)",
        ]
        return "\n".join(lines)


def convert_single(source_path: str | Path, 
                   output_path: str | Path = None,
                   target_format: Literal["WEBP", "PNG", "JPEG"] = "WEBP",
                   quality: int = 90,
                   preserve_metadata: bool = True) -> ConversionResult:
    """
    Convierte una imagen a otro formato preservando metadata.
    
    Args:
        source_path: Ruta al archivo original
        output_path: Ruta de salida (genera automáticamente si None)
        target_format: Formato destino (WEBP, PNG, JPEG)
        quality: Calidad para formatos lossy (1-100)
        preserve_metadata: Si True, transfiere metadata
    
    Returns:
        ConversionResult con estadísticas
    """
    source_path = Path(source_path)
    
    result = ConversionResult(
        source_path=str(source_path),
        output_path="",
        success=False
    )
    
    if not source_path.exists():
        result.error = f"File not found: {source_path}"
        return result
    
    # Generate output path
    ext_map = {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg"}
    target_ext = ext_map.get(target_format.upper(), ".webp")
    
    if output_path is None:
        output_dir = CachePaths.get_tool_cache("format_converter")
        output_path = output_dir / (source_path.stem + target_ext)
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    
    result.output_path = str(output_path)
    
    try:
        # Extract metadata from source
        source_bundle = None
        if preserve_metadata:
            source_bundle = MetadataExtractor.extract(source_path)
        
        # Load and convert image
        img = Image.open(source_path)
        original_format = img.format
        
        # Skip if already in target format and no conversion needed
        if source_path.suffix.lower() == target_ext.lower():
            result.error = "Already in target format"
            result.success = False
            return result
        
        # Prepare save arguments
        save_kwargs = {}
        
        if target_format.upper() == "WEBP":
            save_kwargs["quality"] = quality
            save_kwargs["method"] = 6  # Best compression
            if img.mode == "P":
                img = img.convert("RGBA")
        
        elif target_format.upper() == "PNG":
            save_kwargs["optimize"] = True
            save_kwargs["compress_level"] = 9
            if img.mode == "P" and "transparency" in img.info:
                img = img.convert("RGBA")
        
        elif target_format.upper() == "JPEG":
            save_kwargs["quality"] = quality
            save_kwargs["optimize"] = True
            if img.mode in ("RGBA", "P"):
                # Create white background
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    rgb_img.paste(img, mask=img.split()[3])
                else:
                    rgb_img.paste(img)
                img = rgb_img
        
        # Save converted image
        img.save(str(output_path), **save_kwargs)
        img.close()
        
        # Transfer metadata
        if preserve_metadata and source_bundle and source_bundle.is_valid():
            MetadataStamper.stamp(output_path, source_bundle)
            result.metadata_preserved = True
        
        # Calculate stats
        result.original_size = source_path.stat().st_size
        result.new_size = output_path.stat().st_size
        result.saved_bytes = result.original_size - result.new_size
        result.saved_percent = (result.saved_bytes / result.original_size) * 100 if result.original_size > 0 else 0
        result.success = True
        
        return result
    
    except Exception as e:
        result.error = str(e)
        return result


def convert_batch(files: list[str | Path],
                  output_dir: str | Path = None,
                  target_format: Literal["WEBP", "PNG", "JPEG"] = "WEBP",
                  quality: int = 90,
                  preserve_metadata: bool = True,
                  skip_existing: bool = True,
                  progress_callback: Optional[Callable[[int, int, str], None]] = None) -> BatchConversionReport:
    """
    Convierte múltiples archivos en batch.
    
    Args:
        files: Lista de rutas de archivos
        output_dir: Directorio de salida
        target_format: Formato destino
        quality: Calidad para formatos lossy
        preserve_metadata: Preservar metadata
        skip_existing: Saltar si ya existe el archivo
        progress_callback: Función callback(current, total, filename)
    
    Returns:
        BatchConversionReport con estadísticas completas
    """
    if output_dir is None:
        output_dir = CachePaths.get_tool_cache("format_converter")
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    ext_map = {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg"}
    target_ext = ext_map.get(target_format.upper(), ".webp")
    
    report = BatchConversionReport(
        total_files=len(files),
        output_dir=str(output_dir)
    )
    
    for i, source in enumerate(files):
        source = Path(source)
        output_path = output_dir / (source.stem + target_ext)
        
        if progress_callback:
            progress_callback(i + 1, len(files), source.name)
        
        # Skip if exists
        if skip_existing and output_path.exists():
            report.skipped_count += 1
            continue
        
        # Skip if already in target format
        if source.suffix.lower() == target_ext.lower():
            report.skipped_count += 1
            continue
        
        # Convert
        result = convert_single(
            source, output_path,
            target_format=target_format,
            quality=quality,
            preserve_metadata=preserve_metadata
        )
        
        if result.success:
            report.converted_count += 1
            report.total_original_bytes += result.original_size
            report.total_new_bytes += result.new_size
            report.results.append(result)
        else:
            report.failed_count += 1
            report.failed_files.append((str(source), result.error))
    
    report.total_saved_bytes = report.total_original_bytes - report.total_new_bytes
    
    return report


def verify_batch_conversion(report: BatchConversionReport,
                            progress_callback: Optional[Callable[[int, int], None]] = None) -> BatchVerificationReport:
    """
    Verifica la integridad de metadata después de conversión batch.
    
    Args:
        report: Reporte de conversión batch
        progress_callback: Callback de progreso
    
    Returns:
        BatchVerificationReport con resultados de verificación
    """
    # Create pairs for verification
    verifier = BatchVerifier(
        original_dir=Path(report.results[0].source_path).parent if report.results else Path("."),
        copy_dir=Path(report.output_dir)
    )
    
    # Add pairs manually from conversion results
    for result in report.results:
        if result.success:
            verifier.add_pair(result.source_path, result.output_path)
    
    return verifier.verify_all(progress_callback=progress_callback)


def scan_folder_for_conversion(folder: str | Path,
                               target_format: str = "WEBP",
                               recursive: bool = True) -> list[Path]:
    """
    Escanea una carpeta para encontrar archivos a convertir.
    
    Args:
        folder: Carpeta a escanear
        target_format: Formato destino (para excluir archivos ya en ese formato)
        recursive: Si True, escanea subcarpetas
    
    Returns:
        Lista de paths de archivos a convertir
    """
    folder = Path(folder)
    
    # Extensions to convert
    source_extensions = {".png", ".jpg", ".jpeg"}
    if target_format.upper() != "WEBP":
        source_extensions.add(".webp")
    if target_format.upper() != "PNG":
        source_extensions.add(".png")
    if target_format.upper() != "JPEG":
        source_extensions.discard(".jpg")
        source_extensions.discard(".jpeg")
    
    # Target extension to skip
    target_ext = {".webp": "WEBP", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
    skip_ext = f".{target_format.lower()}"
    if target_format.upper() == "JPEG":
        skip_ext = ".jpg"
    
    files = []
    pattern = "**/*" if recursive else "*"
    
    for ext in source_extensions:
        for f in folder.glob(f"{pattern}{ext}"):
            if f.is_file():
                files.append(f)
    
    return sorted(files)
