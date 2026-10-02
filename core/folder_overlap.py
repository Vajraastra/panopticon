"""Solapamiento nativo por conjuntos de hashes; jamás autorización de fusión."""
from core.library_scopes import folder_paths,observed_paths

from core.duplicate_candidates import DIGEST, VALID
from core.duplicate_snapshot import encode

MAX_FOLDERS = 32
MAX_ACTIVE_COPIES = 250_000


def _scopes(folders, normalize, evidence=None):
    try:
        paths=folder_paths(folders,normalize,minimum=2,maximum=MAX_FOLDERS,check_nested=evidence is None)
        if evidence is not None:observed_paths(paths,evidence)
        return paths
    except ValueError as error:
        raise ValueError('folder_overlap_nested_scope' if str(error)=='nested_library_folder_scope'
            else 'invalid_folder_overlap_scope') from error


def compare(conn, folders, *, threshold=51, normalize, evidence=None):
    """Un snapshot SQL, conjuntos únicos y cobertura; salida completa o error."""
    paths=_scopes(folders,normalize,evidence)
    if type(threshold) is not int or not 0<=threshold<=100:
        raise ValueError('invalid_folder_overlap_threshold')
    params=[]
    if evidence is None:
        for index,path in enumerate(paths):
            params.extend((index,path.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'%'))
        scopes='scopes(n,pattern) AS (VALUES '+','.join('(?,?)' for _ in paths)+')'
        members="scopes JOIN files ON files.path LIKE scopes.pattern ESCAPE '\\'"
    else:
        from core.folder_queries import bounds
        for index,path in enumerate(observed_paths(paths,evidence)):
            params.extend((index,*bounds(path)))
        scopes='scopes(n,low,high) AS (VALUES '+','.join('(?,?,?)' for _ in paths)+')'
        members=("scopes CROSS JOIN path_identity_records r INDEXED BY idx_path_identity_canonical ON r.canonical>=scopes.low COLLATE BINARY "
                 "AND r.canonical<scopes.high COLLATE BINARY AND r.status='resolved' AND r.is_offline=0 "
                 "CROSS JOIN files ON files.id=r.file_id AND files.path=r.stored_path COLLATE BINARY")
    valid=VALID.replace('is_offline=0','files.is_offline=0')
    # No materializar toda la biblioteca en Python ni construir N² sets por par.
    # Un límite interno detectado invalida TODO el resultado, no aprueba un prefijo.
    rows=conn.execute(f'''
        WITH {scopes},
        active AS MATERIALIZED (
            SELECT scopes.n,CASE WHEN {valid} THEN {DIGEST} END digest
            FROM {members}
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
