"""Inspección y cuarentena explícitas de recursos de una exportación interrumpida.

Sin DB central, sin promoción de temporales y sin borrar bytes. Ejecutar en worker/CLI.
Sólo coopera con exportadores que usan el guard persistente; locks antiguos se rechazan.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from core.curation_export_lock import export_guard, GUARD

LOCK = '.panopticon.curation.db.lock'
PREFIX = '.panopticon.curation.db-'


def _info(path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ValueError('unsafe_recovery_resource')
    return dict(dev=value.st_dev, ino=value.st_ino, size=value.st_size, mtime=value.st_mtime_ns)


def _snapshot(folder):
    lock = folder / LOCK
    info = _info(lock)
    if info is None:
        return dict(status='clean', fingerprint=None, files=[])
    if not 1 <= info['size'] <= 4096:
        raise ValueError('unverifiable_export_lock')
    with lock.open('rb') as stream:
        raw = stream.read(4097)
    try:
        manifest = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ValueError('unverifiable_export_lock') from error
    if (not isinstance(manifest, dict) or set(manifest) != {'owner', 'version', 'token', 'temporary', 'dev', 'ino'} or
            manifest['owner'] != 'panopticon.curation.export' or type(manifest['version']) is not int or manifest['version'] != 1 or
            not isinstance(manifest['token'], str) or not re.fullmatch('[0-9a-f]{32}', manifest['token']) or
            not isinstance(manifest['temporary'], str) or not re.fullmatch(re.escape(PREFIX) + r'[a-z0-9_]{1,64}\.tmp', manifest['temporary']) or
            any(type(manifest[key]) is not int or manifest[key] < 0 for key in ('dev', 'ino'))):
        raise ValueError('unverifiable_export_lock')
    if _info(lock) != info:
        raise ValueError('recovery_changed')
    files = []
    for name in (manifest['temporary'], *(manifest['temporary'] + suffix for suffix in ('-journal', '-wal', '-shm'))):
        observed = _info(folder / name)
        if observed is not None:
            if name == manifest['temporary'] and (observed['dev'], observed['ino']) != (manifest['dev'], manifest['ino']):
                raise ValueError('recovery_identity_changed')
            files.append(dict(name=name, **observed))
    files.append(dict(name=LOCK, **info))
    state = dict(manifest=manifest, files=files)
    digest = hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(status='recoverable', fingerprint=digest, files=files, token=manifest['token'])


def inspect_recovery(folder):
    folder = Path(folder).absolute()
    if not folder.is_dir():
        raise ValueError('invalid_export_folder')
    if _info(folder / LOCK) is None:
        return dict(status='clean', fingerprint=None, files=[])
    if not (folder / GUARD).exists():
        raise ValueError('unverifiable_export_lock')
    with export_guard(folder, create=False):
        return _snapshot(folder)


def quarantine_recovery(folder, *, expected_fingerprint):
    """Revalida inspección bajo guard y mueve sólo recursos declarados; lock al final.

Si hay fallo parcial conserva cuarentena y lock pendiente; no afirma recuperación completa.
No protege frente a terceros que sustituyan guard/archivos ignorando el protocolo.
"""
    if not isinstance(expected_fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', expected_fingerprint):
        raise ValueError('invalid_recovery_fingerprint')
    folder = Path(folder).absolute()
    if not folder.is_dir():
        raise ValueError('invalid_export_folder')
    with export_guard(folder, create=False):
        snapshot = _snapshot(folder)
        if snapshot['fingerprint'] != expected_fingerprint:
            raise ValueError('recovery_changed')
        quarantine = folder / ('.panopticon.curation.recovery-' + uuid.uuid4().hex)
        quarantine.mkdir()
        # Plan duradero antes de mover: permite revisar un fallo parcial sin perder bytes.
        with (quarantine / 'recovery.json').open('x', encoding='utf-8') as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        moved = []
        try:
            for record in snapshot['files']:
                path = folder / record['name']
                if _info(path) != {key: record[key] for key in ('dev', 'ino', 'size', 'mtime')}:
                    raise ValueError('recovery_changed')
                path.rename(quarantine / record['name'])
                moved.append(record['name'])
        except Exception as error:
            raise RuntimeError('recovery_incomplete:' + quarantine.name) from error
        return dict(status='quarantined', folder=str(quarantine), files=moved)
