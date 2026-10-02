"""Cooperate with BBox saves and preserve concurrent draft edits."""
from contextlib import contextmanager
import os
from pathlib import Path
import uuid
from core.image_preview import _identity
from core.output_paths import safe_path
from .editor_save import digest


@contextmanager
def draft_guard(path):
    path = Path(path).absolute()
    safe_path(path.parent, portable=True)
    if any(os.path.lexists(path.parent / marker) for marker in ('catalog.db', 'panopticon.curation.db')):
        raise ValueError('metadata_cherry_original_protected')
    guard = path.with_name(path.name + '.bbox.guard')
    fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        stream.write(uuid.uuid4().hex)
        stream.flush()
        os.fsync(stream.fileno())
    identity = _identity(guard)
    before = _identity(path)
    try:
        expected_digest = digest(path) if before is not None else None
        def current():
            safe_path(path, exists=os.path.lexists(path), portable=True)
            if _identity(guard) != identity or _identity(path) != before:
                raise ValueError('ideogram_draft_changed; reload before saving')
            if before is not None and digest(path) != expected_digest:
                raise ValueError('ideogram_draft_changed; reload before saving')
        yield current
    finally:
        if _identity(guard) == identity:
            guard.unlink()
