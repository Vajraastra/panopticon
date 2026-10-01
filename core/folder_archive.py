"""Vista previa acotada de miembros; archivar catálogo no requiere acceso al disco."""
import hashlib
import os
import sqlite3
import time
from core.duplicate_archive import roots
from core.duplicate_policy import SHA
from core.duplicate_snapshot import encode

MAX_COPIES = 1_000_000
MAX_SECONDS = 10
MAX_WRITE_SECONDS = 30
MAX_REPORT = 256*1024


def preview(conn, folder, *, normalize, folder_like):
    if (not isinstance(folder, str) or not folder or len(folder) > 4096 or '\x00' in folder or
            not os.path.isabs(folder) or normalize(folder) != folder):
        raise ValueError('invalid_folder_archive_scope')
    watched = roots(conn)
    if folder not in watched:
        raise ValueError('folder_archive_not_watched')
    deadline = time.monotonic()+MAX_SECONDS
    digest = hashlib.sha256(encode(dict(format='panopticon.folder-archive.v1',folder=folder,watched=watched)))
    active = offline = 0
    cursor = conn.execute('SELECT id,substr(path,1,4097),is_offline FROM files '
        "WHERE path LIKE ? ESCAPE '\\' ORDER BY id", (folder_like(folder),))
    try:
        if time.monotonic() >= deadline:
            raise ValueError('folder_archive_check_budget_exceeded')
        while block := cursor.fetchmany(256):
            if time.monotonic() >= deadline:
                raise ValueError('folder_archive_check_budget_exceeded')
            for key, path, state in block:
                if (type(key) is not int or not isinstance(path, str) or len(path) > 4096 or
                        type(state) is not int or state not in (0, 1)):
                    raise ValueError('folder_archive_identity_requires_review')
                if state: offline += 1
                else: active += 1
                if active+offline > MAX_COPIES:
                    raise ValueError('folder_archive_copy_limit')
                digest.update(encode([key,path,state])+b'\n')
    finally:
        cursor.close()
    prefix = folder.rstrip('/')+'/'
    key = (lambda value:value.casefold()) if os.name == 'nt' else (lambda value:value)
    nested = [root for root in watched if root != folder and key(root).startswith(key(prefix))]
    result = dict(folder=folder,active=active,offline=offline,total=active+offline,
        nested_watched=nested,fingerprint=digest.hexdigest())
    if len(encode(result)) > MAX_REPORT:
        raise ValueError('folder_archive_report_limit')
    return result


def archive(conn, folder, *, expected_fingerprint, normalize, folder_like, remove):
    if not conn.in_transaction:
        raise RuntimeError('folder_archive_requires_transaction')
    if not isinstance(expected_fingerprint,str) or not SHA.fullmatch(expected_fingerprint):
        raise ValueError('invalid_folder_archive_fingerprint')
    deadline = time.monotonic()+MAX_WRITE_SECONDS
    phase_deadline = min(deadline,time.monotonic()+MAX_SECONDS)
    reviewing = True
    timed_out = False
    def interrupt():
        nonlocal timed_out
        timed_out = time.monotonic() >= phase_deadline
        return int(timed_out)
    conn.set_progress_handler(interrupt,1000)
    try:
        if interrupt():
            raise ValueError('folder_archive_check_budget_exceeded')
        current = preview(conn,folder,normalize=normalize,folder_like=folder_like)
        if current['fingerprint'] != expected_fingerprint:
            raise ValueError('folder_archive_members_changed')
        reviewing = False
        phase_deadline = deadline
        if interrupt():
            raise ValueError('folder_archive_write_budget_exceeded')
        if remove(folder) is not True:
            if timed_out:
                raise ValueError('folder_archive_write_budget_exceeded')
            raise ValueError('folder_archive_commit_unconfirmed')
        if interrupt():
            raise ValueError('folder_archive_write_budget_exceeded')
        if (conn.execute('SELECT 1 FROM watched_folders WHERE path=?',(folder,)).fetchone() or
                conn.execute("SELECT 1 FROM files WHERE is_offline=0 AND path LIKE ? ESCAPE '\\' LIMIT 1",
                    (folder_like(folder),)).fetchone()):
            raise ValueError('folder_archive_commit_unconfirmed')
        if interrupt():
            raise ValueError('folder_archive_write_budget_exceeded')
        return True
    except sqlite3.Error as exc:
        if timed_out:
            raise ValueError('folder_archive_check_budget_exceeded' if reviewing else 'folder_archive_write_budget_exceeded') from exc
        raise
    finally:
        conn.set_progress_handler(None,0)
