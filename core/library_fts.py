"""Comparación del contenido FTS con la biblioteca; sin conexiones ni commits propios.

La revisión usa la transacción RO del lector. La reparación requiere la transacción
BEGIN IMMEDIATE del escritor: SQLite conserva el estado anterior hasta el commit.
No verifica las estructuras internas del tokenizer; rebuild_fts permite regenerarlas.
"""

FIELDS = ('filename', 'meta_positive', 'meta_negative', 'meta_model', 'meta_tool')
PROJECTION = ', '.join(f"COALESCE(f.{name}, '')" for name in FIELDS)
TAGS = """COALESCE((SELECT group_concat(name, ' ') FROM (
    SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id
    WHERE ft.file_id=f.id ORDER BY ft.tag_id
)), '')"""
DIFFERENCE = ' OR '.join(
    f"x.{name} IS NOT COALESCE(f.{name}, '')" for name in FIELDS
) + f' OR x.tags IS NOT {TAGS}'
MISMATCH = f'(x.rowid IS NULL OR {DIFFERENCE})'


def check_fts(conn):
    """Conteos exactos, sin lista de IDs ni escrituras. Coste lineal en biblioteca."""
    missing, stale = conn.execute(f"""
        SELECT COALESCE(sum(x.rowid IS NULL), 0),
               COALESCE(sum(x.rowid IS NOT NULL AND ({DIFFERENCE})), 0)
        FROM files f LEFT JOIN files_fts x ON x.rowid=f.id
    """).fetchone()
    orphaned = conn.execute("""
        SELECT count(*) FROM files_fts x
        WHERE NOT EXISTS(SELECT 1 FROM files f WHERE f.id=x.rowid)
    """).fetchone()[0]
    return dict(missing=missing, stale=stale, orphaned=orphaned,
                consistent=not (missing or stale or orphaned))


def repair_fts(conn):
    """Repara sólo discrepancias; cada lista Python contiene hasta 256 filas.

    Keyset por ID, sin OFFSET ni precarga completa. Los errores se propagan al
    dueño para rollback de toda la operación, incluso después de varios bloques.
    """
    last = None
    while True:
        after = '' if last is None else 'AND f.id > ?'
        rows = conn.execute(f"""
            SELECT f.id, {PROJECTION}, {TAGS}
            FROM files f LEFT JOIN files_fts x ON x.rowid=f.id
            WHERE {MISMATCH} {after} ORDER BY f.id LIMIT 256
        """, () if last is None else (last,)).fetchall()
        if not rows:
            break
        conn.executemany('DELETE FROM files_fts WHERE rowid=?',
                         ((row[0],) for row in rows))
        conn.executemany('INSERT INTO files_fts(rowid, filename, meta_positive, '
                         'meta_negative, meta_model, meta_tool, tags) '
                         'VALUES (?,?,?,?,?,?,?)', rows)
        last = rows[-1][0]
    # Los rowid FTS no pertenecientes a files también pueden ser negativos/cero.
    while True:
        rows = conn.execute("""
            SELECT x.rowid FROM files_fts x
            WHERE NOT EXISTS(SELECT 1 FROM files f WHERE f.id=x.rowid)
            LIMIT 256
        """).fetchall()
        if not rows:
            break
        conn.executemany('DELETE FROM files_fts WHERE rowid=?', rows)
    return True
