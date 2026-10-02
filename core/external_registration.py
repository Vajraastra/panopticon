"""Explicit external image registration; verified bytes outside Native SQL."""
import hashlib
from pathlib import Path
from PIL import Image
from core.derived_publication import write as confirm_write
from core.image_preview import MAX_SOURCE_BYTES
from core.metadata.bundle import MetadataBundle
from core.metadata.extractor import MetadataExtractor
from core.output_paths import safe_path, path_variants
from core.pinned_file import PinnedFile


def register(service, source, event):
    from core.library_path_ingest import register as plan
    if event.is_set():
        raise ValueError('external_registration_cancelled')
    source = safe_path(Path(source).absolute())
    with PinnedFile(source) as pin:
        identity = pin.check()
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError('preview_file_limit')
        with Image.open(source) as image:
            if (image.width * image.height > 20_000_000 or getattr(image, 'n_frames', 1) != 1 or
                    image.format not in ('PNG', 'JPEG', 'WEBP', 'BMP', 'AVIF')):
                raise ValueError('external_registration_image_requires_review')
            dimensions = image.size
            image.load()
        bundle = (MetadataExtractor.extract(source, strict=True)
                  if source.suffix.lower() in MetadataExtractor.SUPPORTED_EXTENSIONS else MetadataBundle())
        pin.stream.seek(0)
        digest = hashlib.file_digest(pin.stream, 'sha256').hexdigest()
        pin.verify(digest, identity)
        info = source.stat()
        data = dict(path=str(source), filename=source.name, size=info.st_size,
                    created=info.st_ctime, hash_original=digest)
        generation = dict(width=dimensions[0], height=dimensions[1], tool=bundle.tool,
                          model=bundle.model, positive=bundle.positive_prompt,
                          negative=bundle.negative_prompt, seed=str(bundle.seed))
        def commit(values):
            pin.verify(digest, identity)
            confirm_write(service, 'register_external_copy_observed', values,
                          destination_paths=path_variants(source, source),
                          generation=generation, tags=bundle.tags, rating=bundle.rating, event=event)
            # A submitted command is drained even after late cancellation.
            # Confirmation is historical; report uncertain transport as error.
            pin.verify(digest, identity)
            result = service.read('get_copy_version_state', service.read('get_file_id', str(source)))
            if (result['state']['file']['hash_original'] != digest or
                    result['state']['file']['is_offline'] != 0):
                raise ValueError('external_registration_confirmation_requires_review')
            return result['state']['file']['id']
        return plan(service, [data], event, commit=commit)


def write(repo, values, *, destination_paths, generation, tags, rating):
    from core.derived_copies import _budget
    from core.generation_history import FIELDS, COLUMNS, path_keys
    from core.library_path_ingest import write as ingest
    if (type(values) not in (list, tuple) or len(values) != 1 or
            type(values[0]) is not dict or type(values[0].get('data')) is not dict):
        raise ValueError('invalid_external_registration')
    data = values[0]['data']
    path = repo._normalize_path(data['path'])
    keys = tuple(sorted(set(path_keys(destination_paths, normalize=repo._normalize_path))))
    if (path.casefold() not in keys or
            type(generation) is not dict or set(generation) != set(FIELDS)):
        raise ValueError('invalid_external_registration')
    for name, value in generation.items():
        if name in ('width', 'height'):
            if type(value) is not int or not 1 <= value <= 32768:
                raise ValueError('invalid_external_generation')
        elif not isinstance(value, str) or len(value) > 65536 or '\x00' in value:
            raise ValueError('invalid_external_generation')
    if type(rating) is not int or not 0 <= rating <= 5:
        raise ValueError('invalid_external_rating')
    if (type(tags) not in (list, tuple) or len(tags) > 256 or
            any(not isinstance(tag, str) or not tag.strip() or len(tag) > 256 or '\x00' in tag for tag in tags)):
        raise ValueError('invalid_external_tags')
    from core.generation_history import SHA
    if not isinstance(data.get('hash_original'), str) or not SHA.fullmatch(data['hash_original']):
        raise ValueError('invalid_external_digest')
    with _budget(repo.conn) as check:
        # Conservative aliases/history guard: never turn registration into an
        # update, adoption or restoration of another curated/offline identity.
        from core.path_identity_registry import prepared
        if prepared(repo.conn):
            identity = values[0].get('identity') or {}
            if (values[0].get('target') is not None or values[0].get('historical') is not False or
                    repo.conn.execute('SELECT 1 FROM path_identity_records WHERE canonical=? LIMIT 1',
                                      (identity.get('canonical'),)).fetchone() is not None or
                    repo.conn.execute('SELECT 1 FROM files WHERE path=? LIMIT 1', (path,)).fetchone() is not None):
                raise ValueError('external_registration_has_native_history')
        else:
            from core.generation_history import path_status
            occupied = (repo._derived_paths.occupied(keys, check) if repo._derived_paths is not None else
                        path_status(repo.conn, keys, normalize=lambda value: value)['occupied'])
            if occupied:
                raise ValueError('external_registration_has_native_history')
        ingest(repo, values)  # Folder evidence/clock, bindings and FTS are atomic.
        key = repo.get_file_id(path)
        if key is None:
            raise ValueError('external_registration_unconfirmed')
        row = repo.conn.execute('SELECT hash_original,is_offline FROM files WHERE id=?', (key,)).fetchone()
        if row != (data['hash_original'], 0):
            raise ValueError('external_registration_identity_changed')
        repo.conn.execute('UPDATE files SET ' + ','.join(column + '=?' for column in COLUMNS) +
                          ' WHERE id=?', (*[generation[name] for name in FIELDS], key))
        if repo.edit_curation(path, add_tags=tags, rating=rating, expected_copy_id=key) is not True:
            raise ValueError('external_registration_curation_unconfirmed')
        if not tags:
            repo._fts_sync_file(key)
        # Extra metadata/curation changed the SQL token after ingestion. Do not
        # reuse its intermediate coverage as if it described the final commit.
        repo._path_ingest_transition = None
        check()
    return True
