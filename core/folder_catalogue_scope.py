"""Bounded catalogue membership; only worker probes consult Windows names.

Archival includes offline history, so operational resolved-only scopes cannot be
reused. Inspect directory prefixes rather than image bytes and keep SQL snapshots
closed during probes. A disconnected root has an explicitly literal scope.
"""
import ctypes
from ctypes import wintypes
import os
import time

from core.duplicate_archive import roots
from core.duplicate_snapshot import encode
from core.path_identity import local_path, observe_path
from core.path_identity_registry import prepared

MAX_PREFIXES = 256
MAX_ROWS = 1_000_000
MAX_SECONDS = 10
MAX_PROOF = 256 * 1024


def prefix(path, depth, *, directory=False):
    path = local_path(path)
    parts = path.rstrip('/').split('/')
    if len(parts) < depth or (len(parts) == depth and not directory):
        return None
    return '/'.join(parts[:depth]) + ('/' if depth == 1 else '')


def catalogue(conn, folder):
    folder = local_path(folder)
    watched = roots(conn)
    if folder not in watched:
        raise ValueError('folder_archive_not_watched')
    depth = len(folder.rstrip('/').split('/'))
    names = {folder}
    def add(path, *, directory=False):
        try:
            value = prefix(path, depth, directory=directory)
        except ValueError as exc:
            raise ValueError('folder_archive_identity_requires_review') from exc
        if value is not None:
            names.add(value)
        if len(names) > MAX_PREFIXES:
            raise ValueError('folder_archive_scope_limit')
    for path in watched:
        add(path, directory=True)
    deadline = time.monotonic() + MAX_SECONDS
    count = 0
    cursor = conn.execute('SELECT substr(path,1,4097) FROM files ORDER BY id')
    try:
        while block := cursor.fetchmany(256):
            if time.monotonic() >= deadline:
                raise ValueError('folder_archive_check_budget_exceeded')
            count += len(block)
            if count > MAX_ROWS:
                raise ValueError('folder_archive_scope_limit')
            for row in block:
                add(row[0])
    finally:
        cursor.close()
    result = dict(folder=folder, watched=watched, prefixes=sorted(names))
    if len(encode(result)) > MAX_PROOF:
        raise ValueError('folder_archive_report_limit')
    return result


def metadata(repo, folder):
    if not prepared(repo.conn):
        return dict(prepared=False)
    return dict(prepared=True, catalogue=catalogue(repo.conn, folder))


def signature(value):
    return dict(status=value.status, canonical=value.canonical, kind=value.kind,
                physical=None if value.physical is None else list(value.physical[:3]), reason=value.reason)


def possible_alias(left, right):
    # Conservative ambiguity check only. This never assigns a canonical name
    # or decides whether a directory is case-sensitive. Use the Windows uppercase
    # table, not Python casefold (which expands e.g. sharp s).
    if os.name != 'nt':
        raise ValueError('folder_archive_scope_unavailable')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    compare = api.CompareStringOrdinal
    compare.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL]
    compare.restype = ctypes.c_int
    result = compare(left, -1, right, -1, 1)
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result == 2


def membership(scope):
    if (type(scope) is not dict or set(scope) != {'catalogue', 'evidence'} or
            type(scope['catalogue']) is not dict or set(scope['catalogue']) != {'folder', 'watched', 'prefixes'}):
        raise ValueError('folder_archive_scope_requires_review')
    saved, evidence = scope['catalogue'], scope['evidence']
    folder, names = saved['folder'], saved['prefixes']
    if (type(names) is not list or not 1 <= len(names) <= MAX_PREFIXES or
            any(type(p) is not str or local_path(p) != p for p in names) or names != sorted(set(names)) or
            folder not in names or type(evidence) is not dict or set(evidence) != set(names)):
        raise ValueError('folder_archive_scope_requires_review')
    for record in evidence.values():
        if (type(record) is not dict or set(record) != {'status', 'canonical', 'kind', 'physical', 'reason'} or
                record['status'] not in ('resolved', 'unavailable', 'unsupported', 'changed')):
            raise ValueError('folder_archive_scope_requires_review')
        if record['status'] == 'resolved':
            if (record['kind'] != 'directory' or local_path(record['canonical']) != record['canonical'] or
                    type(record['physical']) not in (list, tuple) or len(record['physical']) != 3 or
                    any(type(v) is not int or not 0 <= v <= 0xffffffff for v in record['physical']) or record['reason'] is not None):
                raise ValueError('folder_archive_scope_requires_review')
        elif (record['canonical'] is not None or record['kind'] is not None or record['physical'] is not None or
                type(record['reason']) is not str or len(record['reason']) > 256):
            raise ValueError('folder_archive_scope_requires_review')
    root = evidence[folder]
    if root['status'] == 'resolved':
        allowed = []
        for path in names:
            record = evidence[path]
            if record['status'] == 'resolved' and record['canonical'] == root['canonical']:
                if list(record['physical']) != list(root['physical']):
                    raise ValueError('folder_archive_scope_requires_review')
                allowed.append(path)
        mode = 'observed_directory'
    elif root['status'] == 'unavailable' or (root['status'] == 'unsupported' and root['reason'] == 'identity_local_drive_required'):
        allowed, mode = [folder], 'literal_offline'
    else:
        raise ValueError('folder_archive_scope_unavailable')
    if len(encode(scope)) > MAX_PROOF:
        raise ValueError('folder_archive_report_limit')
    return allowed, dict(mode=mode, canonical=root['canonical'], physical=root['physical'], prefixes=allowed)


def selector(scope, *, alias=''):
    if scope is None:
        raise ValueError('folder_archive_scope_requires_review')
    allowed, stamp = membership(scope)
    column = alias + 'path'
    clauses, params = [], []
    for path in allowed:
        low = path.rstrip('/') + '/'
        clauses.append('(' + column + '>=? COLLATE BINARY AND ' + column + '<? COLLATE BINARY)')
        params.extend((low, low[:-1] + '0'))
    return '(' + ' OR '.join(clauses) + ')', params, stamp


def condition(conn, folder, scope, *, alias=''):
    value = selector(scope, alias=alias)
    if scope['catalogue'] != catalogue(conn, folder):
        raise ValueError('folder_archive_members_changed')
    return value


def revalidate(scope, event):
    for path, record in scope['evidence'].items():
        if signature(observe_path(path, cancel_event=event)) != record:
            raise ValueError('folder_archive_scope_changed')


def inspect(service, folder, event):
    initial = service.read('get_folder_archive_scope', folder, cancel_event=event)
    if not initial['prepared']:
        return service.read('get_folder_archive_preview', folder, cancel_event=event), None
    saved = initial['catalogue']
    evidence = {path: signature(observe_path(path, cancel_event=event)) for path in saved['prefixes']}
    scope = dict(catalogue=saved, evidence=evidence)
    allowed, stamp = membership(scope)
    root = evidence[folder]
    # Unavailable spellings that could alias the root require review. A missing
    # root never borrows rules from another directory; its preview says literal.
    for path, record in evidence.items():
        if path not in allowed and (root['status'] != 'resolved' or record['status'] != 'resolved'):
            if possible_alias(path, folder) or (root['canonical'] and possible_alias(path, root['canonical'])):
                raise ValueError('folder_archive_scope_requires_review')
    value = service.read('get_folder_archive_preview', folder, scope=scope, cancel_event=event)
    revalidate(scope, event)
    return value, scope
