from PIL import Image
import os

def apply_watermark_pattern(base_image, watermark_path, angle=0, scale=1.0, opacity=0.3):
    """
    Applies a repeating watermark pattern across the base image.
    
    Args:
        base_image: PIL Image object (RGBA)
        watermark_path: Path to watermark image
        angle: Rotation angle in degrees (0, 45, 90, 135, 180, 225, 270, 315)
        scale: Scale factor (0.1 to 2.0)
        opacity: Opacity (0.0 to 1.0)
    
    Returns:
        PIL Image with watermark applied
    """
    try:
        # Ensure base image is RGBA
        if base_image.mode != 'RGBA':
            base_image = base_image.convert('RGBA')
        
        # Load and process watermark
        watermark = Image.open(watermark_path).convert('RGBA')
        
        # Rotate watermark
        if angle != 0:
            watermark = watermark.rotate(angle, expand=True, resample=Image.BICUBIC)
        
        # Scale watermark
        if scale != 1.0:
            new_size = (int(watermark.width * scale), int(watermark.height * scale))
            watermark = watermark.resize(new_size, Image.LANCZOS)
        
        # Create pattern layer
        pattern = Image.new('RGBA', base_image.size, (0, 0, 0, 0))
        
        # Tile watermark across the pattern
        wm_width, wm_height = watermark.size
        for y in range(0, base_image.height, wm_height):
            for x in range(0, base_image.width, wm_width):
                pattern.paste(watermark, (x, y), watermark)
        
        # Apply opacity to entire pattern
        if opacity < 1.0:
            alpha = pattern.split()[3]
            alpha = alpha.point(lambda p: int(p * opacity))
            pattern.putalpha(alpha)
        
        # Composite pattern onto base image
        result = Image.alpha_composite(base_image, pattern)
        
        return result
        
    except Exception as e:
        raise Exception(f"Watermark application failed: {str(e)}")


def apply_logo(base_image, logo_path, position="top-right", size=150):
    """
    Applies a logo overlay at a specified corner position.
    
    Args:
        base_image: PIL Image object (RGBA)
        logo_path: Path to logo image
        position: "top-left", "top-right", "bottom-left", "bottom-right"
        size: Logo size in pixels (width, aspect ratio preserved)
    
    Returns:
        PIL Image with logo applied
    """
    try:
        # Ensure base image is RGBA
        if base_image.mode != 'RGBA':
            base_image = base_image.convert('RGBA')
        
        # Load logo
        logo = Image.open(logo_path).convert('RGBA')
        
        # Resize logo maintaining aspect ratio
        aspect_ratio = logo.height / logo.width
        new_width = size
        new_height = int(size * aspect_ratio)
        logo = logo.resize((new_width, new_height), Image.LANCZOS)
        
        # Calculate position with margin
        margin = 20
        positions = {
            "top-left": (margin, margin),
            "top-right": (base_image.width - logo.width - margin, margin),
            "bottom-left": (margin, base_image.height - logo.height - margin),
            "bottom-right": (base_image.width - logo.width - margin, 
                           base_image.height - logo.height - margin)
        }
        
        coords = positions.get(position, positions["top-right"])
        
        # Create a copy to avoid modifying original
        result = base_image.copy()
        
        # Paste logo using alpha channel as mask
        result.paste(logo, coords, logo)
        
        return result
        
    except Exception as e:
        raise Exception(f"Logo application failed: {str(e)}")


def process_image(source_path, output_path, watermark_path=None, logo_path=None,
                 wm_angle=0, wm_scale=1.0, wm_opacity=0.3,
                 logo_position="top-right", logo_size=150):
    """
    Processes a single image with watermark and/or logo.
    
    Args:
        source_path: Path to source image
        output_path: Path to save processed image
        watermark_path: Path to watermark image (optional)
        logo_path: Path to logo image (optional)
        wm_angle: Watermark rotation angle
        wm_scale: Watermark scale factor
        wm_opacity: Watermark opacity
        logo_position: Logo corner position
        logo_size: Logo size in pixels
    
    Returns:
        (success: bool, message: str)
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
