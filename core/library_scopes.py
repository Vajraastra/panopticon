"""Alcances lexicales nativos; sin resolver ni observar el filesystem."""
from pathlib import PureWindowsPath


def folder_paths(folders,normalize,*,minimum=1,maximum=32,check_nested=True):
    if (not isinstance(folders,(list,tuple)) or not minimum<=len(folders)<=maximum or
            any(not isinstance(value,str) or not value or len(value)>4096 or '\x00' in value
                or not PureWindowsPath(value).is_absolute() for value in folders)):
        raise ValueError('invalid_library_folder_scope')
    paths=[normalize(value).rstrip('/')+'/' for value in folders]
    if not check_nested:
        return paths
    for index,path in enumerate(paths):
        for other in paths[:index]:
            if path.casefold().startswith(other.casefold()) or other.casefold().startswith(path.casefold()):
                raise ValueError('nested_library_folder_scope')
    return paths


def folder_filter(paths):
    return '('+' OR '.join("path LIKE ? ESCAPE '\\'" for _ in paths)+')',[
        path.replace('\\','\\\\').replace('%',r'\%').replace('_',r'\_')+'%' for path in paths]


def observed_paths(paths,evidence):
    """Validate actual namespaces after worker observation, never case folding."""
    from core.path_identity import local_path
    resolved=[]
    for path in paths:
        record=evidence[local_path(path)]
        if record['kind']!='directory':
            raise ValueError('folder_scope_requires_directory')
        canonical=record['canonical'].rstrip('/')+'/'
        if any(canonical.startswith(other) or other.startswith(canonical) for other in resolved):
            raise ValueError('nested_library_folder_scope')
        resolved.append(canonical)
    return resolved
