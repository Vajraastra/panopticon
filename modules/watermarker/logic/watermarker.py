from PIL import Image
import os

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
            # On some platforms, bits() returns a buffer that needs to be copied
            # ARGB32 is BGRA in bytes for PIL
            img_data = bits.tobytes()
            pil_img = Image.frombuffer('RGBA', (qimg.width(), qimg.height()), img_data, 'raw', 'BGRA', 0, 1)
            return pil_img.copy() # Ensure data is owned by PIL
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
            # expand=True keeps the whole image, but we might want to crop margins later
            # resample=Image.BICUBIC for quality
            watermark = watermark.rotate(angle, expand=True, resample=Image.BICUBIC)
        
        # Create pattern layer
        pattern = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        
        # Tile watermark across the pattern
        wm_width, wm_height = watermark.size
        # Add some padding between tiles if they are rotated to avoid overlapping bboxes? 
        # For now, stick to standard tiling.
        for y in range(0, base_image.height, wm_height):
            for x in range(0, base_image.width, wm_width):
                pattern.paste(watermark, (x, y), watermark)
        
        # Apply opacity to entire pattern
        if opacity < 1.0:
            # Blend with empty to apply opacity correctly to the whole layer
            # This is more robust than just modifying the alpha channel of pixels
            mask = Image.new('L', pattern.size, int(255 * opacity))
            # We only want to apply this mask to the areas where there IS a watermark
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
        # For SVG, we can render it directly at the target width for maximum quality
        if logo_path.lower().endswith('.svg'):
            # First pass to get aspect ratio
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
        
        # Composite logo using alpha channel as mask
        # Pattern for logo: create a layer, paste logo, then alpha composite
        logo_layer = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        logo_layer.paste(logo, coords, logo)
        
        result = Image.alpha_composite(base_image, logo_layer)
        
        return result
        
    except Exception as e:
        raise Exception(f"Logo application failed: {str(e)}")

def process_image(source_path, output_path, watermark_path=None, logo_path=None,
                 wm_angle=0, wm_scale=1.0, wm_opacity=0.3,
                 logo_position="top-right", logo_size=150):
    """
    Processes a single image with watermark and/or logo.
    """
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
        if output_path.lower().endswith(('.jpg', '.jpeg')):
            # Create white background for JPEG
            rgb_image = Image.new('RGB', base_image.size, (255, 255, 255))
            rgb_image.paste(base_image, mask=base_image.split()[3])
            rgb_image.save(output_path, quality=95, optimize=True)
        else:
            # Save as PNG with transparency
            base_image.save(output_path, optimize=True)
        
        return True, f"Processed: {os.path.basename(output_path)}"
        
    except Exception as e:
        return False, f"Error: {str(e)}"

def get_export_path(source_path, export_dir="watermarked", suffix="_watermarked"):
    """Generates export path for watermarked images."""
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)
    
    filename = os.path.basename(source_path)
    name, ext = os.path.splitext(filename)
    return os.path.join(export_dir, f"{name}{suffix}{ext}")
