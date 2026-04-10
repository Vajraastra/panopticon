"""
Format Converter Logic - Conversión masiva de imágenes preservando metadata.
Parámetros optimizados para datasets de entrenamiento: lossless o mínima pérdida,
máxima compresión, preservación completa de color (4:4:4).
"""
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Literal
from dataclasses import dataclass, field
from PIL import Image

try:
    import pyoxipng
    _PYOXIPNG_AVAILABLE = True
except ImportError:
    _PYOXIPNG_AVAILABLE = False

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
    ext_map = {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg", "AVIF": ".avif"}
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
        
        # Prepare save arguments — all formats optimized for dataset training:
        # lossless or minimum perceptible loss, full color fidelity (4:4:4).
        save_kwargs = {}

        if target_format.upper() == "WEBP":
            # Lossless WebP: zero quality loss, ~15-34% smaller than PNG.
            # method=6 → best compression algorithm (slower but done once).
            # quality=80 → compression effort level in lossless mode (not quality).
            # exact=True → preserve RGB values in fully-transparent pixels.
            save_kwargs["lossless"] = True
            save_kwargs["quality"] = 80
            save_kwargs["method"] = 6
            save_kwargs["exact"] = True
            if img.mode == "P":
                img = img.convert("RGBA")

        elif target_format.upper() == "PNG":
            save_kwargs["optimize"] = True
            save_kwargs["compress_level"] = 9
            if img.mode == "P" and "transparency" in img.info:
                img = img.convert("RGBA")

        elif target_format.upper() == "JPEG":
            # Fixed high quality + 4:4:4 subsampling: no chroma loss.
            save_kwargs["quality"] = 95
            save_kwargs["optimize"] = True
            save_kwargs["subsampling"] = 0  # 4:4:4 — full chroma fidelity
            if img.mode in ("RGBA", "P"):
                rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "RGBA":
                    rgb_img.paste(img, mask=img.split()[3])
                else:
                    rgb_img.paste(img)
                img = rgb_img

        elif target_format.upper() == "AVIF":
            # Near-lossless: 4:4:4 subsampling preserves full color.
            # quality=95 + speed=4 → best balance of size and fidelity.
            save_kwargs["quality"] = 95
            save_kwargs["subsampling"] = "4:4:4"
            save_kwargs["speed"] = 4
            if img.mode == "P":
                img = img.convert("RGBA")

        # Save converted image
        img.save(str(output_path), **save_kwargs)

        # PNG post-processing: pyoxipng lossless optimizer (10-30% smaller).
        if target_format.upper() == "PNG" and _PYOXIPNG_AVAILABLE:
            pyoxipng.optimize(str(output_path), level=4)
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


def _worker(args: tuple) -> ConversionResult:
    """Worker top-level para ProcessPoolExecutor (debe ser serializable).
    Cada proceso tiene su propio GIL — sin contención entre workers."""
    source, output_path, target_format, preserve_metadata = args
    return convert_single(
        source, output_path,
        target_format=target_format,
        preserve_metadata=preserve_metadata
    )


def convert_batch(files: list[str | Path],
                  output_dir: str | Path = None,
                  target_format: Literal["WEBP", "PNG", "JPEG"] = "WEBP",
                  preserve_metadata: bool = True,
                  skip_existing: bool = True,
                  progress_callback: Optional[Callable[[int, int, str], None]] = None) -> BatchConversionReport:
    """
    Convierte múltiples archivos en batch usando procesos paralelos.
    ProcessPoolExecutor garantiza que cada worker tenga su propio GIL:
    una imagen lenta no bloquea a las demás. Los resultados se recogen
    en orden de finalización (as_completed), no de envío.

    Args:
        files: Lista de rutas de archivos
        output_dir: Directorio de salida
        target_format: Formato destino
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

    ext_map = {"WEBP": ".webp", "PNG": ".png", "JPEG": ".jpg", "AVIF": ".avif"}
    target_ext = ext_map.get(target_format.upper(), ".webp")

    report = BatchConversionReport(
        total_files=len(files),
        output_dir=str(output_dir)
    )

    # Separar archivos que se deben procesar de los que se saltan
    pending = []
    for source in files:
        source = Path(source)
        output_path = output_dir / (source.stem + target_ext)
        if skip_existing and output_path.exists():
            report.skipped_count += 1
        elif source.suffix.lower() == target_ext.lower():
            report.skipped_count += 1
        else:
            pending.append((str(source), str(output_path), target_format, preserve_metadata))

    total = len(files)
    completed = report.skipped_count

    workers = min(os.cpu_count() or 4, 8)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_worker, args): args for args in pending}
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if progress_callback:
                progress_callback(completed, total, Path(result.source_path).name)
            if result.success:
                report.converted_count += 1
                report.total_original_bytes += result.original_size
                report.total_new_bytes += result.new_size
                report.results.append(result)
            else:
                report.failed_count += 1
                report.failed_files.append((result.source_path, result.error))

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
    
    # All supported source extensions; remove only the target format
    source_extensions = {".png", ".jpg", ".jpeg", ".webp", ".avif"}
    if target_format.upper() == "PNG":
        source_extensions.discard(".png")
    elif target_format.upper() == "WEBP":
        source_extensions.discard(".webp")
    elif target_format.upper() == "JPEG":
        source_extensions.discard(".jpg")
        source_extensions.discard(".jpeg")
    elif target_format.upper() == "AVIF":
        source_extensions.discard(".avif")
    
    files = []
    pattern = "**/*" if recursive else "*"
    
    for ext in source_extensions:
        for f in folder.glob(f"{pattern}{ext}"):
            if f.is_file():
                files.append(f)
    
    return sorted(files)
