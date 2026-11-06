"""Exporta una selección a carpetas hermanas sin registrar archivos en Native."""
from contextlib import ExitStack
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import stat
import tempfile

from core.output_paths import safe_path
from core.path_identity import local_path
from core.pinned_file import PinnedFile


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".avif"}
_SIDECAR_EXTENSIONS = IMAGE_EXTENSIONS | {".txt"}
_BLOCK_SIZE = 1024 * 1024
_KNOWN_ERRORS = {"invalid_scope", "invalid_name", "destination_conflict", "source_changed",
                 "caption_conflict", "copy_failed"}
FileSignature = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class ExportResult:
    """Source paths for resuming, and verified destination images for handoff."""

    copied: int = 0
    skipped: int = 0
    cancelled: bool = False
    completed: tuple[Path, ...] = ()
    destinations: tuple[tuple[str, Path], ...] = ()


class ExportError(Exception):
    def __init__(self, code, result, path=None):
        super().__init__(code)
        self.code, self.result, self.path = code, result, path


class _Cancelled(Exception):
    pass


def validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("invalid_name")
    name = name.strip()
    try:
        if name and name not in (".", "..") and len(name.encode("utf-16-le")) <= 510:
            if not ntpath.isreserved(name) and not any(mark in name for mark in "\\/:"):
                return name
    except UnicodeEncodeError:
        pass
    raise ValueError("invalid_name")


def signature(info: os.stat_result) -> FileSignature:
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("source_changed")
    created = getattr(info, "st_birthtime_ns", info.st_ctime_ns)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, created


def file_signature(path: Path) -> FileSignature:
    return signature(path.lstat())


def _directory(path, code):
    try:
        resolved = safe_path(local_path(str(path)))
        if resolved.is_dir():
            return resolved
    except (OSError, ValueError):
        pass
    raise ValueError(code)


def _by_stem(directory):
    entries = {}
    for entry in directory.iterdir():
        if entry.suffix.lower() in _SIDECAR_EXTENSIONS:
            entries.setdefault(entry.stem.casefold(), []).append(entry)
    return entries


def _siblings(path, entries):
    siblings = [entry for entry in entries.get(path.stem.casefold(), ()) if os.path.lexists(entry)]
    names = {entry.name.casefold() for entry in siblings}
    for suffix in _SIDECAR_EXTENSIONS:
        candidate = path.with_suffix(suffix)
        if candidate.name.casefold() not in names and os.path.lexists(candidate):
            siblings.append(candidate)
    return siblings


def _caption(path, entries):
    siblings = _siblings(path, entries)
    captions = [entry for entry in siblings if entry.suffix.lower() == ".txt"]
    if not captions:
        return None
    images = [entry for entry in siblings if entry.suffix.lower() in IMAGE_EXTENSIONS]
    if len(captions) != 1 or len(images) != 1:
        raise ValueError("caption_conflict")
    return captions[0]


def _check_cancel(cancelled):
    if cancelled():
        raise _Cancelled


def _same_contents(pin, target, cancelled):
    try:
        with PinnedFile(target) as other:
            if pin.check()[2] != other.check()[2]:
                return False
            pin.stream.seek(0)
            while block := pin.stream.read(_BLOCK_SIZE):
                _check_cancel(cancelled)
                if block != other.stream.read(len(block)):
                    return False
            return True
    except (OSError, ValueError):
        return False


def _choose_destination(source, folder, entries, image, caption, cancelled):
    for index in range(10000):
        _check_cancel(cancelled)
        name = source.name if index == 0 else f"{source.stem}_{index + 1}{source.suffix}"
        target = folder / name
        siblings = _siblings(target, entries)
        if not siblings:
            return target, False
        names = {entry.name.casefold(): entry for entry in siblings}
        image_name = target.name.casefold()
        caption_name = target.with_suffix(".txt").name.casefold()
        expected = {image_name, caption_name} if caption else {image_name}
        if (len(siblings) == len(expected) and names.keys() == expected
                and _same_contents(image, names[image_name], cancelled)
                and (caption is None or _same_contents(caption, names[caption_name], cancelled))):
            return names[image_name], True
    raise ValueError("destination_conflict")


def _remove_owned(path, identity):
    try:
        with PinnedFile(path, delete=True) as pin:
            if pin.check()[:2] == identity:
                pin.mark_delete()
    except FileNotFoundError:
        pass


def _stage(pin, folder, cancelled, cleanup):
    fd, name = tempfile.mkstemp(prefix=".character-separator-", suffix=".tmp", dir=folder)
    temporary = Path(name)
    with os.fdopen(fd, "wb") as output:
        info = os.fstat(output.fileno())
        cleanup.callback(_remove_owned, temporary, (info.st_dev, info.st_ino))
        pin.stream.seek(0)
        while block := pin.stream.read(_BLOCK_SIZE):
            _check_cancel(cancelled)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    original = os.fstat(pin.stream.fileno())
    os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
    if not _same_contents(pin, temporary, cancelled):
        raise ValueError("copy_failed")
    return temporary, (info.st_dev, info.st_ino)


def _copy_pair(target, folder, entries, image, caption, cancelled):
    with ExitStack() as temporary:
        prepared_image, _ = _stage(image, folder, cancelled, temporary)
        if caption:
            prepared_caption, caption_identity = _stage(caption, folder, cancelled, temporary)
        _check_cancel(cancelled)
        image.check()
        if caption:
            caption.check()
        if _directory(folder, "destination_conflict") != folder or _siblings(target, entries):
            raise ValueError("destination_conflict")
        # En Windows rename falla si el destino apareció mientras se copiaba.
        if caption:
            caption_target = target.with_suffix(".txt")
            os.rename(prepared_caption, caption_target)
            try:
                os.rename(prepared_image, target)
            except OSError:
                _remove_owned(caption_target, caption_identity)
                raise
            entries.setdefault(target.stem.casefold(), []).append(caption_target)
        else:
            os.rename(prepared_image, target)
        entries.setdefault(target.stem.casefold(), []).append(target)


def _target_directory(mother, name):
    matches = [entry for entry in mother.iterdir() if entry.name.casefold() == name.casefold()]
    if len(matches) > 1:
        raise ValueError("destination_conflict")
    if matches:
        return _directory(matches[0], "destination_conflict")
    target = mother / name
    if os.path.lexists(target):  # Alias 8.3 o nombre distinto según NTFS.
        raise ValueError("destination_conflict")
    return target


def export(mother: Path, source: Path, assignments: dict[str, tuple[Path, ...]],
           cancelled: Callable[[], bool], progress: Callable[[int, int], None], *,
           expected: Mapping[Path, FileSignature] | None = None) -> ExportResult:
    copied = skipped = 0
    completed = []
    destinations = []
    current = source
    try:
        _check_cancel(cancelled)
        if os.name != "nt":
            raise ValueError("invalid_scope")
        mother = _directory(mother, "invalid_scope")
        source = _directory(source, "invalid_scope")
        if source != mother and source.parent != mother:
            raise ValueError("invalid_scope")
        work = []
        seen = set()
        for name, paths in assignments.items():
            name = validate_name(name)
            if name.casefold() in seen:
                raise ValueError("destination_conflict")
            seen.add(name.casefold())
            if paths:
                current = mother / name
                folder = _target_directory(mother, name)
                if folder.parent != mother:
                    raise ValueError("destination_conflict")
                work.append((name, folder, paths))
        total = sum(len(paths) for _, _, paths in work)
        progress(0, total)
        sources = _by_stem(source)
        for name, folder, paths in work:
            _check_cancel(cancelled)
            current = folder
            folder.mkdir(exist_ok=True)
            if _directory(folder, "destination_conflict") != folder:
                raise ValueError("destination_conflict")
            entries = _by_stem(folder)
            for path in paths:
                _check_cancel(cancelled)
                current = path
                if path.suffix.lower() not in IMAGE_EXTENSIONS or path.parent != source:
                    raise ValueError("invalid_scope")
                with ExitStack() as inputs:
                    try:
                        image = inputs.enter_context(PinnedFile(path))
                    except (OSError, ValueError) as error:
                        raise ValueError("source_changed") from error
                    if expected is not None and signature(os.fstat(image.stream.fileno())) != expected.get(path):
                        raise ValueError("source_changed")
                    if folder == source:
                        skipped += 1
                    else:
                        caption_path = _caption(path, sources)
                        caption = inputs.enter_context(PinnedFile(caption_path)) if caption_path else None
                        target, exists = _choose_destination(path, folder, entries, image, caption, cancelled)
                        if exists:
                            skipped += 1
                        else:
                            current = target
                            _copy_pair(target, folder, entries, image, caption, cancelled)
                            copied += 1
                        completed.append(path)
                        destinations.append((name, target))
                progress(copied + skipped, total)
    except _Cancelled:
        return ExportResult(copied, skipped, True, tuple(completed), tuple(destinations))
    except (OSError, ValueError) as error:
        code = str(error) if isinstance(error, ValueError) and str(error) in _KNOWN_ERRORS else "copy_failed"
        if isinstance(error, FileExistsError):
            code = "destination_conflict"
        result = ExportResult(copied, skipped, completed=tuple(completed), destinations=tuple(destinations))
        raise ExportError(code, result, current) from error
    return ExportResult(copied, skipped, completed=tuple(completed), destinations=tuple(destinations))
