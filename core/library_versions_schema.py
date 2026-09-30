"""Migración de identidad por versión; sólo se ejecuta bajo el dueño central.

Sigue la reconstrucción documentada por SQLite (lang_altertable.html, sección 8).
No abre archivos de imágenes ni cambia IDs, metadata, vínculos o filas FTS.
"""
import os
from pathlib import Path
import re
import sqlite3
import uuid
from contextlib import closing

MARKER = 'copy_versions_v1'
AVAILABILITY = 'is_offline INTEGER NOT NULL DEFAULT 0 CHECK(is_offline IN (0,1))'
INDEXES = (
    'CREATE UNIQUE INDEX idx_files_online_path ON files(path) WHERE is_offline=0',
    'CREATE INDEX idx_files_version_path ON files(path,id)',
)


def backup_database(conn, db_path, label):
    """Backup SQLite consistente (incluye WAL), publicado sólo cuando termina."""
    if conn.in_transaction:
        raise RuntimeError('backup_requires_idle_connection')
    path = Path(str(db_path) + f'.pre-{label}-{uuid.uuid4().hex}.bak')
    temporary = Path(str(path) + '.tmp')
    try:
        with closing(sqlite3.connect(temporary)) as output:
            conn.backup(output)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def ready(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='panopticon_schema'").fetchone():
        return False
    row = conn.execute('SELECT value FROM panopticon_schema WHERE name=?', (MARKER,)).fetchone()
    if row is not None and row != ('1',):
        raise ValueError('unsupported_copy_versions_schema')
    return row == ('1',)


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def initialize_new(conn):
    """Sólo al crear la tabla nativa nueva, antes de admitir trabajo."""
    for definition in INDEXES:
        conn.execute(definition)
    conn.execute('CREATE TABLE IF NOT EXISTS panopticon_schema(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
    conn.execute('INSERT INTO panopticon_schema VALUES(?,?)', (MARKER, '1'))


def upgrade_table(conn):
    """Transacción del llamador, FK desactivadas antes de BEGIN.

Sólo admite el DDL nativo conocido. Un schema modificado requiere inspección;
no intenta adivinar constraints ni descartar columnas/índices ajenos.
"""
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone() != (0,):
        raise RuntimeError('version_migration_requires_transaction_without_fk')
    if ready(conn):
        return
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
    if ddl is None:
        raise ValueError('files_schema_missing')
    columns = conn.execute('PRAGMA table_xinfo(files)').fetchall()
    if any(row[6] for row in columns):
        raise ValueError('unsupported_files_generated_columns')
    names = [row[1] for row in columns]
    if 'is_offline' in names:
        column = next(row for row in columns if row[1] == 'is_offline')
        actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='files'"))
        def normalized(sql):
            return ' '.join(sql.lower().replace('"', '').split()) if sql else ''
        if column[2:5] != ('INTEGER', 1, '0') or any(
                normalized(actual.get(re.search(r'INDEX (\w+)', definition)[1])) != normalized(definition) for definition in INDEXES):
            raise ValueError('unmarked_copy_versions_schema')
        for index in conn.execute('PRAGMA index_list(files)').fetchall():
            if index[2] and index[1] != 'idx_files_online_path':
                if 'path' in [row[2] for row in conn.execute('PRAGMA index_info(' + _quoted(index[1]) + ')')]:
                    raise ValueError('unsupported_files_path_index')
        if conn.execute("SELECT 1 FROM files WHERE typeof(is_offline)<>'integer' OR is_offline NOT IN (0,1) LIMIT 1").fetchone():
            raise ValueError('invalid_copy_versions_state')
        conn.execute('CREATE TABLE IF NOT EXISTS panopticon_schema(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
        conn.execute('INSERT INTO panopticon_schema VALUES(?,?)', (MARKER, '1'))
        return
    # Deliberadamente estrecho: misma definición nativa, sin UNIQUE global de ruta.
    sql, changed = re.subn(r'(?i)(\bpath\s+TEXT\s+)UNIQUE(\s+NOT\s+NULL\b)',
                          r'\1\2', ddl[0])
    if changed != 1 or not re.match(r'(?i)^CREATE TABLE (?:"files"|files)\s*\(', sql):
        raise ValueError('unsupported_files_path_constraint')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='view'").fetchone():
        raise ValueError('version_migration_requires_view_review')
    temp = 'panopticon_files_versions_migration'
    if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (temp,)).fetchone():
        raise ValueError('version_migration_name_occupied')
    sql = re.sub(r'(?i)^CREATE TABLE (?:"files"|files)', 'CREATE TABLE ' + temp, sql, count=1)
    closing_paren = sql.rfind(')')
    sql = sql[:closing_paren] + ',' + AVAILABILITY + sql[closing_paren:]
    objects = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='files' "
                           "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
    reserved = {'idx_files_online_path', 'idx_files_version_path'}
    if any(name in reserved for _, name, _ in objects):
        raise ValueError('version_migration_index_occupied')
    # Otra unicidad de ruta, ajena al DDL nativo, no se elimina por inferencia.
    for index in conn.execute('PRAGMA index_list(files)').fetchall():
        fields = [row[2] for row in conn.execute('PRAGMA index_info(' + _quoted(index[1]) + ')')]
        if index[2] and 'path' in fields and (index[3] != 'u' or fields != ['path']):
            raise ValueError('unsupported_files_path_index')
    sequence = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='files'").fetchone()
    conn.execute(sql)
    fields = ','.join(_quoted(name) for name in names)
    conn.execute(f'INSERT INTO {temp}({fields}) SELECT {fields} FROM files')
    # Detectar cualquier transformación inesperada antes de retirar la tabla original.
    if conn.execute(f'SELECT {fields} FROM files EXCEPT SELECT {fields} FROM {temp} LIMIT 1').fetchone():
        raise sqlite3.IntegrityError('version_migration_content_changed')
    if conn.execute(f'SELECT count(*) FROM files').fetchone() != conn.execute(f'SELECT count(*) FROM {temp}').fetchone():
        raise sqlite3.IntegrityError('version_migration_count_changed')
    conn.execute('DROP TABLE files')
    conn.execute(f'ALTER TABLE {temp} RENAME TO files')
    for _, _, definition in objects:
        conn.execute(definition)
    if sequence is not None:
        conn.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='files'", sequence)
    initialize_new(conn)


def migrate(conn, db_path):
    """No-op para una DB lista; backup y rollback íntegro en cualquier fallo."""
    if ready(conn):
        return None
    if conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
        raise RuntimeError('version_migration_requires_idle_owner_with_fk')
    backup = backup_database(conn, db_path, 'copy-versions')
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        upgrade_table(conn)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('version_migration_foreign_key_check_failed')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
        if conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
            raise sqlite3.DatabaseError('foreign_keys unavailable')
    return backup
