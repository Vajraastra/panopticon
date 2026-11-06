"""Read confirmed originals for the gallery without computing embeddings."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from pathlib import Path

from PIL import Image

from .export import FileSignature, file_signature
from .recognition import Sample, _Sampler, _check_cancelled, image_thumbnail, read_image


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfirmedPreview:
    paths: tuple[Path, ...]
    signatures: tuple[FileSignature, ...]
    thumbnails: tuple[bytes, ...]


def _require_signature(path, expected):
    try:
        current = file_signature(path)
    except OSError as error:
        raise ValueError("source_changed") from error
    if current != expected:
        raise ValueError("source_changed")


def load_previews(signatures: Mapping[Path, FileSignature], cancelled: Callable[[], bool],
                  progress: Callable[[int, int], None]) -> ConfirmedPreview:
    """Load one gallery page while keeping failed originals visible."""
    paths, thumbnails = tuple(signatures), []
    progress(0, len(paths))
    for done, path in enumerate(paths, start=1):
        _check_cancelled(cancelled)
        expected = signatures[path]
        try:
            _require_signature(path, expected)
            with read_image(path, expected) as image:
                thumbnail = image_thumbnail(image)
            _require_signature(path, expected)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            log.debug("Character Separator could not preview %s", path, exc_info=True)
            thumbnail = b""
        thumbnails.append(thumbnail)
        progress(done, len(paths))
    _check_cancelled(cancelled)
    return ConfirmedPreview(paths, tuple(signatures.values()), tuple(thumbnails))


def load_samples(signatures: Mapping[Path, FileSignature], cache: dict[Path, Sample],
                 cancelled: Callable[[], bool]) -> dict[Path, Sample]:
    """Recover exact CCIP samples for assignments that only have export receipts."""
    sampler = _Sampler(cancelled, cache)
    samples = {}
    for path, expected in signatures.items():
        _check_cancelled(cancelled)
        _require_signature(path, expected)
        result = sampler.read(path, thumbnail=False)
        if result is None or result[0].signature != expected:
            raise ValueError("source_changed")
        _require_signature(path, expected)
        samples[path] = result[0]
    _check_cancelled(cancelled)
    return samples
