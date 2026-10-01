"""Presupuesto de respuestas paginadas, sin truncar ni materializar fetchall.

Acota el objeto acumulado, no toda la RAM del proceso ni una celda temporal que
SQLite ya haya decodificado. Las consultas de inventario completo requieren
su propia migración a páginas; no se les aplica un límite silencioso.
"""
import sys

MAX_PAGE_ROWS = 10000
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_CELL_BYTES = 1024 * 1024


class ReadResultError(ValueError):
    pass


def validate_page(limit, offset=0):
    if (type(limit) is not int or not 1 <= limit <= MAX_PAGE_ROWS or
            type(offset) is not int or offset < 0 or offset > 2**63 - 1):
        raise ReadResultError('invalid_read_page')


class ResultBudget:
    def __init__(self):
        self.rows = 0
        self.bytes = sys.getsizeof([])

    def _charge(self, value):
        size = sys.getsizeof(value)
        if isinstance(value, (str, bytes)) and size > MAX_CELL_BYTES:
            raise ReadResultError('read_result_too_large')
        self.bytes += size
        if self.bytes > MAX_RESULT_BYTES:
            raise ReadResultError('read_result_too_large')
        if isinstance(value, dict):
            for key, item in value.items():
                self._charge(key)
                self._charge(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                self._charge(item)

    def add(self, value):
        self.rows += 1
        # Conservative list slot/allocation allowance; charge shared fields again.
        self.bytes += 32
        if self.rows > MAX_PAGE_ROWS:
            raise ReadResultError('read_result_too_large')
        self._charge(value)


def collect_rows(cursor, transform=None):
    budget = ResultBudget()
    values = []
    try:
        for row in cursor:
            value = transform(row) if transform is not None else row
            budget.add(value)
            values.append(value)
        return values
    finally:
        cursor.close()
