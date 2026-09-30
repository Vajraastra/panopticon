"""Guard cooperativo persistente: el kernel libera la exclusión al morir el proceso."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

GUARD = '.panopticon.curation.db.guard'
MAGIC = b'PANOPTICON_CURATION_EXPORT_GUARD_V1\n'


def _lock(fd, acquire):
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


@contextmanager
def export_guard(folder, *, create=True):
    """Nunca borrar/reemplazar guard; export y recovery usan el mismo inode."""
    path = Path(folder) / GUARD
    if path.is_symlink():
        raise ValueError('invalid_export_guard')
    flags = os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    new = False
    if create:
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            new = True
        except FileExistsError:
            fd = os.open(path, flags)
    else:
        fd = os.open(path, flags)
    locked = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('invalid_export_guard')
        try:
            _lock(fd, True)
        except OSError as error:
            raise ValueError('sidecar_export_busy') from error
        locked = True
        if new:
            if os.write(fd, MAGIC) != len(MAGIC):
                raise OSError('incomplete_export_guard')
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        current = path.lstat()
        if (path.is_symlink() or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or
                os.read(fd, len(MAGIC) + 1) != MAGIC):
            raise ValueError('invalid_export_guard')
        yield
    finally:
        try:
            if locked:
                _lock(fd, False)
        finally:
            os.close(fd)
