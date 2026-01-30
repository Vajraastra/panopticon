"""
MetadataVerifier: Verificación de integridad de metadata entre archivos.
Compara metadata de origen vs destino después de operaciones de copia.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from .bundle import MetadataBundle
from .extractor import MetadataExtractor


@dataclass
class VerificationResult:
    """
    Resultado de verificación de metadata entre dos archivos.
    
    Attributes:
        success: True si la verificación fue exitosa (metadata preservada)
        source_path: Ruta al archivo original
        dest_path: Ruta al archivo verificado
        status: Estado de la verificación (OK, WARNING, FAIL)
        integrity_score: Porcentaje de metadata preservada (0-100)
        checks: Diccionario con resultados por campo
        missing: Lista de campos que faltan en destino
        changed: Diccionario de campos que cambiaron
    """
    success: bool
    source_path: str
    dest_path: str
    status: Literal["OK", "WARNING", "FAIL"] = "OK"
    integrity_score: float = 100.0
    checks: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    changed: dict = field(default_factory=dict)
    error: Optional[str] = None
    
    def __str__(self) -> str:
        if self.status == "OK":
            return f"✅ {self.integrity_score}% - {Path(self.dest_path).name}"
        elif self.status == "WARNING":
            return f"⚠️ {self.integrity_score}% - {Path(self.dest_path).name} (missing: {', '.join(self.missing)})"
        else:
            return f"❌ FAIL - {Path(self.dest_path).name}: {self.error or 'Unknown error'}"


class MetadataVerifier:
    """
    Verifica la integridad de metadata después de operaciones de copia.
    
    Uso:
        # Verificar transferencia
        result = MetadataVerifier.verify_transfer("original.png", "copy.png")
        
        if result.status == "OK":
            print("Metadata preserved!")
        elif result.status == "WARNING":
            print(f"Some fields missing: {result.missing}")
        else:
            print(f"Verification failed: {result.error}")
    """
    
    # Campos críticos que deben preservarse
    CRITICAL_FIELDS = {'positive_prompt', 'negative_prompt', 'tags'}
    
    # Campos importantes pero no críticos
    IMPORTANT_FIELDS = {'rating', 'quality_score', 'model', 'seed'}
    
    # Campos informativos
    OPTIONAL_FIELDS = {'steps', 'cfg', 'sampler', 'vae', 'tool', 'loras'}
    
    @classmethod
    def verify_transfer(cls, source: str | Path, dest: str | Path) -> VerificationResult:
        """
        Verifica que la metadata se transfirió correctamente de source a dest.
        
        Args:
            source: Ruta al archivo original
            dest: Ruta al archivo destino
        
        Returns:
            VerificationResult con el estado de la verificación
        """
        source = Path(source)
        dest = Path(dest)
        
        result = VerificationResult(
            success=False,
            source_path=str(source),
            dest_path=str(dest)
        )
        
        # Verificar que ambos archivos existen
        if not source.exists():
            result.status = "FAIL"
            result.error = f"Source file not found: {source}"
            return result
        
        if not dest.exists():
            result.status = "FAIL"
            result.error = f"Destination file not found: {dest}"
            return result
        
        try:
            # Extraer metadata de ambos archivos
            source_bundle = MetadataExtractor.extract(source)
            dest_bundle = MetadataExtractor.extract(dest)
            
            # Comparar
            return cls.compare(source_bundle, dest_bundle, str(source), str(dest))
        
        except Exception as e:
            result.status = "FAIL"
            result.error = str(e)
            return result
    
    @classmethod
    def compare(cls, source: MetadataBundle, dest: MetadataBundle,
                source_path: str = "", dest_path: str = "") -> VerificationResult:
        """
        Compara dos MetadataBundles y genera un resultado de verificación.
        
        Args:
            source: Bundle del archivo original
            dest: Bundle del archivo destino
            source_path: Ruta para referencia (opcional)
            dest_path: Ruta para referencia (opcional)
        
        Returns:
            VerificationResult con detalles de la comparación
        """
        result = VerificationResult(
            success=True,
            source_path=source_path,
            dest_path=dest_path,
            status="OK"
        )
        
        # Si el source no tiene metadata válida, skip (no hay nada que verificar)
        if not source.is_valid():
            result.integrity_score = 100.0
            result.checks["source_empty"] = True
            return result
        
        # Calcular diferencias
        differences = source.compare(dest)
        
        # Categorizar diferencias
        missing = []
        changed = {}
        
        for field_name, (src_val, dest_val) in differences.items():
            # Si el source tiene valor pero dest no, es missing
            if src_val and not dest_val:
                missing.append(field_name)
            # Si ambos tienen valor pero son diferentes, es changed
            elif src_val != dest_val:
                changed[field_name] = (src_val, dest_val)
        
        result.missing = missing
        result.changed = changed
        
        # Calcular integrity score
        result.integrity_score = source.integrity_score(dest)
        
        # Determinar status
        critical_missing = set(missing) & cls.CRITICAL_FIELDS
        important_missing = set(missing) & cls.IMPORTANT_FIELDS
        
        if critical_missing:
            result.status = "FAIL"
            result.success = False
            result.error = f"Critical fields missing: {', '.join(critical_missing)}"
        elif important_missing or result.integrity_score < 90:
            result.status = "WARNING"
            result.success = True  # Still success, just with warnings
        else:
            result.status = "OK"
            result.success = True
        
        # Detalles de checks
        all_fields = cls.CRITICAL_FIELDS | cls.IMPORTANT_FIELDS | cls.OPTIONAL_FIELDS
        for field_name in all_fields:
            src_val = getattr(source, field_name, None)
            dest_val = getattr(dest, field_name, None)
            
            if field_name == 'tags':
                match = set(src_val or []) == set(dest_val or [])
            elif field_name == 'loras':
                match = set(src_val or []) == set(dest_val or [])
            else:
                match = src_val == dest_val
            
            result.checks[field_name] = {
                "source": src_val,
                "dest": dest_val,
                "match": match
            }
        
        return result
    
    @classmethod
    def quick_check(cls, source: str | Path, dest: str | Path) -> bool:
        """
        Verificación rápida: solo retorna True/False.
        Útil para checks en loops donde no necesitas detalles.
        
        Args:
            source: Ruta al archivo original
            dest: Ruta al archivo destino
        
        Returns:
            True si la metadata se preservó correctamente
        """
        result = cls.verify_transfer(source, dest)
        return result.status in ("OK", "WARNING")
