"""Diarios durables independientes: temp exclusivo, flush/fsync y replace."""
import os
import uuid
from core.duplicate_snapshot import encode
from .file_guards import plain_chain


def write_journal(path,value):
    plain_chain(path.parent)
    temporary=path.parent/('journal-'+uuid.uuid4().hex+'.tmp')
    with temporary.open('xb') as stream:
        stream.write(encode(value));stream.flush();os.fsync(stream.fileno())
    plain_chain(path.parent)
    os.replace(temporary,path)
