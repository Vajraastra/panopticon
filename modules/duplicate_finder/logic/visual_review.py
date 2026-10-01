"""pHash vivo acotado, sólo revisión. No autoriza retirar archivos similares."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

from core.curation_apply import _check_cancel, _identity
from core.duplicate_snapshot import encode
from .file_guards import linked, plain_chain


def _hash(path, event):
    # Imports costosos sólo cuando se solicita el modo visual.
    import imagehash
    from PIL import Image
    _check_cancel(event)
    path = Path(path)
    try:
        plain_chain(path)
        before = _identity(path.stat())
        with Image.open(path) as image:
            if image.width*image.height > 10000000:
                raise ValueError('duplicate_visual_pixel_limit')
            value = imagehash.phash(image)
        _check_cancel(event)
        if _identity(path.stat()) != before:
            raise ValueError('copy_version_file_changed')
        return value, None
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        return None, str(error)


def _review_hash(path,event,record=None):
    if record is not None and record['phash'] is not None:
        import imagehash
        from .perceptual_backfill import calculate
        proof,error=calculate(record,event)
        return (imagehash.hex_to_hash(proof['record']['phash']) if proof else None),error
    return _hash(path,event)


def scan(folder, threshold, progress, event,*,service=None):
    import numpy as np
    root = Path(folder)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('duplicate_review_folder_unavailable')
    plain_chain(root)
    root = root.resolve()
    paths, issues, issue_count = [], [], 0
    def problem(path, reason):
        nonlocal issue_count
        issue_count += 1
        if len(issues) < 20:
            issues.append(dict(path=str(path), reason=str(reason)))
    for directory, dirs, files in os.walk(root, followlinks=False,
            onerror=lambda error: problem(error.filename, error.errno)):
        _check_cancel(event)
        safe = []
        for name in sorted(dirs):
            path = Path(directory)/name
            if linked(path):
                problem(path, 'linked_directory_requires_review')
            else:
                safe.append(name)
        dirs[:] = safe
        for name in sorted(files):
            if name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif')):
                paths.append((Path(directory)/name).as_posix())
                if len(paths) > 2000:
                    raise ValueError('duplicate_visual_inventory_limit')
    hashes = {}
    algorithm=None
    if service is not None:
        from .perceptual_backfill import algorithm_id
        algorithm=algorithm_id()
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4)) as pool:
        for start in range(0, len(paths), 256):
            _check_cancel(event)
            block = paths[start:start+256]
            records=[None]*len(block)
            if service is not None:
                records=service.read('get_phash_states',block,algorithm,cancel_event=event)
                if len(records)!=len(block) or [row['path'] for row in records]!=block:
                    raise ValueError('phash_state_identity_changed')
            jobs=zip(block,records,strict=True)
            for index, (path, (value, error)) in enumerate(zip(block, pool.map(lambda job:_review_hash(job[0],event,job[1]),jobs)), start+1):
                _check_cancel(event)
                if error is not None:
                    problem(path, error)
                else:
                    hashes[path] = value
                progress(index, len(paths), path)
    names = list(hashes)
    bits = np.array([value.hash.flatten() for value in hashes.values()], dtype=bool)
    visited = np.zeros(len(names), dtype=bool)
    groups = {}
    for index in range(len(names)):
        _check_cancel(event)
        if visited[index]:
            continue
        visited[index] = True
        near = np.nonzero(np.count_nonzero(bits[index+1:] != bits[index], axis=1) <= threshold)[0]+index+1
        members = [int(value) for value in near if not visited[value]]
        if members:
            visited[members] = True
            groups[f'visual_{index}'] = [names[index], *[names[value] for value in members]]
    reports = {key: dict(status='review', reason='visual_similarity_requires_exact_review', preferred=None,
        keep=tuple(values), remove=(), copies=[dict(path=path, state=None, fingerprint=None) for path in values])
        for key, values in groups.items()}
    _check_cancel(event)
    result = dict(groups=groups, reports=reports, proofs={}, issues=issues, issue_count=issue_count, complete=issue_count==0)
    if len(encode(result)) > 4*1024*1024:
        raise ValueError('duplicate_review_payload_limit')
    return result
