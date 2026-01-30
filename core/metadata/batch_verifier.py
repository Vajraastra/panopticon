"""
BatchVerifier: Verificación y reparación de metadata para operaciones batch.
Diseñado para manejar 100K+ imágenes con auto-reparación.
"""
import os
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Optional

from .bundle import MetadataBundle
from .extractor import MetadataExtractor
from .stamper import MetadataStamper
from .verifier import MetadataVerifier, VerificationResult


@dataclass
class FileVerificationResult:
    """
    Resultado de verificación para un archivo individual.
    Incluye información sobre intentos de reparación.
    """
    original_path: str
    copy_path: str
    status: Literal["OK", "REPAIRED", "FAILED"] = "OK"
    integrity_score: float = 100.0
    issues: list = field(default_factory=list)
    repair_attempted: bool = False
    repair_success: bool = False
    error: Optional[str] = None
    
    def __str__(self) -> str:
        icon = {"OK": "✅", "REPAIRED": "🔧", "FAILED": "❌"}[self.status]
        return f"{icon} {self.status} ({self.integrity_score}%) - {Path(self.copy_path).name}"


@dataclass
class BatchVerificationReport:
    """
    Reporte completo de verificación batch.
    Incluye estadísticas, lista de resultados y métodos de exportación.
    """
    total_files: int = 0
    ok_count: int = 0
    repaired_count: int = 0
    failed_count: int = 0
    
    results: list = field(default_factory=list)
    
    # Stats calculadas
    avg_integrity: float = 100.0
    processing_time_seconds: float = 0.0
    
    # Directorios
    source_dir: str = ""
    dest_dir: str = ""
    
    # Timestamp
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    @property
    def safe_to_cleanup(self) -> bool:
        """True si no hay errores y es seguro proceder con cleanup."""
        return self.failed_count == 0
    
    @property 
    def success_rate(self) -> float:
        """Porcentaje de archivos procesados exitosamente."""
        if self.total_files == 0:
            return 100.0
        return ((self.ok_count + self.repaired_count) / self.total_files) * 100
    
    def get_ok_files(self) -> list[str]:
        """Retorna lista de paths de archivos OK."""
        return [r.copy_path for r in self.results if r.status == "OK"]
    
    def get_repaired_files(self) -> list[str]:
        """Retorna lista de paths de archivos reparados."""
        return [r.copy_path for r in self.results if r.status == "REPAIRED"]
    
    def get_failed_files(self) -> list[str]:
        """Retorna lista de paths de archivos fallidos."""
        return [r.copy_path for r in self.results if r.status == "FAILED"]
    
    def export_log(self, path: str | Path) -> bool:
        """
        Exporta el reporte como archivo de texto.
        
        Args:
            path: Ruta donde guardar el log
        
        Returns:
            True si se guardó exitosamente
        """
        path = Path(path)
        
        try:
            lines = [
                "=" * 60,
                "BATCH VERIFICATION REPORT",
                "=" * 60,
                f"Date: {self.timestamp}",
                f"Source: {self.source_dir}",
                f"Output: {self.dest_dir}",
                "",
                "SUMMARY",
                "-" * 30,
                f"Total: {self.total_files}",
                f"OK: {self.ok_count} ({self.ok_count/max(1,self.total_files)*100:.1f}%)",
                f"Repaired: {self.repaired_count} ({self.repaired_count/max(1,self.total_files)*100:.1f}%)",
                f"Failed: {self.failed_count} ({self.failed_count/max(1,self.total_files)*100:.1f}%)",
                f"Average Integrity: {self.avg_integrity:.1f}%",
                f"Processing Time: {self.processing_time_seconds:.1f}s",
                "",
            ]
            
            if self.repaired_count > 0:
                lines.extend([
                    "REPAIRED FILES",
                    "-" * 30,
                ])
                for r in self.results:
                    if r.status == "REPAIRED":
                        issues = ", ".join(r.issues) if r.issues else "Unknown"
                        lines.append(f"  {Path(r.copy_path).name} - {issues}")
                lines.append("")
            
            if self.failed_count > 0:
                lines.extend([
                    "FAILED FILES",
                    "-" * 30,
                ])
                for r in self.results:
                    if r.status == "FAILED":
                        lines.append(f"  {r.original_path} -> {r.copy_path}")
                        lines.append(f"    Error: {r.error or 'Unknown'}")
                lines.append("")
            
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            
            return True
        
        except Exception as e:
            print(f"[BatchVerifier] Error exporting log: {e}")
            return False
    
    def export_csv(self, path: str | Path) -> bool:
        """
        Exporta el reporte como CSV.
        
        Args:
            path: Ruta donde guardar el CSV
        
        Returns:
            True si se guardó exitosamente
        """
        path = Path(path)
        
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Status", "Original", "Copy", "Integrity", 
                    "Issues", "Repaired", "Error"
                ])
                
                for r in self.results:
                    writer.writerow([
                        r.status,
                        r.original_path,
                        r.copy_path,
                        f"{r.integrity_score:.1f}%",
                        "; ".join(r.issues),
                        "Yes" if r.repair_success else "No",
                        r.error or ""
                    ])
            
            return True
        
        except Exception as e:
            print(f"[BatchVerifier] Error exporting CSV: {e}")
            return False


class BatchVerifier:
    """
    Sistema de verificación batch con auto-reparación.
    
    Diseñado para:
    - Verificar 100K+ archivos
    - Auto-reparar metadata cuando sea posible
    - Generar reportes detallados
    - Proporcionar estadísticas para decisión de cleanup
    
    Uso:
        verifier = BatchVerifier(source_dir, dest_dir)
        verifier.find_pairs()
        report = verifier.verify_all(progress_callback=update_ui)
        
        if report.safe_to_cleanup:
            cleanup_result = BatchVerifier.cleanup(report, delete_originals=True)
    """
    
    def __init__(self, original_dir: str | Path, copy_dir: str | Path):
        """
        Inicializa el verificador.
        
        Args:
            original_dir: Directorio con archivos originales
            copy_dir: Directorio con copias a verificar
        """
        self.original_dir = Path(original_dir)
        self.copy_dir = Path(copy_dir)
        self.pairs: list[tuple[Path, Path]] = []
        
        # Extensiones soportadas
        self.supported_extensions = {'.png', '.jpg', '.jpeg', '.webp'}
    
    def find_pairs(self) -> int:
        """
        Encuentra pares de archivos original/copia por nombre.
        
        Returns:
            Número de pares encontrados
        """
        self.pairs = []
        
        if not self.original_dir.exists() or not self.copy_dir.exists():
            return 0
        
        # Indexar copias por stem (nombre sin extensión)
        copy_index = {}
        for copy_file in self.copy_dir.rglob("*"):
            if copy_file.is_file() and copy_file.suffix.lower() in self.supported_extensions:
                copy_index[copy_file.stem.lower()] = copy_file
        
        # Buscar originales y emparejar
        for orig_file in self.original_dir.rglob("*"):
            if orig_file.is_file() and orig_file.suffix.lower() in self.supported_extensions:
                stem = orig_file.stem.lower()
                if stem in copy_index:
                    self.pairs.append((orig_file, copy_index[stem]))
        
        return len(self.pairs)
    
    def add_pair(self, original: str | Path, copy: str | Path):
        """Añade un par manualmente."""
        self.pairs.append((Path(original), Path(copy)))
    
    def verify_all(self, progress_callback: Optional[Callable[[int, int], None]] = None,
                   auto_repair: bool = True) -> BatchVerificationReport:
        """
        Verifica todos los pares encontrados.
        
        Args:
            progress_callback: Función callback(current, total) para progreso
            auto_repair: Si True, intenta reparar metadata cuando falla
        
        Returns:
            BatchVerificationReport con todos los resultados
        """
        import time
        start_time = time.time()
        
        report = BatchVerificationReport(
            total_files=len(self.pairs),
            source_dir=str(self.original_dir),
            dest_dir=str(self.copy_dir)
        )
        
        integrity_sum = 0.0
        
        for i, (orig, copy) in enumerate(self.pairs):
            if progress_callback:
                progress_callback(i + 1, len(self.pairs))
            
            result = self._verify_single(orig, copy, auto_repair)
            report.results.append(result)
            
            if result.status == "OK":
                report.ok_count += 1
            elif result.status == "REPAIRED":
                report.repaired_count += 1
            else:
                report.failed_count += 1
            
            integrity_sum += result.integrity_score
        
        # Calcular promedios
        if report.total_files > 0:
            report.avg_integrity = integrity_sum / report.total_files
        
        report.processing_time_seconds = time.time() - start_time
        
        return report
    
    def _verify_single(self, orig: Path, copy: Path, 
                       auto_repair: bool) -> FileVerificationResult:
        """
        Verifica un par de archivos.
        
        Args:
            orig: Path al archivo original
            copy: Path a la copia
            auto_repair: Si True, intenta reparar si falla
        
        Returns:
            FileVerificationResult
        """
        result = FileVerificationResult(
            original_path=str(orig),
            copy_path=str(copy)
        )
        
        try:
            # Verificar transferencia
            verification = MetadataVerifier.verify_transfer(orig, copy)
            
            if verification.status == "OK":
                result.status = "OK"
                result.integrity_score = verification.integrity_score
                return result
            
            # Hay problemas - intentar reparar si está habilitado
            result.issues = verification.missing.copy()
            result.integrity_score = verification.integrity_score
            
            if auto_repair and verification.status in ("WARNING", "FAIL"):
                result.repair_attempted = True
                
                # Intentar re-transferir metadata
                transfer_result = MetadataStamper.transfer(orig, copy)
                
                if transfer_result.success:
                    # Verificar de nuevo
                    re_verification = MetadataVerifier.verify_transfer(orig, copy)
                    
                    if re_verification.status == "OK":
                        result.status = "REPAIRED"
                        result.repair_success = True
                        result.integrity_score = re_verification.integrity_score
                        return result
                
                # Reparación falló
                result.status = "FAILED"
                result.error = verification.error or "Repair failed"
            else:
                result.status = "FAILED" if verification.status == "FAIL" else "OK"
                result.error = verification.error
            
            return result
        
        except Exception as e:
            result.status = "FAILED"
            result.error = str(e)
            return result
    
    @staticmethod
    def cleanup(report: BatchVerificationReport,
                delete_originals: bool = False,
                move_copies: bool = False,
                failed_dir: Optional[str | Path] = None) -> dict:
        """
        Realiza limpieza post-verificación.
        Solo actúa sobre archivos OK y REPAIRED. FAILED nunca se tocan.
        
        Args:
            report: Reporte de verificación
            delete_originals: Si True, elimina originales (solo OK/REPAIRED)
            move_copies: Si True, mueve copias a ubicación original
            failed_dir: Directorio donde mover archivos fallidos
        
        Returns:
            dict con estadísticas de la operación
        """
        import shutil
        
        stats = {
            "originals_deleted": 0,
            "copies_moved": 0,
            "failed_preserved": 0,
            "errors": []
        }
        
        # Manejar archivos fallidos primero
        if failed_dir:
            failed_path = Path(failed_dir)
            failed_path.mkdir(parents=True, exist_ok=True)
            
            for r in report.results:
                if r.status == "FAILED":
                    try:
                        # Mover original a failed/
                        orig = Path(r.original_path)
                        if orig.exists():
                            dest = failed_path / orig.name
                            shutil.move(str(orig), str(dest))
                            stats["failed_preserved"] += 1
                    except Exception as e:
                        stats["errors"].append(f"Failed to preserve {r.original_path}: {e}")
        
        # Procesar archivos exitosos
        for r in report.results:
            if r.status in ("OK", "REPAIRED"):
                try:
                    orig = Path(r.original_path)
                    copy = Path(r.copy_path)
                    
                    if move_copies and copy.exists() and orig.parent.exists():
                        # Mover copia a ubicación del original
                        new_path = orig.parent / copy.name
                        shutil.move(str(copy), str(new_path))
                        stats["copies_moved"] += 1
                    
                    if delete_originals and orig.exists():
                        orig.unlink()
                        stats["originals_deleted"] += 1
                
                except Exception as e:
                    stats["errors"].append(f"Cleanup error for {r.original_path}: {e}")
        
        return stats
