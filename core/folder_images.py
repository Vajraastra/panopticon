"""Bounded external-folder discovery for an owned LibraryReader worker."""
import os
import sys
import time
from pathlib import Path
from core.inventory import directory_chain_identity
from core.inventory_walk import entries
from core.library_payload import MAX_BYTES, MAX_CONTAINER_ITEMS

EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif')
MAX_SECONDS = 30


class FolderImages:
    def read(self, operation, folder, *, cancel_event=None):
        if operation != 'folder_images' or not isinstance(folder, str) or not folder:
            raise ValueError('invalid_folder_images')
        deadline = time.monotonic() + MAX_SECONDS

        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError('folder_images_cancelled')
            if time.monotonic() > deadline:
                raise ValueError('folder_images_budget_exceeded')
            return False

        def fail(error):
            raise error  # Never publish a partial prefix as a complete folder.

        cancelled()
        root = Path(folder).absolute()
        identity = directory_chain_identity(root)
        paths, used = [], sys.getsizeof([])
        iterator = entries(str(root), EXTENSIONS, cancelled=cancelled,
                           onerror=fail, is_catalog=lambda path: False)
        try:
            for kind, path, name in iterator:
                cancelled()
                path = os.path.normpath(path).replace('\\', '/')
                used += sys.getsizeof(path) + 32
                if len(paths) >= MAX_CONTAINER_ITEMS or used > MAX_BYTES:
                    raise ValueError('folder_images_requires_staging')
                paths.append(path)
        finally:
            iterator.close()
        cancelled()
        if directory_chain_identity(root) != identity:
            raise ValueError('folder_images_root_changed')
        cancelled()
        return paths
