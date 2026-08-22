"""Storage backends for :class:`~adcp.server.idempotency.IdempotencyStore`.

A backend owns two responsibilities:

1. Retrieve a cached response by ``(principal_id, idempotency_key)``, honoring
   the seller's replay TTL.
2. Hold an execution lock across lookup, handler execution, and cache commit.
3. Atomically insert webhook-dedup markers with first-writer-wins semantics.

The lock prevents concurrent duplicate execution. Atomicity with unrelated
business writes remains the adopter's responsibility.

Backends expose async methods. The in-process :class:`MemoryBackend` is
synchronous under the hood but wrapped in ``async`` signatures so the store
can remain backend-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import weakref
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401

    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False

_PG_INSTALL_HINT = (
    "PgBackend requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'`."
)

# Byte-level ASCII identifier guard — same rationale as PgReplayStore /
# PgWebhookDeliverySupervisor. ``str.islower()`` accepts non-ASCII Unicode
# letters which would format verbatim into SQL as a different table than
# configured.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

DEFAULT_IDEMPOTENCY_TABLE = "adcp_idempotency"


@dataclass
class _LegacyBackendLockState:
    guard: asyncio.Lock
    locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock]


_LEGACY_LOCK_STATES: weakref.WeakKeyDictionary[object, _LegacyBackendLockState] = (
    weakref.WeakKeyDictionary()
)
_LEGACY_LOCK_STATES_GUARD = threading.Lock()


def _legacy_backend_lock_state(backend: object) -> _LegacyBackendLockState:
    """Share compatibility locks across coordinators using one backend."""
    with _LEGACY_LOCK_STATES_GUARD:
        state = _LEGACY_LOCK_STATES.get(backend)
        if state is None:
            state = _LegacyBackendLockState(
                guard=asyncio.Lock(),
                locks=weakref.WeakValueDictionary(),
            )
            _LEGACY_LOCK_STATES[backend] = state
        return state


def _safe_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Table name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), got {name!r}"
        )
    return name


@dataclass(frozen=True)
class CachedResponse:
    """A single cached handler response keyed by ``(principal_id, key)``.

    :param payload_hash: Canonical JSON SHA-256 of the *original* request. On
        replay we compare the new request's hash to this value; mismatch is
        ``IDEMPOTENCY_CONFLICT``.
    :param response: The response dict the handler returned. On replay,
        :meth:`IdempotencyStore.wrap` injects ``replayed: true`` at the
        envelope level per AdCP L1/security idempotency rule 4 before
        returning to the caller — the cached value here stays clean so
        the same entry can serve multiple replays without compounding.
    :param expires_at_epoch: Unix timestamp (seconds) when this entry becomes
        eligible for eviction. Reads after this time return None.
    """

    payload_hash: str
    response: dict[str, Any]
    expires_at_epoch: float


class IdempotencyBackend(ABC):
    """Abstract storage backend contract.

    All methods are async. Implementations MUST be safe to call concurrently
    from multiple asyncio tasks. ``hold`` must coordinate all processes that
    share the backend namespace.
    """

    @abstractmethod
    async def get(self, scope_key: str, key: str) -> CachedResponse | None:
        """Return the cached entry, or None if missing or expired.

        ``scope_key`` is the caller-composed identity scope — typically
        ``tenant_id + caller_identity``. Backends treat it as an opaque
        string; the composition is owned by
        :class:`~adcp.server.idempotency.IdempotencyStore`.
        """

    @abstractmethod
    async def put(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> None:
        """Store ``entry`` under ``(scope_key, key)``. Overwrites any prior
        entry — the store only calls ``put`` after verifying the slot is empty
        or expired, so an overwrite in that window is a legitimate retry of
        the write itself."""

    async def replace(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> None:
        """Replace a live entry while the caller holds this key's lock.

        Request idempotency uses first-writer-wins :meth:`put` semantics.
        Stateful protocols such as webhook delivery claims also need an
        owner-fenced live-state transition. Callers MUST invoke ``replace``
        only inside :meth:`hold` after validating the current entry. The
        default delegates to ``put`` for legacy/custom backends whose put is
        a true overwrite; backends with first-writer-wins put semantics must
        override it.
        """
        await self.put(scope_key, key, entry)

    async def current_time(self) -> float:
        """Return the clock authoritative for this backend's expiry checks."""
        return time.time()

    def hold(self, scope_key: str, key: str) -> Any:
        """Return an async context manager holding the key's execution lock.

        Custom backends must implement this operation to be usable by
        :class:`IdempotencyStore`. It is concrete only to keep existing
        backend subclasses importable while adopters migrate.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement atomic hold(scope_key, key)"
        )

    async def supports_atomic_hold(self) -> bool:
        """Return whether ``hold`` provides the backend's atomic guarantee.

        This explicit capability is delegable by wrappers such as
        :class:`LazyBackend`; method-identity inspection is not.
        """
        return type(self).hold is not IdempotencyBackend.hold

    async def supports_atomic_put_if_absent(self) -> bool:
        """Return whether ``put_if_absent`` is implemented atomically."""
        return (
            type(self).put_if_absent is not IdempotencyBackend.put_if_absent
            or await self.supports_atomic_hold()
        )

    async def put_if_absent(self, scope_key: str, key: str, entry: CachedResponse) -> bool:
        """Atomically insert a fresh/expired slot; return whether it won.

        The default composes ``hold`` with the legacy ``get``/``put`` API,
        allowing custom backends to implement one locking primitive. Backends
        with a native conditional insert should override this method.
        """
        async with self.hold(scope_key, key):
            if await self.get(scope_key, key) is not None:
                return False
            await self.put(scope_key, key, entry)
            return True

    @abstractmethod
    async def delete_expired(self, now_epoch: float | None = None) -> int:
        """Best-effort sweep of expired entries. Returns the count removed.

        Sweeping is optional — :meth:`get` MUST self-filter expired entries.
        Backends that have natural TTL primitives (Redis ``EXPIRE``, Postgres
        partial indexes) may implement this as a no-op."""


class MemoryBackend(IdempotencyBackend):
    """In-process dict-backed store.

    Suitable for tests, single-process reference implementations, and local
    development. **Not suitable for multi-process deployments** — each worker
    has its own cache, so a retry that lands on a different worker is treated
    as a fresh request.

    Thread safety: the backend uses an :class:`asyncio.Lock` to serialize
    mutations of the shared dict. Reads go through the lock too; for a pure
    in-process backend this is cheap and prevents torn reads across concurrent
    ``get``/``put`` interleaving.

    :param clock: Callable returning the current epoch seconds. Override for
        tests that need to advance time deterministically without monkeypatching
        :mod:`time`. Defaults to :func:`time.time`.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._store: dict[tuple[str, str], CachedResponse] = {}
        self._lock = asyncio.Lock()
        self._key_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._clock = clock

    async def get(self, scope_key: str, key: str) -> CachedResponse | None:
        async with self._lock:
            entry = self._store.get((scope_key, key))
            if entry is None:
                return None
            if entry.expires_at_epoch <= self._clock():
                # Lazy expiry — drop the stale entry so the next request
                # treats the slot as fresh and races to repopulate.
                del self._store[(scope_key, key)]
                return None
            return entry

    async def put(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> None:
        async with self._lock:
            self._store[(scope_key, key)] = entry

    async def current_time(self) -> float:
        return self._clock()

    @asynccontextmanager
    async def hold(self, scope_key: str, key: str) -> AsyncIterator[None]:
        """Serialize one idempotent handler execution in this process."""
        slot = (scope_key, key)
        async with self._lock:
            key_lock = self._key_locks.get(slot)
            if key_lock is None:
                key_lock = asyncio.Lock()
                self._key_locks[slot] = key_lock
        async with key_lock:
            yield

    async def put_if_absent(self, scope_key: str, key: str, entry: CachedResponse) -> bool:
        """Atomically claim a missing or expired slot."""
        slot = (scope_key, key)
        async with self._lock:
            existing = self._store.get(slot)
            if existing is not None and existing.expires_at_epoch > self._clock():
                return False
            self._store[slot] = entry
            return True

    async def delete_expired(self, now_epoch: float | None = None) -> int:
        cutoff = now_epoch if now_epoch is not None else self._clock()
        async with self._lock:
            stale = [k for k, v in self._store.items() if v.expires_at_epoch <= cutoff]
            for k in stale:
                del self._store[k]
            return len(stale)

    async def clear(self) -> None:
        """Remove all cached entries.

        Test-suite hook — handy for resetting state between fixtures when a
        single :class:`MemoryBackend` is shared across multiple tests.
        """
        async with self._lock:
            self._store.clear()

    async def _size(self) -> int:
        """Test-only: return the current entry count."""
        async with self._lock:
            return len(self._store)


class PgBackend(IdempotencyBackend):
    """PostgreSQL-backed :class:`IdempotencyBackend`.

    Multi-worker durable replay cache. Adopters running ≥2 processes wire
    this in place of :class:`MemoryBackend` so a retry that lands on a
    different worker still replays the cached response.

    Example::

        from psycopg_pool import AsyncConnectionPool
        from adcp.server.idempotency import IdempotencyStore, PgBackend

        pool = AsyncConnectionPool("postgresql://...", min_size=2, max_size=10)
        lock_pool = AsyncConnectionPool("postgresql://...", min_size=2, max_size=10)
        backend = PgBackend(pool=pool, lock_pool=lock_pool)
        await backend.create_schema()  # idempotent; safe to call on every boot

        store = IdempotencyStore(backend=backend, ttl_seconds=86400)

    **Atomicity caveat.** The backend holds a Postgres advisory lock and writes
    the cache in its transaction, but the cache write is NOT automatically in
    the same transaction as the handler's unrelated business writes. A crash
    after an external side effect but before cache commit can still leave the
    slot empty; the next retry re-executes the handler.
    Idempotent handlers absorb this without harm. **Handlers with
    non-idempotent side effects** (e.g., ``INSERT INTO media_buys``
    without a unique constraint on the buyer's idempotency_key) need
    either: (a) handler-level dedupe via a database unique constraint
    that maps to the same key the SDK uses, or (b) the co-tx variant
    once it ships. Co-tx — handler passes its own connection so the
    cache write commits atomically with side effects — is planned as a
    follow-on enhancement.

    **Schema bootstrap caveat.** :meth:`create_schema` uses
    ``CREATE TABLE IF NOT EXISTS`` — if a table with the same name but
    a different shape already exists (Alembic migration drift, manual
    DDL with ``response JSON`` instead of ``JSONB``, missing
    ``COLLATE "C"``), this method is a no-op and the backend will run
    against the wrong column types. If you manage the schema with
    Alembic / dbmate, copy the DDL inside :meth:`create_schema`
    verbatim into a migration revision — keep ``COLLATE "C"`` and
    ``JSONB`` identical — and skip calling :meth:`create_schema` at
    boot.

    **Response payload contract.** :attr:`CachedResponse.response` is
    serialized via ``json.dumps`` for the JSONB column. Values must be
    JSON-safe — no ``datetime``, ``Decimal``, ``set``, or ``bytes``.
    Coerce in your handler before returning.

    **Cardinality / DoS.** This backend has no row cap; only TTL
    bounds the table size. Per AdCP spec, per-principal rate limiting
    at the auth tier is required — the backend trusts that. Schedule
    :meth:`delete_expired` as a cron / pg_cron / app-loop sweep
    (``get`` self-filters expired rows, but they accumulate on disk
    until something deletes them).

    **Schema.** Created idempotently by :meth:`create_schema`:

    .. code-block:: sql

        CREATE TABLE IF NOT EXISTS adcp_idempotency (
            scope_key    TEXT        COLLATE "C" NOT NULL,
            key          TEXT        COLLATE "C" NOT NULL,
            payload_hash TEXT        NOT NULL,
            response     JSONB       NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (scope_key, key)
        );
        CREATE INDEX IF NOT EXISTS adcp_idempotency_expires_idx
            ON adcp_idempotency (expires_at);

    ``COLLATE "C"`` on identifier columns avoids locale-driven equivalence
    (``Principal-A`` ≡ ``principal-a`` under Turkish/locale-aware
    collations) collapsing distinct tenants into the same cache slot.

    :param pool: ``psycopg_pool.AsyncConnectionPool`` owned by the caller.
        Each operation acquires a short-lived connection. We don't open,
        own, or close the pool.
    :param lock_pool: A distinct caller-owned pool reserved for advisory-lock
        transactions. It MUST NOT be the business/cache ``pool``: ``hold``
        keeps one connection checked out while adopter code runs, and sharing
        that pool with handler SQL can deadlock under saturation.
    :param table_name: Override the default table name. Useful for
        multi-tenant schema scoping. Default ``adcp_idempotency``.

    :raises ImportError: when psycopg/psycopg-pool are not installed.
        Install via the ``pg`` extra: ``pip install 'adcp[pg]'``.
    :raises ValueError: when ``table_name`` is not a safe ASCII
        identifier (``[a-z_][a-z0-9_]{0,62}``).
    """

    def __init__(
        self,
        *,
        pool: Any,  # psycopg_pool.AsyncConnectionPool — Any avoids runtime psycopg import
        lock_pool: Any,
        table_name: str = DEFAULT_IDEMPOTENCY_TABLE,
    ) -> None:
        if not _PG_AVAILABLE:
            raise ImportError(_PG_INSTALL_HINT)
        if lock_pool is pool:
            raise ValueError("lock_pool must be distinct from pool to prevent handler deadlocks")
        self._pool = pool
        self._lock_pool = lock_pool
        self._table = _safe_identifier(table_name)
        self._active_connection: ContextVar[tuple[Any, asyncio.Task[Any] | None] | None] = (
            ContextVar(f"adcp_idempotency_connection_{id(self)}", default=None)
        )

        # Pre-format SQL once. Validated identifier so f-string interpolation
        # is byte-safe; values always go through %s parameterization. Same
        # convention as PgWebhookDeliverySupervisor / PgReplayStore.
        t = self._table
        self._sql_get = (
            f"SELECT payload_hash, response, expires_at "  # noqa: S608
            f"FROM {t} WHERE scope_key = %s AND key = %s "
            f"AND expires_at > clock_timestamp()"
        )
        # First-writer-wins under concurrent put. The store's pre-check
        # ("slot is empty or expired") is NOT a lock — two workers can
        # both see an empty slot and race into put. With a naive
        # last-writer-wins ON CONFLICT, the second put would overwrite
        # the first's payload_hash, violating the cache invariant
        # "same (scope, key) → same hash". The WHERE on the UPDATE
        # arm restricts the overwrite to actually-expired rows: a
        # concurrent fresh write becomes a no-op, both callers
        # observe an equivalent cached entry from the first writer.
        self._sql_put = (
            f"INSERT INTO {t} "  # noqa: S608
            f"(scope_key, key, payload_hash, response, expires_at) "
            f"VALUES (%s, %s, %s, %s::jsonb, %s) "
            f"ON CONFLICT (scope_key, key) DO UPDATE SET "
            f"  payload_hash = EXCLUDED.payload_hash, "
            f"  response     = EXCLUDED.response, "
            f"  expires_at   = EXCLUDED.expires_at "
            f"WHERE {t}.expires_at <= clock_timestamp()"
        )
        self._sql_delete_expired = f"DELETE FROM {t} WHERE expires_at <= %s"  # noqa: S608
        self._sql_delete_expired_now = (
            f"DELETE FROM {t} WHERE expires_at <= clock_timestamp()"  # noqa: S608
        )
        self._sql_lock = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 6217))"
        self._sql_now = "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
        self._sql_put_if_absent = (
            f"INSERT INTO {t} "  # noqa: S608
            f"(scope_key, key, payload_hash, response, expires_at) "
            f"VALUES (%s, %s, %s, %s::jsonb, %s) "
            f"ON CONFLICT (scope_key, key) DO UPDATE SET "
            f"  payload_hash = EXCLUDED.payload_hash, response = EXCLUDED.response, "
            f"  expires_at = EXCLUDED.expires_at "
            f"WHERE {t}.expires_at <= clock_timestamp() RETURNING 1"
        )
        self._sql_replace = (
            f"UPDATE {t} SET payload_hash = %s, response = %s::jsonb, "  # noqa: S608
            f"expires_at = %s WHERE scope_key = %s AND key = %s"
        )

    async def create_schema(self) -> None:
        """Bootstrap the table + index. Idempotent.

        Safe to call on every app boot. Each DDL statement is executed
        separately — psycopg does not split on ``;``.
        """
        t = self._table
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {t} (
                scope_key    TEXT        COLLATE "C" NOT NULL,
                key          TEXT        COLLATE "C" NOT NULL,
                payload_hash TEXT        NOT NULL,
                response     JSONB       NOT NULL,
                expires_at   TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (scope_key, key)
            )""",
            # Partial-free expiry index — cheap eviction sweep.
            f"""CREATE INDEX IF NOT EXISTS {t}_expires_idx
                ON {t} (expires_at)""",
        ]
        async with self._pool.connection() as conn:
            for stmt in statements:
                await conn.execute(stmt)

    async def get(self, scope_key: str, key: str) -> CachedResponse | None:
        """Read the cached entry, filtering expired rows in the WHERE clause.

        Lazy expiry — expired rows stay on disk until ``delete_expired``
        sweeps them. ``get`` self-filters via ``clock_timestamp()`` so a
        stale row never replays.
        """
        active = self._active_connection.get()
        if active is not None and active[1] is asyncio.current_task():
            return await self._get_on_connection(active[0], scope_key, key)
        async with self._pool.connection() as conn:
            return await self._get_on_connection(conn, scope_key, key)

    async def _get_on_connection(
        self, conn: Any, scope_key: str, key: str
    ) -> CachedResponse | None:
        cur = await conn.execute(self._sql_get, (scope_key, key))
        row = await cur.fetchone()
        if row is None:
            return None
        payload_hash, response, expires_at = row
        return CachedResponse(
            payload_hash=payload_hash,
            response=response if isinstance(response, dict) else json.loads(response),
            expires_at_epoch=_to_epoch(expires_at),
        )

    async def put(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> None:
        """Atomic upsert under ``(scope_key, key)``.

        ``ON CONFLICT DO UPDATE`` because the store only calls ``put``
        after verifying the slot is empty or expired — an overwrite in
        that window is a legitimate retry of the write itself.
        """
        expires_at_dt = datetime.fromtimestamp(entry.expires_at_epoch, tz=timezone.utc)
        params = (
            scope_key,
            key,
            entry.payload_hash,
            json.dumps(entry.response),
            expires_at_dt,
        )
        active = self._active_connection.get()
        if active is not None and active[1] is asyncio.current_task():
            await active[0].execute(self._sql_put, params)
            return
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_put, params)

    async def replace(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> None:
        """Replace one live row while the caller holds its advisory lock."""
        params = (
            entry.payload_hash,
            json.dumps(entry.response),
            datetime.fromtimestamp(entry.expires_at_epoch, tz=timezone.utc),
            scope_key,
            key,
        )
        active = self._active_connection.get()
        if active is not None and active[1] is asyncio.current_task():
            await active[0].execute(self._sql_replace, params)
            return
        raise RuntimeError("PgBackend.replace() requires hold(scope_key, key)")

    async def current_time(self) -> float:
        """Return PostgreSQL time so leases and row expiry share one clock."""
        active = self._active_connection.get()
        if active is not None and active[1] is asyncio.current_task():
            cur = await active[0].execute(self._sql_now)
            row = await cur.fetchone()
            return float(row[0])
        async with self._pool.connection() as conn:
            cur = await conn.execute(self._sql_now)
            row = await cur.fetchone()
            return float(row[0])

    @asynccontextmanager
    async def hold(self, scope_key: str, key: str) -> AsyncIterator[None]:
        """Hold a cross-process transaction advisory lock for this key.

        The same pooled connection remains checked out while the handler runs;
        nested ``get``/``put`` calls reuse it via a context-local binding.
        """
        lock_identity = json.dumps([scope_key, key], separators=(",", ":"))
        async with self._lock_pool.connection() as conn, conn.transaction():
            await conn.execute(self._sql_lock, (lock_identity,))
            token = self._active_connection.set((conn, asyncio.current_task()))
            try:
                yield
            finally:
                self._active_connection.reset(token)

    async def put_if_absent(self, scope_key: str, key: str, entry: CachedResponse) -> bool:
        """Atomically insert a webhook dedup marker, including stale replace."""
        expires_at_dt = datetime.fromtimestamp(entry.expires_at_epoch, tz=timezone.utc)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_put_if_absent,
                (
                    scope_key,
                    key,
                    entry.payload_hash,
                    json.dumps(entry.response),
                    expires_at_dt,
                ),
            )
            return await cur.fetchone() is not None

    async def delete_expired(self, now_epoch: float | None = None) -> int:
        """Best-effort sweep of expired entries. Returns rows removed."""
        async with self._pool.connection() as conn:
            if now_epoch is None:
                cur = await conn.execute(self._sql_delete_expired_now)
            else:
                cutoff_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
                cur = await conn.execute(self._sql_delete_expired, (cutoff_dt,))
            return cur.rowcount or 0


def _to_epoch(dt: Any) -> float:
    """Convert a psycopg-returned ``TIMESTAMPTZ`` to epoch seconds.

    psycopg returns ``datetime`` for ``TIMESTAMPTZ`` columns. A
    tz-naive datetime here means schema drift — adopters managing the
    schema with Alembic / dbmate may have created the column as
    ``TIMESTAMP WITHOUT TIME ZONE`` instead of ``TIMESTAMPTZ``. Per
    project fail-fast policy, raise rather than silently coerce —
    silent UTC defaults will produce wrong replay windows when the
    server's local time is not UTC.
    """
    if not isinstance(dt, datetime):
        return float(dt)
    if dt.tzinfo is None:
        raise ValueError(
            "PgBackend received a naive datetime from expires_at. "
            "This usually means the column was created as "
            "TIMESTAMP WITHOUT TIME ZONE instead of TIMESTAMPTZ — "
            "adopter Alembic migration drift from the SDK schema. "
            "Recreate the column as TIMESTAMPTZ (see "
            "PgBackend.create_schema for the canonical DDL)."
        )
    return float(dt.timestamp())
