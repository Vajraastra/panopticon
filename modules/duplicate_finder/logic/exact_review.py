"""Inventario/SHA actuales y curación por bloques. Sólo prepara una revisión."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import threading

from core.copy_version_tasks import observe
from core.curation_apply import _check_cancel, _identity
from core.duplicate_policy import DuplicatePolicy, recommend
from core.duplicate_snapshot import encode
from .file_guards import documented, linked as _linked

EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.avif', '.zip', '.rar')


@dataclass(frozen=True)
class ReviewLimits:
    files: int = 100000
    groups: int = 1000
    copies: int = 2000
    payload_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or not 1 <= value <= cap for value, cap in
               zip(vars(self).values(), (100000, 1000, 2000, 4 * 1024 * 1024))):
            raise ValueError('invalid_duplicate_review_limits')


def _probe(path, cancel_event):
    _check_cancel(cancel_event)
    path = Path(path)
    try:
        before = _identity(path.stat())
        digest, observation = observe(path, cancel_event)
        if _identity(path.stat()) != before:
            raise ValueError('copy_version_file_changed')
        return dict(digest=digest, identity=before, size=observation['size']), None
    except (OSError, ValueError) as error:
        return None, getattr(error, 'errno', None) or str(error)


class ExactDuplicateReview:
    def __init__(self, service, policy=None, limits=None):
        self.service = service
        self.policy = DuplicatePolicy() if policy is None else policy
        self.limits = ReviewLimits() if limits is None else limits
        if not isinstance(self.policy, DuplicatePolicy) or not isinstance(self.limits, ReviewLimits):
            raise ValueError('invalid_duplicate_review_configuration')

    def scan(self, folder, progress=None, cancel_event=None):
        if not isinstance(folder, str) or not folder or not Path(folder).is_absolute():
            raise ValueError('invalid_duplicate_review_folder')
        event = threading.Event() if cancel_event is None else cancel_event
        _check_cancel(event)
        root = Path(folder)
        if _linked(root) or not root.is_dir():
            raise ValueError('duplicate_review_folder_unavailable')
        root = root.resolve()
        issues = []
        issue_count = 0
        def problem(path, reason):
            nonlocal issue_count
            issue_count += 1
            if len(issues) < 20:
                issues.append(dict(path=str(path), reason=str(reason)))
        sizes = {}
        total = 0
        for directory, dirs, files in os.walk(root, followlinks=False,
                onerror=lambda error: problem(error.filename or root, error.errno)):
            _check_cancel(event)
            safe_dirs = []
            for name in sorted(dirs):
                path = Path(directory) / name
                if _linked(path):
                    problem(path, 'linked_directory_requires_review')
                else:
                    safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in sorted(files):
                _check_cancel(event)
                if not name.lower().endswith(EXTENSIONS):
                    continue
                path = Path(directory) / name
                total += 1
                if total > self.limits.files:
                    raise ValueError('duplicate_review_inventory_limit')
                if _linked(path):
                    problem(path, 'copy_version_file_symlink')
                    continue
                try:
                    sizes.setdefault(path.stat().st_size, []).append(path.as_posix())
                except OSError as error:
                    problem(path, error.errno)
        candidates = [path for paths in sizes.values() if len(paths) > 1 for path in paths]
        proofs = {}
        hashed = {}
        processed = total - len(candidates)
        # No map() de todo el inventario: como máximo256 trabajos admitidos por tanda.
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
            for start in range(0, len(candidates), 256):
                _check_cancel(event)
                block = candidates[start:start + 256]
                for path, (proof, error) in zip(block, pool.map(lambda value: _probe(value, event), block)):
                    _check_cancel(event)
                    processed += 1
                    if proof is None:
                        problem(path, error)
                    else:
                        proofs[path] = proof
                        hashed.setdefault(proof['digest'], []).append(path)
                    if progress:
                        progress(processed, total, path)
        groups = {digest: paths for digest, paths in hashed.items() if len(paths) > 1}
        if len(groups) > self.limits.groups:
            raise ValueError('duplicate_review_group_limit')
        wanted = [path for paths in groups.values() for path in paths]
        if len(wanted) > self.limits.copies:
            raise ValueError('duplicate_review_copy_limit')
        proofs = {path: proofs[path] for path in wanted}
        records = {}
        failed = set()
        consumed = 0
        for start in range(0, len(wanted), 256):
            _check_cancel(event)
            block = wanted[start:start + 256]
            try:
                if self.service is None:
                    raise ValueError('duplicate_review_data_service_unavailable')
                snapshot = self.service.read('get_duplicate_snapshot', block, cancel_event=event)
            except Exception as error:
                _check_cancel(event)
                failed.update(block)
                problem(root, getattr(error, 'reason', str(error)))
                snapshot = [dict(path=path, state=None, fingerprint=None) for path in block]
            consumed += len(encode(snapshot))
            if consumed > self.limits.payload_bytes:
                raise ValueError('duplicate_review_payload_limit')
            records.update((record['path'], record) for record in snapshot)
        sidecars = {}
        reports = {}
        for digest, paths in groups.items():
            _check_cancel(event)
            copies = [records[path] for path in paths]
            current = {}
            identities = set()
            ambiguous = False
            for path, record in zip(paths, copies):
                proof = proofs[path]
                try:
                    if _linked(Path(path)) or _identity(Path(path).stat()) != proof['identity']:
                        problem(path, 'copy_version_file_changed')
                        continue
                except OSError as error:
                    problem(path, error.errno)
                    continue
                inode = tuple(proof['identity'][:2])
                ambiguous |= inode in identities
                identities.add(inode)
                current[path] = proof['digest']
                record['external_documentation'] = documented(Path(path), sidecars)
            if len(paths) > 256:
                result = dict(status='review', reason='duplicate_group_limit', preferred=None, keep=tuple(paths), remove=())
            else:
                result = recommend(copies, verified_hashes=current, policy=self.policy)
            if ambiguous or any(path in failed for path in paths):
                result = dict(status='review', reason='ambiguous_path_identity' if ambiguous else 'central_snapshot_failed',
                              preferred=None, keep=tuple(paths), remove=())
            reports[digest] = dict(result, copies=copies)
        result = dict(groups=groups, reports=reports, proofs=proofs, issues=issues,
                      issue_count=issue_count, complete=issue_count == 0)
        if len(encode(result)) > self.limits.payload_bytes:
            raise ValueError('duplicate_review_payload_limit')
        _check_cancel(event)
        return result
