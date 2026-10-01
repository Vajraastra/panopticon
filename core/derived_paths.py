"""Caché privada del escritor para approve/register, nunca una reserva.

Sólo el dueño con BEGIN IMMEDIATE puede reutilizarla: data_version en SU conexión
detecta commits externos; total_changes y los hooks de commit/rollback cubren los
propios. Sin DDL, UDF persistente ni transacciones retenidas entre comandos.
"""
import sys

from core import metadata_destinations as limits
from core.generation_history import path_status


class DerivedPaths:
    def __init__(self, conn):
        self.conn = conn
        self.clear()

    def clear(self):
        self.keys = None
        self.version = None
        self.changes = None
        self.rows = self.size = self.memory = 0
        self.pending = False

    def occupied(self, aliases, check):
        if not self.conn.in_transaction:
            raise RuntimeError('derived_write_requires_transaction')
        check()
        version = self.conn.execute('PRAGMA data_version').fetchone()[0]
        if version != self.version or self.changes != self.conn.total_changes:
            self.clear()
            keys, rows, size, memory = set(), 0, 0, 0
            cursor = self.conn.execute('SELECT substr(path,1,4097) FROM files')
            try:
                for (path,) in cursor:
                    check()
                    if not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path:
                        raise ValueError('invalid_stored_metadata_path')
                    key = path.casefold()
                    rows += 1
                    size += len(key.encode('utf-8'))
                    if key not in keys:
                        memory += sys.getsizeof(key)
                        keys.add(key)
                    if (rows > limits.MAX_ROWS or size > limits.MAX_BYTES or
                            memory + sys.getsizeof(keys) > limits.MAX_KEY_MEMORY):
                        keys = None
                        break
            finally:
                cursor.close()
            check()
            self.keys, self.rows, self.size, self.memory = keys, rows, size, memory
            self.version, self.changes = version, self.conn.total_changes
        if self.keys is None:
            # Exceder límites jamás permite aprobar usando sólo el prefijo leído.
            value = path_status(self.conn, aliases, normalize=lambda path: path)['occupied']
        else:
            value = any(key in self.keys for key in aliases)
        check()
        return value

    def inserted(self, path):
        # Triggers ajenos pueden cambiar otras rutas: no inferir sus efectos.
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                             "UNION ALL SELECT 1 FROM sqlite_temp_master WHERE type='trigger' LIMIT 1").fetchone():
            self.clear()
            return
        if self.version is None:
            return
        self.pending = True
        if self.keys is not None:
            key = path.casefold()
            self.rows += 1
            self.size += len(key.encode('utf-8'))
            if key not in self.keys:
                self.memory += sys.getsizeof(key)
                self.keys.add(key)
            if (self.rows > limits.MAX_ROWS or self.size > limits.MAX_BYTES or
                    self.memory + sys.getsizeof(self.keys) > limits.MAX_KEY_MEMORY):
                self.keys = None

    def finish(self, committed, operation):
        if not committed or operation not in ('approve_derived_copy', 'register_derived_copy'):
            self.clear()
        elif self.pending:
            self.changes = self.conn.total_changes
            self.pending = False
        # No releer data_version DESPUÉS del commit: perdería un commit externo
        # que llegue entre la liberación del bloqueo y este hook.
