import os
from PIL import PngImagePlugin

def modify_metadata(source_path, output_path, metadata_text=None):
    """
    Saves a copy of the image, either stripping all metadata or replacing it with custom text.
    - If metadata_text is None/empty: Strips everything.
    - If metadata_text is provided: Injects it into the image (PNG: Parameters, JPEG: UserComment).
    Returns (True, message) or (False, error).
    """
    try:
        with Image.open(source_path) as img:
            if not metadata_text:
                # Strip mode: save without 'info' (metadata)
                img.save(output_path, quality=95, optimize=True)
                return True, f"Cleaned copy saved to {output_path}"
            else:
                # Injection mode
                format_lower = img.format.lower() if img.format else ""
                
                if format_lower == 'png':
                    metadata = PngImagePlugin.PngInfo()
                    # We inject into 'parameters' as it's the standard for AI/Prompt tools
                    metadata.add_text("parameters", metadata_text)
                    img.save(output_path, pnginfo=metadata, optimize=True)
                elif format_lower in ['jpeg', 'jpg']:
                    # Simple EXIF injection for JPEG
                    exif = img.getexif()
                    # 0x9286 is UserComment in EXIF
                    exif[0x9286] = metadata_text
                    img.save(output_path, exif=exif, quality=95, optimize=True)
                else:
                    # Fallback for other formats: just strip and notify
                    img.save(output_path, optimize=True)
                    return True, f"Injected text saved to {output_path} (Format: {format_lower})"
                
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
