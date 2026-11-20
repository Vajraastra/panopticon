"""Worker: reserve Native source names before Sync, under shared scope.

No image scan, hash, content adoption or Native write. The caller must hold the
same scope for the complete Sync, not release it after counter preparation.
"""
import asyncio
from pathlib import Path, PurePosixPath
import re

from core.output_paths import path_variants
from core.path_identity import local_path
from third_party.cherry_dl.downloads import build_filename
from third_party.cherry_dl.file_routing import route, routed_name

NUMBER = re.compile(r'(?:_|-)([0-9]{5,})$')
MAX_ARTIST_PREFIXES = 65536


def artist_prefix(artist):
    if not isinstance(artist,str) or not artist.strip() or len(artist) > 1024:
        raise ValueError('invalid_counter_artist')
    return PurePosixPath(build_filename(artist,1,'source.png')).stem[:-6].casefold()


def namespace_counter(artist, stored_path):
    name = PurePosixPath(local_path(stored_path)).name
    candidate = NUMBER.search(PurePosixPath(name).stem)
    if candidate is None:
        return None
    number = int(candidate[1])
    # Round-trip through the engine, including its Unicode artist sanitization,
    # extension normalization and v2 type marker. No loose artist prefix match.
    expected = {build_filename(artist,number,name).casefold(), routed_name(artist,number,name).casefold()}
    if name.casefold() not in expected:
        return None
    if not 1 <= number <= 2**63 - 2:
        raise ValueError('source_counter_out_of_range')
    return route(name).key, number


def indexed_counter(stored_path):
    """Index canonical spellings, but only the resolved producer may use a bucket."""
    name = PurePosixPath(local_path(stored_path)).name
    stem = PurePosixPath(name).stem
    candidate = NUMBER.search(stem)
    if candidate is None:
        return None
    artist = stem[:candidate.start()]
    if stem[candidate.start()] == '-':
        spec = route(name)
        if not spec.prefix or not artist.casefold().startswith(spec.prefix+'-'):
            return None
        artist = artist[len(spec.prefix)+1:]
    try:
        parsed = namespace_counter(artist,stored_path)
    except ValueError as exc:
        if str(exc) != 'source_counter_out_of_range':raise
        return artist_prefix(artist), None
    return None if parsed is None else (artist_prefix(artist),parsed)


def root_aliases(ticket):
    ticket.assert_current()
    marker = '__native_counter_alias_probe__'
    return sorted({local_path(str(Path(value).parent)) for value in
                   path_variants(Path(ticket.canonical)/marker, Path(ticket.folder)/marker)})


def revision(service):
    value = service.revision_snapshot()
    if type(value) not in (list,tuple) or len(value) != 3 or value[2] is not True:
        raise ValueError('source_names_owner_unavailable')
    return tuple(value)


def compile_minima(service, ticket, artist=None, *, cancel_event=None):
    if service is None:
        raise ValueError('source_names_native_required')
    if artist is not None:artist_prefix(artist)
    roots = root_aliases(ticket)
    baseline = revision(service)
    snapshot, after_id, maxima = None, 0, {}
    buckets, invalid = {}, set()
    scanned = matched = pages = 0
    while True:
        ticket.assert_current()
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('source_names_cancelled')
        if revision(service) != baseline:
            raise ValueError('source_names_owner_changed')
        result = service.read('get_source_names_page',roots,after_id=after_id,snapshot=snapshot,
                              cancel_event=cancel_event)
        if revision(service) != baseline:
            raise ValueError('source_names_owner_changed')
        snapshot = result['snapshot']; pages += 1; scanned += result['scanned']
        if result['after_id'] < after_id or (not result['done'] and result['after_id'] == after_id):
            raise ValueError('source_names_cursor_not_advancing')
        after_id = result['after_id']
        for row in result['records']:
            for name in row['paths']:
                if artist is None:
                    indexed = indexed_counter(name)
                    if indexed is None:continue
                    prefix,parsed = indexed
                    if parsed is None:
                        if prefix not in invalid and len(invalid) + len(buckets) >= MAX_ARTIST_PREFIXES:
                            raise ValueError('source_names_artist_limit')
                        invalid.add(prefix);continue
                    if prefix not in buckets and len(buckets) + len(invalid) >= MAX_ARTIST_PREFIXES:
                        raise ValueError('source_names_artist_limit')
                    target = buckets.setdefault(prefix,{})
                else:
                    parsed = namespace_counter(artist,name)
                    target = maxima
                if parsed is not None:
                    namespace, number = parsed
                    target[namespace] = max(target.get(namespace,0),number); matched += 1
        if result['done']:
            ticket.assert_current()
            if revision(service) != baseline:
                raise ValueError('source_names_owner_changed')
            return dict(minima=maxima, revision=baseline, snapshot=snapshot,
                        scanned=scanned, matched=matched, pages=pages, roots=roots,
                        buckets=buckets, invalid_prefixes=invalid)


async def prepare_artist_highwater(service, ticket, artist, catalog_api, *, cancel_event=None):
    review = await asyncio.to_thread(compile_minima,service,ticket,artist,cancel_event=cancel_event)
    ticket.assert_current()
    if revision(service) != review['revision']:
        raise ValueError('source_names_owner_changed')
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError('source_names_cancelled')
    result = {}
    if review['minima']:
        result = await catalog_api.ensure_counter_at_least(Path(ticket.canonical),review['minima'])
    ticket.assert_current()
    return dict(review, counters=result)


class ArtistHighwaterHook:
    """Apply a captured bucket before the engine starts that source's workers."""
    def __init__(self,ticket,review,catalog_api,cancel_event):
        self.ticket,self.review,self.catalog_api,self.cancel_event = ticket,review,catalog_api,cancel_event
        self.applied = {};self.lock = asyncio.Lock()

    async def __call__(self,artist):
        prefix = artist_prefix(artist)
        async with self.lock:
            self.ticket.assert_current()
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise ValueError('source_names_cancelled')
            if prefix in self.review['invalid_prefixes']:
                raise ValueError('source_counter_out_of_range')
            if prefix not in self.applied:
                minima = self.review['buckets'].get(prefix,{})
                counters = await self.catalog_api.ensure_counter_at_least(Path(self.ticket.canonical),minima) if minima else {}
                self.ticket.assert_current()
                self.applied[prefix] = counters
            return dict(self.applied[prefix])


async def prepare_highwater(service,ticket,*,catalog_api=None,cancel_event=None):
    """One Native pass, then an async hook for each actual artist_info.name.

The caller retains the profile scope throughout Sync. Later successful Native
ingestion may advance its revision; it must not invalidate its own captured
names. This is prevention for identities known at capture, not a reservation
against producers that ignore the shared guard or the ingest review gate.
"""
    if catalog_api is None:
        from third_party.cherry_dl import catalog as catalog_api
    review = await asyncio.to_thread(compile_minima,service,ticket,cancel_event=cancel_event)
    ticket.assert_current()
    if revision(service) != review['revision']:
        raise ValueError('source_names_owner_changed')
    return ArtistHighwaterHook(ticket,review,catalog_api,cancel_event)


async def guarded_sync(profile, *, data_service, scope_factory, catalog_api, run_sync, cancel_event=None):
    """App/Native CLI integration contract: None dependencies fail closed."""
    if data_service is None or scope_factory is None:
        raise ValueError('source_names_native_scope_required')
    async with scope_factory(Path(profile['folder_path'])) as ticket:
        hook = await prepare_highwater(data_service,ticket,catalog_api=catalog_api,cancel_event=cancel_event)
        ticket.assert_current()
        summary = await run_sync(hook)
        ticket.assert_current()
        return summary, hook.review
