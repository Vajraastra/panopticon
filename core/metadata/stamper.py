import os
import json
from PIL import Image, PngImagePlugin

class StampLib:
    """
    Librería estática para la persistencia de metadatos de Panopticon.
    Permite 'estampar' etiquetas y ratings en archivos físicos sin dañar
    los metadatos de IA (Stable Diffusion / ComfyUI).
    """
    
    @staticmethod
    def get_payload_json(tags: list = None, rating: int = 0) -> str:
        """Genera el string JSON estandarizado de Panopticon."""
        tags = tags or []
        payload = {
            "tags": tags,
            "rating": rating,
            "software": "Panopticon"
        }
        return json.dumps(payload)

    @staticmethod
    def stamp_file(path: str, tags: list = None, rating: int = 0):
        """
        Incrusta metadatos de Panopticon en el archivo de imagen.
        """
        if not os.path.exists(path):
            return False
            
        json_payload = StampLib.get_payload_json(tags, rating)
        
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == '.png':
                return StampLib._stamp_png(path, json_payload)
            elif ext in ('.jpg', '.jpeg'):
                return StampLib._stamp_jpeg(path, json_payload)
            elif ext == '.webp':
                return StampLib._stamp_webp(path, json_payload)
            return False
        except Exception as e:
            print(f"[StampLib] Error stamping {path}: {e}")
            return False

    @staticmethod
    def _stamp_png(path, json_payload):
        img = Image.open(path)
        existing_info = img.info
        
        # Preservar todo (incluyendo parameters de A1111)
        new_info = PngImagePlugin.PngInfo()
        for k, v in existing_info.items():
            if isinstance(k, str) and isinstance(v, (str, bytes)):
                 new_info.add_text(k, str(v))
        
        # Añadir nuestro payload
        new_info.add_text("panopticon_data", json_payload)
        
        img.save(path, pnginfo=new_info)
        img.close()
        return True

    @staticmethod
    def _stamp_jpeg(path, json_payload):
        img = Image.open(path)
        
        # Strategy: Use getexif() but ensure we write it back correctly
        exif = img.getexif()
        
        # 0x9286 is UserComment. 
        # Structure: First 8 bytes = encoding (ASCII\0\0\0 or UNICODE\0)
        # We will use ASCII prefix for simplicity as our JSON is standard
        prefix = b'ASCII\x00\x00\x00'
        payload_bytes = prefix + json_payload.encode('utf-8')
        
        exif[0x9286] = payload_bytes
        
        img.save(path, exif=exif, quality=95, optimize=True)
        img.close()
        return True

    @staticmethod
    def _stamp_webp(path, json_payload):
        img = Image.open(path)
        # XMP simple wrapper
        xmp = f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:pan="http://panopticon/ns/"><pan:data>{json_payload}</pan:data></rdf:Description></rdf:RDF></x:xmpmeta>'
        img.save(path, "WEBP", xmp=xmp.encode('utf-8'))
        img.close()
        return True


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
    metadata_preserved: bool = True


class MetadataStamper:
    """
    Sistema avanzado para escritura y transferencia de metadata.
    
    Diferencia con StampLib:
    - StampLib: Solo escribe tags/rating de Panopticon
    - MetadataStamper: Transfiere TODA la metadata (prompts, params, tags)
    
    Uso:
        # Transferir metadata de original a copia
        result = MetadataStamper.transfer("original.png", "copy.png")
        
        # Escribir bundle completo
        MetadataStamper.stamp("image.png", bundle)
    """
    
    @classmethod
    def transfer(cls, source: str | Path, dest: str | Path, 
                 verify: bool = False) -> TransferResult:
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
        
        try:
            # Extraer metadata del original
            bundle = MetadataExtractor.extract(source)
            
            if not bundle.is_valid():
                # No hay metadata que transferir, pero no es error
                result.success = True
                result.metadata_preserved = False
                return result
            
            # Escribir al destino
            success = cls.stamp(dest, bundle)
            
            if success:
                result.success = True
                result.metadata_preserved = True
            else:
                result.error = "Failed to stamp metadata to destination"
            
            return result
        
        except Exception as e:
            result.error = str(e)
            return result
    
    @classmethod
    def stamp(cls, path: str | Path, bundle) -> bool:
        """
        Escribe un MetadataBundle completo a un archivo.
        Preserva metadata existente de AI y añade/actualiza datos de Panopticon.
        
        Args:
            path: Ruta al archivo
            bundle: MetadataBundle con los datos a escribir
        
        Returns:
            True si exitoso
        """
        path = Path(path)
        ext = path.suffix.lower()
        
        try:
            if ext == '.png':
                return cls._stamp_png_bundle(path, bundle)
            elif ext in ('.jpg', '.jpeg'):
                return cls._stamp_jpeg_bundle(path, bundle)
            elif ext == '.webp':
                return cls._stamp_webp_bundle(path, bundle)
            else:
                return False
        except Exception as e:
            print(f"[MetadataStamper] Error stamping {path}: {e}")
            return False
    
    @classmethod
    def _stamp_png_bundle(cls, path: Path, bundle) -> bool:
        """Escribe bundle a PNG preservando metadata existente."""
        img = Image.open(path)
        existing_info = img.info
        
        new_info = PngImagePlugin.PngInfo()
        
        # Preservar metadata existente (prompts de A1111, etc.)
        for k, v in existing_info.items():
            if isinstance(k, str) and isinstance(v, (str, bytes)):
                new_info.add_text(k, str(v))
        
        # Añadir/actualizar datos de Panopticon
        panopticon_data = {
            "tags": bundle.tags,
            "rating": bundle.rating,
            "quality_score": bundle.quality_score,
            "software": "Panopticon"
        }
        new_info.add_text("panopticon_data", json.dumps(panopticon_data))
        
        # Si el bundle tiene prompts y el archivo no los tiene, añadirlos
        if bundle.positive_prompt and "parameters" not in existing_info:
            params = bundle.positive_prompt
            if bundle.negative_prompt:
                params += f"\nNegative prompt: {bundle.negative_prompt}"
            if bundle.steps:
                params += f"\nSteps: {bundle.steps}"
                if bundle.sampler:
                    params += f", Sampler: {bundle.sampler}"
                if bundle.cfg:
                    params += f", CFG scale: {bundle.cfg}"
                if bundle.seed:
                    params += f", Seed: {bundle.seed}"
                if bundle.model:
                    params += f", Model: {bundle.model}"
            new_info.add_text("parameters", params)
        
        img.save(str(path), pnginfo=new_info)
        img.close()
        return True
    
    @classmethod
    def _stamp_jpeg_bundle(cls, path: Path, bundle) -> bool:
        """Escribe bundle a JPEG."""
        img = Image.open(path)
        exif = img.getexif()
        
        # Crear payload combinado
        panopticon_data = {
            "tags": bundle.tags,
            "rating": bundle.rating,
            "quality_score": bundle.quality_score,
            "software": "Panopticon"
        }
        
        # Para JPEG, usamos UserComment con prefijo especial
        prefix = b'ASCII\x00\x00\x00'
        payload_bytes = prefix + json.dumps(panopticon_data).encode('utf-8')
        
        # Solo escribir si no hay UserComment existente con prompts
        if 0x9286 not in exif:
            exif[0x9286] = payload_bytes
        
        img.save(str(path), exif=exif, quality=95, optimize=True)
        img.close()
        return True
    
    @classmethod
    def _stamp_webp_bundle(cls, path: Path, bundle) -> bool:
        """Escribe bundle a WebP usando XMP."""
        img = Image.open(path)
        
        panopticon_data = {
            "tags": bundle.tags,
            "rating": bundle.rating,
            "quality_score": bundle.quality_score,
            "positive_prompt": bundle.positive_prompt,
            "negative_prompt": bundle.negative_prompt,
            "model": bundle.model,
            "seed": bundle.seed,
            "steps": bundle.steps,
            "cfg": bundle.cfg,
            "sampler": bundle.sampler,
            "tool": bundle.tool,
            "software": "Panopticon"
        }
        
        json_payload = json.dumps(panopticon_data)
        xmp = f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" xmlns:pan="http://panopticon/ns/"><pan:data>{json_payload}</pan:data></rdf:Description></rdf:RDF></x:xmpmeta>'
        
        img.save(str(path), "WEBP", xmp=xmp.encode('utf-8'))
        img.close()
        return True
    
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
                clean_img.save(str(path), "PNG", optimize=True)
            elif ext in ('.jpg', '.jpeg'):
                clean_img.save(str(path), "JPEG", quality=95, optimize=True)
            elif ext == '.webp':
                clean_img.save(str(path), "WEBP", quality=95)
            else:
                img.close()
                return False
            
            img.close()
            return True
        
        except Exception as e:
            print(f"[MetadataStamper] Error stripping {path}: {e}")
            return False
