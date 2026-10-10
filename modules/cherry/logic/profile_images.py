"""Read current profile images without touching Cherry catalogs or originals."""
import os
import stat
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from core.file_transfers import EXTENSIONS
from core.inventory import directory_chain_identity

MAX_IMAGES = 500000
MAX_BYTES = 64 * 1024 * 1024
MAX_SECONDS = 60
MAX_DEPTH = 64


def profile_images(folder, cancel_event):
    """Complete, bounded snapshot; hidden storage and linked paths are excluded."""
    deadline = time.monotonic() + MAX_SECONDS

    def check():
        if cancel_event.is_set():
            raise ValueError('profile_images_cancelled')
        if time.monotonic() > deadline:
            raise ValueError('profile_images_timeout')

    check()
    root = Path(folder).absolute()
    identity = directory_chain_identity(root)
    paths, stack = [], []
    used = sys.getsizeof(paths)
    try:
        stack.append(os.scandir(root))
        while stack:
            check()
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                continue
            if entry.name.startswith('.'):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                continue
            if stat.S_ISDIR(info.st_mode):
                if len(stack) >= MAX_DEPTH:
                    raise ValueError('profile_images_depth')
                stack.append(os.scandir(entry.path))
            elif stat.S_ISREG(info.st_mode) and Path(entry.name).suffix.lower() in EXTENSIONS:
                path = os.path.normpath(entry.path).replace('\\', '/')
                used += sys.getsizeof(path) + 32
                if len(paths) >= MAX_IMAGES or used > MAX_BYTES:
                    raise ValueError('profile_images_limit')
                paths.append(path)
        check()
        if directory_chain_identity(root) != identity:
            raise ValueError('profile_images_root_changed')
        paths.sort(key=str.casefold)
        check()
        return paths
    finally:
        for iterator in reversed(stack):
            iterator.close()


class ProfileImagesWorker(QThread):
    finished_signal = Signal(object, object)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self._cancel = threading.Event()

    def stop(self):
        self._cancel.set()
        self.requestInterruption()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def run(self):
        paths = error = None
        try:
            paths = profile_images(self.folder, self._cancel)
        except Exception as exc:
            error = str(exc)
        self.finished_signal.emit(paths, error)
