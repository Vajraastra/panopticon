"""Owned, durable folder hints beside Native; never inside Cherry projects."""
import sqlite3
import time
import uuid

MAX_PENDING = 4096
SCHEMA = ('CREATE TABLE pending(path TEXT PRIMARY KEY COLLATE BINARY, '
          'revision TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0, '
          'error TEXT NOT NULL DEFAULT \'\')')


def valid_path(path):
    if type(path) is not str or not path or len(path) > 4096 or '\0' in path:
        raise ValueError('invalid_cherry_ingest_folder')
    return path


class PendingFolders:
    """One worker owns this connection. Acknowledgement compares the revision."""
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=1)
        try:
            rows = self.conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
            if rows and rows != [('pending', SCHEMA)]:
                raise ValueError('cherry_ingest_queue_requires_review')
            version = self.conn.execute('PRAGMA user_version').fetchone()[0]
            if version != (1 if rows else 0):
                raise ValueError('cherry_ingest_queue_requires_review')
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
                raise ValueError('cherry_ingest_queue_requires_review')
            self.conn.execute('PRAGMA cache_size=-1024')
            self.conn.execute('PRAGMA synchronous=FULL')
            page_size = self.conn.execute('PRAGMA page_size').fetchone()[0]
            self.conn.execute(f'PRAGMA max_page_count={128 * 1024 * 1024 // page_size}')
            if not rows:
                self.conn.execute(SCHEMA)
                self.conn.execute('PRAGMA user_version=1')
                self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    def mark(self, paths):
        if type(paths) not in (list, tuple) or len(paths) > MAX_PENDING:
            raise ValueError('cherry_ingest_queue_capacity')
        paths = list(dict.fromkeys(valid_path(p) for p in paths))
        with self.conn:
            for path in paths:
                self.conn.execute('INSERT INTO pending(path,revision) VALUES(?,?) '
                    'ON CONFLICT(path) DO UPDATE SET revision=excluded.revision,retry_at=0,error=\'\'',
                    (path, uuid.uuid4().hex))
            if self.conn.execute('SELECT count(*) FROM pending').fetchone()[0] > MAX_PENDING:
                raise ValueError('cherry_ingest_queue_capacity')

    def page(self, now=None):
        rows = self.conn.execute('SELECT path,revision FROM pending WHERE retry_at<=? '
                                 'ORDER BY path COLLATE BINARY LIMIT 128',
                                 (time.time() if now is None else now,)).fetchall()
        for path, revision in rows:
            valid_path(path)
            if type(revision) is not str or len(revision) != 32:
                raise ValueError('cherry_ingest_queue_requires_review')
        return rows

    def acknowledge(self, path, revision):
        with self.conn:
            self.conn.execute('DELETE FROM pending WHERE path=? AND revision=?', (path, revision))

    def failed(self, path, revision, error):
        with self.conn:
            self.conn.execute('UPDATE pending SET retry_at=?,error=? WHERE path=? AND revision=?',
                              (time.time() + 60, str(error)[:512], path, revision))

    def close(self):
        self.conn.close()


def register_catalog_files(repo, records, watched_folder):
    """SQL-only scope check and identity-aware ingest in the same Native commit."""
    from core.library_path_ingest import write
    valid_path(watched_folder)
    if not repo.conn.in_transaction:
        raise RuntimeError('catalog_ingest_requires_transaction')
    if not repo.conn.execute('SELECT 1 FROM watched_folders WHERE path=? COLLATE BINARY',
                             (watched_folder,)).fetchone():
        raise ValueError('cherry_ingest_watch_removed')
    if type(records) not in (list, tuple) or len(records) > 100:
        raise ValueError('invalid_path_ingest_batch')
    names = []
    for value in records:
        if type(value) is not dict or type(value.get('data')) is not dict:
            raise ValueError('invalid_path_ingest_batch')
        names.append(repo._normalize_path(valid_path(value['data'].get('path'))))
    saved = {}
    if names:
        fields = ('hash_original', 'origin_url', 'origin_site', 'artist')
        marks = ','.join('?' * len(names))
        saved = {row[0]: row[1:] for row in repo.conn.execute(
            'SELECT path,hash_original,origin_url,origin_site,artist FROM files '
            f'WHERE is_offline=0 AND path IN ({marks})', names)}
        records = [dict(value, data=dict(value['data'])) for value in records]
        for value, path in zip(records, names):
            for field, old in zip(fields, saved.get(path, ())):
                if old is not None:
                    value['data'][field] = old
    return write(repo, records)


class WatchedService:
    """Retain the existing observed-ingest/FIFO contract, adding a SQL watch CAS."""
    def __init__(self, service, watched_folder):
        self.service, self.folder = service, watched_folder

    def read(self, *args, **kwargs):
        return self.service.read(*args, **kwargs)

    def submit(self, operation, records):
        if operation != 'register_files_observed':
            raise ValueError('invalid_cherry_ingest_operation')
        return self.service.submit('register_catalog_files_observed', records, self.folder)
