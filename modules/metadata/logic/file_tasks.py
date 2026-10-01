"""Preparar/verificar/publicar una imagen; SQLite sólo mediante el dueño central.

El diario y respaldo conservan evidencia si disco y commit Native divergen. Las
revalidaciones detectan cambios observados; no son un bloqueo de editores externos.
"""
import hashlib
import ctypes
import itertools
import json
import os
from pathlib import Path
import stat
import tempfile
from uuid import uuid4

from PIL import Image
from core.duplicate_snapshot import encode
from core.generation_history import OBSERVED, FIELDS, COLUMNS
from .modifier import modify_metadata
from .reader import UniversalParser

MAX_FILE = 256 * 1024 * 1024
MAX_PIXELS = 20_000_000
SUPPORTED = frozenset(('.png', '.jpg', '.jpeg', '.webp', '.avif'))


def _cancel(event):
    if event is not None and event.is_set(): raise ValueError('metadata_cancelled')


def identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
        raise ValueError('metadata_unsupported_file')
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def safe_path(path, *, exists=True, portable=False):
    path = Path(path)
    if not path.is_absolute() or len(str(path)) > 4096 or path.drive.startswith('\\') or any(':' in part for part in path.parts[1:]):
        raise ValueError('metadata_requires_local_path')
    if os.name == 'nt':
        drive = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        drive.argtypes, drive.restype = (ctypes.c_wchar_p,), ctypes.c_uint32
        if drive(path.anchor) not in (2, 3): raise ValueError('metadata_requires_local_path')
    check = path if exists else path.parent
    for parent in (check, *check.parents):
        info = parent.stat(follow_symlinks=False)
        if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('metadata_link_requires_review')
    path = path.resolve(strict=exists)
    if portable:
        for parent in (path.parent, *path.parents):
            if os.path.lexists(parent / 'catalog.db') or os.path.lexists(parent / 'panopticon.curation.db'):
                raise ValueError('metadata_cherry_original_protected')
    return path


def digest(path, event=None, *, output=None):
    value = hashlib.sha256()
    size = 0
    with open(path, 'rb') as source:
        while block := source.read(1024*1024):
            _cancel(event)
            size += len(block)
            if size > MAX_FILE: raise ValueError('metadata_file_limit')
            value.update(block)
            if output is not None: output.write(block)
    return value.hexdigest()


def metadata(path):
    parsed = UniversalParser.parse_image(str(path))
    if parsed.get('error'): raise ValueError('metadata_parse_failed: '+str(parsed['error'])[:500])
    if len(encode(parsed)) > 512*1024: raise ValueError('metadata_payload_limit')
    with Image.open(path) as image:
        if image.width*image.height > MAX_PIXELS or max(image.size) > 32768 or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('metadata_image_limit')
        dimensions = image.size
        image.verify()
    return parsed, dimensions


def _observed(parsed):
    return {key: json.dumps(parsed[key], ensure_ascii=False) if isinstance(parsed[key], (list, dict)) else str(parsed[key])
            for key in OBSERVED if key in parsed and parsed[key] is not None}


def _generation(parsed, dimensions, *, clear):
    return dict(width=dimensions[0], height=dimensions[1],
        **{key: '' if clear else str(parsed.get(key, '')) for key in FIELDS if key not in ('width', 'height')})


def _journal(path, data):
    raw = encode(data)
    if len(raw) > 2*1024*1024: raise ValueError('metadata_journal_limit')
    temporary = path.with_suffix('.tmp')
    with open(temporary, 'xb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def path_variants(output, entered):
    values = {str(output), str(Path(entered).absolute())}
    if os.name == 'nt':
        api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        api.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        api.restype = ctypes.c_uint32
        size = api(str(output.parent), None, 0)
        if not size or size > 32768: raise ValueError('metadata_alias_requires_review')
        buffer = ctypes.create_unicode_buffer(size)
        count = api(str(output.parent), buffer, size)
        if not count or count >= size: raise ValueError('metadata_alias_requires_review')
        short = Path(buffer.value)
        if len(short.parts) != len(output.parent.parts): raise ValueError('metadata_alias_requires_review')
        options, count = [], 1
        for long_part, short_part in zip(output.parent.parts, short.parts):
            parts = [long_part] if long_part.casefold() == short_part.casefold() else [long_part, short_part]
            count *= len(parts)
            if count > 30: raise ValueError('metadata_alias_requires_review')
            options.append(parts)
        for parts in itertools.product(*options): values.add(str(Path(*parts) / output.name))
    if len(values) > 32: raise ValueError('metadata_alias_requires_review')
    return sorted(values)


class MetadataTasks:
    def __init__(self, service, journal_root=None):
        self.service = service
        self.journal_root = Path(journal_root) if journal_root is not None else None

    def load(self, path, event):
        original = str(Path(path).absolute())
        actual = safe_path(original)
        if actual.suffix.lower() not in SUPPORTED: raise ValueError('metadata_unsupported_format')
        before = identity(actual)
        source_digest = digest(actual, event)
        parsed, dimensions = metadata(actual)
        with Image.open(actual) as image:
            image.thumbnail((1600, 1600))
            preview = image.convert('RGBA')
            pixels = (preview.tobytes(), preview.width, preview.height)
        _cancel(event)
        if identity(actual) != before or digest(actual, event) != source_digest:
            raise ValueError('metadata_source_changed')
        native, native_error = None, ''
        if self.service is not None:
            try:
                native = self.service.read('get_metadata_edit_state', original, cancel_event=event)
            except Exception as exc:
                _cancel(event)
                native_error = str(exc)[:500]
        editable_original = native is not None
        if editable_original:
            try:
                safe_path(actual, portable=True)
                if not actual.stat().st_mode & stat.S_IWRITE: raise ValueError('metadata_original_readonly')
                known = native['state']['file']['hash_mod'] or native['state']['file']['hash_original']
                if known is not None and (not isinstance(known, str) or known.lower() != source_digest):
                    raise ValueError('metadata_source_hash_changed')
            except Exception as exc:
                editable_original, native_error = False, str(exc)[:500]
        return dict(path=original, actual=str(actual), identity=before, digest=source_digest,
            metadata=parsed, dimensions=dimensions, pixels=pixels, native=native, native_error=native_error,
            editable_original=editable_original)

    def history(self, copy_id, after_id, event, labels=None):
        if self.service is None: raise ValueError('metadata_native_required')
        result = self.service.read('get_generation_history_page', copy_id, after_id=after_id, limit=2, cancel_event=event)
        labels = labels or {}
        lines = []
        for record in result['records']:
            lines.append(labels.get('history_record', 'Record {id} — {date}').format(id=record['id'], date=record['recorded_at']+' UTC'))
            for section, heading in (('native', 'history_native'), ('observed', 'history_observed')):
                lines.append(labels.get(heading, heading))
                for key, value in record['previous'][section].items():
                    if key in ('hash_original', 'hash_mod'): continue
                    label = labels.get('field_'+key.removeprefix('meta_'), key.removeprefix('meta_'))
                    lines.append(f'{label}:\n{value if value is not None else ""}\n')
            lines.append('')
        result['text'] = '\n'.join(lines) if lines else labels.get('history_empty', 'No generation history recorded.')
        if len(result['text'].encode('utf-8')) > 1024*1024: raise ValueError('generation_history_page_payload_limit')
        return result

    def _fresh(self, captured, event):
        actual = safe_path(captured['path'])
        if str(actual) != captured['actual'] or identity(actual) != tuple(captured['identity']) or digest(actual, event) != captured['digest']:
            raise ValueError('metadata_source_changed')
        return actual

    def _recovery_scope(self, event):
        actual = safe_path(self.journal_root, exists=os.path.lexists(self.journal_root), portable=True)
        if os.path.lexists(actual/'catalog.db') or os.path.lexists(actual/'panopticon.curation.db'):
            raise ValueError('metadata_cherry_original_protected')
        for watched in self.service.read('get_duplicate_retirement_roots', cancel_event=event):
            _cancel(event)
            root = Path(watched)
            if not root.is_absolute(): raise ValueError('metadata_recovery_scope_requires_review')
            if root.drive.casefold() != actual.drive.casefold(): continue
            resolved = root.resolve(strict=False)
            try: within = os.path.commonpath((str(actual).casefold(), str(resolved).casefold())) == str(resolved).casefold()
            except ValueError: within = False
            if within: raise ValueError('metadata_recovery_inside_library')
        return actual

    def _destination(self, source, destination, event):
        output = safe_path(destination, exists=False, portable=True)
        if os.path.normcase(str(source)) == os.path.normcase(str(output)) or os.path.lexists(output):
            raise ValueError('metadata_export_requires_new_path')
        if output.suffix.lower() != source.suffix.lower(): raise ValueError('metadata_export_extension_changed')
        if self.service is None: raise ValueError('metadata_native_required')
        if self.service.read('get_metadata_path_status', path_variants(output, destination), cancel_event=event)['occupied']:
            raise ValueError('metadata_export_path_has_history')
        return output

    def edit(self, captured, positive, negative, *, destination=None, event=None):
        temporary = None
        published = native_committed = False
        journal_path = None
        data = None
        try:
            _cancel(event)
            if any(not isinstance(value, str) or len(value) > 65536 or '\x00' in value for value in (positive, negative)):
                raise ValueError('metadata_payload_limit')
            positive, negative = positive.strip(), negative.strip()
            text = f'{positive}\nNegative prompt: {negative}' if positive or negative else None
            source = self._fresh(captured, event)
            overwrite = destination is None
            if overwrite:
                safe_path(source, portable=True)
                if self.service is None or not captured.get('native'): raise ValueError('metadata_copy_not_indexed')
                if not source.stat().st_mode & stat.S_IWRITE: raise ValueError('metadata_original_readonly')
                output = source
            else:
                output = self._destination(source, destination, event)
                if os.name != 'nt': raise ValueError('metadata_export_requires_windows')
            job = str(uuid4())
            if self.journal_root is None:
                from core.paths import CachePaths
                self.journal_root = CachePaths.get_tool_cache('metadata_recovery')
            self._recovery_scope(event)
            folder = self.journal_root / job
            safe_path(self.journal_root, exists=False)
            self.journal_root.mkdir(exist_ok=True)
            safe_path(self.journal_root)
            folder.mkdir()
            backup = folder / ('original'+source.suffix)
            with open(backup, 'xb') as saved:
                copied = digest(source, event, output=saved)
                saved.flush()
                os.fsync(saved.fileno())
            if copied != captured['digest']: raise ValueError('metadata_source_changed')
            previous, dimensions = metadata(backup)
            journal_path = folder / 'journal.json'
            data = dict(version=1, operation_id=job, phase='prepared', source=str(source), destination=str(output),
                backup=str(backup), source_sha256=copied, copy_id=None, expected_fingerprint=None,
                source_generation=_observed(previous), published=False, native_committed=False)
            if overwrite:
                native = captured['native']
                data.update(copy_id=native['state']['file']['id'], expected_fingerprint=native['fingerprint'])
                data['native_previous'] = {key: native['state']['file'][key] for key in (*COLUMNS, 'hash_original', 'hash_mod')}
            _journal(journal_path, data)
            fd, name = tempfile.mkstemp(prefix='.panopticon-metadata-', suffix=output.suffix, dir=output.parent)
            os.close(fd)
            temporary = Path(name)
            success, message = modify_metadata(str(backup), str(temporary), text)
            if not success: raise ValueError('metadata_transform_failed: '+message[:500])
            _cancel(event)
            current, result_dimensions = metadata(temporary)
            if result_dimensions != dimensions or current.get('positive', '').strip() != positive or current.get('negative', '').strip() != negative:
                raise ValueError('metadata_verification_failed')
            with Image.open(temporary) as image: image.load()  # Verificar decode antes de publicar.
            result_digest = digest(temporary, event)
            with open(temporary, 'r+b') as prepared: os.fsync(prepared.fileno())
            if overwrite: os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
            info = temporary.stat()
            args = dict(expected_fingerprint=data['expected_fingerprint'], operation_id=job,
                source_digest=copied, result_digest=result_digest, generation=_generation(current, result_dimensions, clear=text is None),
                observation=dict(filename=output.name, size=info.st_size, created=info.st_ctime), source_generation=data['source_generation'])
            data.update(result_sha256=result_digest, request=args if overwrite else None, prepared_output=str(temporary))
            if overwrite:
                if self.service.submit('prepare_generation_history').result() is not True: raise ValueError('metadata_prepare_failed')
                _cancel(event)
                if self.service.submit('approve_metadata_edit', data['copy_id'], **args).result() is not True:
                    raise ValueError('metadata_approval_failed')
            _cancel(event)
            self._fresh(captured, event)
            self._recovery_scope(event)
            if not overwrite: self._destination(source, destination, event)
            else: safe_path(source, portable=True)
            data['phase'] = 'ready_to_publish'
            _journal(journal_path, data)  # Persistir todo lo necesario ANTES del efecto físico.
            _cancel(event)
            if overwrite: os.replace(temporary, output)
            else: os.rename(temporary, output)  # Windows: no reemplazar un destino que apareció.
            temporary = None
            published = True
            # Después de publicar no cancelar el commit admitido ni simular rollback de disco.
            if digest(output) != result_digest: raise ValueError('metadata_published_file_changed')
            if not overwrite and self.service.read('get_metadata_path_status', path_variants(output, destination))['occupied']:
                raise ValueError('metadata_export_path_has_history')
            info = output.stat()
            args['observation'].update(size=info.st_size, created=info.st_ctime)
            data.update(phase='published', published=True, request=args if overwrite else None)
            _journal(journal_path, data)
            if overwrite:
                if self.service.submit('commit_metadata_edit', data['copy_id'], **args).result() is not True:
                    raise ValueError('metadata_commit_unconfirmed')
                native_committed = True
            data.update(phase='complete', native_committed=native_committed)
            _journal(journal_path, data)
            return dict(status='complete', published=True, native_committed=native_committed, destination=str(output), journal=str(journal_path))
        except Exception as exc:
            error = str(exc)[:1000]
            if data is not None and journal_path is not None:
                data.update(phase='review_required' if published else 'not_published', published=published,
                    native_committed=native_committed, error=error)
                try: _journal(journal_path, data)
                except Exception: pass  # El último diario persistido sigue siendo evidencia, no éxito.
            return dict(status='partial' if published else 'error', published=published, native_committed=native_committed,
                error=error, journal=str(journal_path) if journal_path is not None else '')
        finally:
            if temporary is not None:
                try: temporary.unlink()
                except OSError: pass
