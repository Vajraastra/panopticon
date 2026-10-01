"""Explicit worker preparation/resolution; no automatic legacy identity merge."""
import threading
from core.library_batch import BatchCancelled, commit_control
from core.path_identity import observe_path


def backfill(service, *, cancel_event=None, progress=None, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_path_identity_page')
    event = cancel_event if cancel_event is not None else threading.Event()
    confirmed = pages = 0
    counts = dict(resolved=0, offline=0, unsupported=0, unavailable=0, changed=0)
    upper_id = after_id = None
    try:
        commit_control(service, 'prepare_path_identity', event)
        while True:
            if event.is_set():
                raise BatchCancelled('observation_batch_cancelled')
            page = service.inspect_path_identity_page(after_id=after_id, upper_id=upper_id,
                                                      limit=limit, cancel_event=event)
            if page['stale']:
                raise ValueError('path_identity_preview_stale')
            service.publish_path_identity_page(page, cancel_event=event)
            # Count a running batch that committed before cancellation settled.
            confirmed += len(page['records'])
            pages += bool(page['records'])
            for row in page['records']:
                status = 'unsupported' if row['status'] == 'resolved' and row['kind'] != 'file' else row['status']
                counts[status] += 1
            after_id, upper_id = page['after_id'], page['upper_id']
            if progress is not None:
                progress(dict(confirmed=confirmed, pages=pages, after_id=after_id,
                              upper_id=upper_id, counts=dict(counts)))
            if event.is_set():
                raise BatchCancelled('observation_batch_cancelled')
            if not page['more']:
                break
    except (BatchCancelled, ValueError) as exc:
        if not isinstance(exc, BatchCancelled) and str(exc) != 'identity_cancelled':
            raise
        return dict(cancelled=True, confirmed=confirmed, pages=pages,
                    after_id=after_id, upper_id=upper_id, counts=counts, coverage=None)
    from core.library_service import LibraryError
    try:
        coverage = service.read('get_path_identity_status', cancel_event=event)
    except LibraryError as exc:
        if exc.reason != 'cancelled':
            raise
        return dict(cancelled=True, confirmed=confirmed, pages=pages, after_id=after_id,
                    upper_id=upper_id, counts=counts, coverage=None)
    return dict(cancelled=False, confirmed=confirmed, pages=pages, after_id=after_id,
                upper_id=upper_id, counts=counts, coverage=coverage)


def resolve(service, path, *, cancel_event=None):
    """Worker-only; no key is used before Windows resolves the requested name.

    SQL coverage and matches share one short snapshot after filesystem I/O.
    A unique result requires complete coverage with no unresolved online rows;
    otherwise return review status without selecting an ID. This conservative
    guard prevents partly migrated legacy aliases from choosing a false winner.
    Evidence expires; mutations must still use explicit existing copy identity.
    """
    observation = observe_path(path, cancel_event=cancel_event)
    if observation.status != 'resolved' or observation.kind != 'file':
        return dict(status=observation.status if observation.status != 'resolved' else 'unsupported',
                    path=observation.path, canonical=None, records=[], file_id=None)
    revision = service.revision_snapshot()
    result = service.read('get_path_identity_resolution', observation.canonical,
                          cancel_event=cancel_event)
    if revision != service.revision_snapshot():
        result.update(status='stale', file_id=None)
    result['path'] = observation.path
    return result
