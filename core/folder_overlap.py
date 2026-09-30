"""Solapamiento nativo por conjuntos de hashes; jamás autorización de fusión."""
from pathlib import PureWindowsPath

from core.duplicate_candidates import DIGEST, VALID
from core.duplicate_snapshot import encode

MAX_FOLDERS = 32
MAX_ACTIVE_COPIES = 250_000


def _scopes(folders, normalize):
    if (not isinstance(folders,(list,tuple)) or not 2<=len(folders)<=MAX_FOLDERS or
            any(not isinstance(value,str) or not value or len(value)>4096 or '\x00' in value
                or not PureWindowsPath(value).is_absolute() for value in folders)):
        raise ValueError('invalid_folder_overlap_scope')
    paths=[normalize(value).rstrip('/')+'/' for value in folders]
    for index,path in enumerate(paths):
        for other in paths[:index]:
            if path.casefold().startswith(other.casefold()) or other.casefold().startswith(path.casefold()):
                raise ValueError('folder_overlap_nested_scope')
    return paths


def compare(conn, folders, *, threshold=51, normalize):
    """Un snapshot SQL, conjuntos únicos y cobertura; salida completa o error."""
    paths=_scopes(folders,normalize)
    if type(threshold) is not int or not 0<=threshold<=100:
        raise ValueError('invalid_folder_overlap_threshold')
    params=[]
    for index,path in enumerate(paths):
        params.extend((index,path.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'%'))
    # No materializar toda la biblioteca en Python ni construir N² sets por par.
    # Un límite interno detectado invalida TODO el resultado, no aprueba un prefijo.
    rows=conn.execute(f'''
        WITH scopes(n,pattern) AS (VALUES {','.join('(?,?)' for _ in paths)}),
        active AS MATERIALIZED (
            SELECT scopes.n,CASE WHEN {VALID} THEN {DIGEST} END digest
            FROM scopes JOIN files ON files.path LIKE scopes.pattern ESCAPE '\\'
            WHERE files.is_offline=0 LIMIT {MAX_ACTIVE_COPIES+1}
        ),
        hashes AS MATERIALIZED (
            SELECT n,digest FROM active WHERE digest IS NOT NULL GROUP BY n,digest
        ),
        counts AS (
            SELECT n,count(*) total,count(digest) known FROM active GROUP BY n
        ),
        unique_counts AS (SELECT n,count(*) hashes FROM hashes GROUP BY n),
        shared AS (
            SELECT a.n first,b.n second,count(*) common FROM hashes a JOIN hashes b
            ON a.digest=b.digest AND a.n<b.n GROUP BY a.n,b.n
        ),
        totals AS (
            SELECT scopes.n,coalesce(counts.total,0) total,coalesce(counts.known,0) known,
                coalesce(unique_counts.hashes,0) hashes
            FROM scopes LEFT JOIN counts USING(n) LEFT JOIN unique_counts USING(n)
        )
        SELECT 0 kind,n,-1,total,known,hashes FROM totals
        UNION ALL
        SELECT 1,a.n,b.n,coalesce(shared.common,0),0,0 FROM totals a JOIN totals b ON a.n<b.n
        LEFT JOIN shared ON shared.first=a.n AND shared.second=b.n
        ORDER BY 1,2,3
        ''',params).fetchall()
    records=[dict(folder=path.rstrip('/') if len(path)>3 else path,active_copies=total,
        hashed_copies=known,unknown_copies=total-known,unique_hashes=unique)
        for path,(_,_,_,total,known,unique) in zip(paths,rows[:len(paths)],strict=True)]
    if sum(record['active_copies'] for record in records)>MAX_ACTIVE_COPIES:
        raise ValueError('folder_overlap_copy_budget_exceeded')
    pairs=[]
    for _,first,second,common,_,_ in rows[len(paths):]:
        left,right=records[first]['unique_hashes'],records[second]['unique_hashes']
        union=left+right-common
        jaccard=common/union if union else None
        classification=('insufficient_hashes' if not left or not right else 'disjoint' if not common else
            'high_overlap' if common*100>=threshold*union else 'partial_overlap')
        pairs.append(dict(first=first,second=second,shared_hashes=common,union_hashes=union,
            jaccard=jaccard,first_coverage=common/left if left else None,
            second_coverage=common/right if right else None,classification=classification))
    result=dict(folders=records,pairs=pairs,threshold=threshold,basis='stored_active_effective_sha256')
    if len(encode(result))>256*1024:
        raise ValueError('folder_overlap_payload_budget_exceeded')
    return result
