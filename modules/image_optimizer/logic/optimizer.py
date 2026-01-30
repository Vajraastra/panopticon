"""
Image Optimizer Logic - Refactored v2
Usa el nuevo sistema de metadata centralizado.
"""
import os
from pathlib import Path
from PIL import Image, PngImagePlugin

from core.paths import CachePaths
from core.metadata import MetadataExtractor, MetadataStamper, MetadataBundle


# =========================
# ANALYSIS LOGIC
# =========================

def has_real_transparency(image):
    """Detects if an alpha channel actually contains transparent pixels."""
    if image.mode not in ('RGBA', 'LA') and 'transparency' not in image.info:
        return False
    
    if image.mode == 'P':
        image = image.convert('RGBA')
    
    alpha = image.split()[-1]
    extrema = alpha.getextrema()
    return extrema[0] < 255


def analyze_image(path):
    """Analyzes image complexity to suggest optimal format."""
    try:
        with Image.open(path) as img:
            has_alpha = has_real_transparency(img)
            
            colors = img.getcolors(5000)
            color_count = len(colors) if colors else 5001
            
            suggestion = "JPEG"
            reason = "High complexity/photo-like"
            
            if has_alpha:
                suggestion = "PNG"
                reason = "Has transparency"
            elif color_count < 256:
                suggestion = "PNG"
                reason = "Low color count (graphics/UI)"
            elif color_count < 4096:
                suggestion = "WebP"
                reason = "Medium complexity"
                
            return {
                "format": img.format,
                "mode": img.mode,
                "size": img.size,
                "has_transparency": has_alpha,
                "color_count": color_count if color_count <= 5000 else ">5000",
                "suggested_format": suggestion,
                "suggestion_reason": reason
            }
    except Exception as e:
        return {"error": str(e)}


# =========================
# PROCESSING PIPELINE
# =========================

def resize_image(image, max_side=None, width=None, height=None, lock_aspect=True):
    """Resizes image using Lanczos resampling for best quality."""
    orig_w, orig_h = image.size
    
    if max_side:
        if orig_w > orig_h:
            ratio = max_side / orig_w
        else:
            ratio = max_side / orig_h
        new_w = int(orig_w * ratio)
        new_h = int(orig_h * ratio)
    elif width and height:
        if lock_aspect:
            ratio = min(width / orig_w, height / orig_h)
            new_w = int(orig_w * ratio)
            new_h = int(orig_h * ratio)
        else:
            new_w, new_h = width, height
    elif width:
        ratio = width / orig_w
        new_w = width
        new_h = int(orig_h * ratio)
    elif height:
        ratio = height / orig_h
        new_w = int(orig_w * ratio)
        new_h = height
    else:
        return image

    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def optimize_image(source_path, output_path, 
                   format_override=None, 
                   quality=90, 
                   max_side=None,
                   resize_width=None, 
                   resize_height=None,
                   lock_aspect=True,
                   preserve_metadata=True,
                   tags=None, rating=0):
    """
    Complete optimization pipeline with new metadata system.
    
    Steps:
    1. Extract metadata from source (using MetadataExtractor)
    2. Load and resize image
    3. Determine output format (auto-convert if AI metadata)
    4. Save optimized image
    5. Transfer metadata to new file (using MetadataStamper)
    
    Args:
        source_path: Path to source image
        output_path: Path for output (may be adjusted for format)
        format_override: Force output format (PNG, JPEG, WEBP)
        quality: JPEG/WebP quality (1-100)
        max_side: Max dimension for longest side
        resize_width/height: Specific dimensions
        lock_aspect: Maintain aspect ratio
        preserve_metadata: Transfer AI prompts/tags
        tags: Additional Panopticon tags to add
        rating: Panopticon rating to add
    
    Returns:
        dict with success status, paths, and size stats
    """
    source_path = Path(source_path)
    output_path = Path(output_path)
    
    try:
        # 1. Extract metadata from source
        source_bundle = None
        if preserve_metadata:
            source_bundle = MetadataExtractor.extract(source_path)
        
        # 2. Load image
        img = Image.open(source_path)
        original_format = img.format
        
        # 3. Resize if needed
        if max_side or resize_width or resize_height:
            img = resize_image(img, max_side=max_side, 
                             width=resize_width, height=resize_height, 
                             lock_aspect=lock_aspect)
        
        # 4. Determine output format
        target_format = format_override.upper() if format_override else original_format
        if not target_format:
            target_format = "PNG"
        
        # Auto-convert: JPEG with AI prompts -> PNG (preserves prompts better)
        has_ai_prompts = source_bundle and source_bundle.has_prompts()
        if target_format in ("JPEG", "JPG") and has_ai_prompts:
            target_format = "PNG"
        
        # 5. Adjust output path extension
        ext_map = {"JPEG": ".jpg", "JPG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
        target_ext = ext_map.get(target_format, ".png")
        output_path = output_path.with_suffix(target_ext)
        
        # 6. Prepare save arguments
        save_kwargs = {"optimize": True}
        
        if target_format == "PNG":
            save_kwargs["compress_level"] = 9
            # Mode conversion if needed
            if img.mode == "P" and has_real_transparency(img):
                img = img.convert("RGBA")
        
        elif target_format in ("JPEG", "JPG"):
            save_kwargs["quality"] = quality
            # JPEG requires RGB
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
        
        elif target_format == "WEBP":
            save_kwargs["quality"] = quality
            save_kwargs["method"] = 6
        
        # 7. Save image (without metadata first)
        img.save(str(output_path), **save_kwargs)
        img.close()
        
        # 8. Transfer metadata from source to output
        if preserve_metadata and source_bundle and source_bundle.is_valid():
            # Add any new tags/rating
            if tags:
                source_bundle.tags = list(set(source_bundle.tags + tags))
            if rating:
                source_bundle.rating = rating
            
            # Stamp to output
            MetadataStamper.stamp(output_path, source_bundle)
        
        elif tags or rating:
            # No source metadata, but user wants to add tags
            new_bundle = MetadataBundle(tags=tags or [], rating=rating)
            MetadataStamper.stamp(output_path, new_bundle)
        
        # 9. Calculate stats
        orig_size = source_path.stat().st_size
        new_size = output_path.stat().st_size
        
        return {
            "success": True,
            "output_path": str(output_path),
            "original_size": orig_size,
            "new_size": new_size,
            "saved_bytes": orig_size - new_size,
            "saved_percent": ((orig_size - new_size) / orig_size) * 100 if orig_size > 0 else 0,
            "format_changed": target_format != original_format,
            "metadata_preserved": preserve_metadata and source_bundle is not None
        }
    
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_export_path(source_path, export_dir=None, suffix=""):
    """
    Generates export path using centralized cache system.
    
    Args:
        source_path: Original file path
        export_dir: Custom export dir (uses CachePaths if None)
        suffix: Optional suffix before extension
    
    Returns:
        Path object for output file
    """
    if export_dir is None:
        export_dir = CachePaths.get_tool_cache("optimizer")
    else:
        export_dir = Path(export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
    
    source_path = Path(source_path)
    filename = source_path.name
    
    if suffix:
        stem = source_path.stem
        ext = source_path.suffix
        filename = f"{stem}{suffix}{ext}"
    
    return export_dir / filename


def batch_optimize(files, output_dir=None, **kwargs):
    """
    Optimizes multiple files with progress tracking.
    
    Args:
        files: List of file paths
        output_dir: Output directory (uses cache if None)
        **kwargs: Arguments passed to optimize_image
    
    Yields:
        (index, total, result_dict) for each file
    """
    if output_dir is None:
        output_dir = CachePaths.get_tool_cache("optimizer")
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    total = len(files)
    
    for i, source in enumerate(files):
        source = Path(source)
        output = output_dir / source.name
        
        result = optimize_image(str(source), str(output), **kwargs)
        result["source_path"] = str(source)
        result["index"] = i + 1
        result["total"] = total
        
        yield i + 1, total, result
