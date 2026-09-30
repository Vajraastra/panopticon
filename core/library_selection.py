"""Contratos SQL de selección; no abre conexiones ni toca archivos."""


def validate_selection(terms, folder):
    if (not isinstance(terms, (list, tuple)) or len(terms) > 256 or
            any(not isinstance(t, str) or not t.strip() or len(t) > 256 for t in terms) or
            (folder is not None and (not isinstance(folder, str) or not folder))):
        raise ValueError('invalid_selection')


def term_filter(terms):
    """Conserva AND entre términos y FTS/LIKE históricos de Librarian."""
    conditions, params = [], []
    long_terms = [t.strip() for t in terms if len(t.strip()) >= 3]
    short_terms = [t.strip() for t in terms if 0 < len(t.strip()) < 3]
    if long_terms:
        conditions.append('files.id IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)')
        params.append(' '.join('"' + t.replace('"', '""') + '"' for t in long_terms))
    for term in short_terms:
        conditions.append('''(
            meta_positive LIKE ? OR meta_negative LIKE ? OR meta_model LIKE ?
            OR meta_tool LIKE ? OR filename LIKE ?
            OR EXISTS (SELECT 1 FROM file_tags ft JOIN tags tg ON ft.tag_id=tg.id
                       WHERE ft.file_id=files.id AND tg.name LIKE ?)
        )''')
        params.extend([f'%{term}%'] * 6)
    return ' AND '.join(conditions), params


def select(conn, where, params, limit, offset=0, *, complete=False):
    if not where:
        return dict(count=0, paths=[], offset=0, limit=limit)
    where = 'is_offline=0 AND (' + where + ')'
    count = conn.execute(f'SELECT count(*) FROM files WHERE {where}', params).fetchone()[0]
    if complete and count > limit:
        raise ValueError('selection_requires_streaming')
    # Una página desaparecida por borrados vuelve a la última página válida.
    offset = min(offset, max(0, (count - 1) // limit) * limit)
    paths = [row[0] for row in conn.execute(
        f'SELECT path FROM files WHERE {where} ORDER BY id LIMIT ? OFFSET ?',
        [*params, limit, offset])]
    return dict(count=count, paths=paths, offset=offset, limit=limit)
