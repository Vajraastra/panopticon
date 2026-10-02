"""Publish a verified image/caption pair; interrupted pairs stay for review."""
from contextlib import ExitStack
from glob import escape
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from core.image_preview import _identity, MAX_SOURCE_BYTES
from core.output_paths import safe_path
from core.pinned_file import PinnedFile
from .sidecar import IMAGE_EXTS, caption_path, should_process

CAPTION_LIMIT = 8 * 1024 * 1024


def caption_snapshot(path):
    identity = _identity(path)
    if identity is None:
        if os.path.lexists(path):
            raise ValueError('caption_target_requires_review')
        return None, None
    safe_path(path, portable=True)
    with open(path, 'rb') as stream:
        data = stream.read(CAPTION_LIMIT + 1)
    if len(data) > CAPTION_LIMIT:
        raise ValueError('caption_size_limit')
    if _identity(path) != identity:
        raise ValueError('caption_changed; reload before writing')
    return identity, data


class CaptionPair:
    def __init__(self, source, out, policy, cancel, *, caption_ext='.txt',
                 source_sha256=None, image_sha256=None):
        self.source = Path(source).absolute()
        self.out = Path(out).absolute()
        self.policy, self.cancel = policy, cancel
        if caption_ext not in ('.txt', '.json'):
            raise ValueError('caption_extension')
        self.caption_ext = caption_ext
        self.expected_source = source_sha256
        self.expected_image = image_sha256
        self.stack = ExitStack()
        self.guard = self.guard_identity = None
        self.temps = []
        self.published_image = self.published_caption = False
        self.token = uuid.uuid4().hex

    def __enter__(self):
        try:
            self.source = safe_path(self.source)
            self.out = safe_path(self.out, portable=True)
            info = self.out.stat()
            self.out_identity = (info.st_dev, info.st_ino)
            self.image = safe_path(self.out / self.source.name,
                                   exists=os.path.lexists(self.out / self.source.name), portable=True)
            caption = caption_path(self.out, self.source, self.caption_ext)
            self.caption = safe_path(caption, exists=os.path.lexists(caption), portable=True)
            if self.source == self.image or (self.image.exists() and os.path.samefile(self.source, self.image)):
                raise ValueError('caption_original_protected')
            self.guard = self.caption.with_name(self.caption.name + '.caption.guard')
            safe_path(self.guard, exists=os.path.lexists(self.guard), portable=True)
            fd = os.open(self.guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w', encoding='ascii') as stream:
                stream.write(self.token)
                stream.flush()
                os.fsync(stream.fileno())
            self.guard_identity = _identity(self.guard)
            self.pin = self.stack.enter_context(PinnedFile(self.source))
            self.source_identity = self.pin.check()
            if self.source.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError('preview_file_limit')
            self.pin.stream.seek(0)
            self.source_hash = hashlib.file_digest(self.pin.stream, 'sha256').hexdigest()
            self.pin.verify(self.source_hash, self.source_identity)
            if self.expected_source is not None and self.expected_source != self.source_hash:
                raise ValueError('caption_draft_source_changed; review the draft')
            target_hash = self.expected_image or self.source_hash
            if target_hash != self.source_hash and self.expected_source is None:
                raise ValueError('caption_composition_requires_source_proof')
            # A caption stem can describe only one image, including PNG/JPEG collisions.
            self._check_stem()
            self.image_pin = None
            if self.image.exists():
                self.image_pin = self.stack.enter_context(PinnedFile(self.image))
                self.image_identity = self.image_pin.check()
                self.image_pin.verify(target_hash, self.image_identity)
            elif target_hash != self.source_hash:
                raise ValueError('caption_composition_missing')
            self.caption_before, self.caption_bytes = caption_snapshot(self.caption)
            if self.caption_before is not None and self.image_pin is None:
                raise ValueError('caption_without_verified_image; review the existing output')
            self.process, self.mode = should_process(self.caption, self.policy)
            self.record = dict(version=1, token=self.token, source=str(self.source),
                               source_sha256=self.source_hash, image_sha256=target_hash, image=str(self.image),
                               caption=str(self.caption), phase='prepared')
            self._record()
            return self
        except BaseException as error:
            self._close(error)
            raise

    def _record(self):
        if _identity(self.guard) != self.guard_identity:
            raise ValueError('caption_guard_changed')
        fd, name = tempfile.mkstemp(prefix='.caption-guard-', suffix='.tmp', dir=self.out)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.guard)
            self.guard_identity = _identity(self.guard)
        finally:
            Path(name).unlink(missing_ok=True)

    def _current(self):
        safe_path(self.out, portable=True)
        info = self.out.stat()
        if (info.st_dev, info.st_ino) != self.out_identity:
            raise ValueError('caption_output_folder_changed')
        self._check_stem()
        if _identity(self.guard) != self.guard_identity:
            raise ValueError('caption_guard_changed')
        self.pin.check()
        if self.image_pin:
            self.image_pin.check()
        elif os.path.lexists(self.image):
            raise ValueError('caption_image_destination_occupied')
        if caption_snapshot(self.caption) != (self.caption_before, self.caption_bytes):
            raise ValueError('caption_changed; reload before writing')

    def _check_stem(self):
        for entry in self.out.glob(escape(self.source.stem) + '.*'):
            if self.cancel():
                raise ValueError('caption_cancelled')
            if entry.suffix.lower() in IMAGE_EXTS and entry != self.image:
                # Ask the filesystem whether this spelling shares the stem.
                # Python's Windows glob can also find distinct case-sensitive names.
                spelling = self.out / (self.source.stem + entry.suffix)
                if os.path.lexists(spelling) and os.path.samefile(entry, spelling):
                    raise ValueError('caption_stem_conflict; separate these images before exporting')

    def _temp(self, suffix):
        fd, name = tempfile.mkstemp(prefix='.panopticon-caption-', suffix=suffix, dir=self.out)
        os.close(fd)
        path = Path(name)
        self.temps.append(path)
        return path

    def publish(self, text, sep=', '):
        if self.cancel():
            return False
        data = (text or '').encode('utf-8')
        if self.mode == 'a' and text:
            data = (self.caption_bytes or b'') + sep.encode('utf-8') + data
        elif self.mode == 'a':
            data = self.caption_bytes or b''
        if len(data) > CAPTION_LIMIT:
            raise ValueError('caption_size_limit')
        caption_temp = self._temp('.txt.tmp')
        with caption_temp.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        image_temp = None
        if self.image_pin is None:
            image_temp = self._temp(self.source.suffix)
            shutil.copy2(self.source, image_temp)
            with image_temp.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != self.source_hash:
                    raise ValueError('caption_copy_verification_failed')
            with image_temp.open('r+b') as stream:
                os.fsync(stream.fileno())
        if self.cancel():
            return False
        self._current()
        if image_temp:
            if os.name != 'nt':
                raise ValueError('caption_publication_requires_windows')
            os.rename(image_temp, self.image)  # Never replace a competing image.
            self.published_image = True
            self.record['phase'] = 'image_published'
            self._record()
            self.image_pin = self.stack.enter_context(PinnedFile(self.image))
            self.image_identity = self.image_pin.check()
            self.image_pin.verify(self.source_hash, self.image_identity)
        # Finish a started publication; late cancellation is not a rollback.
        if caption_snapshot(self.caption) != (self.caption_before, self.caption_bytes):
            raise ValueError('caption_changed; reload before writing')
        if self.caption_before is None:
            if os.name != 'nt':
                raise ValueError('caption_publication_requires_windows')
            os.rename(caption_temp, self.caption)
        else:
            os.replace(caption_temp, self.caption)
        self.published_caption = True
        self.record['phase'] = 'complete'
        self._record()
        return True

    def _close(self, error=None):
        self.stack.close()
        for path in self.temps:
            path.unlink(missing_ok=True)
        if self.guard is None or self.guard_identity is None:
            return
        if error is not None and (self.published_image or self.published_caption):
            self.record.update(phase='review_required', error=str(error)[:512])
            try:
                self._record()
            except Exception:
                pass  # The last durable guard is retained for explicit review.
        elif _identity(self.guard) == self.guard_identity:
            self.guard.unlink()

    def __exit__(self, kind, error, traceback):
        self._close(error)
        if error is not None and (self.published_image or self.published_caption):
            raise ValueError(f'{error}; caption_pair_requires_review: {self.guard}') from error
