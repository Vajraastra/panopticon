"""Filter explicit external paths in a bounded Native read snapshot, no writes."""
from pathlib import PurePosixPath
from core.library_results import ResultBudget, validate_page

MAX_PATHS = 10000  # Existing Native request container budget; never accept a prefix.
BLOCK = 256


def page(repo, paths, *, tags=(), search_terms=(), min_rating=0, limit=50, offset=0):
    validate_page(limit, offset)
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_PATHS:
        raise ValueError('external_selection_requires_staging')
    if any(not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path for path in paths):
        raise ValueError('invalid_external_path')
    for values in (tags, search_terms):
        if (not isinstance(values, (list, tuple)) or len(values) > 256 or
                any(not isinstance(value, str) or not value.strip() or len(value) > 256 or '\x00' in value for value in values)):
            raise ValueError('invalid_external_filter')
    if type(min_rating) is not int or not 0 <= min_rating <= 5:
        raise ValueError('invalid_external_rating')
    clauses, params = ['coalesce(f.rating,0)>=?'], [min_rating]
    for term in search_terms:
        clauses.append('(' + ' OR '.join(column + ' LIKE ?' for column in
            ('f.meta_positive', 'f.meta_model', 'f.meta_tool', 'coalesce(f.filename,i.filename)')) + ')')
        params.extend(['%' + term + '%'] * 4)
    for tag in tags:
        clauses.append('EXISTS (SELECT 1 FROM file_tags ft JOIN tags tg ON tg.id=ft.tag_id '
                       'WHERE ft.file_id=f.id AND tg.name=?)')
        params.append(tag)
    rows, total, budget = [], 0, ResultBudget()
    for start in range(0, len(paths), BLOCK):
        block = paths[start:start + BLOCK]
        normalized = [repo._normalize_path(path) for path in block]
        unique = tuple(dict.fromkeys(normalized))
        placeholders = ','.join('?' for _ in unique)
        if repo.conn.execute(f'SELECT 1 FROM files WHERE is_offline=0 AND path IN ({placeholders}) '
                             'GROUP BY path HAVING count(*)>1 LIMIT 1', unique).fetchone():
            raise ValueError('external_path_identity_requires_review')
        values = ','.join('(?,?,?,?)' for _ in block)
        bound = []
        for number, (original, path) in enumerate(zip(block, normalized)):
            bound.extend((number, path, PurePosixPath(path).name, original))
        query = (f'WITH input(n,path,filename,original) AS (VALUES {values}) '
                 'SELECT i.original,coalesce(f.rating,0) FROM input i '
                 'LEFT JOIN files f ON f.path=i.path AND f.is_offline=0 WHERE ' +
                 ' AND '.join(clauses) + ' ORDER BY i.n')
        with_cursor = repo.conn.execute(query, (*bound, *params))
        try:
            for row in with_cursor:
                if offset <= total < offset + limit:
                    budget.add(row)
                    rows.append(row)
                total += 1
        finally:
            with_cursor.close()
    return total, rows
