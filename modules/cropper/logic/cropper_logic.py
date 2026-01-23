import os
from PIL import Image

def crop_image(source_path, selection_norm, output_path):
    """
    Crops an image based on normalized coordinates.
    Selection: QRectF (normalized 0..1)
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source image not found: {source_path}")

    with Image.open(source_path) as img:
        w, h = img.size
        
        left = int(selection_norm.x() * w)
        top = int(selection_norm.y() * h)
        right = int(selection_norm.right() * w)
        bottom = int(selection_norm.bottom() * h)
        
        # Ensure coordinates are within bounds and valid
        left = max(0, min(left, w - 1))
        top = max(0, min(top, h - 1))
        right = max(left + 1, min(right, w))
        bottom = max(top + 1, min(bottom, h))
        
        cropped = img.crop((left, top, right, bottom))
        
        # Save maintaining format (if possible) or as PNG
        ext = os.path.splitext(output_path)[1].lower()
        if ext in ['.jpg', '.jpeg']:
            cropped.save(output_path, quality=95, subsampling=0)
        else:
            cropped.save(output_path)
            
    return output_path
