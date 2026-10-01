"""Transactional SQL token for one cached coverage result, including foreigners.

No filesystem, WAL checkpoint, curation cache or new connection. Triggers belong
only to Native and participate in the caller's transaction/rollback. A clock
nonce distinguishes newly prepared registries; readers validate owned DDL before
using cached coverage. Clients must not disable/drop these guards silently.
"""
import threading
from uuid import UUID, uuid4

MARKER = 'path_identity_clock_v1'
TABLE = 'path_identity_clock'
DDL = ('CREATE TABLE path_identity_clock (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
       "nonce TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(typeof(epoch)='integer' AND epoch>=0))")
TRIGGERS = {
    'pi_files_insert': 'AFTER INSERT ON files',
    'pi_files_delete': 'AFTER DELETE ON files',
    'pi_files_binding': 'AFTER UPDATE OF path,is_offline ON files '
        'WHEN OLD.path!=NEW.path COLLATE BINARY OR OLD.is_offline!=NEW.is_offline',
    'pi_records_insert': 'AFTER INSERT ON path_identity_records',
    'pi_records_update': 'AFTER UPDATE ON path_identity_records',
    'pi_records_delete': 'AFTER DELETE ON path_identity_records',
}
SQL = {name: f'CREATE TRIGGER {name} {body} BEGIN '
       'UPDATE path_identity_clock SET epoch=epoch+1 WHERE singleton=1; END'
       for name, body in TRIGGERS.items()}


def token(conn):
    marker = conn.execute('SELECT value FROM panopticon_schema WHERE name=?', (MARKER,)).fetchone()
    names = [TABLE, *SQL]
    actual = dict(conn.execute('SELECT name,sql FROM sqlite_master WHERE name IN (' +
                              ','.join('?' for _ in names) + ')', names))
    if marker is None:
        if actual:
            raise ValueError('path_identity_unknown_clock')
        return None
    if marker != ('1',) or actual != {TABLE: DDL, **SQL}:
        raise ValueError('path_identity_unknown_clock')
    row = conn.execute('SELECT nonce,epoch FROM path_identity_clock WHERE singleton=1').fetchone()
    if row is None or type(row[1]) is not int or row[1] < 0:
        raise ValueError('path_identity_invalid_clock')
    try:
        nonce = UUID(row[0])
        if nonce.version != 4 or str(nonce) != row[0]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('path_identity_invalid_clock') from exc
    return row


def prepare(conn):
    if token(conn) is not None:
        return
    conn.execute(DDL)
    conn.execute('INSERT INTO path_identity_clock VALUES(1,?,0)', (str(uuid4()),))
    for value in SQL.values():
        conn.execute(value)
    conn.execute('INSERT INTO panopticon_schema VALUES(?,?)', (MARKER, '1'))


class CoverageCache:
    """One immutable-sized entry. SQL work never holds this Python lock."""
    def __init__(self):
        self._lock = threading.Lock()
        self._key = self._value = None

    def get(self, key):
        with self._lock:
            return dict(self._value) if key is not None and self._key == key else None

    def put(self, key, value):
        if key is not None:
            with self._lock:
                self._key, self._value = key, dict(value)
