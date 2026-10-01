"""Native owner approval of one reviewed backup purge; no file or SQL mutations."""
import hashlib

from core.duplicate_archive import roots
from core.duplicate_snapshot import encode
from core.generation_history import SHA, historical_record, path_status
from core.library_versions import _reviewed


def history_fingerprint(record):
    return hashlib.sha256(encode(record)).hexdigest()


def approve(conn, copy_id, *, expected_fingerprint, operation_id, expected_history_fingerprint,
            current_digest, expected_path, expected_scope, backup_paths, normalize):
    if not conn.in_transaction:
        raise RuntimeError('metadata_backup_purge_requires_transaction')
    if (any(not isinstance(value,str) or not SHA.fullmatch(value) for value in
            (expected_history_fingerprint,current_digest)) or
            not isinstance(expected_path,str) or not expected_path or len(expected_path)>4096 or
            not isinstance(expected_scope,(list,tuple)) or list(expected_scope)!=roots(conn)):
        raise ValueError('metadata_backup_purge_invalid_approval')
    row = _reviewed(conn,copy_id,expected_fingerprint)
    if (row['is_offline']!=0 or row['path']!=expected_path or
            (row['hash_mod'] or row['hash_original'])!=current_digest):
        raise ValueError('metadata_backup_purge_current_copy_changed')
    record = historical_record(conn,operation_id)['record']
    if record is None or record['copy_id']!=copy_id or history_fingerprint(record)!=expected_history_fingerprint:
        raise ValueError('metadata_backup_purge_history_changed')
    if path_status(conn,backup_paths,normalize=normalize)['occupied']:
        raise ValueError('metadata_backup_purge_backup_has_native_identity')
    return True
