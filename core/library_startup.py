"""Presentación de fallos conocidos de apertura; no oculta errores de programación."""
from core.library_service import LibraryError

MESSAGES = {
    'database_writer_active': 'Another instance is using this library. Close it before starting this instance.',
    'invalid_database_guard': 'The library control file requires manual review; it was preserved.',
    'ambiguous_database_alias': 'The database has hard link aliases and requires review before opening.',
}


def create_library_window(factory, translate, notify):
    try:
        return factory()
    except LibraryError as error:
        if error.reason in MESSAGES:
            message = translate('app.library_error.' + error.reason, MESSAGES[error.reason])
        else:
            message = translate('app.library_error', 'Could not open the library: {reason}').format(reason=error.reason)
        notify(translate('app.library_unavailable', 'Library unavailable'), message)
        return None
