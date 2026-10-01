"""Lectura por ID y candidatos temporales: ningún archivado antes de completar la lectura.

El límite superior evita incorporar nuevos IDs del escritor central a un barrido
en curso. Cada página tiene su snapshot; no promete uno común a todas las páginas.
El temporal no es un diario ni se reutiliza tras reiniciar: sólo almacena revisión
pendiente. Se cierra/elimina en todos los caminos y nunca contiene generación.
"""
from collections import OrderedDict
import hashlib
import json
import tempfile

MAX_CANDIDATE_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024


class InventoryCancelled(Exception):
    pass


def _id(value):
    return type(value) is int and -2**63 <= value <= 2**63-1


def pages(db, folder, cancelled, cancel_event):
    after, upper, first = None, None, True
    while True:
        if cancelled():
            raise InventoryCancelled()
        value = db.get_inventory_page(folder, after_id=after, upper_id=upper,
                                      limit=256, cancel_event=cancel_event)
        if cancelled():
            raise InventoryCancelled()
        if (type(value) is not dict or set(value) != {'rows', 'upper_id', 'after_id', 'done'} or
                type(value['rows']) is not list or len(value['rows']) > 256 or
                type(value['done']) is not bool or
                (value['after_id'] is not None and not _id(value['after_id'])) or
                (value['upper_id'] is not None and not _id(value['upper_id'])) or
                (not first and value['upper_id'] != upper)):
            raise ValueError('invalid_inventory_page')
        upper = value['upper_id']
        rows, previous = value['rows'], after
        for row in rows:
            if (not isinstance(row, (tuple, list)) or len(row) != 2 or not _id(row[0]) or
                    upper is None or row[0] > upper or
                    (previous is not None and row[0] <= previous) or
                    not isinstance(row[1], str) or not row[1] or len(row[1]) > 4096 or '\x00' in row[1]):
                raise ValueError('invalid_inventory_page')
            previous = row[0]
        expected_done = len(rows) < 256 or (previous is not None and previous == upper)
        if (value['after_id'] != previous or value['done'] != expected_done or
                (upper is None and (rows or not value['done']))):
            raise ValueError('invalid_inventory_page')
        yield rows
        if value['done']:
            return
        after, first = previous, False


class IdentityCache:
    def __init__(self, probe, capacity=256):
        self.probe, self.capacity = probe, capacity
        self.values = OrderedDict()

    def read(self, parent):
        if parent not in self.values:
            value = self.probe(parent)
            self.values[parent] = value
            if len(self.values) > self.capacity:
                self.values.popitem(last=False)
        self.values.move_to_end(parent)
        return self.values[parent]


class InventoryCandidates:
    def __init__(self):
        self.file = tempfile.TemporaryFile(mode='w+b')
        self.count = self.bytes = 0
        self.digest = hashlib.sha256()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()

    @staticmethod
    def _validate(record):
        if (not isinstance(record, (tuple, list)) or len(record) != 4 or not _id(record[0]) or
                not isinstance(record[1], str) or not record[1] or len(record[1]) > 4096 or '\x00' in record[1] or
                any(type(value) is not int or value < 0 for value in record[2:])):
            raise ValueError('invalid_inventory_candidates')

    def add(self, copy_id, path, parent_identity):
        record = (copy_id, path, *parent_identity)
        self._validate(record)
        raw = (json.dumps(record, ensure_ascii=True, separators=(',', ':'))+'\n').encode('ascii')
        if len(raw) > MAX_RECORD_BYTES or self.bytes + len(raw) > MAX_CANDIDATE_BYTES:
            raise ValueError('inventory_candidates_too_large')
        if self.file.write(raw) != len(raw):
            raise OSError('inventory_candidates_write_failed')
        self.digest.update(raw)
        self.count += 1
        self.bytes += len(raw)

    def blocks(self):
        self.file.seek(0)
        block, count, size = [], 0, 0
        digest = hashlib.sha256()
        while True:
            raw = self.file.readline(MAX_RECORD_BYTES+1)
            if not raw:
                if count != self.count or size != self.bytes or digest.digest() != self.digest.digest():
                    raise ValueError('invalid_inventory_candidates')
                if block:
                    yield block
                return
            size += len(raw)
            count += 1
            digest.update(raw)
            if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b'\n') or count > self.count or size > self.bytes:
                raise ValueError('invalid_inventory_candidates')
            record = json.loads(raw)
            self._validate(record)
            block.append(record)
            if len(block) == 256:
                yield block
                block = []
