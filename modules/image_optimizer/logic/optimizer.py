"""
Image Optimizer Logic - Refactored v2
Usa el nuevo sistema de metadata centralizado.
"""
import os
import hashlib
import tempfile
from pathlib import Path
from PIL import Image, PngImagePlugin

from core.paths import CachePaths
from core.derived_publication import DerivedPublication
from core.output_paths import safe_path, path_variants
from core.metadata.output_stamp import MAX_FILE
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

    if min(new_w, new_h) < 1 or max(new_w, new_h) > 32768 or new_w*new_h > 20_000_000:
        raise ValueError('optimizer_resize_limit')
    return image.resize((new_w, new_h), Image.Resampling.LANCZOS)


def optimize_image(source_path, output_path, 
                   format_override=None, 
                   quality=90, 
                   max_side=None,
                   resize_width=None, 
                   resize_height=None,
                   lock_aspect=True,
                   preserve_metadata=True,
                   tags=None, rating=0, service=None, cancel_event=None,
                   source_snapshot=None, native_service=None, journal_root=None):
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
        source_snapshot: Captured native ID/path/full revision; absence retains export only
        native_service: Writer facade with explicitly prepared derivative schema
        journal_root: Recovery directory outside watched and Cherry folders
    
    Returns:
        dict with publication/registration status, journal, paths and size stats;
        success=False with published=True requires review, never blind retry/deletion
    """
    source_path = Path(source_path)
    output_path = Path(output_path)
    temporary, img = None, None
    publication, published = None, False
    indexed = source_snapshot is not None and source_snapshot.get("id") is not None
    if indexed and native_service is None:
        return dict(success=False, error="derived_native_service_required", published=False, native_registered=False)

    def cancelled():
        if cancel_event is not None and cancel_event.is_set(): raise ValueError('optimizer_cancelled')

    def signature(path):
        info = path.stat(follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def digest(path, *, honour_cancel=True):
        value, total = hashlib.sha256(), 0
        with open(path, 'rb') as source:
            while block := source.read(1024*1024):
                if honour_cancel: cancelled()
                total += len(block)
                if total > MAX_FILE: raise ValueError('optimizer_file_limit')
                value.update(block)
        return value.digest()

    def destination(*, check_native=True):
        output = safe_path(output_path, exists=False, portable=True)
        if os.path.normcase(str(source_path)) == os.path.normcase(str(output)) or os.path.lexists(output):
            raise ValueError('optimizer_requires_new_output')
        if check_native and service is not None and service.read('get_metadata_path_status', path_variants(output, output_path),
                                               cancel_event=cancel_event)['occupied']:
            raise ValueError('optimizer_output_has_native_history')
        return output

    try:
        cancelled()
        source_path = safe_path(source_path)
        before, source_digest = signature(source_path), digest(source_path)
        source_bundle = MetadataExtractor.extract(source_path, strict=True) if preserve_metadata else None
        img = Image.open(source_path)
        if img.width*img.height > 20_000_000 or getattr(img, 'n_frames', 1) != 1:
            raise ValueError('optimizer_image_limit')
        original_format = img.format

        # 3. Resize if needed
        if max_side or resize_width or resize_height:
            resized = resize_image(img, max_side=max_side,
                             width=resize_width, height=resize_height, 
                             lock_aspect=lock_aspect)
            if resized is not img: img.close()
            img = resized
        
        # 4. Determine output format
        target_format = format_override.upper() if format_override else original_format
        if not target_format:
            target_format = "PNG"
        
        # Auto-convert: JPEG with AI prompts -> PNG (preserves prompts better)
        has_ai_prompts = source_bundle and source_bundle.has_prompts()
        if target_format in ("JPEG", "JPG") and has_ai_prompts:
            target_format = "PNG"
        
        # 5. Adjust output path extension
        ext_map = {"JPEG": ".jpg", "JPG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif"}
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

        elif target_format == "AVIF":
            save_kwargs["quality"] = quality
            save_kwargs.pop("optimize", None)  # AVIF no soporta optimize=True
            if img.mode == "P":
                img = img.convert("RGBA")

        # Metadata queda verificada en el temporal; jamás publicar para estampar después.
        bundle = None
        if preserve_metadata and source_bundle and source_bundle.is_valid():
            if tags: source_bundle.tags = list(dict.fromkeys(source_bundle.tags + list(tags)))
            if rating: source_bundle.rating = rating
            bundle = source_bundle
        elif tags or rating:
            bundle = MetadataBundle(tags=list(tags or []), rating=rating)
        output_path = destination(check_native=False)
        fd, name = tempfile.mkstemp(prefix='.panopticon-optimize-', suffix=target_ext, dir=output_path.parent)
        os.close(fd)
        temporary = Path(name)
        if target_format == 'AVIF' and bundle is not None:
            save_kwargs.update(MetadataStamper.encoder_options(bundle))
        img.save(str(temporary), **save_kwargs)
        img.close()
        img = None
        if bundle is not None:
            confirmed = (MetadataStamper.verify_prepared(temporary, bundle) if target_format == 'AVIF'
                         else MetadataStamper.stamp(temporary, bundle, prepared=True))
            if confirmed is not True: raise ValueError('optimizer_metadata_not_preserved')
        with Image.open(temporary) as verified:
            if verified.width*verified.height > 20_000_000: raise ValueError('optimizer_image_limit')
            dimensions = verified.size
            verified.load()
        with open(temporary, 'r+b') as prepared: os.fsync(prepared.fileno())
        cancelled()
        if signature(source_path) != before or digest(source_path) != source_digest:
            raise ValueError('optimizer_source_changed')
        new_size = temporary.stat().st_size
        if new_size > MAX_FILE: raise ValueError('optimizer_file_limit')
        result_identity, result_digest = signature(temporary), digest(temporary)
        if signature(temporary) != result_identity: raise ValueError('optimizer_prepared_file_changed')
        if indexed:
            if safe_path(source_snapshot['path']) != source_path:
                raise ValueError('optimizer_source_identity_path_changed')
            observed = MetadataExtractor.extract(temporary, strict=True)
            generation = dict(width=dimensions[0], height=dimensions[1], tool=observed.tool,
                model=observed.model, positive=observed.positive_prompt,
                negative=observed.negative_prompt, seed=str(observed.seed))
            request = dict(expected_fingerprint=source_snapshot['fingerprint'],
                source_path=source_snapshot['path'], path=str(output_path),
                destination_paths=path_variants(output_path, output_path), producer='image_optimizer',
                source_digest=source_digest.hex(), result_digest=result_digest.hex(),
                generation=generation, observation=dict(filename=output_path.name,
                    size=new_size, created=temporary.stat().st_ctime))
            publication = DerivedPublication(native_service, source_snapshot['id'], request, journal_root=journal_root)
            publication.ready(before, result_identity, temporary, cancel_event)
        # La aprobación Native no reserva ni congela el disco: repetir guardas físicas.
        cancelled()
        if safe_path(source_path) != source_path or signature(source_path) != before or digest(source_path) != source_digest:
            raise ValueError('optimizer_source_changed')
        if signature(temporary) != result_identity or digest(temporary) != result_digest:
            raise ValueError('optimizer_prepared_file_changed')
        if publication is not None: publication.scope(cancel_event)
        destination(check_native=not indexed)
        cancelled()
        if os.name == 'nt': os.rename(temporary, output_path)
        else:
            os.link(temporary, output_path)
            published = True  # El link ya publicó incluso si unlink falla.
            temporary.unlink()
        temporary = None
        published = True
        # No cancelar hashing/ack después del efecto físico. Un fallo es parcial.
        if indexed:
            safe_path(output_path, portable=True)
            final_identity = signature(output_path)
            if digest(output_path, honour_cancel=False) != result_digest or signature(output_path) != final_identity:
                raise ValueError('optimizer_published_file_changed')
            if signature(source_path) != before or digest(source_path, honour_cancel=False) != source_digest:
                raise ValueError('optimizer_source_changed')
            info = output_path.stat()
            publication.register(dict(filename=output_path.name, size=info.st_size, created=info.st_ctime), final_identity)
            if (signature(output_path) != final_identity or digest(output_path, honour_cancel=False) != result_digest
                    or signature(output_path) != final_identity):
                raise ValueError('optimizer_published_file_changed')
            publication.complete()

        # 9. Calculate stats
        orig_size = before[2]
        
        return {
            "success": True,
            "published": True,
            "status": "complete",
            "native_registered": publication.native_registered if publication else False,
            "copy_id": publication.receipt["copy_id"] if publication and publication.receipt else None,
            "journal": str(publication.path) if publication and publication.path else "",
            "output_path": str(output_path),
            "original_size": orig_size,
            "new_size": new_size,
            "saved_bytes": orig_size - new_size,
            "saved_percent": ((orig_size - new_size) / orig_size) * 100 if orig_size > 0 else 0,
            "format_changed": target_format != original_format,
            "metadata_preserved": bundle is not None
        }
    
    except Exception as e:
        error = str(e)[:1000]
        if publication is not None: publication.failed(error, published)
        return dict(success=False, error=error, status='partial' if published else 'error',
            published=published, native_registered=publication.native_registered if publication else False,
            journal=str(publication.path) if publication and publication.path else '',
            output_path=str(output_path) if published else '')
    finally:
        if img is not None: img.close()
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


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
    key = hashlib.sha256(os.path.normcase(str(source_path.resolve())).encode("utf-8")).hexdigest()[:16]
    filename = f"{source_path.stem[:140]}--{key}{source_path.suffix}"
    
    if suffix:
        stem = source_path.stem
        ext = source_path.suffix
        filename = f"{stem[:140]}{suffix}--{key}{ext}"
    
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
        output = get_export_path(source, export_dir=output_dir)
        
        result = optimize_image(str(source), str(output), **kwargs)
        result["source_path"] = str(source)
        result["index"] = i + 1
        result["total"] = total
        
        yield i + 1, total, result
