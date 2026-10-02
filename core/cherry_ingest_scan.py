"""Worker-side, addition-only reconciliation of committed Cherry observations."""
from pathlib import Path

from core.catalog_reader import is_cherry_catalog, read_image_files, get_artist_info
from core.cherry_ingest_queue import WatchedService
from core.inventory_candidates import InventoryCancelled
from core.inventory_spool import InventorySpool
from core.inventory_walk import entries
from core.library_path_ingest import register
from core.path_identity import observe_path


def within(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def directory(path, event):
    result = observe_path(path, cancel_event=event)
    if result.status != 'resolved' or result.kind != 'directory':
        raise ValueError('cherry_ingest_folder_unavailable_or_ambiguous')
    return result


def same_directory(before, after):
    return before.canonical == after.canonical and before.physical[:3] == after.physical[:3]


def scope(service, folder, event):
    """Resolve actual Windows names; a Python casefold never authorizes ingest."""
    watches = service.read('get_watched_folders', cancel_event=event)
    if not watches:
        return None
    target = directory(folder, event)
    unresolved = False
    for watch in watches:
        try:
            parent = directory(watch, event)
        except ValueError:
            if event.is_set():
                raise InventoryCancelled()
            unresolved = True
            continue
        if within(target.canonical, parent.canonical):
            return watch, parent, target
    if unresolved:
        raise ValueError('cherry_ingest_watch_unavailable_or_ambiguous')
    return None


def reconcile(service, folder, event):
    """Never remove/archive files or alter Cherry rows, even on a complete pass."""
    selected = scope(service, folder, event)
    if selected is None:
        return 0
    watch, parent, initial = selected
    errors, count = [], 0

    def onerror(error):
        if not errors:
            errors.append(str(error)[:512])

    if is_cherry_catalog(folder):
        catalogs = iter([('catalog', folder, Path(folder).name)])
    else:
        catalogs = entries(folder, (), cancelled=event.is_set, onerror=onerror,
                           is_catalog=is_cherry_catalog)
    try:
        for _, catalog, _ in catalogs:
            if event.is_set():
                raise InventoryCancelled()
            root = directory(catalog, event)
            if not within(root.canonical, initial.canonical):
                raise ValueError('cherry_ingest_catalog_outside_scope')
            artist = get_artist_info(catalog) or {}
            with InventorySpool(cancel_event=event) as spool:
                def accept(row):
                    spool.append(dict(path=row['path'], filename=row['filename'],
                        size=row['size'], created=row['created'],
                        hash_original=row.get('cherry_hash'), origin_url=row.get('cherry_url'),
                        origin_site=artist.get('primary_site'), artist=artist.get('display_name')))
                source = read_image_files(catalog, cancelled=event.is_set, on_file=accept)
                if event.is_set():
                    raise InventoryCancelled()
                spool.seal()
                for start in range(0, len(spool), 100):
                    batch = spool[start:start + 100]
                    if (not same_directory(parent, directory(watch, event)) or
                            not same_directory(initial, directory(folder, event)) or
                            not same_directory(root, directory(catalog, event))):
                        raise ValueError('cherry_ingest_folder_changed')
                    for row in batch:
                        file = observe_path(row['path'], cancel_event=event)
                        if (file.status != 'resolved' or file.kind != 'file' or
                                not within(file.canonical, root.canonical)):
                            raise ValueError('cherry_ingest_file_requires_review')
                    if register(WatchedService(service, watch), batch, event) is not True:
                        raise RuntimeError('cherry_ingest_unconfirmed_batch')
                    count += len(batch)
                if not source.complete:
                    onerror(source.issues[0] if source.issues else source.status.value)
        if not same_directory(initial, directory(folder, event)):
            raise ValueError('cherry_ingest_folder_changed')
        if errors:
            raise RuntimeError(errors[0])
        return count
    finally:
        close = getattr(catalogs, 'close', None)
        if close is not None:
            close()
