import os
import json
import shutil
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
                if format_lower in ('jpeg', 'jpg'):
                    _rewrite_jpeg(source_path, output_path, None)
                    return True, 'Metadata removed without recompressing JPEG'
                img.info.clear()
                if ext == '.avif':
                    img.save(output_path, "AVIF", quality=95)
                elif ext == '.webp':
                    img.save(output_path, 'WEBP', lossless=True)
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
                exif[0x9286] = metadata_text.encode('utf-8')
                _rewrite_jpeg(source_path, output_path, exif.tobytes())

            elif ext in ('.avif', '.webp'):
                # Leer pan:data existente para preservar tags/rating/quality
                existing = {}
                if hasattr(img, 'info') and 'xmp' in img.info:
                    import xml.etree.ElementTree as ET
                    from core.metadata.output_stamp import PAN, MAX_METADATA
                    xmp_raw = img.info['xmp']
                    if isinstance(xmp_raw, bytes):
                        xmp_raw = xmp_raw.decode('utf-8', errors='ignore')
                    if len(xmp_raw.encode('utf-8')) > MAX_METADATA or '<!DOCTYPE' in xmp_raw or '<!ENTITY' in xmp_raw:
                        raise ValueError('Invalid existing Panopticon XMP')
                    nodes = list(ET.fromstring(xmp_raw).iter(PAN))
                    if len(nodes) > 1: raise ValueError('Ambiguous existing Panopticon XMP')
                    if nodes:
                        try:
                            existing = json.loads(nodes[0].text)
                        except json.JSONDecodeError as exc:
                            raise ValueError('Invalid existing Panopticon XMP') from exc

                # Dividir texto editado en positivo/negativo
                if "negative prompt:" in metadata_text.lower():
                    import re as _re
                    parts = _re.split(r'negative prompt:', metadata_text,
                                      flags=_re.IGNORECASE)
                    pos = parts[0].strip()
                    neg = parts[1].strip() if len(parts) > 1 else ""
                else:
                    pos = metadata_text.strip()
                    neg = ""

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
                # JSON escape del segundo corchete evita cerrar CDATA desde el prompt.
                payload = json.dumps(pan_data).replace(']]>', ']\\u005d>')
                xmp = (
                    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                    '<rdf:Description rdf:about="" xmlns:pan="http://panopticon/ns/">'
                    f'<pan:data><![CDATA[{payload}]]></pan:data>'
                    '</rdf:Description></rdf:RDF></x:xmpmeta>'
                )
                fmt = "AVIF" if ext == '.avif' else "WEBP"
                options = dict(quality=95) if fmt == 'AVIF' else dict(lossless=True)
                img.save(output_path, fmt, xmp=xmp.encode('utf-8'), **options)

            else:
                raise ValueError('Unsupported metadata format')

            return True, f"Modified metadata saved to {output_path}"
    except Exception as e:
        return False, str(e)


def _rewrite_jpeg(source, output, exif):
    """Cambiar APP1 sin recodificar el flujo de píxeles; preservar ICC/Adobe/JFIF."""
    if exif is not None and len(exif)+2 > 65535:
        raise ValueError('JPEG metadata exceeds segment capacity')
    if os.path.abspath(source) == os.path.abspath(output):
        raise ValueError('JPEG transformation requires a separate prepared output')
    with open(source, 'rb') as src, open(output, 'wb') as dst:
        if src.read(2) != b'\xff\xd8': raise ValueError('Invalid JPEG')
        dst.write(b'\xff\xd8')
        if exif is not None:
            dst.write(b'\xff\xe1'+(len(exif)+2).to_bytes(2, 'big')+exif)
        for _ in range(100000):
            prefix = src.read(1)
            if prefix != b'\xff': raise ValueError('Invalid JPEG marker')
            prefix = bytearray(prefix)
            marker = src.read(1)
            while marker == b'\xff':
                prefix.extend(marker)
                if len(prefix) > 65536: raise ValueError('JPEG fill limit exceeded')
                marker = src.read(1)
            if not marker: raise ValueError('Truncated JPEG')
            if marker == b'\xda':
                dst.write(prefix+marker)
                shutil.copyfileobj(src, dst, 1024*1024)
                return
            if marker in (b'\x00', b'\xd8', b'\xd9'):
                raise ValueError('Unsupported JPEG structure')
            length = src.read(2)
            if len(length) != 2 or int.from_bytes(length, 'big') < 2:
                raise ValueError('Invalid JPEG segment')
            data = src.read(int.from_bytes(length, 'big')-2)
            if len(data) != int.from_bytes(length, 'big')-2: raise ValueError('Truncated JPEG')
            skip = marker == b'\xe1' and data.startswith((b'Exif\x00\x00', b'http://ns.adobe.com/xap/'))
            if exif is None:
                skip = marker in (b'\xe1', b'\xfe') or b'\xe3' <= marker <= b'\xed' or marker == b'\xef'
            if not skip: dst.write(prefix+marker+length+data)
        raise ValueError('JPEG segment limit exceeded')

def get_export_path(source_path, export_dir="exports", suffix="_clean"):
    """Generates a new path for the exported file in a specific folder."""
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
        
    filename = os.path.basename(source_path)
    name, ext = os.path.splitext(filename)
    return os.path.join(export_dir, f"{name}{suffix}{ext}")
