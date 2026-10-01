import os
import json
import logging
import tempfile
from pathlib import Path
from PIL import Image, PngImagePlugin

log = logging.getLogger(__name__)


def _atomic_save(img, path, format=None, **save_kwargs):
    """Guarda la imagen en un archivo temporal del mismo directorio y
    reemplaza el original con os.replace() solo si el save fue exitoso.
    Evita corromper el original si el proceso muere a media escritura."""
    path = str(path)
    fd, tmp_path = tempfile.mkstemp(
        suffix=os.path.splitext(path)[1],
        dir=os.path.dirname(path) or '.'
    )
    os.close(fd)
    try:
        img.save(tmp_path, format, **save_kwargs)
        # mkstemp crea con permisos 0600; conservar los del original
        os.chmod(tmp_path, os.stat(path).st_mode & 0o777)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)


def _webp_is_lossless(path) -> bool:
    """Detecta si un WebP es lossless leyendo el chunk RIFF.
    VP8L = lossless, VP8  = lossy."""
    try:
        with open(path, 'rb') as f:
            f.seek(12)
            return f.read(4) == b'VP8L'
    except Exception:
        return False


class StampLib:
    """Curación embebida en una salida temporal; nunca edita originales Native."""

    @staticmethod
    def get_payload_json(tags=None, rating=0):
        from .bundle import MetadataBundle
        from .output_stamp import payload, _json
        return _json(payload(MetadataBundle(tags=tags or [], rating=rating)))

    @staticmethod
    def stamp_file(path, tags=None, rating=0, *, prepared=False):
        if not prepared:
            log.error("StampLib requires a prepared output; indexed originals need Metadata Hub")
            return False
        from .extractor import MetadataExtractor
        try:
            bundle = MetadataExtractor.extract(path, strict=True)
            bundle.tags, bundle.rating = list(tags or []), rating
            return MetadataStamper.stamp(path, bundle, prepared=True)
        except Exception as exc:
            log.error("Invalid prepared curation output %s: %s", path, exc)
            return False


# =============================================================================
# MetadataStamper: Sistema avanzado de transferencia de metadata
# =============================================================================

from dataclasses import dataclass
from typing import Optional
from pathlib import Path


@dataclass
class TransferResult:
    """Resultado de una operación de transferencia de metadata."""
    success: bool
    source_path: str
    dest_path: str
    error: Optional[str] = None
    metadata_preserved: bool = False


class MetadataStamper:
    """
    Sistema avanzado para escritura y transferencia de metadata.
    
    Diferencia con StampLib:
    - StampLib: Solo escribe tags/rating de Panopticon
    - MetadataStamper: Transfiere TODA la metadata (prompts, params, tags)
    
    Uso:
        # Transferir metadata de original a copia
        result = MetadataStamper.transfer("original.png", "prepared.png", prepared=True)
        
        # Escribir bundle completo
        MetadataStamper.stamp("prepared.png", bundle, prepared=True)
    """
    
    @classmethod
    def transfer(cls, source: str | Path, dest: str | Path, 
                 verify: bool = True, *, prepared: bool = False) -> TransferResult:
        """
        Transfiere metadata de archivo fuente a destino.
        
        Args:
            source: Ruta al archivo original con metadata
            dest: Ruta al archivo destino donde escribir metadata
            verify: Si True, verifica la transferencia después
        
        Returns:
            TransferResult con estado de la operación
        """
        from .extractor import MetadataExtractor
        
        source = Path(source)
        dest = Path(dest)
        
        result = TransferResult(
            success=False,
            source_path=str(source),
            dest_path=str(dest)
        )
        
        if not source.exists():
            result.error = f"Source file not found: {source}"
            return result
        
        if not dest.exists():
            result.error = f"Destination file not found: {dest}"
            return result

        if source.resolve() == dest.resolve() or os.path.samefile(source, dest):
            result.error = "Transfer destination must differ from source"
            return result
        
        try:
            # Extraer metadata del original
            bundle = MetadataExtractor.extract(source, strict=True)
            
            if not bundle.is_valid():
                # No hay metadata que transferir, pero no es error
                result.success = True
                result.metadata_preserved = False
                return result
            
            # Escribir al destino
            success = cls.stamp(dest, bundle, prepared=prepared)
            
            if success:
                result.success = True
                result.metadata_preserved = True
            else:
                result.error = "Published files require a reviewed Metadata Hub edit; prepare a new output before stamping"
            
            return result
        
        except Exception as e:
            result.error = str(e)
            return result
    
    @classmethod
    def stamp(cls, path, bundle, *, prepared=False):
        """Verificar y estampar una salida temporal antes de publicar.

        prepared es un contrato del productor, no autorización para modificar
        un original indexado. No abre DB ni puede sincronizar una fila Native.
        AVIF recibe encoder_options antes del primer save; nunca recodificar aquí.
        """
        if not prepared:
            log.error("Stamp requires a prepared output; published originals need Metadata Hub")
            return False
        try:
            from .output_stamp import stamp_prepared
            return stamp_prepared(path, bundle)
        except Exception as exc:
            log.error("Error stamping prepared output %s: %s", path, exc)
            return False

    @classmethod
    def encoder_options(cls, bundle, *, existing_xmp=None):
        """XMP AVIF para incluir en su primera codificación, sin segunda pasada."""
        from .output_stamp import payload, xmp_payload
        return dict(xmp=xmp_payload(existing_xmp, payload(bundle)))

    @classmethod
    def verify_prepared(cls, path, bundle):
        from .output_stamp import payload, verify
        return verify(path, payload(bundle))

    @classmethod
    def strip_metadata(cls, path: str | Path) -> bool:
        """
        Elimina TODA la metadata de una imagen.
        Usado por Watermarker para proteger propiedad intelectual.
        
        Args:
            path: Ruta al archivo
        
        Returns:
            True si exitoso
        """
        path = Path(path)
        ext = path.suffix.lower()
        
        try:
            img = Image.open(path)
            
            # Crear imagen limpia sin metadata
            clean_img = Image.new(img.mode, img.size)
            clean_img.putdata(list(img.getdata()))
            
            if ext == '.png':
                _atomic_save(clean_img, path, "PNG", optimize=True)
            elif ext in ('.jpg', '.jpeg'):
                _atomic_save(clean_img, path, "JPEG", quality=95, optimize=True)
            elif ext == '.webp':
                _atomic_save(clean_img, path, "WEBP", quality=95)
            elif ext == '.avif':
                _atomic_save(clean_img, path, "AVIF", quality=95)
            else:
                img.close()
                return False
            
            img.close()
            return True
        
        except Exception as e:
            log.error("Error eliminando metadata de %s: %s", path, e)
            return False
