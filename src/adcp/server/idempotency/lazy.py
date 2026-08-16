"""Deferred-construction wrapper for :class:`IdempotencyBackend`.

Mirrors the JS SDK ``createLazyBackend`` (adcp-client#2136). Use this when the
real backend depends on application infrastructure that resolves asynchronously
*after* the SDK server is constructed — for example a Postgres pool or Redis
client produced by an async bootstrap::

    from adcp.server.idempotency import (
        IdempotencyStore,
        LazyBackend,
        PgBackend,
    )

    async def _resolve() -> IdempotencyBackend:
        pool = await app.get_pg_pool()
        lock_pool = await app.get_idempotency_lock_pool()
        backend = PgBackend(pool=pool, lock_pool=lock_pool)
        await backend.create_schema()
        return backend

    store = IdempotencyStore(backend=LazyBackend(_resolve), ttl_seconds=86400)

The underlying backend is resolved on first use and memoized (resolve-once).
Concurrent first calls share a single factory invocation. If the factory
raises, the wrapper forgets that failed attempt so a later call can retry.

``clear_all`` is **not** exposed by default: per the JS contract, the presence
of a bulk-clear method is treated as the backend's explicit "safe to flush"
signal, so it must be opted into via ``allow_clear_all=True``. Enable it only
when every backend the factory can return safely permits bulk clearing (for
example a dedicated test/dev :class:`MemoryBackend`, never a shared Redis).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from adcp.server.idempotency.backends import CachedResponse, IdempotencyBackend

# A factory may be sync (returns a backend) or async (returns an awaitable that
# resolves to a backend) — both are normalized through ``_resolve`` below.
LazyBackendFactory = Callable[[], "IdempotencyBackend | Awaitable[IdempotencyBackend]"]


class LazyBackend(IdempotencyBackend):
    """Resolve an :class:`IdempotencyBackend` lazily on first use.

    :param factory: A zero-arg callable returning an :class:`IdempotencyBackend`
        or an awaitable that resolves to one. Invoked at most once across the
        wrapper's lifetime once it succeeds; re-invoked only if a prior attempt
        raised.
    :param allow_clear_all: When ``True``, expose :meth:`clear_all`, delegating
        to the resolved backend's ``clear_all`` or ``clear`` method. Defaults to
        ``False`` because bulk clearing is dangerous on shared production stores
        and the SDK uses method presence as the reset-safety contract.

    Concurrency: the first ``get``/``put``/``delete_expired`` triggers
    resolution. Multiple concurrent first operations share a single factory
    invocation via an :class:`asyncio.Lock`; later callers reuse the cached
    instance without locking on the hot path.
    """

    def __init__(
        self,
        factory: LazyBackendFactory,
        *,
        allow_clear_all: bool = False,
    ) -> None:
        self._factory = factory
        self._allow_clear_all = allow_clear_all
        self._backend: IdempotencyBackend | None = None
        self._lock = asyncio.Lock()

    async def _resolve(self) -> IdempotencyBackend:
        """Return the resolved backend, invoking the factory once on first use.

        Resolve-once + concurrency-safe: the fast path returns the memoized
        instance without locking. The slow path holds ``_lock`` so concurrent
        first callers share a single factory invocation; the double-check inside
        the lock means a caller that waited on the lock sees the instance the
        winner resolved. A factory that raises is not memoized — the next call
        retries.
        """
        cached = self._backend
        if cached is not None:
            return cached
        async with self._lock:
            # Re-read under the lock: a task that lost the race to acquire it
            # must observe the winner's resolved instance, not re-run the
            # factory. (Read into a local so the narrowing is on the local,
            # not the instance attribute another task may have mutated.)
            cached = self._backend
            if cached is not None:
                return cached
            result = self._factory()
            resolved = await result if isinstance(result, Awaitable) else result
            if not isinstance(resolved, IdempotencyBackend):
                raise TypeError(
                    "LazyBackend factory must resolve to an IdempotencyBackend, "
                    f"got {type(resolved).__name__}"
                )
            self._backend = resolved
            return resolved

    async def get(self, scope_key: str, key: str) -> CachedResponse | None:
        return await (await self._resolve()).get(scope_key, key)

    async def put(self, scope_key: str, key: str, entry: CachedResponse) -> None:
        await (await self._resolve()).put(scope_key, key, entry)

    @asynccontextmanager
    async def hold(self, scope_key: str, key: str) -> AsyncIterator[None]:
        async with (await self._resolve()).hold(scope_key, key):
            yield

    async def put_if_absent(self, scope_key: str, key: str, entry: CachedResponse) -> bool:
        return await (await self._resolve()).put_if_absent(scope_key, key, entry)

    async def supports_atomic_hold(self) -> bool:
        return await (await self._resolve()).supports_atomic_hold()

    async def supports_atomic_put_if_absent(self) -> bool:
        return await (await self._resolve()).supports_atomic_put_if_absent()

    async def delete_expired(self, now_epoch: float | None = None) -> int:
        return await (await self._resolve()).delete_expired(now_epoch)

    async def _clear_all(self) -> None:
        """Delegate a bulk clear to the resolved backend.

        Resolves the backend (so the factory runs if it hasn't yet) and
        delegates to its ``clear_all`` or ``clear`` method, raising if the
        resolved backend supports neither. Exposed as ``clear_all`` only when
        the wrapper is constructed with ``allow_clear_all=True`` (see
        :meth:`__getattr__`).
        """
        backend = await self._resolve()
        clear = getattr(backend, "clear_all", None) or getattr(backend, "clear", None)
        if clear is None:
            raise NotImplementedError(
                f"Resolved backend {type(backend).__name__} does not support "
                "clear_all() or clear()."
            )
        await clear()

    def __getattr__(self, name: str) -> object:
        """Expose ``clear_all`` only when opted in.

        Mirrors the JS wrapper, which attaches ``clearAll`` to the returned
        object solely when ``{ clearAll: true }`` — reset-safety code uses
        ``hasattr``/method presence as the "safe to flush" contract, so the
        attribute must genuinely be absent otherwise. ``__getattr__`` is only
        consulted for names not found normally, so this never shadows the
        delegating methods above.
        """
        if name == "clear_all" and self.__dict__.get("_allow_clear_all"):
            return self._clear_all
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


def create_lazy_backend(
    factory: LazyBackendFactory,
    *,
    allow_clear_all: bool = False,
) -> LazyBackend:
    """Construct a :class:`LazyBackend` — functional alias mirroring the JS
    ``createLazyBackend`` factory shape.

    See :class:`LazyBackend` for semantics and parameter documentation.
    """
    return LazyBackend(factory, allow_clear_all=allow_clear_all)
