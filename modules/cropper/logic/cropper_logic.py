"""
Smart Cropper Logic - Refactored v2
Usa el nuevo sistema de metadata centralizado.
"""
import os
import tempfile
from pathlib import Path
from PIL import Image

from core.paths import CachePaths
from core.metadata import MetadataExtractor, MetadataStamper, MetadataBundle


class CropCancelled(Exception):
    """Cancellation before publication; temporary output is discarded."""


class SourceDestinationError(ValueError):
    """The requested output aliases the source image."""


def _check_destination(source, destination):
    if os.path.normcase(os.path.realpath(source)) == os.path.normcase(os.path.realpath(destination)):
        raise SourceDestinationError('Destination is the source image')
    if destination.exists() and os.path.samefile(source, destination):
        raise SourceDestinationError('Destination aliases the source image')


def crop_image(source_path, selection_norm, output_path=None, 
               preserve_metadata=True, tags=None, rating=0, cancel_event=None):
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
    
    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise CropCancelled()

    check_cancelled()
    _check_destination(source_path, output_path)
    # Work only on a private sibling; a failed encode/stamp leaves the destination intact.
    fd, temporary = tempfile.mkstemp(prefix='.panopticon-crop-', suffix=output_path.suffix,
                                     dir=output_path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        source_bundle = MetadataExtractor.extract(source_path) if preserve_metadata else None
        check_cancelled()
        with Image.open(source_path) as img:
            w, h = img.size
            left = max(0, min(int(selection_norm.x() * w), w - 1))
            top = max(0, min(int(selection_norm.y() * h), h - 1))
            right = max(left + 1, min(int(selection_norm.right() * w), w))
            bottom = max(top + 1, min(int(selection_norm.bottom() * h), h))
            with img.crop((left, top, right, bottom)) as cropped:
                ext = output_path.suffix.lower()
                if ext in ['.jpg', '.jpeg']:
                    cropped.save(str(temporary), quality=95, subsampling=0)
                elif ext == '.webp':
                    cropped.save(str(temporary), quality=95)
                else:
                    cropped.save(str(temporary))

        # Retain the existing additive export policy; CF5/CF6b define authority separately.
        bundle = None
        if preserve_metadata and source_bundle and source_bundle.is_valid():
            if tags:
                source_bundle.tags = list(set(source_bundle.tags + tags))
            if rating:
                source_bundle.rating = rating
            bundle = source_bundle
        elif tags or rating:
            bundle = MetadataBundle(tags=tags or [], rating=rating)
        if bundle is not None and not MetadataStamper.stamp(temporary, bundle):
            raise OSError('Metadata stamping failed')
        check_cancelled()
        _check_destination(source_path, output_path)
        os.replace(temporary, output_path)
        return str(output_path)
    finally:
        temporary.unlink(missing_ok=True)


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
