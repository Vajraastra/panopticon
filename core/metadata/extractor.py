"""
MetadataExtractor: Extracción universal de metadata de imágenes.
Soporta PNG (tEXt/iTXt), JPEG (EXIF), WebP (XMP).
"""
import os
import struct
import json
import re
from pathlib import Path
from typing import Optional

from PIL import Image

from .bundle import MetadataBundle


class MetadataExtractor:
    """
    Extractor universal de metadata para imágenes.
    
    Soporta:
    - PNG: chunks tEXt, iTXt (A1111, ComfyUI, NAI)
    - JPEG: EXIF UserComment
    - WebP: XMP embedded data
    
    Uso:
        bundle = MetadataExtractor.extract("image.png")
        if bundle.has_prompts():
            print(bundle.positive_prompt)
    """
    
    SUPPORTED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
    
    @classmethod
    def extract(cls, path: str | Path) -> MetadataBundle:
        """
        Extrae metadata de cualquier formato de imagen soportado.
        
        Args:
            path: Ruta al archivo de imagen
        
        Returns:
            MetadataBundle con toda la metadata encontrada
        """
        path = Path(path)
        
        if not path.exists():
            return MetadataBundle()
        
        ext = path.suffix.lower()
        
        if ext == '.png':
            return cls.extract_png(path)
        elif ext in ('.jpg', '.jpeg'):
            return cls.extract_jpeg(path)
        elif ext == '.webp':
            return cls.extract_webp(path)
        else:
            return MetadataBundle()
    
    @classmethod
    def extract_png(cls, path: str | Path) -> MetadataBundle:
        """
        Extrae metadata de archivos PNG.
        Lee chunks tEXt e iTXt donde A1111/ComfyUI/NAI guardan prompts.
        """
        path = Path(path)
        raw_metadata = {}
        
        try:
            with open(path, 'rb') as f:
                sig = f.read(8)
                if sig != b'\x89PNG\r\n\x1a\n':
                    return MetadataBundle(source_format="PNG")
                
                while True:
                    length_bin = f.read(4)
                    if not length_bin:
                        break
                    
                    length = struct.unpack('>I', length_bin)[0]
                    chunk_type = f.read(4)
                    data = f.read(length)
                    f.read(4)  # CRC
                    
                    if chunk_type == b'tEXt':
                        parts = data.split(b'\x00', 1)
                        if len(parts) == 2:
                            key = parts[0].decode('latin-1', errors='ignore')
                            value = parts[1].decode('latin-1', errors='ignore')
                            raw_metadata[key] = value
                    
                    elif chunk_type == b'iTXt':
                        parts = data.split(b'\x00', 5)
                        if len(parts) >= 6:
                            key = parts[0].decode('utf-8', errors='ignore')
                            value = parts[5].decode('utf-8', errors='ignore')
                            raw_metadata[key] = value
            
            return cls._parse_raw_metadata(raw_metadata, "PNG")
        
        except Exception as e:
            print(f"[MetadataExtractor] Error reading PNG {path}: {e}")
            return MetadataBundle(source_format="PNG")
    
    @classmethod
    def extract_jpeg(cls, path: str | Path) -> MetadataBundle:
        """
        Extrae metadata de archivos JPEG.
        Lee EXIF UserComment donde A1111 guarda prompts.
        """
        path = Path(path)
        raw_metadata = {}
        
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                
                if exif:
                    # 0x9286 = UserComment
                    if 0x9286 in exif:
                        user_comment = exif[0x9286]
                        if isinstance(user_comment, bytes):
                            # Remove encoding prefix if present
                            if user_comment.startswith(b'ASCII\x00\x00\x00'):
                                user_comment = user_comment[8:]
                            elif user_comment.startswith(b'UNICODE\x00'):
                                user_comment = user_comment[8:]
                            user_comment = user_comment.decode('utf-8', errors='ignore')
                        raw_metadata["parameters"] = user_comment
                    
                    # 0x0131 = Software
                    if 0x0131 in exif:
                        raw_metadata["Software"] = str(exif[0x0131])
            
            return cls._parse_raw_metadata(raw_metadata, "JPEG")
        
        except Exception as e:
            print(f"[MetadataExtractor] Error reading JPEG {path}: {e}")
            return MetadataBundle(source_format="JPEG")
    
    @classmethod
    def extract_webp(cls, path: str | Path) -> MetadataBundle:
        """
        Extrae metadata de archivos WebP.
        Lee XMP embedded data y EXIF si está presente.
        """
        path = Path(path)
        raw_metadata = {}
        
        try:
            with Image.open(path) as img:
                # Check for XMP data
                if hasattr(img, 'info') and 'xmp' in img.info:
                    xmp_data = img.info['xmp']
                    if isinstance(xmp_data, bytes):
                        xmp_data = xmp_data.decode('utf-8', errors='ignore')
                    
                    # Parse XMP for Panopticon data
                    panopticon_data = cls._parse_xmp_panopticon(xmp_data)
                    if panopticon_data:
                        raw_metadata.update(panopticon_data)
                    
                    # Store raw XMP for reference
                    raw_metadata["xmp_raw"] = xmp_data
                
                # Also check EXIF
                exif = img.getexif()
                if exif and 0x9286 in exif:
                    user_comment = exif[0x9286]
                    if isinstance(user_comment, bytes):
                        user_comment = user_comment.decode('utf-8', errors='ignore')
                    raw_metadata["parameters"] = user_comment
            
            return cls._parse_raw_metadata(raw_metadata, "WEBP")
        
        except Exception as e:
            print(f"[MetadataExtractor] Error reading WebP {path}: {e}")
            return MetadataBundle(source_format="WEBP")
    
    @classmethod
    def _parse_xmp_panopticon(cls, xmp: str) -> dict:
        """Extrae datos de Panopticon desde XMP."""
        result = {}
        
        # Look for pan:data tag
        match = re.search(r'<pan:data>(.+?)</pan:data>', xmp, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if 'tags' in data:
                    result['panopticon_tags'] = data['tags']
                if 'rating' in data:
                    result['panopticon_rating'] = data['rating']
                if 'quality_score' in data:
                    result['panopticon_quality'] = data['quality_score']
            except json.JSONDecodeError:
                pass
        
        return result
    
    @classmethod
    def _parse_raw_metadata(cls, raw: dict, source_format: str) -> MetadataBundle:
        """
        Procesa metadata raw y extrae prompts/parámetros estructurados.
        Soporta formatos A1111, ComfyUI, NAI.
        """
        bundle = MetadataBundle(raw=raw, source_format=source_format)
        
        # Normalize keys
        norm = {k.lower(): v for k, v in raw.items()}
        
        # === A1111 / Forge format ===
        if "parameters" in norm:
            bundle.tool = "A1111 / Forge"
            params = norm["parameters"]
            cls._parse_a1111_format(params, bundle)
        
        # === ComfyUI format ===
        elif "prompt" in norm:
            bundle.tool = "ComfyUI"
            try:
                prompt_json = json.loads(norm["prompt"])
                cls._parse_comfyui_format(prompt_json, bundle)
            except json.JSONDecodeError:
                bundle.positive_prompt = norm["prompt"]
        
        # === NovelAI format ===
        elif "comment" in norm:
            bundle.tool = "NovelAI"
            try:
                comment_data = json.loads(norm["comment"])
                bundle.positive_prompt = comment_data.get("prompt", "")
                bundle.negative_prompt = comment_data.get("uc", "")
                bundle.seed = str(comment_data.get("seed", ""))
                bundle.steps = str(comment_data.get("steps", ""))
                bundle.cfg = str(comment_data.get("scale", ""))
                bundle.sampler = comment_data.get("sampler", "")
            except json.JSONDecodeError:
                pass
        
        # === Panopticon data ===
        if "panopticon_data" in norm:
            try:
                pan_data = json.loads(norm["panopticon_data"])
                bundle.tags = pan_data.get("tags", [])
                bundle.rating = pan_data.get("rating", 0)
                bundle.quality_score = pan_data.get("quality_score", 0)
            except json.JSONDecodeError:
                pass
        
        # From XMP parsing
        if "panopticon_tags" in raw:
            bundle.tags = raw["panopticon_tags"]
        if "panopticon_rating" in raw:
            bundle.rating = raw["panopticon_rating"]
        if "panopticon_quality" in raw:
            bundle.quality_score = raw["panopticon_quality"]
        
        return bundle
    
    @classmethod
    def _parse_a1111_format(cls, params: str, bundle: MetadataBundle):
        """Parsea formato de A1111/Forge."""
        # Split by "Negative prompt:"
        if "negative prompt:" in params.lower():
            parts = re.split(r'negative prompt:', params, flags=re.IGNORECASE)
            bundle.positive_prompt = parts[0].strip()
            
            if len(parts) > 1:
                remainder = parts[1]
                # Find where technical params start (usually "Steps:")
                if "\nsteps:" in remainder.lower():
                    neg_parts = re.split(r'\nsteps:', remainder, flags=re.IGNORECASE)
                    bundle.negative_prompt = neg_parts[0].strip()
                    tech_line = "Steps:" + neg_parts[1]
                    cls._parse_a1111_tech_line(tech_line, bundle)
                else:
                    bundle.negative_prompt = remainder.strip()
        else:
            # No negative prompt
            if "\nsteps:" in params.lower():
                parts = re.split(r'\nsteps:', params, flags=re.IGNORECASE)
                bundle.positive_prompt = parts[0].strip()
                tech_line = "Steps:" + parts[1]
                cls._parse_a1111_tech_line(tech_line, bundle)
            else:
                bundle.positive_prompt = params.strip()
        
        # Extract LoRAs from prompt
        lora_matches = re.findall(r'<lora:([^:]+):([^>]+)>', bundle.positive_prompt)
        bundle.loras = [f"{name} ({weight})" for name, weight in lora_matches]
    
    @classmethod
    def _parse_a1111_tech_line(cls, line: str, bundle: MetadataBundle):
        """Parsea línea técnica de A1111."""
        parts = line.split(",")
        for p in parts:
            p = p.strip()
            if ":" not in p:
                continue
            k, v = p.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            
            if k == "steps":
                bundle.steps = v
            elif k == "sampler":
                bundle.sampler = v
            elif k == "cfg scale":
                bundle.cfg = v
            elif k == "seed":
                bundle.seed = v
            elif k == "model":
                bundle.model = v
            elif k == "vae":
                bundle.vae = v
    
    @classmethod
    def _parse_comfyui_format(cls, prompt_json: dict, bundle: MetadataBundle):
        """Parsea formato JSON de ComfyUI."""
        pos_prompts = []
        neg_prompts = []
        
        for node_id, node in prompt_json.items():
            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            meta = node.get("_meta", {})
            title = meta.get("title", "").lower()
            
            # CLIPTextEncode nodes contain prompts
            if class_type == "CLIPTextEncode":
                text = str(inputs.get("text", "")).strip()
                if text:
                    if "negative" in title or "neg" in title:
                        neg_prompts.append(text)
                    else:
                        pos_prompts.append(text)
            
            # KSampler has generation params
            if "KSampler" in class_type:
                bundle.seed = str(inputs.get("seed", bundle.seed))
                bundle.steps = str(inputs.get("steps", bundle.steps))
                bundle.cfg = str(inputs.get("cfg", bundle.cfg))
                bundle.sampler = inputs.get("sampler_name", bundle.sampler)
            
            # CheckpointLoader has model info
            if "CheckpointLoader" in class_type:
                bundle.model = inputs.get("ckpt_name", bundle.model)
            
            # VAELoader
            if "VAELoader" in class_type:
                bundle.vae = inputs.get("vae_name", bundle.vae)
            
            # LoraLoader
            if "LoraLoader" in class_type:
                lora_name = inputs.get("lora_name", "")
                strength = inputs.get("strength_model", "1.0")
                if lora_name:
                    bundle.loras.append(f"{lora_name} ({strength})")
        
        bundle.positive_prompt = "\n---\n".join(dict.fromkeys(pos_prompts))
        bundle.negative_prompt = "\n---\n".join(dict.fromkeys(neg_prompts))
