"""
Smart Cropper Logic - Refactored v2
Usa el nuevo sistema de metadata centralizado.
"""
import os
import hashlib
import math
import tempfile
from pathlib import Path
from PIL import Image

from core.paths import CachePaths
from core.output_paths import safe_path, path_variants
from core.derived_publication import DerivedPublication
from core.metadata.output_stamp import MAX_FILE
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


class CropPublicationError(OSError):
    """Published bytes remain for review; callers must not blindly repeat."""
    def __init__(self, result):
        super().__init__(result['error'])
        self.result = result


def crop_image(source_path, selection_norm, output_path=None,
               preserve_metadata=True, tags=None, rating=0, cancel_event=None,
               source_snapshot=None, native_service=None, journal_root=None,
               return_details=False):
    """Crop to a new local file; originals and existing destinations are protected.

    A captured native source requires explicitly prepared derived schema. Its
    output receives its own ID/SHA/generation and recovery journal. External
    sources remain unregistered. By default return the path or raise; workers
    request details to report partial publication honestly. Never retry a
    published failure without reviewing its file, journal and historical receipt.
    """
    source_path = Path(source_path)
    output_path = Path(output_path) if output_path is not None else get_export_path(source_path)
    indexed = source_snapshot is not None and source_snapshot.get('id') is not None
    temporary, publication, published = None, None, False

    def cancelled():
        if cancel_event is not None and cancel_event.is_set(): raise CropCancelled()

    def signature(path):
        info = path.stat(follow_symlinks=False)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def digest(path, honour_cancel=True):
        value, size = hashlib.sha256(), 0
        with open(path, 'rb') as image:
            while block := image.read(1024*1024):
                if honour_cancel: cancelled()
                size += len(block)
                if size > MAX_FILE: raise ValueError('cropper_file_limit')
                value.update(block)
        return value.hexdigest()

    def destination(check_native):
        _check_destination(source_path, output_path)
        output = safe_path(output_path, exists=False, portable=True)
        if os.path.lexists(output): raise ValueError('cropper_requires_new_output')
        if check_native and native_service is not None and native_service.read(
                'get_metadata_path_status', path_variants(output, output_path),
                cancel_event=cancel_event)['occupied']:
            raise ValueError('cropper_output_has_native_history')
        return output

    try:
        cancelled()
        if indexed and native_service is None: raise ValueError('derived_native_service_required')
        source_path = safe_path(source_path)
        before = signature(source_path)
        source_digest = digest(source_path)
        if signature(source_path) != before: raise ValueError('cropper_source_changed')
        _check_destination(source_path, output_path)
        values = (selection_norm.x(), selection_norm.y(), selection_norm.width(), selection_norm.height())
        if not all(math.isfinite(value) for value in values) or min(values[2:]) <= 0:
            raise ValueError('cropper_invalid_selection')
        source_bundle = MetadataExtractor.extract(source_path, strict=True) if preserve_metadata else None
        cancelled()
        bundle = None
        if preserve_metadata and source_bundle and source_bundle.is_valid():
            if tags: source_bundle.tags = list(dict.fromkeys(source_bundle.tags + list(tags)))
            if rating: source_bundle.rating = rating
            bundle = source_bundle
        elif tags or rating:
            bundle = MetadataBundle(tags=list(tags or []), rating=rating)
        # Fresh path first; a dialog must not authorize replacing another copy.
        output_path = destination(check_native=False)
        fd, name = tempfile.mkstemp(prefix='.panopticon-crop-', suffix=output_path.suffix,
                                    dir=output_path.parent)
        os.close(fd)
        temporary = Path(name)
        with Image.open(source_path) as img:
            w, h = img.size
            if w*h > 20_000_000 or max(w,h) > 32768 or getattr(img,'n_frames',1) != 1:
                raise ValueError('cropper_image_limit')
            left = max(0, min(int(selection_norm.x()*w), w-1))
            top = max(0, min(int(selection_norm.y()*h), h-1))
            right = max(left+1, min(int(selection_norm.right()*w), w))
            bottom = max(top+1, min(int(selection_norm.bottom()*h), h))
            with img.crop((left, top, right, bottom)) as cropped:
                ext = output_path.suffix.lower()
                if ext in ('.jpg','.jpeg'): cropped.save(str(temporary), quality=95, subsampling=0)
                elif ext == '.avif':
                    options = MetadataStamper.encoder_options(bundle) if bundle is not None else {}
                    cropped.save(str(temporary), quality=95, **options)
                elif ext == '.webp': cropped.save(str(temporary), quality=95)
                else: cropped.save(str(temporary))
        if bundle is not None:
            confirmed = (MetadataStamper.verify_prepared(temporary,bundle) if ext == '.avif'
                         else MetadataStamper.stamp(temporary,bundle,prepared=True))
            if confirmed is not True: raise OSError('Metadata stamping failed')
        with Image.open(temporary) as verified:
            dimensions = verified.size
            if dimensions != (right-left,bottom-top): raise ValueError('cropper_invalid_output')
            verified.load()
        with open(temporary,'r+b') as output: os.fsync(output.fileno())
        result_identity = signature(temporary)
        result_digest = digest(temporary)
        if signature(temporary) != result_identity: raise ValueError('cropper_prepared_file_changed')
        if indexed:
            if safe_path(source_snapshot['path']) != source_path:
                raise ValueError('cropper_source_identity_path_changed')
            observed = MetadataExtractor.extract(temporary,strict=True)
            request = dict(expected_fingerprint=source_snapshot['fingerprint'],
                source_path=source_snapshot['path'], path=str(output_path),
                destination_paths=path_variants(output_path,output_path), producer='image_cropper',
                source_digest=source_digest,result_digest=result_digest,
                generation=dict(width=dimensions[0],height=dimensions[1],tool=observed.tool,
                    model=observed.model,positive=observed.positive_prompt,
                    negative=observed.negative_prompt,seed=str(observed.seed)),
                observation=dict(filename=output_path.name,size=result_identity[2],created=temporary.stat().st_ctime))
            publication = DerivedPublication(native_service,source_snapshot['id'],request,journal_root=journal_root)
            publication.ready(before,result_identity,temporary,cancel_event)
        cancelled()
        if safe_path(source_path) != source_path or signature(source_path) != before or digest(source_path) != source_digest:
            raise ValueError('cropper_source_changed')
        if signature(temporary) != result_identity or digest(temporary) != result_digest:
            raise ValueError('cropper_prepared_file_changed')
        if publication is not None: publication.scope(cancel_event)
        destination(check_native=not indexed)
        cancelled()
        if os.name == 'nt': os.rename(temporary,output_path)
        else:
            os.link(temporary,output_path)
            published = True
            temporary.unlink()
        temporary = None
        published = True
        # The physical effect cannot be undone by cancelling registration/ack.
        if indexed:
            safe_path(output_path,portable=True)
            final_identity = signature(output_path)
            if digest(output_path,False) != result_digest or signature(output_path) != final_identity:
                raise ValueError('cropper_published_file_changed')
            if signature(source_path) != before or digest(source_path,False) != source_digest:
                raise ValueError('cropper_source_changed')
            info = output_path.stat()
            publication.register(dict(filename=output_path.name,size=info.st_size,created=info.st_ctime),final_identity)
            if signature(output_path) != final_identity or digest(output_path,False) != result_digest or signature(output_path) != final_identity:
                raise ValueError('cropper_published_file_changed')
            publication.complete()
        result = dict(path=str(output_path),error=None,cancelled=False,source_destination=False,
            published=True,native_registered=publication.native_registered if publication else False,
            copy_id=publication.receipt['copy_id'] if publication and publication.receipt else None,
            journal=str(publication.path) if publication and publication.path else '',status='complete')
        return result if return_details else str(output_path)
    except Exception as error:
        reason = str(error)[:1000]
        if publication is not None: publication.failed(reason,published)
        result = dict(path=str(output_path) if published else None,error=reason,
            cancelled=isinstance(error,CropCancelled) or (not published and cancel_event is not None and cancel_event.is_set()),
            source_destination=isinstance(error,SourceDestinationError),published=published,
            native_registered=publication.native_registered if publication else False,
            journal=str(publication.path) if publication and publication.path else '',
            status='partial' if published else 'error')
        if return_details: return result
        if published: raise CropPublicationError(result) from error
        raise
    finally:
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


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
    key = hashlib.sha256(os.path.normcase(str(source_path.resolve())).encode("utf-8")).hexdigest()[:16]
    filename = f"{stem[:140]}_{key}{suffix}{ext}"
    
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
