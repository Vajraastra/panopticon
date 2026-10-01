"""Captura acotada antes de inferencia; nunca rebindea el tail al reintentar etiquetas."""
import os,time
from core.duplicate_snapshot import encode
from core.curation_bindings import valid
MAX_PATHS=100_000
MAX_BYTES=8*1024*1024
MAX_SECONDS=30


def normalized(path):
    return os.path.normpath(path).replace('\\','/')


def capture(service,paths,*,running=lambda:True,cancel_event=None):
    if not isinstance(paths,(list,tuple)) or len(paths)>MAX_PATHS:
        raise ValueError('recognition_copy_scope_limit')
    deadline=time.monotonic()+MAX_SECONDS;seen={};scoped_bytes=2
    for index,path in enumerate(paths):
        if index%256==0:
            if not running():return None
            if time.monotonic()>=deadline:raise ValueError('recognition_copy_check_budget_exceeded')
        if not isinstance(path,str) or not path or len(path)>4096 or '\x00' in path:
            raise ValueError('recognition_copy_scope_limit')
        path=normalized(path)
        if path not in seen:
            scoped_bytes+=len(encode(path))+1
            if scoped_bytes>MAX_BYTES:raise ValueError('recognition_copy_scope_limit')
            seen[path]=None
    unique=tuple(seen);result={};consumed=2
    for offset in range(0,len(unique),256):
        if not running():return None
        if time.monotonic()>=deadline:raise ValueError('recognition_copy_check_budget_exceeded')
        block=unique[offset:offset+256]
        rows=service.read('get_curation_snapshot',block,include_revision=True,cancel_event=cancel_event)
        if not running():return None
        if time.monotonic()>=deadline:raise ValueError('recognition_copy_check_budget_exceeded')
        if (not isinstance(rows,list) or len(rows)!=len(block) or
                any(not valid(row,path) for row,path in zip(rows,block))):
            raise ValueError('recognition_copy_snapshot_incomplete')
        for row in rows:
            binding=dict(id=row['id'],path=row['path'],fingerprint=row['fingerprint'])
            consumed+=len(encode(binding))+len(encode(row['path']))+2
            if consumed>MAX_BYTES:raise ValueError('recognition_copy_scope_limit')
            result[row['path']]=binding
    return result
