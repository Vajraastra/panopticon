"""Read-only bounded copy evidence pages; no filesystem calls or chosen winner."""
from core.library_results import collect_rows, ReadResultError
from core.path_identity_registry import prepared


def page(conn, *, after_id=None, upper_id=None, scope='issues', limit=100):
    if (scope not in ('issues','all') or type(limit) is not int or not 1<=limit<=100 or
            any(v is not None and (type(v) is not int or not -(2**63)<=v<2**63) for v in (after_id,upper_id))):
        raise ReadResultError('invalid_path_identity_page')
    if upper_id is None:
        upper_id=conn.execute('SELECT MAX(id) FROM files').fetchone()[0]
    if upper_id is None:
        return dict(records=[],after_id=after_id,upper_id=None,more=False)
    params=[upper_id]
    where='f.id<=?'
    if after_id is not None:
        where+=' AND f.id>?'
        params.append(after_id)
    if prepared(conn):
        cte=("WITH conflicts AS (SELECT r.canonical FROM path_identity_records r JOIN files f ON f.id=r.file_id "
             "WHERE r.status='resolved' AND r.is_offline=0 AND f.is_offline=0 AND f.path=r.stored_path COLLATE BINARY "
             'GROUP BY r.canonical HAVING COUNT(*)>1) ')
        columns=("f.id,f.path,COALESCE(f.rating,0),f.is_offline,CASE WHEN f.is_offline=1 THEN 'offline' "
                 "WHEN r.file_id IS NULL THEN 'pending' WHEN c.canonical IS NOT NULL THEN 'conflict' ELSE r.status END,r.canonical")
        joins=('LEFT JOIN path_identity_records r ON r.file_id=f.id AND r.stored_path=f.path COLLATE BINARY '
               'AND r.is_offline=f.is_offline LEFT JOIN conflicts c ON c.canonical=r.canonical COLLATE BINARY ')
        if scope=='issues':
            where+=" AND f.is_offline=0 AND (r.file_id IS NULL OR r.status!='resolved' OR c.canonical IS NOT NULL)"
    else:
        cte,joins='', ''
        columns="f.id,f.path,COALESCE(f.rating,0),f.is_offline,CASE WHEN f.is_offline=1 THEN 'offline' ELSE 'pending' END,NULL"
        if scope=='issues':
            where+=' AND f.is_offline=0'
    rows=collect_rows(conn.execute(cte+'SELECT '+columns+' FROM files f '+joins+
         'WHERE '+where+' ORDER BY f.id LIMIT ?',(*params,limit+1)))
    more=len(rows)>limit
    rows=rows[:limit]
    return dict(records=rows,after_id=rows[-1][0] if rows else after_id,upper_id=upper_id,more=more)
