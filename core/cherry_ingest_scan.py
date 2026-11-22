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


def reconcile(service, folder, event, *, on_batch=None, on_activity=None, checkpoint=None):
    """Report confirmed batches and optionally the current catalog's progress.

    The catalog total stays unknown (-1) until its complete inventory is sealed.
    """
    def cancelled():
        if checkpoint is not None:
            checkpoint()
        return event.is_set()

    if cancelled():
        raise InventoryCancelled()
    selected = scope(service, folder, event)
    if selected is None:
        return 0
    from core.library_batch import commit_control
    if commit_control(service, 'prepare_ingest_reviews', event) is not True:
        raise RuntimeError('ingest_review_preparation_unconfirmed')
    watch, parent, initial = selected
    errors, count = [], 0

    def onerror(error):
        if not errors:
            errors.append(str(error)[:512])

    if is_cherry_catalog(folder):
        catalogs = iter([('catalog', folder, Path(folder).name)])
    else:
        catalogs = entries(folder, (), cancelled=cancelled, onerror=onerror,
                           is_catalog=is_cherry_catalog)
    try:
        for _, catalog, _ in catalogs:
            if cancelled():
                raise InventoryCancelled()
            if on_activity is not None:
                on_activity(catalog, 0, -1)
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
                    if on_activity is not None and len(spool) % 100 == 0:
                        on_activity(catalog, 0, -1)
                source = read_image_files(catalog, cancelled=event.is_set, on_file=accept,
                                          checkpoint=checkpoint)
                if cancelled():
                    raise InventoryCancelled()
                spool.seal()
                observed_count = len(spool)
                total = observed_count if source.complete else -1
                if on_activity is not None:
                    on_activity(catalog, 0, total)
                for start in range(0, observed_count, 100):
                    if cancelled():
                        raise InventoryCancelled()
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
                    watched = WatchedService(service, watch)
                    if register(watched, batch, event) is not True:
                        raise RuntimeError('cherry_ingest_unconfirmed_batch')
                    from core.ingest_review import review_notice
                    notice = review_notice(service.read('get_ingest_review_receipt', watched.operation_id))
                    count += notice['accepted']
                    if on_batch is not None:
                        on_batch(notice['accepted'], notice['conflicts'])
                    if on_activity is not None:
                        on_activity(catalog, start + len(batch), total)
                if not source.complete:
                    onerror(source.issues[0] if source.issues else source.status.value)
        if cancelled():
            raise InventoryCancelled()
        if not same_directory(initial, directory(folder, event)):
            raise ValueError('cherry_ingest_folder_changed')
        if errors:
            raise RuntimeError(errors[0])
        return count
    finally:
        close = getattr(catalogs, 'close', None)
        if close is not None:
            close()
