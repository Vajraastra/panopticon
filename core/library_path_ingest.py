"""Worker-side observations and SQL-only, atomic identity-aware ingestion.

Prepared libraries require complete evidence. This is observational ingestion,
not a file deletion/publication guard: Windows handles close before SQL.
"""
from dataclasses import asdict

from core.library_batch import BatchCancelled, commit_batch
from core.library_results import collect_rows
from core.path_identity import observe_path, local_path
from core.path_identity_clock import token
from core.path_identity_registry import prepared, status, store, PHYSICAL_COLUMNS, physical_index


def state(conn, canonicals, paths, cache=None, *, coverage=None, physicals=()):
    if (type(canonicals) not in (list, tuple) or type(paths) not in (list, tuple)
            or len(canonicals) > 100 or len(paths) > 100 or type(physicals) not in (list, tuple)
            or len(physicals) > 100):
        raise ValueError('invalid_path_ingest_batch')
    coverage = status(conn, cache) if coverage is None else coverage
    result = dict(coverage=coverage, clock=token(conn), matches=[], historical=[], candidates=[])
    if not coverage['prepared'] or not canonicals:
        return result
    names = list(dict.fromkeys(local_path(p) for p in canonicals))
    placeholders = ','.join('?' * len(names))
    # At most one target per input; reject an overflowing result, never approve
    # its prefix. Keep conflicting IDs separately reviewable.
    rows = collect_rows(conn.execute('SELECT r.canonical,f.id,f.path FROM '
        'path_identity_records r JOIN files f ON f.id=r.file_id '
        f'WHERE r.canonical IN ({placeholders}) AND r.status=\'resolved\' '
        'AND f.is_offline=0 AND r.is_offline=0 AND f.path=r.stored_path COLLATE BINARY '
        'ORDER BY r.canonical,f.id LIMIT 101', names))
    if len(rows) > 100:
        raise ValueError('path_identity_review_required')
    result['matches'] = rows
    # Physical IDs are bounded search hints, never copy identity. A worker
    # must reopen each saved name: hard links remain independent named copies.
    keys = []
    for physical in physicals:
        if (type(physical) not in (tuple, list) or len(physical) != 7 or
                any(type(v) is not int or not 0 <= v <= 0xffffffff for v in physical)):
            raise ValueError('invalid_path_ingest_batch')
        if tuple(physical[:3]) not in keys:
            keys.append(tuple(physical[:3]))
    if keys:
        wanted = ','.join('(?,?,?)' for _ in keys)
        columns = [part.replace('physical', 'r.physical') for part in PHYSICAL_COLUMNS]
        if physical_index(conn):
            table = '(VALUES '+wanted+') w CROSS JOIN path_identity_records r'
            equal = ' AND '.join(column+'=w.column'+str(i+1) for i, column in enumerate(columns))
        else:
            # Prior owned schemas remain usable without implicit DDL. One
            # bounded scan per batch, rather than one scan per wanted ID.
            table = 'path_identity_records r'
            equal = '('+','.join(columns)+') IN (VALUES '+wanted+')'
        result['candidates'] = collect_rows(conn.execute(
            'SELECT '+','.join(columns)+',f.id,f.path FROM '+table+' JOIN files f ON f.id=r.file_id WHERE '+equal+
            " AND r.status='resolved' AND r.is_offline=0 AND f.is_offline=0 "
            'AND f.path=r.stored_path COLLATE BINARY ORDER BY f.id LIMIT 257',
            [part for key in keys for part in key]))
        if len(result['candidates']) > 256:
            raise ValueError('path_identity_review_required')
    names = list(dict.fromkeys([*names, *(local_path(p) for p in paths)]))
    placeholders = ','.join('?' * len(names))
    result['historical'] = collect_rows(conn.execute('SELECT DISTINCT path FROM files '
        f'WHERE is_offline=1 AND path IN ({placeholders})', names))
    return result


def _ready(coverage):
    if not coverage['prepared'] or coverage['pending'] or coverage['unresolved']:
        raise ValueError('path_identity_review_required')


def _read(service, canonicals, paths, event, physicals=()):
    from core.library_service import LibraryError
    try:
        return service.read('get_path_identity_ingest_state', canonicals, paths, physicals=physicals, cancel_event=event)
    except LibraryError as exc:
        if exc.reason == 'cancelled':
            raise BatchCancelled('observation_batch_cancelled') from exc
        raise


def register(service, records, cancel_event):
    """One <=100 batch, no reader held during filesystem I/O; FIFO admission."""
    if type(records) not in (list, tuple) or len(records) > 100:
        raise ValueError('invalid_path_ingest_batch')
    if cancel_event.is_set():
        raise BatchCancelled('observation_batch_cancelled')
    if not records:
        return True
    data = [dict(row) for row in records]
    initial = _read(service, [], [], cancel_event)
    if not initial['coverage']['prepared']:
        # No implicit migration. The writer checks this again, so explicit
        # preparation racing admission cannot let a legacy batch slip through.
        return commit_batch(service, 'register_files_observed', [dict(data=row,
            identity=None, clock=None, target=None, historical=False) for row in data], cancel_event)
    _ready(initial['coverage'])
    try:
        observations = [observe_path(row['path'], cancel_event=cancel_event) for row in data]
        if any(o.status != 'resolved' or o.kind != 'file' for o in observations):
            raise ValueError('path_identity_review_required')
        proof = _read(service, [o.canonical for o in observations],
                      [o.path for o in observations], cancel_event, [o.physical for o in observations])
        _ready(proof['coverage'])
        if proof['clock'] is None:
            raise ValueError('path_identity_not_prepared')
        targets = {}
        historical = {path for (path,) in proof['historical']}
        checked = {}
        incoming = {}
        for observation in observations:
            previous = incoming.setdefault(observation.canonical, observation)
            if previous.physical != observation.physical:
                raise ValueError('path_identity_physical_state_changed')
        for canonical, observation in incoming.items():
            exact = {(file_id, path) for name, file_id, path in proof['matches'] if name == canonical}
            hints = {(file_id, path) for volume, high, low, file_id, path in proof['candidates']
                     if (volume, high, low) == observation.physical[:3]}
            matches = set()
            for file_id, stored_path in exact | hints:
                if stored_path not in checked:
                    checked[stored_path] = observe_path(stored_path, cancel_event=cancel_event)
                saved = checked[stored_path]
                if saved.status != 'resolved' or saved.kind != 'file':
                    raise ValueError('path_identity_physical_state_changed')
                if saved.canonical == canonical:
                    if saved.physical != observation.physical:
                        raise ValueError('path_identity_physical_state_changed')
                    matches.add((file_id, stored_path))
                elif (file_id, stored_path) in exact:
                    raise ValueError('path_identity_physical_state_changed')
            if len(matches) > 1:
                raise ValueError('path_identity_review_required')
            if matches:
                targets[canonical] = next(iter(matches))
        for stored_path, saved in checked.items():
            if observe_path(stored_path, cancel_event=cancel_event) != saved:
                raise ValueError('path_identity_physical_state_changed')
        values = []
        for row, observation in zip(data, observations):
            # Reader is already closed; revalidate the whole batch before any
            # write. Identity/availability changes in SQL are checked by token.
            current = observe_path(observation.path, cancel_event=cancel_event)
            if current != observation:
                raise ValueError('path_identity_physical_state_changed')
            target = targets.get(observation.canonical)
            retained = observation.path in historical or observation.canonical in historical
            if retained and target is not None:
                raise ValueError('path_identity_review_required')
            values.append(dict(data=row, identity=asdict(observation), clock=proof['clock'],
                               target=target, historical=retained))
        return commit_batch(service, 'register_files_observed', values, cancel_event)
    except ValueError as exc:
        if str(exc) == 'identity_cancelled':
            raise BatchCancelled('observation_batch_cancelled') from exc
        raise


def write(repo, values):
    """SQL only, under BEGIN IMMEDIATE. Never select an ambiguous target."""
    if (type(values) not in (list, tuple) or len(values) > 100 or any(
            type(v) is not dict or set(v) != {'data','identity','clock','target','historical'} for v in values)):
        raise ValueError('invalid_path_ingest_batch')
    if not values:
        return True
    repo._path_ingest_transition = None
    conn = repo.conn
    ready = prepared(conn)
    if not ready:
        if any(v['identity'] is not None or v['clock'] is not None or v['target'] is not None
               or v['historical'] is not False for v in values):
            raise ValueError('path_identity_sql_state_changed')
        return repo.register_files_minimal([v['data'] for v in values])
    clock = token(conn)
    if clock is None or any(tuple(v['clock'] or ()) != clock for v in values):
        raise ValueError('path_identity_sql_state_changed')
    # Reuse only a committed reader's evidence for the unchanged token, before
    # the first mutation. Never put uncommitted writer state in this cache.
    cache = repo._path_coverage_cache
    coverage = cache.get(clock) if cache is not None else None
    coverage = coverage if coverage is not None else status(conn)
    _ready(coverage)
    canonicals, paths = [], []
    for value in values:
        identity, data = value['identity'], value['data']
        if (type(identity) is not dict or identity.get('status') != 'resolved'
                or identity.get('kind') != 'file' or type(data) is not dict
                or local_path(data['path']) != identity.get('path')
                or type(value['historical']) is not bool):
            raise ValueError('invalid_path_ingest_batch')
        canonicals.append(local_path(identity['canonical']))
        paths.append(identity['path'])
    from core.library_transfers import guard_ingestion
    guard_ingestion(conn, canonicals)
    current = state(conn, canonicals, paths, coverage=coverage,
                    physicals=[value['identity']['physical'] for value in values])
    targets = {}
    for canonical, file_id, path in current['matches']:
        if canonical in targets:
            raise ValueError('path_identity_review_required')
        targets[canonical] = (file_id, path)
    historical = {path for (path,) in current['historical']}
    repaired = False
    for value, canonical, path in zip(values, canonicals, paths):
        chosen_target = tuple(value['target']) if value['target'] is not None else None
        expected = targets.get(canonical)
        if expected is not None:
            valid = chosen_target == expected
        else:
            # Only an attested existing name for the same physical search hint
            # may repair stale canonical evidence. SQL never merges by inode.
            hints = {(file_id, stored_path) for volume, high, low, file_id, stored_path
                     in current['candidates'] if (volume, high, low) == tuple(value['identity']['physical'][:3])}
            valid = chosen_target is None or chosen_target in hints
        if not valid or value['historical'] != (path in historical or canonical in historical):
            raise ValueError('path_identity_sql_state_changed')
        if value['historical'] and chosen_target is not None:
            raise ValueError('path_identity_review_required')
        if chosen_target is not None:
            repaired = repaired or expected is None
            previous = targets.setdefault(canonical, chosen_target)
            if previous != chosen_target:
                raise ValueError('path_identity_sql_state_changed')
    # Simulate repeated aliases in input order; preserve the saved name and
    # first documented non-NULL provenance, even when Cherry brings a raw copy.
    bindings, normalized = {}, []
    chosen = {c: target[1] for c, target in targets.items()}
    for value, canonical in zip(values, canonicals):
        chosen.setdefault(canonical, value['identity']['path'])
    # Canonical evidence is separate from stored spelling and portable layout.
    # This also keeps legacy watched-folder scopes consistent until migrated.
    names = list(dict.fromkeys(chosen.values()))
    placeholders = ','.join('?' * len(names))
    saved_rows = dict((r[0], r[1:]) for r in conn.execute(
        'SELECT path,hash_original,origin_url,origin_site,artist FROM files '
        f'WHERE is_offline=0 AND path IN ({placeholders})', names))
    saved_rows_before = set(saved_rows)
    for value, canonical in zip(values, canonicals):
        if value['historical']:
            continue
        target = targets.get(canonical)
        path = chosen[canonical]
        row = dict(value['data'], path=path, filename=path.rsplit('/', 1)[-1])
        saved = saved_rows.get(path)
        if saved is not None:
            for key, previous in zip(('hash_original','origin_url','origin_site','artist'), saved):
                if previous is not None:
                    row[key] = previous
        saved_rows[path] = tuple(row.get(k) for k in ('hash_original','origin_url','origin_site','artist'))
        normalized.append(row)
    repo.register_files_minimal(normalized)
    ids = dict(conn.execute('SELECT path,id FROM files '
                           f'WHERE is_offline=0 AND path IN ({placeholders})', names))
    for value, canonical in zip(values, canonicals):
        if value['historical']:
            continue
        target = targets.get(canonical)
        path = chosen[canonical]
        file_id = ids.get(path)
        if file_id is None or (target is not None and file_id != target[0]):
            raise ValueError('path_identity_sql_state_changed')
        bindings[file_id] = dict(file_id=file_id, stored_path=path, is_offline=0,
            status='resolved', canonical=canonical, physical=value['identity']['physical'], reason=None)
    from core.path_identity_registry import _record
    previous = {}
    if bindings:
        placeholders = ','.join('?' * len(bindings))
        previous = {r[0]: r for r in conn.execute(
            'SELECT file_id,stored_path,is_offline,status,canonical,physical,reason '
            f'FROM path_identity_records WHERE file_id IN ({placeholders})', list(bindings))}
    changed = sum(previous.get(file_id) != _record(record) for file_id, record in bindings.items())
    retained = {chosen[c] for v, c in zip(values, canonicals) if not v['historical']}
    inserted = sum(path not in saved_rows_before for path in retained)
    known = not repaired and all(v['historical'] or v['target'] is not None or chosen[c] not in saved_rows_before
                for v, c in zip(values, canonicals))
    # Registration + keys + FTS all commit/rollback as one Native command.
    store(conn, list(bindings.values()))
    from core.path_identity_transition import candidate
    repo._path_ingest_transition = candidate(conn, clock, coverage, inserted=inserted,
                                             changed=changed, known_bindings=known)
    return True
