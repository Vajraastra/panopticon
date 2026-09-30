"""Elección explícita: releer exportación, verificar bytes, enviar CAS al escritor."""
import hashlib
import os
from pathlib import Path
import re
from core.curation_sidecar import read_curation_page
from core.curation_restore import FIELDS, _payload


def _check_cancel(event):
    if event is not None and event.is_set():
        raise RuntimeError('curation_cancelled')


def _identity(stat):
    change = getattr(stat, 'st_birthtime_ns', None) if os.name == 'nt' else stat.st_ctime_ns
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, change


def verify_file(path, digest, cancel_event=None):
    if path.is_symlink():
        raise ValueError('curation_file_symlink')
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        before = _identity(os.fstat(stream.fileno()))
        while True:
            _check_cancel(cancel_event)
            block = stream.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError('curation_file_changed')
    if _identity(path.stat()) != before:
        raise ValueError('curation_file_changed')
    if hasher.hexdigest() != digest.lower():
        raise ValueError('curation_hash_mismatch')
    return before


class CurationApplyTasks:
    def __init__(self, service):
        self.service = service

    def read(self, operation, *, folder, copy_id, relative_path, export_id,
             expected_fingerprint, mode='restore', cancel_event=None):
        if operation != 'apply_curation' or type(copy_id) is not int or copy_id < 1:
            raise ValueError('invalid_curation_apply')
        if mode not in ('restore', 'register', 'adopt'):
            raise ValueError('invalid_curation_apply')
        if mode == 'register':
            if expected_fingerprint is not None:
                raise ValueError('invalid_restore_fingerprint')
        elif not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
            raise ValueError('invalid_restore_fingerprint')
        _check_cancel(cancel_event)
        page = read_curation_page(folder, after=copy_id-1, limit=1, export_id=export_id)
        if not page['copies'] or page['copies'][0]['id'] != copy_id:
            raise ValueError('sidecar_changed')
        record = page['copies'][0]
        if record['relative_path'] != relative_path:
            raise ValueError('sidecar_changed')
        incoming = _payload({key: record[key] for key in FIELDS})
        digest = incoming['hash_mod'] or incoming['hash_original']
        if digest is None:
            raise ValueError('identity_missing')
        source = Path(folder).absolute() / relative_path
        before = verify_file(source, digest, cancel_event)
        # No conexión SQLite abierta durante el hashing ni al esperar el Future.
        check = read_curation_page(folder, after=copy_id-1, limit=1, export_id=export_id)
        if check['copies'] != [record]:
            raise ValueError('sidecar_changed')
        observation = source.stat()
        if _identity(observation) != before:
            raise ValueError('curation_file_changed')
        _check_cancel(cancel_event)
        # Una vez admitida, la escritura sigue a cargo del dueño aunque cierre Qt.
        if mode == 'register':
            return self.service.submit('register_curation_copy', source.as_posix(), incoming=incoming,
                observation=dict(filename=source.name, size=observation.st_size,
                    created=getattr(observation, 'st_birthtime', observation.st_ctime))).result()
        if mode == 'adopt':
            return self.service.submit('adopt_curation_identity', source.as_posix(),
                expected_fingerprint=expected_fingerprint, incoming=incoming).result()
        return self.service.submit('restore_curation', source.as_posix(),
            expected_fingerprint=expected_fingerprint, incoming=incoming).result()
