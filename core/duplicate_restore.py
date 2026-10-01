"""Reactivar sólo el ID revisado; nunca crear otra versión ni transferir curación."""
import os
import sqlite3
import time
from core.duplicate_policy import SHA
from core.library_versions import _reviewed

MAX_CHECK_SECONDS=10


def restore(conn,copy_id,*,expected_fingerprint,path,digest,normalize):
    row=_reviewed(conn,copy_id,expected_fingerprint)
    if (not isinstance(path,str) or not path or len(path)>4096 or normalize(path)!=path or
            row['path']!=path or row['is_offline']!=1):
        raise ValueError('duplicate_restore_identity_changed')
    known=row['hash_mod'] or row['hash_original']
    if (not isinstance(digest,str) or not SHA.fullmatch(digest) or
            not isinstance(known,str) or digest!=known.lower()):
        raise ValueError('duplicate_restore_digest_mismatch')
    # El índice nativo distingue mayúsculas; Windows puede representar la misma
    # ruta con otra caja/Unicode. Chequeo explícito acotado sin índice/UDF persistidos.
    deadline=time.monotonic()+MAX_CHECK_SECONDS
    timed_out=False
    def key(value):
        if not isinstance(value,str) or not value or len(value)>4096:
            raise ValueError('duplicate_restore_path_requires_review')
        return os.path.normpath(value).replace('\\','/').casefold()
    expected=key(path)
    def interrupt():
        nonlocal timed_out
        timed_out=time.monotonic()>=deadline
        return int(timed_out)
    conn.create_function('panopticon_restore_path_key',1,key)
    conn.set_progress_handler(interrupt,1000)
    try:
        cursor=conn.execute('SELECT id FROM files WHERE is_offline=0 '
            'AND panopticon_restore_path_key(path)=? LIMIT 1',(expected,))
        try:occupied=cursor.fetchone()
        finally:cursor.close()
        if occupied is not None:raise ValueError('duplicate_restore_path_occupied')
    except sqlite3.Error as exc:
        if timed_out:raise ValueError('duplicate_restore_check_budget_exceeded') from exc
        raise ValueError('duplicate_restore_path_requires_review') from exc
    finally:
        conn.set_progress_handler(None,0)
        conn.create_function('panopticon_restore_path_key',1,None)
    changed=conn.execute('UPDATE files SET is_offline=0 WHERE id=? AND is_offline=1',(copy_id,))
    if changed.rowcount!=1:raise ValueError('duplicate_restore_identity_changed')
    return True
