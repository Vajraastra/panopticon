"""Exportación explícita de curación portátil. Ejecutar fuera del hilo GUI.

Sólo escribe su sidecar propio. No modifica imágenes, central ni catálogo Cherry.
Los hashes son los catalogados, no una verificación actual de bytes.
"""
import os
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import closing

from core.library_connections import open_reader
from core.curation_export_lock import export_guard
from core.library_repository import LibraryRepository
from third_party.cherry_dl.catalog import _CREATE_FILES, _CREATE_META

NAME = 'panopticon.curation.db'
VERSION = '2'
BLOCK = 256
MAX_PAGE_TAGS = 4096


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise ValueError('cancelled')


def _relative(path, folder):
    relative = Path(path).relative_to(folder)
    if relative.is_absolute() or relative.drive or '..' in relative.parts or not relative.parts:
        raise ValueError('invalid_relative_path')
    return relative.as_posix()


def _digest(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{64}', value)):
        raise ValueError('invalid_catalogued_hash')
    return value


def _tag(name, source, category):
    if (not isinstance(name, str) or not name or len(name) > 4096 or
            any(field is not None and (not isinstance(field, str) or len(field) > 4096)
                for field in (source, category))):
        raise ValueError('invalid_sidecar_tag')
    return dict(name=name, source=source, category=category)


def _identity(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _owner(conn, *, with_version=False):
    metadata = dict(conn.execute("SELECT key,value FROM meta WHERE key IN "
        "('owner','panopticon_curation_version','export_id','copy_count')"))
    version = str(metadata.get('panopticon_curation_version'))
    if metadata.get('owner') != 'panopticon' or version not in ('1', '2'):
        raise ValueError('foreign_or_unsupported_sidecar')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(curation_copies)')}
    expected = {'id', 'relative_path', 'hash_original', 'hash_mod', 'rating', 'file_size', 'created_date'}
    if version == '2':
        expected.add('is_offline')
    if columns != expected:
        raise ValueError('foreign_or_unsupported_sidecar')
    token = metadata.get('export_id')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('invalid_export_id')
    if type(metadata.get('copy_count')) is not int or metadata['copy_count'] < 0:
        raise ValueError('invalid_copy_count')
    return (token, version) if with_version else token


def _existing(target):
    identity = _identity(target)
    if identity is not None:
        # Nunca reemplazar un SQLite con WAL/journal activos.
        if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ValueError('sidecar_in_use')
        with closing(open_reader(target)) as conn:
            _owner(conn)
    return identity


def _publish_new(temporary, target):
    # Windows rename falla si existe destino; funciona también sin hardlinks.
    if os.name == 'nt':
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def export_curation(db_path, folder, *, cancelled=None):
    """Publica un snapshot completo, con bloques de256 y lock propio por carpeta.

    La lectura RO central dura sólo la copia SQL; no hay hashing/decodificación.
    Puede retener WAL mientras copia: exportación manual, no mantenimiento automático.
    La exclusión coopera entre exportadores; no es CAS frente a terceros no cooperativos.
    """
    # Preserva la representación indexada: resolve expande aliases8.3 en Windows.
    folder = Path(folder).absolute()
    folder.resolve(strict=True)
    if not folder.is_dir():
        raise ValueError('invalid_export_folder')
    source = Path(db_path).resolve(strict=True)
    target = folder / NAME
    if target.is_symlink() or source == target or (target.exists() and os.path.samefile(source, target)):
        raise ValueError('source_target_alias')
    with export_guard(folder):
        return _export_curation(source, folder, target, cancelled)


def _export_curation(source, folder, target, cancelled):
    lock = folder / ('.' + NAME + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0))
    lock_info = os.fstat(fd)
    temporary = None
    try:
        previous = _existing(target)
        _check_cancel(cancelled)
        handle, path = tempfile.mkstemp(prefix='.' + NAME + '-', suffix='.tmp', dir=folder)
        os.close(handle)
        temporary = Path(path)
        token = uuid.uuid4().hex
        info = temporary.stat()
        manifest = json.dumps(dict(owner='panopticon.curation.export', version=1, token=token,
            temporary=temporary.name, dev=info.st_dev, ino=info.st_ino)).encode('utf-8')
        if os.write(fd, manifest) != len(manifest):
            raise OSError('incomplete_export_manifest')
        os.fsync(fd)
        with closing(sqlite3.connect(temporary, isolation_level=None)) as output:
            output.execute('PRAGMA synchronous=FULL')
            output.execute('PRAGMA foreign_keys=ON')
            output.execute(_CREATE_FILES)
            output.execute(_CREATE_META)
            output.execute('CREATE TABLE curation_copies(id INTEGER PRIMARY KEY, '
                'relative_path TEXT NOT NULL,hash_original TEXT,hash_mod TEXT,'
                'rating INTEGER NOT NULL CHECK(rating BETWEEN 0 AND 5),file_size INTEGER,created_date,'
                'is_offline INTEGER NOT NULL CHECK(is_offline IN (0,1)))')
            output.execute('CREATE TABLE curation_tags(copy_id INTEGER NOT NULL,name TEXT NOT NULL,'
                'source TEXT,category TEXT,PRIMARY KEY(copy_id,name),'
                'FOREIGN KEY(copy_id) REFERENCES curation_copies(id))')
            output.execute('BEGIN IMMEDIATE')
            output.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
                ('owner', 'panopticon'), ('panopticon_curation_version', VERSION),
                ('export_id', token), ('exported_at', int(time.time()))])
            count = missing_hash = 0
            with closing(open_reader(source)) as reader:
                reader.set_progress_handler(lambda: int(bool(cancelled and cancelled())), 1000)
                reader.execute('BEGIN')
                like = LibraryRepository(reader)._folder_like(str(folder))
                cursor = reader.execute('SELECT id,path,hash_original,hash_mod,rating,file_size,created_date,is_offline '
                    "FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY id", (like,))
                while rows := cursor.fetchmany(BLOCK):
                    _check_cancel(cancelled)
                    copied = {}
                    for file_id, path, original, modified, rating, size, created, offline in rows:
                        relative = _relative(path, folder)
                        original, modified = _digest(original), _digest(modified)
                        rating = 0 if rating is None else rating
                        if type(rating) is not int or not 0 <= rating <= 5:
                            raise ValueError('invalid_catalogued_rating')
                        if type(offline) is not int or offline not in (0, 1):
                            raise ValueError('invalid_copy_versions_state')
                        count += 1
                        copied[file_id] = count
                        output.execute('INSERT INTO curation_copies VALUES(?,?,?,?,?,?,?,?)',
                            (count, relative, original, modified, rating, size, created, offline))
                        # NULL permite conservar copias sin hash sin inventar identidad SHA.
                        digest = original or modified
                        missing_hash += digest is None
                        output.execute('INSERT OR IGNORE INTO files(hash,filename,url_source,date_added,file_size) '
                            'VALUES(?,?,NULL,?,?)', (digest, relative, int(time.time()), size))
                    placeholders = ','.join('?' for _ in copied)
                    for file_id, name, tag_source, category in reader.execute(
                            'SELECT ft.file_id,t.name,ft.source,t.category FROM file_tags ft '
                            f'JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id IN ({placeholders}) ORDER BY t.name',
                            tuple(copied)):
                        _check_cancel(cancelled)
                        _tag(name, tag_source, category)
                        output.execute('INSERT INTO curation_tags VALUES(?,?,?,?)',
                            (copied[file_id], name, tag_source, category))
            # Fuente cerrada antes de verificar/sincronizar/publicar el artefacto.
            _check_cancel(cancelled)
            output.executemany('INSERT INTO meta(key,value) VALUES(?,?)',
                              [('copy_count', count), ('missing_hash', missing_hash)])
            output.commit()
            if output.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('invalid_sidecar')
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        _check_cancel(cancelled)
        if target.is_symlink() or _existing(target) != previous:
            raise ValueError('sidecar_changed')
        if previous is None:
            # Creación exclusiva: si apareció otro archivo, nunca lo reemplaza.
            _publish_new(temporary, target)
        else:
            os.replace(temporary, target)
        temporary = None
        return dict(path=str(target), copies=count, missing_hash=missing_hash)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
            for suffix in ('-journal', '-wal', '-shm'):
                Path(str(temporary) + suffix).unlink(missing_ok=True)
        os.close(fd)
        try:
            info = lock.stat()
            if (info.st_dev, info.st_ino) == (lock_info.st_dev, lock_info.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass


def read_curation_page(folder, *, after=0, limit=256, export_id=None):
    """Página de copias/tags detached; ni rutas absolutas ni conexión persistente."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= BLOCK:
        raise ValueError('invalid_page')
    with closing(open_reader(Path(folder) / NAME)) as conn:
        conn.execute('BEGIN')
        token, version = _owner(conn, with_version=True)
        total = conn.execute("SELECT value FROM meta WHERE key='copy_count'").fetchone()[0]
        if export_id is not None and export_id != token:
            raise ValueError('sidecar_changed')
        offline_column = 'is_offline' if version == '2' else '0'
        rows = conn.execute('SELECT id,relative_path,hash_original,hash_mod,rating,' + offline_column + ' '
            'FROM curation_copies WHERE id>? ORDER BY id LIMIT ?', (after, limit)).fetchall()
        records = {}
        for key, relative, original, modified, rating, offline in rows:
            if not isinstance(relative, str) or not relative or len(relative) > 32768 or '\\' in relative:
                raise ValueError('invalid_relative_path')
            relative_path = PurePosixPath(relative)
            if relative_path.is_absolute() or PureWindowsPath(relative).drive or '..' in relative_path.parts or not relative_path.parts:
                raise ValueError('invalid_relative_path')
            if type(rating) is not int or not 0 <= rating <= 5:
                raise ValueError('invalid_catalogued_rating')
            if type(offline) is not int or offline not in (0, 1):
                raise ValueError('invalid_copy_versions_state')
            records[key] = dict(id=key, relative_path=relative, hash_original=_digest(original),
                                hash_mod=_digest(modified), rating=rating, tags=[], is_offline=offline)
        if records:
            placeholders = ','.join('?' for _ in records)
            seen = 0
            for key, name, source, category in conn.execute('SELECT copy_id,substr(name,1,4097),'
                    'substr(source,1,4097),substr(category,1,4097) '
                    f'FROM curation_tags WHERE copy_id IN ({placeholders}) ORDER BY name LIMIT ?',
                    (*records, MAX_PAGE_TAGS + 1)):
                seen += 1
                if seen > MAX_PAGE_TAGS or any(isinstance(field, str) and len(field) > 4096 for field in (name, source, category)):
                    raise ValueError('sidecar_payload_too_large')
                records[key]['tags'].append(_tag(name, source, category))
        return dict(export_id=token, total=total, copies=list(records.values()), after=rows[-1][0] if rows else after)


def compare_curation_page(db_path, folder, **page):
    """Previsualiza conflictos; nunca registra, fusiona ni modifica curación.

    Cada bloque usa snapshot central propio. La ruta relativa localiza la copia;
    hash diferente/sin hash se reporta, nunca autoriza una fusión entre copias.
    """
    folder = Path(folder).absolute()
    folder.resolve(strict=True)
    value = read_curation_page(folder, **page)
    copies = value['copies']
    paths = [(folder / record['relative_path']).as_posix() for record in copies]
    current = {}
    historical = set()
    if paths:
        with closing(open_reader(db_path)) as conn:
            conn.execute('BEGIN')
            unique_paths = tuple(dict.fromkeys(paths))
            requested = ' UNION ALL '.join(['SELECT ? AS path'] * len(unique_paths))
            # Una fila por ruta: usa el índice de copia operativa y EXISTS del
            # historial. No materializa todas sus versiones ni hace N consultas.
            for key, path, original, modified, rating, offline in conn.execute(
                    'SELECT f.id,r.path,f.hash_original,f.hash_mod,f.rating,(f.id IS NULL) '
                    f'FROM ({requested}) r LEFT JOIN files f ON f.path=r.path AND f.is_offline=0 '
                    'WHERE f.id IS NOT NULL OR EXISTS(SELECT 1 FROM files h WHERE h.path=r.path AND h.is_offline=1)', unique_paths):
                if offline:
                    historical.add(path)
                else:
                    current[path] = dict(id=key, hash_original=original, hash_mod=modified, rating=rating or 0, tags=[])
            ids = {record['id']: record for record in current.values()}
            if ids:
                placeholders = ','.join('?' for _ in ids)
                seen = 0
                for key, name, source, category in conn.execute('SELECT ft.file_id,substr(t.name,1,4097),'
                        'substr(ft.source,1,4097),substr(t.category,1,4097) '
                        'FROM file_tags ft JOIN tags t ON ft.tag_id=t.id '
                        f'WHERE ft.file_id IN ({placeholders}) ORDER BY t.name LIMIT ?', (*ids, MAX_PAGE_TAGS + 1)):
                    seen += 1
                    if seen > MAX_PAGE_TAGS:
                        raise ValueError('sidecar_payload_too_large')
                    ids[key]['tags'].append(_tag(name, source, category))
    reports = []
    for record, path in zip(copies, paths):
        local = None if record['is_offline'] else current.get(path)
        if record['is_offline'] or (local is None and path in historical):
            status = 'offline_history'
        elif local is None:
            status = 'unregistered'
        elif (local['hash_original'], local['hash_mod']) == (None, None) and (record['hash_original'] or record['hash_mod']):
            status = 'central_identity_missing'
        elif (record['hash_original'], record['hash_mod']) != (local['hash_original'], local['hash_mod']):
            status = 'identity_changed'
        elif not (record['hash_original'] or record['hash_mod']):
            status = 'identity_missing'
        else:
            status = 'same' if all(record[key] == local[key] for key in ('rating', 'tags')) else 'conflict'
        reports.append(dict(relative_path=record['relative_path'], status=status, sidecar=record, central=local))
    return dict(export_id=value['export_id'], total=value['total'], after=value['after'], reports=reports)
