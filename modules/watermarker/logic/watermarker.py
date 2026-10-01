"""Public copies: bounded rendering, verified privacy and new-path publication."""
import hashlib
import logging
import math
import os
from pathlib import Path
import tempfile
import uuid

from PIL import Image
from core.output_paths import safe_path, path_variants
from core.paths import CachePaths

MAX_FILE = 256 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_TILES = 1_000_000
FORMATS = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP', '.avif': 'AVIF'}
log = logging.getLogger(__name__)


class WatermarkCancelled(Exception):
    pass


def _cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise WatermarkCancelled('watermarker_cancelled')


def _dimensions(size):
    w, h = size
    if min(w, h) < 1 or max(w, h) > 32768 or w * h > MAX_PIXELS:
        raise ValueError('watermarker_image_limit')


def _identity(path):
    info = path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(path, cancel_event):
    value, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            _cancel(cancel_event)
            size += len(block)
            if size > MAX_FILE:
                raise ValueError('watermarker_file_limit')
            value.update(block)
    return value.hexdigest()


def _inputs(source_path, watermark_path, logo_path, cancel_event):
    paths, observed = [], []
    for entry in (source_path, watermark_path, logo_path):
        if entry is None:
            paths.append(None)
            continue
        path = safe_path(Path(entry).absolute())
        before = _identity(path)
        if before[2] > MAX_FILE:
            raise ValueError('watermarker_file_limit')
        digest = _digest(path, cancel_event)
        if _identity(path) != before:
            raise ValueError('watermarker_input_changed')
        paths.append(path)
        observed.append((path, before, digest))
    return paths, observed


def _unchanged(observed, cancel_event):
    for path, before, digest in observed:
        _cancel(cancel_event)
        if safe_path(path) != path or _identity(path) != before or _digest(path, cancel_event) != digest:
            raise ValueError('watermarker_input_changed')


def _load_asset(path, target_size=None):
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError('watermarker_file_limit')
    if path.suffix.lower() == '.svg':
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtCore import QSize, Qt
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('watermarker_svg_limit')
        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            raise ValueError('watermarker_invalid_svg')
        size = target_size or (renderer.defaultSize().width(), renderer.defaultSize().height())
        _dimensions(size)
        qimg = QImage(QSize(*size), QImage.Format_RGBA8888)
        if qimg.isNull():
            raise ValueError('watermarker_image_limit')
        qimg.fill(Qt.transparent)
        painter = QPainter(qimg)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        return Image.frombytes('RGBA', size, qimg.bits().tobytes(), 'raw', 'RGBA', qimg.bytesPerLine())
    with Image.open(path) as image:
        _dimensions(image.size)
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError('watermarker_single_frame_required')
        result = image.convert('RGBA')
    if target_size:
        _dimensions(target_size)
        resized = result.resize(target_size, Image.Resampling.LANCZOS)
        result.close()
        result = resized
    return result


def apply_watermark_pattern(base_image, watermark_path, angle=0, scale=1.0, opacity=0.3,
                            cancel_event=None):
    if not all(math.isfinite(value) for value in (angle, scale, opacity)) or not 0.1 <= scale <= 2 or not 0 <= opacity <= 1:
        raise ValueError('watermarker_invalid_parameters')
    _dimensions(base_image.size)
    with _load_asset(watermark_path) as asset:
        size = (max(1, int(asset.width * scale)), max(1, int(asset.height * scale)))
        _dimensions(size)
        with asset.resize(size, Image.Resampling.LANCZOS) as scaled:
            # Reject the upper bound before allocating an expanded rotation.
            radians = math.radians(angle)
            _dimensions((math.ceil(abs(size[0]*math.cos(radians))+abs(size[1]*math.sin(radians)))+2,
                         math.ceil(abs(size[0]*math.sin(radians))+abs(size[1]*math.cos(radians)))+2))
            with scaled.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC) as mark:
                columns = math.ceil(base_image.width / mark.width)
                rows = math.ceil(base_image.height / mark.height)
                if rows * columns > MAX_TILES:
                    raise ValueError('watermarker_tile_limit')
                with Image.new('RGBA', base_image.size) as pattern:
                    for y in range(0, base_image.height, mark.height):
                        for x in range(0, base_image.width, mark.width):
                            _cancel(cancel_event)
                            pattern.paste(mark, (x, y), mark)
                    with pattern.getchannel('A') as alpha:
                        with alpha.point(lambda value: int(value * opacity)) as faded:
                            pattern.putalpha(faded)
                    _cancel(cancel_event)
                    return Image.alpha_composite(base_image, pattern)


def apply_logo(base_image, logo_path, position='top-right', size=150, cancel_event=None):
    if position not in ('top-left', 'top-right', 'bottom-left', 'bottom-right') or not isinstance(size, int) or not 1 <= size <= 500:
        raise ValueError('watermarker_invalid_parameters')
    _dimensions(base_image.size)
    with _load_asset(logo_path) as asset:
        target = (size, max(1, int(size * asset.height / asset.width)))
        _dimensions(target)
        with asset.resize(target, Image.Resampling.LANCZOS) as logo:
            margin = 25
            x = margin if position.endswith('left') else base_image.width - logo.width - margin
            y = margin if position.startswith('top') else base_image.height - logo.height - margin
            with Image.new('RGBA', base_image.size) as layer:
                layer.paste(logo, (x, y), logo)
                _cancel(cancel_event)
                return Image.alpha_composite(base_image, layer)


def render_image(source_path, watermark_path=None, logo_path=None, wm_angle=0, wm_scale=1.0,
                 wm_opacity=0.3, logo_position='top-right', logo_size=150, cancel_event=None):
    _cancel(cancel_event)
    image = _load_asset(source_path)
    try:
        if watermark_path:
            next_image = apply_watermark_pattern(image, watermark_path, wm_angle, wm_scale, wm_opacity, cancel_event)
            image.close()
            image = next_image
        if logo_path:
            next_image = apply_logo(image, logo_path, logo_position, logo_size, cancel_event)
            image.close()
            image = next_image
        _cancel(cancel_event)
        # A fresh image cannot inherit EXIF, XMP, ICC, PNG text or prompts.
        clean = Image.new('RGBA', image.size)
        clean.paste(image)
        return clean
    finally:
        image.close()


def render_preview(source_path, *, cancel_event=None, preview_size=(1600, 1200), **options):
    _dimensions(preview_size)
    paths, observed = _inputs(source_path, options.get('watermark_path'), options.get('logo_path'), cancel_event)
    options.update(watermark_path=paths[1], logo_path=paths[2])
    image = render_image(paths[0], cancel_event=cancel_event, **options)
    try:
        image.thumbnail(preview_size, Image.Resampling.LANCZOS)
        _unchanged(observed, cancel_event)
        return image
    except Exception:
        image.close()
        raise


def _destination(output_path, native_service, cancel_event):
    destination = safe_path(output_path, exists=False, portable=True)
    if os.path.lexists(destination):
        raise ValueError('watermarker_requires_new_output')
    if native_service is not None and native_service.read('get_metadata_path_status',
            path_variants(destination, output_path), cancel_event=cancel_event)['occupied']:
        raise ValueError('watermarker_output_has_native_history')
    return destination


def _verify_output(path, dimensions, expected_format):
    with Image.open(path) as image:
        if image.format != expected_format or image.size != dimensions or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('watermarker_invalid_output')
        image.load()
        intrinsic = {'jfif', 'jfif_version', 'jfif_unit', 'jfif_density', 'loop', 'background', 'timestamp', 'duration'}
        if image.getexif() or set(image.info) - intrinsic or getattr(image, 'text', {}):
            raise ValueError('watermarker_privacy_verification_failed')


def process_image(source_path, output_path=None, watermark_path=None, logo_path=None,
                  wm_angle=0, wm_scale=1.0, wm_opacity=0.3,
                  logo_position='top-right', logo_size=150, *, cancel_event=None, native_service=None):
    """Publish a new verified public copy. Never overwrite or inherit Native curation.

    Cancellation is honored before publication. Published bytes are kept; no retry
    or rollback is inferred from a late cancellation. This API returns (bool, text).
    Native is consulted only to protect destination history; this public copy is
    not automatically registered as a dataset derivative.
    """
    temporary, owned_identity = None, None
    try:
        _cancel(cancel_event)
        paths, observed = _inputs(source_path, watermark_path, logo_path, cancel_event)
        output = Path(output_path).absolute() if output_path is not None else get_export_path(paths[0])
        fmt = FORMATS.get(output.suffix.lower())
        if fmt is None:
            raise ValueError('watermarker_unsupported_format')
        output = _destination(output, native_service, cancel_event)
        fd, name = tempfile.mkstemp(prefix='.panopticon-watermark-', suffix=output.suffix, dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        owned_identity = _identity(temporary)[:2]
        with render_image(paths[0], paths[1], paths[2], wm_angle, wm_scale, wm_opacity,
                          logo_position, logo_size, cancel_event) as image:
            dimensions = image.size
            _cancel(cancel_event)
            if fmt == 'JPEG':
                with Image.new('RGB', dimensions, 'white') as rgb:
                    with image.getchannel('A') as alpha:
                        rgb.paste(image, mask=alpha)
                    rgb.save(temporary, fmt, quality=95, optimize=True)
            else:
                options = {'quality': 95} if fmt in ('WEBP', 'AVIF') else {'optimize': True}
                image.save(temporary, fmt, **options)
        if temporary.stat().st_size > MAX_FILE:
            raise ValueError('watermarker_file_limit')
        _verify_output(temporary, dimensions, fmt)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        prepared = _identity(temporary)
        _unchanged(observed, cancel_event)
        if _destination(output, native_service, cancel_event) != output or _identity(temporary) != prepared:
            raise ValueError('watermarker_prepared_file_changed')
        _cancel(cancel_event)
        if os.name == 'nt':
            os.rename(temporary, output)  # Windows rename refuses an existing destination.
        else:
            os.link(temporary, output)   # New directory entry; never replaces a winner.
        return True, f'Processed: {output.name}'
    except Exception as error:
        return False, str(error)
    finally:
        try:
            if temporary is not None and os.path.lexists(temporary) and _identity(temporary)[:2] == owned_identity:
                temporary.unlink()
        except OSError:
            log.warning('Could not remove owned Watermarker temporary %s', temporary, exc_info=True)


def get_export_path(source_path, suffix='_watermarked'):
    source = Path(source_path)
    if len(suffix) > 100 or any(character in suffix for character in '/\\:'):
        raise ValueError('watermarker_invalid_suffix')
    identity = hashlib.sha256(str(source.absolute()).encode('utf-8')).hexdigest()[:12]
    name = f'{source.stem[:100]}{suffix}_{identity}_{uuid.uuid4().hex[:12]}{source.suffix}'
    return CachePaths.get_tool_cache('watermarker') / name


def batch_process(files, **kwargs):
    total = len(files)
    for index, source in enumerate(files, 1):
        _cancel(kwargs.get('cancel_event'))
        success, message = process_image(source, **kwargs)
        yield index, total, {'success': success, 'message': message}
