"""Bounded caption edits tied to one image and the text actually reviewed."""
from glob import escape
import os
from pathlib import Path
import tempfile

from core.image_preview import MAX_SOURCE_BYTES, _identity
from core.output_paths import safe_path
from core.pinned_file import PinnedFile
from .caption_publication import caption_snapshot, CAPTION_LIMIT
from .sidecar import IMAGE_EXTS


def image_for(path):
    path = safe_path(Path(path).absolute(), portable=True)
    if path.suffix.lower() != '.txt':
        raise ValueError('review_caption_extension')
    candidates = []
    for entry in path.parent.glob(escape(path.stem) + '.*'):
        spelling = path.parent / (path.stem + entry.suffix)
        if (entry.suffix.lower() in IMAGE_EXTS and os.path.lexists(spelling)
                and os.path.samefile(entry, spelling)):
            candidates.append(safe_path(entry))
    if len(candidates) != 1:
        raise ValueError('review_requires_one_image; missing or ambiguous image')
    return candidates[0]


def load(path):
    path = safe_path(Path(path).absolute(), portable=True)
    before, raw = caption_snapshot(path)
    if before is None:
        raise ValueError('review_caption_missing')
    text = raw.decode('utf-8')
    image = image_for(path)
    with PinnedFile(image) as pin:
        image_identity = pin.check()
        if image.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('preview_file_limit')
        if caption_snapshot(path) != (before, raw):
            raise ValueError('review_caption_changed; reload before saving')
    return dict(path=path, image=image, image_identity=image_identity,
                identity=before, raw=raw, text=text)


def write(path, text, expected=None):
    expected = load(path) if expected is None else expected
    path = safe_path(Path(path).absolute(), portable=True)
    if path != expected['path']:
        raise ValueError('review_caption_context_changed')
    raw = (text or '').encode('utf-8')
    if len(raw) > CAPTION_LIMIT:
        raise ValueError('caption_size_limit')
    guard = path.with_name(path.name + '.caption.guard')
    fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        stream.write('caption_review')
        stream.flush()
        os.fsync(stream.fileno())
    owned = _identity(guard)
    temporary = None
    committed = False
    try:
        if image_for(path) != expected['image']:
            raise ValueError('review_image_changed')
        with PinnedFile(expected['image']) as pin:
            if pin.check() != expected['image_identity']:
                raise ValueError('review_image_changed')
            fd, name = tempfile.mkstemp(prefix='.caption-edit-', suffix='.tmp', dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            safe_path(path, portable=True)
            if (_identity(guard) != owned or image_for(path) != expected['image']
                    or caption_snapshot(path) != (expected['identity'], expected['raw'])):
                raise ValueError('review_caption_changed; reload before saving')
            pin.check()
            os.replace(temporary, path)
            committed = True
            after, actual = caption_snapshot(path)
            if actual != raw:
                raise ValueError('review_caption_changed_after_commit')
            result = dict(expected, identity=after, raw=raw, text=text or '')
        committed = False  # Complete publication; the cooperative guard can go.
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if not committed and _identity(guard) == owned:
            guard.unlink()
