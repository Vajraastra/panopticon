"""Copia acotada de datos simples antes de admitir una escritura Native."""
from pathlib import PurePath, PurePosixPath, PureWindowsPath, PosixPath, WindowsPath
import sys

MAX_BYTES = 4 * 1024 * 1024
MAX_FIELD_BYTES = 1024 * 1024
MAX_NODES = 65536
MAX_CONTAINER_ITEMS = 10000
MAX_DEPTH = 16
MAX_TAGS_PER_FILE = 256
MAX_TAG_CHARS = 256
_PATHS = {PurePosixPath, PureWindowsPath, PosixPath, WindowsPath}


def snapshot(args, kwargs):
    """No serialize/deepcopy hooks, SQL or filesystem; never truncate a request.

    Charge repeated references conservatively. The budget estimates Python
    object memory, not encoded wire bytes. Mutable inputs must be producer-owned
    while submitted; concurrent mutation may reject, never bypass the budget.
    """
    used = nodes = 0
    ancestors = set()

    def clone(value, depth):
        nonlocal used, nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError('write_payload_too_large')
        kind = type(value)
        if kind in (str, bytes):
            size = sys.getsizeof(value)
            if size > MAX_FIELD_BYTES:
                raise ValueError('write_payload_too_large')
        elif kind in (type(None), bool, int, float):
            size = sys.getsizeof(value)
        elif kind in _PATHS:
            # Only concrete stdlib path types, never arbitrary __fspath__ hooks.
            size = sys.getsizeof(value) + sys.getsizeof(str(value))
            if size > MAX_FIELD_BYTES:
                raise ValueError('write_payload_too_large')
        elif kind in (dict, list, tuple, set, frozenset):
            if len(value) > MAX_CONTAINER_ITEMS:
                raise ValueError('write_payload_too_large')
            size = sys.getsizeof(value)
        else:
            raise ValueError('invalid_write_payload')
        used += size
        if used > MAX_BYTES:
            raise ValueError('write_payload_too_large')
        if kind not in (dict, list, tuple, set, frozenset):
            return value
        identity = id(value)
        if identity in ancestors:
            raise ValueError('invalid_write_payload')
        ancestors.add(identity)
        try:
            if kind is dict:
                return {clone(k, depth+1): clone(v, depth+1) for k, v in value.items()}
            return kind(clone(item, depth+1) for item in value)
        finally:
            ancestors.remove(identity)

    try:
        return clone(args, 0), clone(kwargs, 0)
    except (RuntimeError, TypeError) as exc:
        raise ValueError('invalid_write_payload') from exc


def snapshot_read(args, kwargs):
    try:
        return snapshot(args, kwargs)
    except ValueError as exc:
        reason = {'write_payload_too_large': 'read_payload_too_large',
                  'invalid_write_payload': 'invalid_read_payload'}[str(exc)]
        raise ValueError(reason) from exc
