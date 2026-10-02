"""Lazy depth-first enumeration: no per-directory filename list or recursion."""
import os
from core.inventory_candidates import InventoryCancelled

MAX_OPEN_DIRECTORIES = 64


class InventoryUnavailable(OSError):
    pass


def entries(folder, extensions, *, cancelled, onerror, is_catalog):
    stack = []
    try:
        try:
            stack.append(os.scandir(folder))
        except OSError as exc:
            onerror(exc)
            return
        while stack:
            if cancelled():
                raise InventoryCancelled()
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                continue
            except OSError as exc:
                onerror(exc)
                stack.pop().close()
                continue
            try:
                if entry.is_dir():
                    if entry.is_symlink():
                        # Same no-follow behavior as the former os.walk.
                        continue
                    if getattr(entry.stat(follow_symlinks=False), 'st_file_attributes', 0) & 0x400:
                        onerror(InventoryUnavailable('linked_directory_requires_review'))
                        continue
                    if is_catalog(entry.path):
                        yield 'catalog', entry.path, entry.name
                    elif len(stack) >= MAX_OPEN_DIRECTORIES:
                        onerror(OSError('inventory_tree_depth_exceeded'))
                    else:
                        stack.append(os.scandir(entry.path))
                elif entry.name.lower().endswith(extensions):
                    yield 'image', entry.path, entry.name
            except OSError as exc:
                onerror(exc)
    finally:
        for iterator in reversed(stack):
            iterator.close()
