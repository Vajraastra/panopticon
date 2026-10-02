"""Private bounded disk spool, never a Native/Cherry DB or reusable journal.

Records and path index close together before worker completion. Seal verifies
the entire stream before registration; bounded reads verify each record again.
Storage/validation failures never authorize archival using a partial inventory.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from core.inventory_candidates import InventoryCancelled

MAX_RECORD_BYTES = 64 * 1024
MAX_STREAM_BYTES = 128 * 1024 * 1024
MAX_INDEX_BYTES = 128 * 1024 * 1024
MAX_BATCH = 100


class InventorySpool:
    def __init__(self, *, cancel_event=None):
        self._cancel = cancel_event
        self._dir = self._file = self._conn = None
        self._failed = self._sealed = self._closed = False
        self._count = self._bytes = 0
        self._digest = hashlib.sha256()
        try:
            self._dir = tempfile.TemporaryDirectory(prefix='panopticon-inventory-')
            self._file = tempfile.TemporaryFile(mode='w+b')
            self._conn = sqlite3.connect(Path(self._dir.name)/'paths.db')
            self._conn.execute('PRAGMA cache_size=-2048')
            page_size = self._conn.execute('PRAGMA page_size').fetchone()[0]
            self._conn.execute(f'PRAGMA max_page_count={max(2, MAX_INDEX_BYTES//page_size)}')
            self._conn.execute('CREATE TABLE records(seq INTEGER PRIMARY KEY,path TEXT COLLATE BINARY,offset INTEGER,length INTEGER,digest BLOB)')
            self._conn.execute('CREATE INDEX paths ON records(path)')
            self._conn.commit()
            self._conn.execute('BEGIN')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            try:
                if self._file is not None:
                    self._file.close()
            finally:
                if self._dir is not None:
                    self._dir.cleanup()

    def _check(self, *, sealed=False):
        if self._closed or self._failed or (sealed and not self._sealed):
            raise ValueError('invalid_inventory_spool')
        if self._cancel is not None and self._cancel.is_set():
            raise InventoryCancelled()

    @staticmethod
    def _record(value):
        required = {'path','filename','size','created'}
        optional = {'hash_original','origin_url','origin_site','artist'}
        if (type(value) is not dict or not required <= value.keys() or value.keys()-required-optional
                or any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 4096
                       or '\0' in value[k] for k in ('path','filename'))
                or type(value['size']) is not int or value['size'] < 0
                or type(value['created']) not in (int, float) or not math.isfinite(value['created'])
                or any(v is not None and not isinstance(v, str) for k,v in value.items() if k in optional)):
            raise ValueError('invalid_inventory_spool_record')
        return dict(value, path=os.path.normpath(value['path']).replace('\\', '/'))

    def append(self, value):
        self._check()
        if self._sealed:
            raise ValueError('sealed_inventory_spool')
        try:
            value = self._record(value)
            raw = (json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False)+'\n').encode('ascii')
            if len(raw) > MAX_RECORD_BYTES or self._bytes+len(raw) > MAX_STREAM_BYTES:
                raise ValueError('inventory_spool_too_large')
            if self._file.write(raw) != len(raw):
                raise OSError('inventory_spool_write_failed')
            self._conn.execute('INSERT INTO records VALUES(?,?,?,?,?)',
                (self._count+1, value['path'], self._bytes, len(raw), hashlib.sha256(raw).digest()))
            self._digest.update(raw)
            self._count += 1
            self._bytes += len(raw)
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
            self._failed = True
            raise ValueError('inventory_spool_failed') from exc

    def extend(self, records):
        for row in records:
            self.append(row)

    def __len__(self):
        # Count a confirmed running batch even when Stop arrives before its
        # progress signal. This scalar accessor does not perform I/O.
        if self._closed or self._failed:
            raise ValueError('invalid_inventory_spool')
        return self._count

    def _decode(self, record):
        seq, path, offset, size, digest = record
        self._file.seek(offset)
        raw = self._file.read(size)
        if (len(raw) != size or size > MAX_RECORD_BYTES or not raw.endswith(b'\n')
                or hashlib.sha256(raw).digest() != digest):
            raise ValueError('invalid_inventory_spool')
        value = self._record(json.loads(raw))
        if value['path'] != path:
            raise ValueError('invalid_inventory_spool')
        return value, raw

    def verify(self):
        self._check()
        digest, count, size = hashlib.sha256(), 0, 0
        cursor = self._conn.execute('SELECT seq,path,offset,length,digest FROM records ORDER BY seq')
        try:
            for row in cursor:
                self._check()
                if row[0] != count+1 or row[2] != size:
                    raise ValueError('invalid_inventory_spool')
                _, raw = self._decode(row)
                digest.update(raw)
                count += 1
                size += len(raw)
        finally:
            cursor.close()
        self._file.seek(0, 2)
        if (count != self._count or size != self._bytes or self._file.tell() != size
                or digest.digest() != self._digest.digest()):
            raise ValueError('invalid_inventory_spool')

    def seal(self):
        self._check()
        if self._sealed:
            return
        self._file.flush()
        self.verify()
        self._conn.commit()
        self._conn.execute('PRAGMA query_only=ON')
        self._sealed = True

    def __getitem__(self, key):
        self._check(sealed=True)
        if type(key) is int:
            if not 0 <= key < self._count:
                raise IndexError(key)
            return self[key:key+1][0]
        if (not isinstance(key, slice) or key.step not in (None, 1)
                or type(key.start) is not int or type(key.stop) is not int
                or not 0 <= key.start <= key.stop or key.stop-key.start > MAX_BATCH):
            raise ValueError('invalid_inventory_spool_page')
        cursor = self._conn.execute('SELECT seq,path,offset,length,digest FROM records '
                                    'WHERE seq>? AND seq<=? ORDER BY seq', (key.start, key.stop))
        try:
            values = []
            for row in cursor:
                self._check(sealed=True)
                if row[0] != key.start+len(values)+1:
                    raise ValueError('invalid_inventory_spool')
                values.append(self._decode(row)[0])
            if len(values) != max(0, min(self._count, key.stop)-key.start):
                raise ValueError('invalid_inventory_spool')
            return values
        finally:
            cursor.close()

    def __iter__(self):
        for start in range(0, len(self), MAX_BATCH):
            yield from self[start:start+MAX_BATCH]

    def path_membership(self):
        self._check(sealed=True)
        return self

    def contains_many(self, paths):
        self._check(sealed=True)
        values = list(paths)
        if len(values) > 256 or any(not isinstance(p, str) or not p or len(p) > 4096 or '\0' in p for p in values):
            raise ValueError('invalid_inventory_spool_page')
        if not values:
            return set()
        placeholders = ','.join('?' * len(values))
        cursor = self._conn.execute(f'SELECT DISTINCT path FROM records WHERE path IN ({placeholders})', values)
        try:
            return {row[0] for row in cursor}
        finally:
            cursor.close()

    def __contains__(self, path):
        return bool(self.contains_many([path]))
