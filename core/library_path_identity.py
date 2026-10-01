"""Bounded preparation for path migration; no identity merges or file changes."""
from dataclasses import asdict
from core.library_results import collect_rows, ReadResultError
from core.path_identity import observe_path

MAX_IDENTITY_ROWS = 100


def records(conn, *, after_id=None, upper_id=None, limit=MAX_IDENTITY_ROWS):
    if (type(limit) is not int or not 1 <= limit <= MAX_IDENTITY_ROWS or
            any(value is not None and (type(value) is not int or not -(2**63) <= value < 2**63)
                for value in (after_id, upper_id))):
        raise ReadResultError('invalid_path_identity_page')
    if upper_id is None:
        upper_id = conn.execute('SELECT MAX(id) FROM files').fetchone()[0]
    if upper_id is None:
        return dict(records=[], after_id=after_id, upper_id=None, more=False)
    where, args = 'id<=?', [upper_id]
    if after_id is not None:
        where += ' AND id>?'
        args.append(after_id)
    rows = collect_rows(conn.execute(
        'SELECT id,path,is_offline FROM files WHERE ' + where + ' ORDER BY id LIMIT ?', (*args, limit)))
    last = rows[-1][0] if rows else after_id
    more = bool(rows and conn.execute('SELECT 1 FROM files WHERE id>? AND id<=? LIMIT 1', (last, upper_id)).fetchone())
    return dict(records=rows, after_id=last, upper_id=upper_id, more=more)


def inspect_page(service, *, after_id=None, upper_id=None, limit=MAX_IDENTITY_ROWS, cancel_event=None):
    """Worker-only. SQL snapshot closes BEFORE the first filesystem observation.

    Revision tokens detect published changes conservatively; these observations
    are a review preview, never authority to merge, archive or delete a copy.
    Offline rows remain visible but are not reactivated by finding a new file.
    """
    before = service.revision_snapshot()
    page = service.read('get_path_identity_records', after_id=after_id,
                        upper_id=upper_id, limit=limit, cancel_event=cancel_event)
    observations = []
    for file_id, path, offline in page.pop('records'):
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('identity_cancelled')
        if offline:
            identity = dict(path=path, status='offline', canonical=None, kind=None,
                            physical=None, reason='retained_offline_identity')
        else:
            try:
                identity = asdict(observe_path(path, cancel_event=cancel_event))
            except ValueError as exc:
                if str(exc) == 'identity_cancelled':
                    raise
                identity = dict(path=path, status='unsupported', canonical=None,
                                kind=None, physical=None, reason=str(exc))
        observations.append(dict(file_id=file_id, stored_path=path, **identity))
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError('identity_cancelled')
    # Also catches a concurrent close; foreign commits may be noticed on the
    # owner's next poll. Later migration must recheck SQL state and physical keys.
    after = service.revision_snapshot()
    page.update(records=observations, revision=after,
                stale=before != after or not after[-1])
    return page


def publish_page(service, page, *, cancel_event=None):
    """Explicit preparation only; cannot merge identities or activate offline.

    Page belongs to this worker. Never publish a prefix of a failed page; SQL
    commits all records together. Windows observation does not pin names across
    admission/commit, so this registry remains evidence rather than a destructive
    operation guard or proof of current bytes.
    """
    import threading
    from core.library_batch import commit_batch
    event = cancel_event if cancel_event is not None else threading.Event()
    if (not isinstance(page, dict) or page.get('stale') is not False or
            page.get('revision') != service.revision_snapshot() or
            not isinstance(page.get('records'), list) or len(page['records']) > MAX_IDENTITY_ROWS):
        raise ValueError('path_identity_preview_stale')
    values = []
    for row in page['records']:
        if event.is_set():
            raise ValueError('identity_cancelled')
        status, canonical, physical, reason = (row[k] for k in ('status','canonical','physical','reason'))
        if status == 'resolved':
            current = observe_path(row['stored_path'], cancel_event=event)
            if (current.status != 'resolved' or current.canonical != canonical or
                    current.physical != physical or current.kind != row['kind']):
                raise ValueError('path_identity_physical_state_changed')
            if current.kind != 'file':
                status, canonical, physical, reason = 'unsupported', None, None, 'identity_not_file'
        values.append(dict(file_id=row['file_id'], stored_path=row['stored_path'],
            is_offline=int(status == 'offline'), status=status, canonical=canonical,
            physical=physical, reason=reason))
    if page['revision'] != service.revision_snapshot():
        raise ValueError('path_identity_preview_stale')
    return commit_batch(service, 'store_path_identity_batch', values, event)
