"""Storage backends for :class:`~adcp.server.idempotency.IdempotencyStore`.

A backend owns two responsibilities:

1. Retrieve a cached response by ``(principal_id, idempotency_key)``, honoring
   the seller's replay TTL.
2. Atomically commit ``(payload_hash, response)`` on a fresh key. Atomicity
   with the handler's business writes is the backend's choice —
   :class:`MemoryBackend` makes no such guarantee; :class:`PgBackend` shares
   a connection pool so adopters with the same Postgres can compose their
   handler's transaction with the cache write (v1 commits in a separate
   pool connection — co-tx wiring is a v1.1 affordance).

Backends expose async methods. The in-process :class:`MemoryBackend` is
synchronous under the hood but wrapped in ``async`` signatures so the store
can remain backend-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
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
    :param response: The response dict the handler returned. Returned verbatim
        on replay — the seller injects ``replayed: true`` at the envelope
        level before sending.
    :param expires_at_epoch: Unix timestamp (seconds) when this entry becomes
        eligible for eviction. Reads after this time return None.
    """

    payload_hash: str
    response: dict[str, Any]
    expires_at_epoch: float


class IdempotencyBackend(ABC):
    """Abstract storage backend contract.

    All methods are async. Implementations MUST be safe to call concurrently
    from multiple asyncio tasks — :class:`IdempotencyStore` does not serialize
    access on the caller's behalf.
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
        backend = PgBackend(pool=pool)
        await backend.create_schema()  # idempotent; safe to call on every boot

        store = IdempotencyStore(backend=backend, ttl_seconds=86400)

    **Atomicity caveat (v1).** ``put`` commits on a fresh pool connection —
    the cache write is NOT in the same transaction as the handler's
    business writes. A crash between handler success and cache commit
    leaves the slot empty; the next retry re-executes the handler.
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
        table_name: str = DEFAULT_IDEMPOTENCY_TABLE,
    ) -> None:
        if not _PG_AVAILABLE:
            raise ImportError(_PG_INSTALL_HINT)
        self._pool = pool
        self._table = _safe_identifier(table_name)

        # Pre-format SQL once. Validated identifier so f-string interpolation
        # is byte-safe; values always go through %s parameterization. Same
        # convention as PgWebhookDeliverySupervisor / PgReplayStore.
        t = self._table
        self._sql_get = (
            f"SELECT payload_hash, response, expires_at "  # noqa: S608
            f"FROM {t} WHERE scope_key = %s AND key = %s AND expires_at > now()"
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
            f"WHERE {t}.expires_at <= now()"
        )
        self._sql_delete_expired = f"DELETE FROM {t} WHERE expires_at <= %s"  # noqa: S608

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
        sweeps them. ``get`` self-filters via ``expires_at > now()`` so a
        stale row never replays.
        """
        async with self._pool.connection() as conn:
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
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_put,
                (
                    scope_key,
                    key,
                    entry.payload_hash,
                    json.dumps(entry.response),
                    expires_at_dt,
                ),
            )

    async def delete_expired(self, now_epoch: float | None = None) -> int:
        """Best-effort sweep of expired entries. Returns rows removed."""
        cutoff = now_epoch if now_epoch is not None else time.time()
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        async with self._pool.connection() as conn:
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
