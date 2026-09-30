"""Una LibraryService dueña por DB entre procesos cooperativos, antes de migrar."""
from contextlib import contextmanager
from pathlib import Path
from core.file_guard import file_guard

SUFFIX = '.panopticon-writer.guard'
MAGIC = b'PANOPTICON_CENTRAL_WRITER_GUARD_V1\n'


def _check_alias(path):
    try:
        if path.stat().st_nlink > 1:
            raise ValueError('ambiguous_database_alias')
    except FileNotFoundError:
        pass


@contextmanager
def writer_guard(db_path):
    path = Path(db_path)
    _check_alias(path)
    with file_guard(Path(str(path) + SUFFIX), MAGIC,
            busy_reason='database_writer_active', invalid_reason='invalid_database_guard'):
        _check_alias(path)
        yield
