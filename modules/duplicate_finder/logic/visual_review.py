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


def scan(folder, threshold, progress, event):
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
    with ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 4)) as pool:
        for start in range(0, len(paths), 256):
            _check_cancel(event)
            block = paths[start:start+256]
            for index, (path, (value, error)) in enumerate(zip(block, pool.map(lambda name: _hash(name, event), block)), start+1):
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
