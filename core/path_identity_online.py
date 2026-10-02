"""Verified online transitions: worker pins bytes, Native stores evidence atomically."""
from contextlib import contextmanager
import hashlib, os
from core.curation_apply import _check_cancel
from core.path_identity import observe_path
from core.path_identity_registry import _record, prepared, status, store
from core.pinned_file import PinnedFile


@contextmanager
def pinned_online(path, cancel_event=None, *, expected_digest=None, service, copy_id):
    # Keep the Windows read handle until the accepted Native write settles.
    with PinnedFile(path) as pin:
        before=pin.check()
        first=observe_path(str(path),cancel_event=cancel_event)
        if first.status!='resolved' or first.kind!='file':
            raise ValueError('path_identity_review_required')
        digest=hashlib.sha256()
        while True:
            _check_cancel(cancel_event)
            block=pin.stream.read(1024*1024)
            if not block:break
            digest.update(block)
        current=digest.hexdigest()
        if expected_digest is not None and current!=expected_digest:
            raise ValueError('copy_version_file_changed')
        if pin.check()!=before or observe_path(str(path),cancel_event=cancel_event)!=first:
            raise ValueError('copy_version_file_changed')
        info=os.fstat(pin.stream.fileno())
        snapshot=service.read('get_path_identity_ingest_state',[first.canonical],[first.path],
            physicals=[first.physical],cancel_event=cancel_event)
        if (not snapshot['coverage']['prepared'] or snapshot['coverage']['pending'] or snapshot['coverage']['unresolved']):
            raise ValueError('path_identity_review_required')
        exact={(key,name) for _,key,name in snapshot['matches']}
        hints={(key,name) for _,_,_,key,name in snapshot['candidates']}
        checked={}
        for key,name in exact|hints:
            if key==copy_id:continue
            saved=observe_path(name,cancel_event=cancel_event)
            if saved.status!='resolved' or saved.kind!='file':
                raise ValueError('path_identity_review_required')
            if saved.canonical==first.canonical:
                if saved.physical!=first.physical:raise ValueError('copy_version_file_changed')
                raise ValueError('copy_version_path_occupied')
            if (key,name) in exact:raise ValueError('path_identity_review_required')
            # Hard links with different resolved names remain independent copies.
            checked[name]=saved
        for name,saved in checked.items():
            if observe_path(name,cancel_event=cancel_event)!=saved:
                raise ValueError('copy_version_file_changed')
        if pin.check()!=before or observe_path(str(path),cancel_event=cancel_event)!=first:
            raise ValueError('copy_version_file_changed')
        _check_cancel(cancel_event)
        yield dict(digest=current,identity=dict(canonical=first.canonical,physical=first.physical,
                   matches=snapshot['matches'],candidates=snapshot['candidates']),
            observation=dict(filename=path.name,size=info.st_size,
                             created=getattr(info,'st_birthtime',info.st_ctime)))


def record(conn,copy_id,path,identity):
    """SQL only; require fresh proof when prepared, never infer it from old rows."""
    if not prepared(conn):
        if identity is not None:raise ValueError('path_identity_sql_state_changed')
        return None
    if type(identity) is not dict or set(identity)!={'canonical','physical','matches','candidates'}:
        raise ValueError('path_identity_review_required')
    value=dict(file_id=copy_id,stored_path=path,is_offline=0,status='resolved',
               canonical=identity['canonical'],physical=identity['physical'],reason=None)
    _record(value)
    coverage=status(conn)
    if coverage['pending'] or coverage['unresolved']:
        raise ValueError('path_identity_review_required')
    from core.library_path_ingest import state
    current=state(conn,[value['canonical']],[path],coverage=coverage,physicals=[value['physical']])
    for field in ('matches','candidates'):
        supplied=identity[field]
        if (type(supplied) not in (list,tuple) or any(type(row) not in (list,tuple) for row in supplied)
                or [tuple(row) for row in supplied]!=[tuple(row) for row in current[field]]):
            raise ValueError('path_identity_sql_state_changed')
    from core.library_transfers import guard_ingestion
    guard_ingestion(conn,[path,value['canonical']])
    if conn.execute('SELECT f.id FROM path_identity_records r JOIN files f ON f.id=r.file_id '
        "WHERE r.canonical=? COLLATE BINARY AND r.status='resolved' AND r.is_offline=0 "
        'AND f.is_offline=0 AND f.path=r.stored_path COLLATE BINARY AND f.id<>? LIMIT 1',
        (value['canonical'],copy_id)).fetchone():
        raise ValueError('copy_version_path_occupied')
    return value


def bind(conn,value,copy_id):
    if value is not None:
        store(conn,[dict(value,file_id=copy_id)])
