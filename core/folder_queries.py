"""SQL-only observed folder scopes and bounded album aggregates."""
import os
from core.library_results import collect_rows, validate_page
from core.path_identity import local_path
from core.path_identity_registry import status, prepared

SCOPED = frozenset({'get_folder_overlap','get_phash_page','get_inventory_page','get_folder_count','get_folder_preview','get_files_recursive',
    'get_known_files_in_folder','get_library_page','get_library_selection',
    'resolve_library_selection','search_files_paginated','get_folders_paginated'})


def names(operation, args, options):
    if operation not in SCOPED:
        raise ValueError('invalid_folder_operation')
    if operation in ('get_folder_overlap','get_phash_page'):
        from core.library_scopes import folder_paths
        folders=args[0] if args else options.get('folders')
        try:
            return folder_paths(folders,local_path,minimum=2 if operation=='get_folder_overlap' else 1,check_nested=False)
        except ValueError as error:
            if operation=='get_folder_overlap':
                raise ValueError('invalid_folder_overlap_scope') from error
            raise
    values=[]
    if operation in ('get_inventory_page','get_folder_count','get_folder_preview','get_files_recursive','get_known_files_in_folder'):
        values.append(args[0] if args else options.get('folder_path'))
    elif operation != 'get_folders_paginated':
        if operation in ('get_library_page','get_library_selection','resolve_library_selection') and options.get('terms'):
            return []
        if options.get('folder'):
            values.append(options['folder'])
        if operation == 'search_files_paginated':
            query=options.get('query') or (args[0] if args else None)
            if query and 'path:' in query:
                values.append(query.split('path:')[1].split(' rating:')[0].strip())
    if any(type(path) is not str or not path for path in values):
        raise ValueError('invalid_folder_scope')
    return list(dict.fromkeys(values))


def watch_page(conn, limit, offset):
    validate_page(limit,offset)
    total=conn.execute('SELECT count(*) FROM watched_folders').fetchone()[0]
    # rowid preserves the former album order while making it explicit.
    paths=collect_rows(conn.execute('SELECT path FROM watched_folders ORDER BY rowid LIMIT ? OFFSET ?',
                                   (limit,offset)),lambda row:row[0])
    return total,paths


def metadata(repo, *, albums=False, limit=50, offset=0, requested_operation=None, args=(), options=None):
    if type(albums) is not bool:
        raise ValueError('invalid_folder_scope')
    if requested_operation is not None:
        if requested_operation not in SCOPED or type(args) not in (list,tuple) or type(options) is not dict:
            raise ValueError('invalid_folder_scope')
        if not prepared(repo.conn):
            # Legacy dispatch and page use the same snapshot/connection; no
            # global count scan or duplicate reader transaction per page.
            return dict(coverage=dict(prepared=False),value=getattr(repo,requested_operation)(*args,**options))
    value=dict(coverage=status(repo.conn,repo._path_coverage_cache))
    if albums and value['coverage']['prepared']:
        value['page']=watch_page(repo.conn,limit,offset)
    return value


def ready(coverage):
    if not coverage['prepared'] or coverage['pending'] or coverage['unresolved']:
        raise ValueError('path_identity_review_required')


def scope_read(repo, operation, args, options, evidence, page=None):
    if operation not in SCOPED or type(args) not in (list,tuple) or type(options) is not dict or type(evidence) is not dict:
        raise ValueError('invalid_folder_scope')
    ready(status(repo.conn,repo._path_coverage_cache))
    # Read current coherent coverage. Completed concurrent ingestion may advance
    # Native since the first probe; it does not invalidate a directory identity.
    if operation == 'get_folders_paginated':
        limit=options.get('limit',args[0] if args else 50)
        offset=options.get('offset',args[1] if len(args)>1 else 0)
        current=watch_page(repo.conn,limit,offset)
        if page is None or tuple(page) != current:
            raise ValueError('folder_scope_page_changed')
        expected=set(local_path(path) for path in current[1])
    else:
        expected=set(local_path(path) for path in names(operation,args,options))
    if set(evidence) != expected:
        raise ValueError('invalid_folder_scope')
    for path, record in evidence.items():
        if (local_path(path) != path or type(record) is not dict or set(record) != {'canonical','kind','physical'}
                or local_path(record['canonical']) != record['canonical'] or record['kind'] not in ('file','directory')
                or type(record['physical']) not in (tuple,list) or len(record['physical']) != 7
                or any(type(v) is not int or not 0<=v<=0xffffffff for v in record['physical'])):
            raise ValueError('invalid_folder_scope')
    previous=repo._folder_scope_context
    repo._folder_scope_context=evidence
    try:
        return getattr(repo,operation)(*args,**options)
    finally:
        repo._folder_scope_context=previous


def bounds(canonical):
    prefix=canonical.rstrip('/')+'/'
    return prefix,prefix[:-1]+'0'


def condition(repo, folder, *, exact=False):
    if repo._folder_scope_context is None:
        pattern=repo._folder_like(folder)
        if exact:
            return "(path LIKE ? ESCAPE '\\' OR path = ?)",[pattern,repo._normalize_path(folder)]
        return "path LIKE ? ESCAPE '\\'",[pattern]
    key=local_path(folder)
    if key not in repo._folder_scope_context:
        raise ValueError('invalid_folder_scope')
    record=repo._folder_scope_context[key]
    canonical=record['canonical']
    if record['kind']=='file':
        where,params=('r.canonical=? COLLATE BINARY',[canonical]) if exact else ('0',[])
    else:
        low,high=bounds(canonical)
        where,params='r.canonical>=? COLLATE BINARY AND r.canonical<? COLLATE BINARY',[low,high]
        if exact:
            where='(('+where+') OR r.canonical=? COLLATE BINARY)'; params.append(canonical)
    return ("files.id IN (SELECT r.file_id FROM path_identity_records r JOIN files s ON s.id=r.file_id "
            "WHERE r.status='resolved' AND r.is_offline=0 AND s.is_offline=0 "
            'AND r.stored_path=s.path COLLATE BINARY AND '+where+')'),params


def albums(repo, limit, offset):
    total,paths=watch_page(repo.conn,limit,offset)
    if not paths:
        return total,[]
    if repo._folder_scope_context is None:
        values=[(path,repo._folder_like(path)) for path in paths]
        wanted=','.join('(?,?)' for _ in values)
        query=('WITH w(path,pattern) AS (VALUES '+wanted+'), a AS (SELECT w.path,count(f.id) n,min(f.id) first '
               "FROM w LEFT JOIN files f ON f.is_offline=0 AND f.path LIKE w.pattern ESCAPE '\\' GROUP BY w.path) ")
    else:
        values=[]
        for path in paths:
            record=repo._folder_scope_context[local_path(path)]
            if record['kind']!='directory':
                raise ValueError('folder_scope_requires_directory')
            values.append((path,*bounds(record['canonical'])))
        wanted=','.join('(?,?,?)' for _ in values)
        query=('WITH w(path,low,high) AS (VALUES '+wanted+'), a AS (SELECT w.path,count(f.id) n,min(f.id) first '
               'FROM w LEFT JOIN path_identity_records r ON r.canonical>=w.low COLLATE BINARY '
               "AND r.canonical<w.high COLLATE BINARY AND r.status='resolved' AND r.is_offline=0 "
               'LEFT JOIN files f ON f.id=r.file_id AND f.is_offline=0 AND f.path=r.stored_path COLLATE BINARY '
               'GROUP BY w.path) ')
    query+='SELECT a.path,a.n,c.path FROM a LEFT JOIN files c ON c.id=a.first'
    rows=collect_rows(repo.conn.execute(query,[part for row in values for part in row]),
                      lambda row:dict(path=row[0],name=os.path.basename(row[0]),count=row[1],cover=row[2]))
    indexed={row['path']:row for row in rows}
    return total,[indexed[path] for path in paths]
