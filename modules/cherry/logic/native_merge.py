"""Worker-side bridge for existing Native copies; Cherry owns its catalog writes."""
from pathlib import Path

from core.file_transfers import transfer
from core.output_paths import path_variants, safe_path


def move_indexed(service, source, target, expected_sha, journal_root=None, *, source_root, target_root):
    source = safe_path(source)
    target = Path(target)
    origin = safe_path(source_root)
    destination = safe_path(target_root)
    actual_parent = safe_path(target.parent)
    # These are resolved Windows spellings; do not casefold distinct sensitive siblings.
    within = lambda path, root: path.as_posix() == root.as_posix() or path.as_posix().startswith(root.as_posix().rstrip('/')+'/')
    if not source.is_file() or not within(source,origin) or not within(actual_parent,destination):
        raise ValueError('cherry_merge_path_outside_profile')
    if source.name in ('catalog.db','panopticon.curation.db') or source.name.startswith(('catalog.db-','panopticon.curation.db-')):
        raise ValueError('cherry_merge_catalog_protected')
    resolution = service.resolve_path_identity(str(source))
    rows = service.read('get_transfer_sources', path_variants(source, source))['records']
    if resolution['status'] not in ('unique', 'missing', 'not_prepared'):
        raise ValueError('path_identity_review_required')
    if resolution['status'] == 'unique' or rows:
        if rows and (len(rows) != 1 or rows[0][1]):
            raise ValueError('path_identity_review_required')
        transfer(service, source, target.parent, 'move', journal_root=journal_root,
                 exact=True, target_name=target.name, register_missing=False,
                 expected_sha=expected_sha)
        return True
    # No identity/curation to move. Preserve the engine's existing registration policy.
    return False
