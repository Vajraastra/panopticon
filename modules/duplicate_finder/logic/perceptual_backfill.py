"""Fase explícita pHash: SHA actual, decode acotado y commits por dueño."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import threading

from core.curation_apply import _check_cancel,_identity
from .file_guards import plain_chain


def algorithm_id():
    import imagehash
    import numpy
    import scipy
    import PIL
    return ('phash8-v1/imagehash:'+imagehash.__version__+'/pillow:'+PIL.__version__+
        '/scipy:'+scipy.__version__+'/numpy:'+numpy.__version__)


def _digest(stream,event):
    stream.seek(0)
    digest=hashlib.sha256()
    while True:
        _check_cancel(event)
        chunk=stream.read(1024*1024)
        if not chunk:break
        digest.update(chunk)
    return digest.hexdigest()


def calculate(record,event,*,pixel_limit=10_000_000):
    import imagehash
    from PIL import Image
    _check_cancel(event)
    path=Path(record['path'])
    try:
        plain_chain(path)
        # La segunda pasada debe releer bytes, no reutilizar el buffer del decoder.
        with path.open('rb',buffering=0) as stream:
            before=_identity(os.fstat(stream.fileno()))
            if _identity(path.stat())!=before:raise ValueError('copy_version_file_changed')
            if _digest(stream,event)!=record['source_sha256']:
                raise ValueError('catalog_identity_mismatch')
            cached=record['phash'] is not None
            value=record['phash']
            if not cached:
                stream.seek(0)
                with Image.open(stream) as image:
                    if image.width*image.height>pixel_limit:raise ValueError('duplicate_visual_pixel_limit')
                    _check_cancel(event)
                    value=str(imagehash.phash(image,hash_size=8,highfreq_factor=4))
                    _check_cancel(event)
                    if _digest(stream,event)!=record['source_sha256'] or _identity(os.fstat(stream.fileno()))!=before:
                        raise ValueError('copy_version_file_changed')
            elif _identity(os.fstat(stream.fileno()))!=before:
                raise ValueError('copy_version_file_changed')
        plain_chain(path)
        if _identity(path.stat())!=before:raise ValueError('copy_version_file_changed')
        return dict(record=record|{'phash':value},identity=before,cached=cached),None
    except (OSError,ValueError,Image.DecompressionBombError) as error:
        return None,getattr(error,'errno',None) or str(error)


class PerceptualBackfill:
    def __init__(self,service,folders,*,pixel_limit=10_000_000,max_images=100_000):
        if (type(pixel_limit) is not int or not 1<=pixel_limit<=10_000_000 or
                type(max_images) is not int or not 1<=max_images<=100_000):
            raise ValueError('invalid_phash_backfill_budget')
        self.service,self.folders=service,deepcopy(folders)
        self.pixel_limit,self.max_images=pixel_limit,max_images

    def run(self,*,event=None,progress=None):
        event=threading.Event() if event is None else event
        result=dict(algorithm=None,total_images=0,known_images=0,unknown_images=0,processed=0,
            persisted=0,reused=0,issues=[],issue_count=0,complete=False,cancelled=False,error=None,prepared=False)
        admitted=False
        def problem(path,reason,count=1):
            result['issue_count']+=count
            if len(result['issues'])<20:
                result['issues'].append(dict(path=str(path)[:4096],reason=str(reason)[:256],count=count))
        try:
            _check_cancel(event)
            algorithm=result['algorithm']=algorithm_id()
            first=block=self.service.read('get_phash_page',self.folders,algorithm,cancel_event=event)
            for key in ('total_images','known_images','unknown_images','prepared'):result[key]=first[key]
            if first['known_images']>self.max_images:raise ValueError('phash_backfill_image_budget')
            if first['unknown_images']:problem('', 'phash_source_sha_unknown',first['unknown_images'])
            if not first['known_images']:
                result['complete']=result['issue_count']==0
                return result
            _check_cancel(event)
            if not first['prepared']:
                future=self.service.submit('prepare_phash_registry')
                admitted=True
                if future.result() is not True:
                    raise ValueError('phash_registry_prepare_unconfirmed')
                result['prepared']=True
                admitted=False
            with ThreadPoolExecutor(max_workers=min(4,os.cpu_count() or 4)) as pool:
                while True:
                    _check_cancel(event)
                    for start in range(0,len(block['records']),64):
                        _check_cancel(event)
                        records=block['records'][start:start+64]
                        if result['processed']+len(records)>self.max_images:
                            raise ValueError('phash_backfill_image_budget')
                        observations=list(pool.map(lambda row:calculate(row,event,pixel_limit=self.pixel_limit),records))
                        ready=[]
                        for record,(proof,error) in zip(records,observations,strict=True):
                            result['processed']+=1
                            if error is not None:problem(record['path'],error)
                            elif proof['cached']:result['reused']+=1
                            else:ready.append(proof)
                            if progress:progress(result['processed'],first['known_images'],record['path'])
                        _check_cancel(event)
                        for proof in ready:
                            path=Path(proof['record']['path'])
                            plain_chain(path)
                            if _identity(path.stat())!=proof['identity']:raise ValueError('copy_version_file_changed')
                        if ready:
                            _check_cancel(event)
                            # Después de submit, completar la aceptada aun con stop/cierre.
                            future=self.service.submit('store_phash_batch',[proof['record'] for proof in ready],algorithm)
                            admitted=True
                            if future.result() is not True:
                                raise ValueError('phash_batch_commit_unconfirmed')
                            result['persisted']+=len(ready)
                            admitted=False
                    if not block['has_more']:break
                    block=self.service.read('get_phash_page',self.folders,algorithm,
                        after_id=block['next_after_id'],through_id=first['through_id'],cancel_event=event)
            _check_cancel(event)
            if result['processed']!=first['known_images']:problem('', 'phash_inventory_changed')
            result['complete']=result['issue_count']==0
        except Exception as error:
            result['cancelled']=not admitted and (event.is_set() or str(error)=='curation_cancelled')
            if not result['cancelled']:result['error']=str(getattr(error,'reason',error))[:256] or 'phash_backfill_failed'
        return result
