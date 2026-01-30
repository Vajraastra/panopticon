"""
Watermarker Logic - Refactored v2
USA strip_metadata para eliminar prompts/tags de imágenes de distribución pública.
"""
from PIL import Image
import os
from pathlib import Path

from core.paths import CachePaths
from core.metadata import MetadataStamper


def _load_asset(path, target_size=None):
    """
    Loads an image asset (PNG, JPG, SVG) and returns a PIL Image in RGBA mode.
    If SVG, it renders it using PySide6.QtSvg.
    """
    if path.lower().endswith('.svg'):
        try:
            from PySide6.QtSvg import QSvgRenderer
            from PySide6.QtGui import QImage, QPainter
            from PySide6.QtCore import QSize, Qt
            import io

            renderer = QSvgRenderer(path)
            if not renderer.isValid():
                raise Exception("Invalid SVG file")
            
            # Determine rendering size
            if target_size:
                render_size = QSize(target_size[0], target_size[1])
            else:
                render_size = renderer.defaultSize()
            
            # Render SVG to QImage
            qimg = QImage(render_size, QImage.Format_ARGB32)
            qimg.fill(Qt.transparent)
            painter = QPainter(qimg)
            renderer.render(painter)
            painter.end()
            
            # Convert QImage to PIL
            bits = qimg.bits()
            img_data = bits.tobytes()
            pil_img = Image.frombuffer('RGBA', (qimg.width(), qimg.height()), img_data, 'raw', 'BGRA', 0, 1)
            return pil_img.copy()
        except Exception as e:
            raise Exception(f"Failed to render SVG {path}: {str(e)}")
    else:
        # Standard image loading
        img = Image.open(path).convert('RGBA')
        if target_size:
            img = img.resize(target_size, Image.LANCZOS)
        return img


def apply_watermark_pattern(base_image, watermark_path, angle=0, scale=1.0, opacity=0.3):
    """
    Applies a repeating watermark pattern across the base image.
    """
    try:
        # Ensure base image is RGBA
        if base_image.mode != 'RGBA':
            base_image = base_image.convert('RGBA')
        
        # Load watermark
        watermark = _load_asset(watermark_path)
        
        # Scale watermark
        if scale != 1.0:
            new_size = (int(watermark.width * scale), int(watermark.height * scale))
            watermark = watermark.resize(new_size, Image.LANCZOS)
        
        # Rotate watermark
        if angle != 0:
            watermark = watermark.rotate(angle, expand=True, resample=Image.BICUBIC)
        
        # Create pattern layer
        pattern = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        
        # Tile watermark across the pattern
        wm_width, wm_height = watermark.size
        for y in range(0, base_image.height, wm_height):
            for x in range(0, base_image.width, wm_width):
                pattern.paste(watermark, (x, y), watermark)
        
        # Apply opacity to entire pattern
        if opacity < 1.0:
            orig_alpha = pattern.split()[3]
            final_alpha = Image.eval(orig_alpha, lambda p: int(p * opacity))
            pattern.putalpha(final_alpha)
        
        # Composite pattern onto base image
        result = Image.alpha_composite(base_image, pattern)
        
        return result
        
    except Exception as e:
        raise Exception(f"Watermark application failed: {str(e)}")


def apply_logo(base_image, logo_path, position="top-right", size=150):
    """
    Applies a logo overlay at a specified corner position.
    """
    try:
        # Ensure base image is RGBA
        if base_image.mode != 'RGBA':
            base_image = base_image.convert('RGBA')
        
        # Load logo
        if logo_path.lower().endswith('.svg'):
            temp_svg = _load_asset(logo_path)
            aspect_ratio = temp_svg.height / temp_svg.width
            new_height = int(size * aspect_ratio)
            logo = _load_asset(logo_path, target_size=(size, new_height))
        else:
            logo = Image.open(logo_path).convert('RGBA')
            aspect_ratio = logo.height / logo.width
            new_width = size
            new_height = int(size * aspect_ratio)
            logo = logo.resize((new_width, new_height), Image.LANCZOS)
        
        # Calculate position with margin
        margin = 25
        positions = {
            "top-left": (margin, margin),
            "top-right": (base_image.width - logo.width - margin, margin),
            "bottom-left": (margin, base_image.height - logo.height - margin),
            "bottom-right": (base_image.width - logo.width - margin, 
                           base_image.height - logo.height - margin)
        }
        
        coords = positions.get(position, positions["top-right"])
        
        # Composite logo
        logo_layer = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        logo_layer.paste(logo, coords, logo)
        
        result = Image.alpha_composite(base_image, logo_layer)
        
        return result
        
    except Exception as e:
        raise Exception(f"Logo application failed: {str(e)}")


def process_image(source_path, output_path=None, watermark_path=None, logo_path=None,
                 wm_angle=0, wm_scale=1.0, wm_opacity=0.3,
                 logo_position="top-right", logo_size=150):
    """
    Processes a single image with watermark and/or logo.
    
    PRIVACY MODE: Strips ALL metadata from output.
    Watermarked images are for public distribution and should NOT
    contain prompts, tags, or other proprietary information.
    
    Args:
        source_path: Path to source image
        output_path: Output path (uses cache if None)
        watermark_path: Path to watermark asset
        logo_path: Path to logo asset
        wm_angle: Watermark rotation angle
        wm_scale: Watermark scale factor
        wm_opacity: Watermark opacity (0-1)
        logo_position: Logo position (top-left, top-right, etc.)
        logo_size: Logo width in pixels
    
    Returns:
        (success, message) tuple
    """
    source_path = Path(source_path)
    
    # Generate output path if not provided
    if output_path is None:
        output_path = get_export_path(source_path)
    else:
        output_path = Path(output_path)
    
    try:
        # Load base image
        base_image = Image.open(source_path).convert('RGBA')
        
        # Apply watermark if provided
        if watermark_path and os.path.exists(watermark_path):
            base_image = apply_watermark_pattern(
                base_image, watermark_path, wm_angle, wm_scale, wm_opacity
            )
        
        # Apply logo if provided
        if logo_path and os.path.exists(logo_path):
            base_image = apply_logo(
                base_image, logo_path, logo_position, logo_size
            )
        
        # Convert back to RGB if saving as JPEG
        output_str = str(output_path)
        if output_str.lower().endswith(('.jpg', '.jpeg')):
            # Create white background for JPEG
            rgb_image = Image.new('RGB', base_image.size, (255, 255, 255))
            rgb_image.paste(base_image, mask=base_image.split()[3])
            rgb_image.save(output_str, quality=95, optimize=True)
        else:
            # Save as PNG with transparency
            base_image.save(output_str, optimize=True)
        
        # ⚠️ PRIVACY MODE: Strip ALL metadata
        # Watermarked images should NOT contain AI prompts or tags
        MetadataStamper.strip_metadata(output_path)
        
        return True, f"Processed: {output_path.name}"
        
    except Exception as e:
        return False, f"Error: {str(e)}"


def get_export_path(source_path, suffix="_watermarked"):
    """
    Generates export path using centralized cache system.
    
    Args:
        source_path: Original file path
        suffix: Suffix to add before extension
    
    Returns:
        Path object for output file
    """
    source_path = Path(source_path)
    export_dir = CachePaths.get_tool_cache("watermarker")
    
    stem = source_path.stem
    ext = source_path.suffix
    filename = f"{stem}{suffix}{ext}"
    
    return export_dir / filename


def batch_process(files, **kwargs):
    """
    Processes multiple files with watermark/logo.
    
    Args:
        files: List of file paths
        **kwargs: Arguments passed to process_image
    
    Yields:
        (index, total, result_dict) for each file
    """
    total = len(files)
    
    for i, source in enumerate(files):
        success, message = process_image(source, **kwargs)
        yield i + 1, total, {"success": success, "message": message}
