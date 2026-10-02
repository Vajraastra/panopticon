"""Reconcile known missing bindings only after a complete discovered inventory."""
from core.folder_catalogue_scope import signature,membership,possible_alias,revalidate as revalidate_scope
from core.path_identity import local_path,observe_path
from core.library_batch import commit_archive_batch
from core.inventory_candidates import InventoryCancelled


def _cancelled_probe(function,*args,**kwargs):
    try:return function(*args,**kwargs)
    except ValueError as exc:
        if str(exc)=='identity_cancelled':raise InventoryCancelled() from exc
        raise


def revalidate(scope,event):
    return _cancelled_probe(revalidate_scope,scope,event)


def require_known_missing(conn):
    # Other incomplete/unsupported bindings remain an explicit review barrier.
    # Never reinterpret permission errors or unknown rows as absent files.
    if conn.execute("SELECT 1 FROM files f LEFT JOIN path_identity_records r ON r.file_id=f.id WHERE f.is_offline=0 AND (r.file_id IS NULL OR r.stored_path!=f.path COLLATE BINARY OR r.is_offline!=0 OR (r.status!='resolved' AND (r.status!='unavailable' OR r.reason IS NULL OR r.reason NOT IN ('winerror_2','winerror_3')))) LIMIT 1").fetchone():
        raise ValueError('path_identity_review_required')


def metadata(repo,folder):
    from core.folder_catalogue_scope import catalogue
    from core.path_identity_registry import prepared
    if not prepared(repo.conn):raise ValueError('path_identity_sql_state_changed')
    require_known_missing(repo.conn)
    return catalogue(repo.conn,folder,watched_required=False)


def page(repo,folder,*,scope,**kwargs):
    from core.folder_catalogue_scope import condition
    from core.library_inventory import page as inventory_page
    from core.path_identity_registry import prepared
    if not prepared(repo.conn):raise ValueError('path_identity_sql_state_changed')
    require_known_missing(repo.conn)
    where,params,stamp=condition(repo.conn,folder,scope,watched_required=False)
    if stamp['mode']!='observed_directory':raise ValueError('inventory_archive_scope_unavailable')
    where+=" AND id IN (SELECT r.file_id FROM path_identity_records r JOIN files s ON s.id=r.file_id WHERE r.status='unavailable' AND r.reason IN ('winerror_2','winerror_3') AND r.is_offline=0 AND r.stored_path=s.path COLLATE BINARY)"
    return inventory_page(repo.conn,folder,folder_like=repo._folder_like,
        folder_filter=lambda _: (where,params),**kwargs)


class UnavailableInventoryReader:
    def __init__(self,service,folder,event):
        self.service,self.folder,self.event=service,local_path(folder),event
        self._context=self._lease=None

    def __enter__(self):
        saved=self.service.read('get_unavailable_inventory_scope',self.folder,cancel_event=self.event)
        evidence={path:signature(_cancelled_probe(observe_path,path,cancel_event=self.event)) for path in saved['prefixes']}
        self.scope=dict(catalogue=saved,evidence=evidence)
        allowed,stamp=membership(self.scope)
        if stamp['mode']!='observed_directory':raise ValueError('inventory_archive_scope_unavailable')
        root=evidence[self.folder]
        for path,record in evidence.items():
            if path not in allowed and record['status']!='resolved' and (
                    possible_alias(path,self.folder) or possible_alias(path,root['canonical'])):
                raise ValueError('path_identity_review_required')
        revalidate(self.scope,self.event)
        self._context=self.service.inventory_reader(cancel_event=self.event)
        self._lease=self._context.__enter__()
        return self

    def __exit__(self,*args):
        if self._context is not None:return self._context.__exit__(*args)

    def get_inventory_page(self,folder,**kwargs):
        if local_path(folder)!=self.folder:raise ValueError('invalid_inventory_page')
        value=self._lease.get_unavailable_inventory_page(folder,scope=self.scope,**kwargs)
        revalidate(self.scope,self.event)
        return value

    def archive_missing_copies(self,records,*,folder,cancel_event=None):
        revalidate(self.scope,cancel_event)
        return commit_archive_batch(self.service,records,folder,dict(catalogue_scope=self.scope),cancel_event)
