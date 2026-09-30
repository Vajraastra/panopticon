"""Acciones explícitas por copia/huella; hashing en worker, CAS en dueño DB."""
import hashlib
import os
from pathlib import Path
import stat

from core.curation_apply import _identity, _check_cancel


def observe(path, cancel_event=None):
    if path.is_symlink():
        raise ValueError('copy_version_file_symlink')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('copy_version_file_not_regular')
        before = _identity(info)
        while block := stream.read(1024 * 1024):
            _check_cancel(cancel_event)
            digest.update(block)
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError('copy_version_file_changed')
    if _identity(path.stat()) != before:
        raise ValueError('copy_version_file_changed')
    _check_cancel(cancel_event)
    return digest.hexdigest(), dict(filename=path.name, size=info.st_size,
                                   created=getattr(info, 'st_birthtime', info.st_ctime))


class CopyVersionTasks:
    def __init__(self, service):
        self.service = service

    def read(self, operation, *, copy_id, expected_fingerprint, cancel_event=None):
        if operation not in ('archive_copy', 'make_copy_online'):
            raise ValueError('invalid_copy_version_action')
        _check_cancel(cancel_event)
        reviewed = self.service.read('get_copy_version_state', copy_id, cancel_event=cancel_event)
        if reviewed['fingerprint'] != expected_fingerprint:
            raise ValueError('copy_version_changed_since_review')
        kwargs = dict(expected_fingerprint=expected_fingerprint)
        if operation == 'make_copy_online':
            path = Path(reviewed['state']['file']['path'])
            if not path.is_absolute():
                raise ValueError('copy_version_path_not_absolute')
            kwargs['digest'], kwargs['observation'] = observe(path, cancel_event)
        _check_cancel(cancel_event)
        # No snapshot atómico FS/SQL. El dueño revalida toda la fila/curación por ID.
        result = self.service.submit(operation, copy_id, **kwargs).result()
        if result is not True:
            raise ValueError('copy_version_commit_unconfirmed')
        return dict(applied=True, action=operation, copy_id=copy_id)
