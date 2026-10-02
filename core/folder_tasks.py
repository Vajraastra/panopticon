"""Worker facade: Windows observations before/after short SQL folder snapshots."""
from core.folder_queries import SCOPED, names, ready
from core.path_identity import local_path, observe_path


def read(service, operation, args, options, event):
    # No explicit scope means an ordinary SQL search, with no filesystem work.
    requested=names(operation,args,options)
    if operation!='get_folders_paginated' and not requested:
        return service.read(operation,*args,cancel_event=event,**options)
    limit=options.get('limit',args[0] if args and operation=='get_folders_paginated' else 50)
    offset=options.get('offset',args[1] if len(args)>1 and operation=='get_folders_paginated' else 0)
    initial=service.read('get_folder_scope_metadata',albums=operation=='get_folders_paginated',
                         limit=limit,offset=offset,cancel_event=event)
    if not initial['coverage']['prepared']:
        # Keep legacy libraries operational without implicit migration/DDL.
        return service.read(operation,*args,cancel_event=event,**options)
    ready(initial['coverage'])
    requested=list(dict.fromkeys(local_path(path) for path in requested))
    page=initial.get('page')
    if page is not None:
        requested=list(dict.fromkeys(local_path(path) for path in page[1]))
    observations={}
    for path in requested:
        observation=observe_path(path,cancel_event=event)
        if observation.status!='resolved' or observation.kind not in ('file','directory'):
            raise ValueError('folder_scope_unavailable_or_ambiguous')
        observations[path]=observation
    evidence={path:dict(canonical=o.canonical,kind=o.kind,physical=o.physical) for path,o in observations.items()}
    result=service.read('read_folder_scope',operation,args,options,evidence,page,
                        cancel_event=event)
    # No SQL connection survives to these probes. External FS changes require
    # review rather than a count/page for a directory that ceased to be the same.
    for path,observation in observations.items():
        current=observe_path(path,cancel_event=event)
        same=(current.status=='resolved' and current.kind==observation.kind and current.canonical==observation.canonical
              and current.physical[:3]==observation.physical[:3]) if observation.kind=='directory' else current==observation
        if not same:
            raise ValueError('folder_scope_physical_state_changed')
    return result
