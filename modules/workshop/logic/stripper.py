import os
from PIL import Image

def strip_metadata(source_path, output_path):
    """
    Saves a copy of the image without any metadata (EXIF, IPTC, XMP, or PNG chunks).
    Returns (True, message) or (False, error).
    """
    try:
        with Image.open(source_path) as img:
            # We preserve the format and mode, but save without 'info' (metadata)
            # This works for PNG (strips chunks like 'Parameters') and JPEG (strips EXIF)
            img.save(output_path, quality=95, optimize=True)
            return True, f"Cleaned copy saved to {output_path}"
    except Exception as e:
        return False, str(e)

def get_export_path(source_path, export_dir="exports", suffix="_clean"):
    """Generates a new path for the exported file in a specific folder."""
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
        
    filename = os.path.basename(source_path)
    name, ext = os.path.splitext(filename)
    return os.path.join(export_dir, f"{name}{suffix}{ext}")
