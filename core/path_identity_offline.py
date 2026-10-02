"""SQL-only availability binding for confirmed Native retirements; no DDL/FS."""
from core.path_identity_registry import prepared


def query(conn, where, params):
    if not conn.in_transaction:
        raise RuntimeError('path_identity_offline_requires_transaction')
    if not prepared(conn):
        return
    # The selector is owned SQL, supplied by its retirement command. Never
    # merge identities or infer a physical key for later online restoration.
    conn.execute("INSERT INTO path_identity_records(file_id,stored_path,is_offline,status,canonical,physical,reason) "
                 "SELECT f.id,f.path,1,'offline',NULL,NULL,NULL FROM files f WHERE f.is_offline=1 AND ("+where+") "
                 "ON CONFLICT(file_id) DO UPDATE SET stored_path=excluded.stored_path,is_offline=1,status='offline',"
                 "canonical=NULL,physical=NULL,reason=NULL WHERE path_identity_records.stored_path!=excluded.stored_path COLLATE BINARY "
                 "OR path_identity_records.is_offline!=1 OR path_identity_records.status!='offline' "
                 "OR path_identity_records.canonical IS NOT NULL OR path_identity_records.physical IS NOT NULL "
                 "OR path_identity_records.reason IS NOT NULL",params)


def ids(conn, copy_ids):
    values=list(copy_ids)
    for start in range(0,len(values),256):
        block=values[start:start+256]
        if any(type(value) is not int or not -(2**63)<=value<2**63 for value in block):
            raise ValueError('invalid_offline_copy_id')
        query(conn,'f.id IN ('+','.join('?' for _ in block)+')',block)
