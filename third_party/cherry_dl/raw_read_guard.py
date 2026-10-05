"""Bound HTTP/1 socket reads to a request's remaining raw-body reservation.

HTTPX's body iterator has already read the chunk when it yields it. Limiting
there is too late near the quota. The network backend bounds reads first;
HTTP framing is not charged; raw body callbacks debit before decoding.
ContextVar keeps concurrent requests and reused connections independent.
"""

from contextvars import ContextVar

from .raw_traffic import RawQuotaExceeded


raw_capacity = ContextVar("cherry_raw_capacity", default=None)


class BoundedStream:
    def __init__(self, stream):
        self.stream = stream

    async def read(self, max_bytes, timeout=None):
        capacity = raw_capacity.get()
        if capacity is not None:
            remaining = max(0, int(capacity()))
            if not remaining:
                raise RawQuotaExceeded("Cuota de tráfico agotada; conservar parcial y esperar")
            max_bytes = min(max_bytes, remaining)
        return await self.stream.read(max_bytes, timeout)

    async def write(self, buffer, timeout=None):
        return await self.stream.write(buffer, timeout)

    async def aclose(self):
        await self.stream.aclose()

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        stream = await self.stream.start_tls(ssl_context, server_hostname, timeout)
        return BoundedStream(stream)

    def get_extra_info(self, info):
        return self.stream.get_extra_info(info)


class BoundedBackend:
    def __init__(self, backend):
        self.backend = backend

    async def connect_tcp(self, *args, **kwargs):
        return BoundedStream(await self.backend.connect_tcp(*args, **kwargs))

    async def connect_unix_socket(self, *args, **kwargs):
        return BoundedStream(await self.backend.connect_unix_socket(*args, **kwargs))

    async def sleep(self, seconds):
        return await self.backend.sleep(seconds)


def install_guard(transport):
    """Adapt HTTPX's existing HTTP/1 pool; never change installed dependencies.

    HTTPX has no public network_backend argument. These two attributes are the
    isolated compatibility boundary, tested with TCP and TLS delegation.
    Mock/custom transports without a pool remain responsible for their reads.
    """
    pool = getattr(transport, "_pool", None)
    if pool is None:
        return
    backend = getattr(pool, "_network_backend", None)
    if backend is None:
        raise RuntimeError("El transporte HTTP no admite el guard de cuota cruda")
    if not isinstance(backend, BoundedBackend):
        pool._network_backend = BoundedBackend(backend)
