"""
Smart Cropper Logic - Refactored v2
Usa el nuevo sistema de metadata centralizado.
"""
import os
from pathlib import Path
from PIL import Image

from core.paths import CachePaths
from core.metadata import MetadataExtractor, MetadataStamper, MetadataBundle


def crop_image(source_path, selection_norm, output_path=None, 
               preserve_metadata=True, tags=None, rating=0):
    """
    Crops an image based on normalized coordinates.
    
    Args:
        source_path: Path to source image
        selection_norm: QRectF with normalized coordinates (0..1)
        output_path: Output path (uses cache if None)
        preserve_metadata: Transfer AI prompts/tags from source
        tags: Additional Panopticon tags to add
        rating: Panopticon rating to add
    
    Returns:
        Path to cropped image
    """
    source_path = Path(source_path)
    
    if not source_path.exists():
        raise FileNotFoundError(f"Source image not found: {source_path}")
    
    # Generate output path if not provided
    if output_path is None:
        output_path = get_export_path(source_path, suffix="_cropped")
    else:
        output_path = Path(output_path)
    
    # Extract metadata from source BEFORE cropping
    source_bundle = None
    if preserve_metadata:
        source_bundle = MetadataExtractor.extract(source_path)
    
    # Crop the image
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
        
        # Save maintaining format
        ext = output_path.suffix.lower()
        if ext in ['.jpg', '.jpeg']:
            cropped.save(str(output_path), quality=95, subsampling=0)
        elif ext == '.webp':
            cropped.save(str(output_path), quality=95)
        else:
            cropped.save(str(output_path))
    
    # Transfer metadata from source to cropped image
    if preserve_metadata and source_bundle and source_bundle.is_valid():
        # Add any new tags/rating
        if tags:
            source_bundle.tags = list(set(source_bundle.tags + tags))
        if rating:
            source_bundle.rating = rating
        
        MetadataStamper.stamp(output_path, source_bundle)
    
    elif tags or rating:
        # No source metadata, but user wants to add tags
        new_bundle = MetadataBundle(tags=tags or [], rating=rating)
        MetadataStamper.stamp(output_path, new_bundle)
    
    return str(output_path)


def get_export_path(source_path, suffix="_cropped"):
    """
    Generates export path using centralized cache system.
    
    Args:
        source_path: Original file path
        suffix: Suffix to add before extension
    
    Returns:
        Path object for output file
    """
    source_path = Path(source_path)
    export_dir = CachePaths.get_tool_cache("cropper")
    
    stem = source_path.stem
    ext = source_path.suffix
    filename = f"{stem}{suffix}{ext}"
    
    return export_dir / filename


def batch_crop(files, selection_norm, **kwargs):
    """
    Crops multiple files with same selection.
    
    Args:
        files: List of file paths
        selection_norm: QRectF with normalized coordinates
        **kwargs: Arguments passed to crop_image
    
    Yields:
        (index, total, result_path) for each file
    """
    total = len(files)
    
    for i, source in enumerate(files):
        try:
            result = crop_image(source, selection_norm, **kwargs)
            yield i + 1, total, {"success": True, "path": result}
        except Exception as e:
            yield i + 1, total, {"success": False, "error": str(e)}
