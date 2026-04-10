import os
import json
from PIL import Image, PngImagePlugin

def modify_metadata(source_path, output_path, metadata_text=None):
    """
    Saves a copy of the image, either stripping all metadata or replacing it with custom text.
    - If metadata_text is None/empty: Strips everything.
    - If metadata_text is provided: Injects it into the image.
      PNG: tEXt 'parameters' | JPEG: UserComment EXIF | AVIF/WebP: XMP pan:data
    Returns (True, message) or (False, error).
    """
    try:
        with Image.open(source_path) as img:
            format_lower = img.format.lower() if img.format else ""
            # Normalizar: pillow reporta 'avif' en minúsculas
            ext = os.path.splitext(source_path)[1].lower()

            if not metadata_text:
                # Strip mode: save without 'info'
                if ext == '.avif':
                    img.save(output_path, "AVIF", quality=95)
                else:
                    img.save(output_path, quality=95, optimize=True)
                return True, f"Cleaned copy saved to {output_path}"

            # Injection mode
            if format_lower == 'png':
                metadata = PngImagePlugin.PngInfo()
                metadata.add_text("parameters", metadata_text)
                img.save(output_path, pnginfo=metadata, optimize=True)

            elif format_lower in ['jpeg', 'jpg']:
                exif = img.getexif()
                exif[0x9286] = metadata_text
                img.save(output_path, exif=exif, quality=95, optimize=True)

            elif ext in ('.avif', '.webp'):
                # Leer pan:data existente para preservar tags/rating/quality
                existing = {}
                if hasattr(img, 'info') and 'xmp' in img.info:
                    import re
                    xmp_raw = img.info['xmp']
                    if isinstance(xmp_raw, bytes):
                        xmp_raw = xmp_raw.decode('utf-8', errors='ignore')
                    m = re.search(r'<pan:data><!\[CDATA\[(.*?)\]\]></pan:data>',
                                  xmp_raw, re.DOTALL)
                    if not m:
                        m = re.search(r'<pan:data>(.*?)</pan:data>',
                                      xmp_raw, re.DOTALL)
                    if m:
                        try:
                            existing = json.loads(m.group(1))
                        except json.JSONDecodeError:
                            pass

                # Dividir texto editado en positivo/negativo
                if "negative prompt:" in metadata_text.lower():
                    import re as _re
                    parts = _re.split(r'negative prompt:', metadata_text,
                                      flags=_re.IGNORECASE)
                    pos = parts[0].strip()
                    neg = parts[1].strip() if len(parts) > 1 else ""
                else:
                    pos = metadata_text.strip()
                    neg = existing.get("negative_prompt", "")

                pan_data = {
                    "tags":            existing.get("tags", []),
                    "rating":          existing.get("rating", 0),
                    "quality_score":   existing.get("quality_score", 0),
                    "positive_prompt": pos,
                    "negative_prompt": neg,
                    "model":           existing.get("model", ""),
                    "seed":            existing.get("seed", ""),
                    "steps":           existing.get("steps", ""),
                    "cfg":             existing.get("cfg", ""),
                    "sampler":         existing.get("sampler", ""),
                    "tool":            existing.get("tool", "Unknown"),
                    "software":        "Panopticon",
                    "a1111_parameters": metadata_text,
                }
                payload = json.dumps(pan_data)
                xmp = (
                    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                    '<rdf:Description rdf:about="" xmlns:pan="http://panopticon/ns/">'
                    f'<pan:data><![CDATA[{payload}]]></pan:data>'
                    '</rdf:Description></rdf:RDF></x:xmpmeta>'
                )
                fmt = "AVIF" if ext == '.avif' else "WEBP"
                img.save(output_path, fmt, quality=95, xmp=xmp.encode('utf-8'))

            else:
                img.save(output_path, optimize=True)
                return True, f"Saved to {output_path} (format {format_lower}: metadata not injected)"

            return True, f"Modified metadata saved to {output_path}"
    except Exception as e:
        return False, str(e)

def get_export_path(source_path, export_dir="exports", suffix="_clean"):
    """Generates a new path for the exported file in a specific folder."""
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
        
    filename = os.path.basename(source_path)
    name, ext = os.path.splitext(filename)
    return os.path.join(export_dir, f"{name}{suffix}{ext}")
