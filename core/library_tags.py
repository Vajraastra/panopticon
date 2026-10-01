"""Vocabulario paginado por nombre; sugerencias Unicode sin materializar todas las etiquetas."""
from core.library_results import collect_rows, ReadResultError


def page(conn, *, query='', after_name=None, limit=200):
    if (type(limit) is not int or not 1 <= limit <= 200 or
            not isinstance(query, str) or len(query) > 256 or '\x00' in query or
            (after_name is not None and (not isinstance(after_name, str) or not after_name or
                                        len(after_name) > 256*1024 or '\x00' in after_name))):
        raise ReadResultError('invalid_tag_page')
    conditions, params = [], []
    if query:
        folded = query.casefold()
        # Literal contains, including %/_ and Unicode; no SQL wildcard semantics.
        conn.create_function('panopticon_tag_contains', 1,
            lambda name: int(folded in name.casefold()) if isinstance(name, str) else 1,
            deterministic=True)
        conditions.append('panopticon_tag_contains(name)')
    if after_name is not None:
        conditions.append('name COLLATE BINARY > ?')
        params.append(after_name)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    names = collect_rows(conn.execute(
        'SELECT name FROM tags' + where + ' ORDER BY name COLLATE BINARY LIMIT ?',
        [*params, limit+1]), lambda row: row[0])
    if any(not isinstance(name, str) or not name or '\x00' in name for name in names):
        raise ReadResultError('invalid_tag_vocabulary')
    more = len(names) > limit
    names = names[:limit]
    return dict(names=names, more=more, next_name=names[-1] if more else None)
