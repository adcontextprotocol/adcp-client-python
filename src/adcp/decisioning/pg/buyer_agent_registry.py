"""PostgreSQL-backed :class:`~adcp.decisioning.BuyerAgentRegistry`.

Durable commercial-identity storage for AdCP v3 sellers — the Tier 2
counterparty allowlist + onboarding state + billing capabilities. The
framework calls the registry on every request to gate dispatch on
the seller's commercial relationship with the agent (recognized?
suspended? billable agent-direct?) BEFORE
:meth:`AccountStore.resolve` runs.

Mirrors the design of :class:`adcp.signing.pg.PgReplayStore`: caller
supplies a :class:`psycopg_pool.ConnectionPool`, the framework runs
short-lived statements per call (no long-lived transactions, no
cross-operation state), and a separate ``.sql`` file ships the DDL
for adopters using a migration tool (Alembic, Flyway, psql).

End-to-end example
------------------

::

    from psycopg_pool import ConnectionPool
    from adcp.decisioning import serve, signing_only_registry
    from adcp.decisioning.pg import PgBuyerAgentRegistry

    pool = ConnectionPool("postgresql://...", min_size=4, max_size=20)
    registry = PgBuyerAgentRegistry(pool=pool)
    registry.create_schema()  # idempotent; safe on every boot

    # Seed the allowlist — typically driven by an admin UI / API.
    registry.upsert(
        BuyerAgent(
            agent_url="https://agent.example/",
            display_name="Acme",
            status="active",
        )
    )

    serve(
        platform=MySalesPlatform(),
        buyer_agent_registry=registry,
        ...,
    )

Async-from-sync bridging
------------------------

The :class:`BuyerAgentRegistry` Protocol is async (called from inside
the framework's dispatch event loop), but psycopg-pool's
:class:`~psycopg_pool.ConnectionPool` is sync. Each ``resolve_*``
method wraps its sync DB call with :func:`asyncio.to_thread` so the
event loop stays responsive — at the cost of a thread-pool hop per
request.

Adopters needing higher throughput swap to a custom Protocol impl
backed by :class:`psycopg_pool.AsyncConnectionPool`. The framework
keeps the simpler sync-pool shape as the bundled default; it matches
:class:`PgReplayStore` and lets adopters share a single sync pool
across replay-store, registry, and (future) audit-sink.

Concurrency
-----------

Safe to share across threads and processes. PostgreSQL provides the
cross-instance locking via PK conflict resolution on
``INSERT ... ON CONFLICT``.

Failure mode
------------

Transport / connection errors propagate from psycopg unchanged
(:class:`OperationalError`, :class:`PoolTimeout`, etc.). The
framework's dispatch layer treats unexpected exceptions as
``INTERNAL_ERROR`` so the wire response stays opaque to the buyer
while the original exception lands in server logs via the
observability hooks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentStatus,
    Credential,
    OAuthCredential,
)

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from adcp.audit_sink import AuditSink
    from adcp.decisioning.registry_cache import CachingBuyerAgentRegistry

logger = logging.getLogger(__name__)

MutationObserver = Callable[[str, str], None]
"""Callback fired after a successful :class:`PgBuyerAgentRegistry`
mutation. Receives ``(operation, agent_url)`` where ``operation`` is
``"upsert"`` / ``"set_status"`` / ``"delete"``. Runs synchronously on
the caller's thread after the DB commit; raised exceptions are logged
but do not propagate to the mutation caller."""

try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False


DEFAULT_TABLE_NAME = "adcp_buyer_agents"

# Byte-level ASCII identifier match — same posture as PgReplayStore.
# Non-ASCII Unicode letters would format verbatim into SQL as a
# DIFFERENT table, which under attacker-influenced configuration is
# a silent table-substitution vector. ASCII-byte-exact only.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_INSTALL_HINT = (
    "PgBuyerAgentRegistry requires psycopg3 and psycopg-pool. Install "
    "the 'pg' extra: `pip install 'adcp[pg]'` (Poetry: "
    "`poetry add 'adcp[pg]'`)."
)

_VALID_STATUSES = frozenset({"active", "suspended", "blocked"})


class PgBuyerAgentRegistry:
    """PostgreSQL-backed :class:`~adcp.decisioning.BuyerAgentRegistry`.

    Parameters
    ----------
    pool:
        A :class:`psycopg_pool.ConnectionPool` owned by the caller.
        Each operation acquires a short-lived connection, runs a
        single statement, and returns the connection.
    table_name:
        Override the default ``adcp_buyer_agents`` table when two
        tenants share a database and need separate registries. Must
        be an ASCII-byte-clean identifier — the constructor validates.

    Concurrency
    -----------

    Safe to share across threads and processes. The
    :meth:`resolve_by_agent_url` / :meth:`resolve_by_credential`
    methods bridge the async Protocol to the sync pool via
    :func:`asyncio.to_thread`; concurrent dispatches each get their
    own thread + connection.
    """

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        table_name: str = DEFAULT_TABLE_NAME,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if not _is_safe_identifier(table_name):
            raise ValueError(
                f"table_name must match [a-z_][a-z0-9_]* (ASCII only), got {table_name!r}"
            )
        self._pool = pool
        self._table = table_name
        self._mutation_observers: list[MutationObserver] = []
        self._mutation_observers_lock = threading.Lock()

        # Pre-format queries so the hot path doesn't f-string per call.
        # All identifier substitutions are validated at __init__; row
        # values flow through psycopg's parameter binding.
        cols = (
            "agent_url, display_name, status, billing_capabilities, "
            "api_key_id, default_terms, allowed_brands, ext"
        )
        self._sql_select_by_agent_url = (
            f"SELECT {cols} FROM {self._table} "  # noqa: S608 — table name validated
            f"WHERE agent_url = %s"
        )
        self._sql_select_by_api_key_id = (
            f"SELECT {cols} FROM {self._table} "  # noqa: S608
            f"WHERE api_key_id = %s LIMIT 2"
        )
        self._sql_upsert = (
            f"INSERT INTO {self._table} ("  # noqa: S608
            f"  agent_url, display_name, status, billing_capabilities, "
            f"  api_key_id, default_terms, allowed_brands, ext, updated_at"
            f") VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, "
            f"          %s::jsonb, now()) "
            f"ON CONFLICT (agent_url) DO UPDATE SET "
            f"  display_name = EXCLUDED.display_name, "
            f"  status = EXCLUDED.status, "
            f"  billing_capabilities = EXCLUDED.billing_capabilities, "
            f"  api_key_id = EXCLUDED.api_key_id, "
            f"  default_terms = EXCLUDED.default_terms, "
            f"  allowed_brands = EXCLUDED.allowed_brands, "
            f"  ext = EXCLUDED.ext, "
            f"  updated_at = now()"
        )
        self._sql_set_status = (
            f"UPDATE {self._table} "  # noqa: S608
            f"SET status = %s, updated_at = now() "
            f"WHERE agent_url = %s"
        )
        self._sql_delete = f"DELETE FROM {self._table} WHERE agent_url = %s"  # noqa: S608

    # ----- schema bootstrap ---------------------------------------------

    def create_schema(self) -> None:
        """Create the registry table + indexes for this store's
        ``table_name``. Idempotent once credential identifiers are unique.

        Existing deployments with duplicated ``api_key_id`` values fail
        before indexes change, with an actionable rotation/removal message.
        The legacy non-unique credential index is dropped only after the
        replacement unique index exists.

        The equivalent raw DDL ships at
        :file:`src/adcp/decisioning/pg/buyer_agent_registry.sql` for
        adopters using a migration tool (Alembic, Flyway, psql) —
        that file uses the canonical ``adcp_buyer_agents`` name.
        """
        table = self._table  # already validated at __init__
        table_ddl = (
            f"CREATE TABLE IF NOT EXISTS {table} ("  # noqa: S608 — validated
            f'    agent_url             TEXT        COLLATE "C" PRIMARY KEY,'
            f"    display_name          TEXT        NOT NULL,"
            f"    status                TEXT        NOT NULL DEFAULT 'active'"
            f"        CHECK (status IN ('active', 'suspended', 'blocked')),"
            f"    billing_capabilities  JSONB       NOT NULL DEFAULT '[\"operator\"]'::jsonb,"
            f'    api_key_id            TEXT        COLLATE "C",'
            f"    default_terms         JSONB,"
            f"    allowed_brands        JSONB,"
            f"    ext                   JSONB       NOT NULL DEFAULT '{{}}'::jsonb,"
            f"    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),"
            f"    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()"
            f");"
        )
        duplicate_preflight = (  # noqa: S608 — validated table name
            f"SELECT api_key_id, COUNT(*) FROM {table} "
            f"WHERE api_key_id IS NOT NULL GROUP BY api_key_id "
            f"HAVING COUNT(*) > 1 LIMIT 1"
        )
        unique_index_ddl = (  # noqa: S608
            f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_api_key_id_uidx "
            f"ON {table} (api_key_id) WHERE api_key_id IS NOT NULL"
        )
        drop_legacy_index_ddl = f"DROP INDEX IF EXISTS {table}_api_key_id_idx"  # noqa: S608
        status_index_ddl = (  # noqa: S608
            f"CREATE INDEX IF NOT EXISTS {table}_status_idx "
            f"ON {table} (status) WHERE status <> 'active'"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(table_ddl)
            cur.execute(duplicate_preflight)
            duplicate = cur.fetchall()
            if duplicate:
                raise RuntimeError(
                    f"Cannot enforce {table}.api_key_id uniqueness: duplicated credential "
                    "identifiers exist. Rotate or remove duplicate bearer credentials, then "
                    "rerun create_schema()."
                )
            cur.execute(unique_index_ddl)
            cur.execute(drop_legacy_index_ddl)
            cur.execute(status_index_ddl)

    # ----- BuyerAgentRegistry Protocol --------------------------------

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        """Resolve a verified ``agent_url`` against the allowlist.

        The framework has already validated the RFC 9421 signature
        before this point — the registry's only job is the commercial
        lookup. Returns ``None`` when the agent isn't recognized;
        the framework converts that to ``PERMISSION_DENIED`` (with
        ``details`` omitted so the unrecognized-agent path is
        wire-indistinguishable from recognized-but-denied).
        """
        return await asyncio.to_thread(self._sync_lookup_by_agent_url, agent_url)

    async def resolve_by_credential(
        self,
        credential: Credential,
    ) -> BuyerAgent | None:
        """Resolve a bearer / API-key / OAuth credential.

        Looks up against the ``api_key_id`` column. For
        :class:`OAuthCredential`, the ``client_id`` is used as the
        lookup key — adopters with separate OAuth-client tables fork
        this registry impl and split the column. The MVP shape
        treats both bearer and OAuth as the same column for the
        common case (one identifier per agent).
        """
        if isinstance(credential, ApiKeyCredential):
            key = credential.key_id
        elif isinstance(credential, OAuthCredential):
            key = credential.client_id
        else:  # defensive: future Credential variants the registry can't dispatch
            return None
        return await asyncio.to_thread(self._sync_lookup_by_api_key_id, key)

    # ----- admin CRUD --------------------------------------------------

    def upsert(self, agent: BuyerAgent, *, api_key_id: str | None = None) -> None:
        """Insert or update a :class:`BuyerAgent` row.

        ``api_key_id`` is separate from the :class:`BuyerAgent` shape
        because the framework's typed model doesn't carry the
        bearer-table FK. Adopters running bearer auth populate this;
        signing-only adopters leave it ``None``.
        """
        if agent.status not in _VALID_STATUSES:
            raise ValueError(
                f"BuyerAgent.status must be one of {sorted(_VALID_STATUSES)!r}, "
                f"got {agent.status!r}"
            )
        terms_json = (
            json.dumps(_terms_to_dict(agent.default_account_terms))
            if agent.default_account_terms is not None
            else None
        )
        allowed_brands_json = (
            json.dumps(sorted(agent.allowed_brands)) if agent.allowed_brands is not None else None
        )
        params = (
            agent.agent_url,
            agent.display_name,
            agent.status,
            json.dumps(sorted(agent.billing_capabilities)),
            api_key_id,
            terms_json,
            allowed_brands_json,
            json.dumps(dict(agent.ext)),
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_upsert, params)
        self._notify_mutation("upsert", agent.agent_url)

    def set_status(self, agent_url: str, status: BuyerAgentStatus) -> None:
        """Update an agent's lifecycle status. Use to suspend / block
        / reactivate without rewriting the full row."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)!r}, got {status!r}")
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_set_status, (status, agent_url))
        self._notify_mutation("set_status", agent_url)

    def delete(self, agent_url: str) -> None:
        """Remove an agent from the registry.

        Hard delete — no row history. Adopters needing audit retention
        keep the row and set ``status='blocked'`` instead.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_delete, (agent_url,))
        self._notify_mutation("delete", agent_url)

    # ----- mutation observability -------------------------------------

    def add_mutation_observer(self, observer: MutationObserver) -> None:
        """Register a callback fired after every successful mutation.

        Observers receive ``(operation, agent_url)`` where
        ``operation`` is one of ``"upsert"`` / ``"set_status"`` /
        ``"delete"``. They run synchronously on the calling thread
        AFTER the DB commit, so a failed commit does not invoke
        observers. Exceptions raised by an observer are logged and
        swallowed — they never block the mutation from succeeding or
        prevent later observers from running.

        Typical use is wiring a cache-invalidation hook so admin
        mutations propagate to read-side caches without manual
        :meth:`CachingBuyerAgentRegistry.invalidate` calls. See
        :meth:`with_caching` for the bundled pre-wired path.

        Observer registration is thread-safe. Mutations notify a
        snapshot of the current observer list; observers added or
        removed while a notification is in flight apply to the next
        mutation.
        """
        with self._mutation_observers_lock:
            self._mutation_observers.append(observer)

    def remove_mutation_observer(self, observer: MutationObserver) -> bool:
        """Unregister a mutation observer.

        Returns ``True`` when ``observer`` was registered and removed,
        ``False`` when it was not present. If the same callback was
        registered multiple times, one registration is removed per call.

        Removal is thread-safe. Mutations notify a snapshot of the
        observer list, so removing an observer while a notification is
        already in flight only affects subsequent mutations.
        """
        with self._mutation_observers_lock:
            try:
                self._mutation_observers.remove(observer)
            except ValueError:
                return False
        return True

    def with_caching(
        self,
        **cache_kwargs: Any,
    ) -> CachingBuyerAgentRegistry:
        """Return a :class:`CachingBuyerAgentRegistry` wrapping this
        registry, pre-wired so mutations through this instance
        automatically invalidate the cache.

        Forwards ``**cache_kwargs`` to
        :class:`CachingBuyerAgentRegistry` (``ttl_seconds``,
        ``max_entries``, ``hit_callback``, ``audit_sink``,
        ``sink_timeout_seconds``, ``time_source``).

        Example::

            pg = PgBuyerAgentRegistry(pool=pool)
            registry = pg.with_caching(ttl_seconds=60, audit_sink=sink)
            serve(buyer_agent_registry=registry, ...)

            # Admin mutations go through `pg` and invalidate the cache:
            pg.upsert(BuyerAgent(agent_url=..., status="suspended"))
            # Next resolve() through `registry` hits DB, sees suspended.

        Adopters with external admin paths (a separate process
        writing to the same DB) still need :meth:`CachingBuyerAgentRegistry.invalidate`
        or :meth:`clear_sync` — the observer hook fires on
        mutations through *this* :class:`PgBuyerAgentRegistry`
        instance only.
        """
        from adcp.decisioning.registry_cache import CachingBuyerAgentRegistry

        cache = CachingBuyerAgentRegistry(self, **cache_kwargs)
        self.add_mutation_observer(lambda _op, _agent_url: cache.clear_sync())
        return cache

    def with_full_stack(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 4096,
        hit_callback: Callable[[str], None] | None = None,
        rps_per_tenant: float = 100.0,
        burst: float | None = None,
        audit_sink: AuditSink | None = None,
        sink_timeout_seconds: float = 5.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> CachingBuyerAgentRegistry:
        """Return the canonical production registry wrapper stack.

        Builds and returns ``Caching(RateLimited(Auditing(self)))``:

        * cache is outermost so cached hits skip rate-limit accounting
          and DB work;
        * rate limiting applies only to cache misses that need inner
          resolution;
        * auditing wraps the SQL-backed store so DB ``resolved`` /
          ``miss`` outcomes are recorded.

        ``audit_sink`` and ``sink_timeout_seconds`` are threaded through
        all three layers, so cache hits/misses, rate-limit rejects, and
        terminal DB outcomes can all land in the same audit trail.
        ``time_source`` is shared by the cache and rate limiter for
        deterministic tests.

        Mutations through this :class:`PgBuyerAgentRegistry` instance
        clear the returned cache via the same observer wiring as
        :meth:`with_caching`. Adopters needing a different layer order
        should compose :class:`CachingBuyerAgentRegistry`,
        :class:`RateLimitedBuyerAgentRegistry`, and
        :class:`AuditingBuyerAgentRegistry` manually.
        """
        from adcp.decisioning.registry_cache import (
            AuditingBuyerAgentRegistry,
            CachingBuyerAgentRegistry,
            RateLimitedBuyerAgentRegistry,
        )

        audited = AuditingBuyerAgentRegistry(
            self,
            audit_sink=audit_sink,
            sink_timeout_seconds=sink_timeout_seconds,
        )
        rate_limited = RateLimitedBuyerAgentRegistry(
            audited,
            rps_per_tenant=rps_per_tenant,
            burst=burst,
            audit_sink=audit_sink,
            sink_timeout_seconds=sink_timeout_seconds,
            time_source=time_source,
        )
        cache = CachingBuyerAgentRegistry(
            rate_limited,
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            hit_callback=hit_callback,
            audit_sink=audit_sink,
            sink_timeout_seconds=sink_timeout_seconds,
            time_source=time_source,
        )
        self.add_mutation_observer(lambda _op, _agent_url: cache.clear_sync())
        return cache

    def _notify_mutation(self, op: str, agent_url: str) -> None:
        """Fire registered observers; log and swallow exceptions."""
        with self._mutation_observers_lock:
            observers = tuple(self._mutation_observers)
        for observer in observers:
            try:
                observer(op, agent_url)
            except Exception:  # noqa: BLE001 — observers must not break mutations
                logger.warning(
                    "[adcp.buyer_agent_registry] mutation observer raised for op=%s agent_url=%s",
                    op,
                    agent_url,
                    exc_info=True,
                )

    # ----- sync helpers (called via asyncio.to_thread) ----------------

    def _sync_lookup_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_select_by_agent_url, (agent_url,))
            row = cur.fetchone()
            return _row_to_agent(row) if row else None

    def _sync_lookup_by_api_key_id(self, key: str) -> BuyerAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_select_by_api_key_id, (key,))
            rows = cur.fetchall()
            if len(rows) > 1:
                # Defense in depth for deployments that have not yet applied
                # the unique-index migration. Never select an arbitrary
                # commercial identity for an ambiguous credential.
                logger.error(
                    "PgBuyerAgentRegistry rejected an ambiguous credential mapping; "
                    "apply the unique api_key_id index migration"
                )
                return None
            return _row_to_agent(rows[0]) if rows else None


def _row_to_agent(row: Any) -> BuyerAgent:
    """Project a DB row to the typed :class:`BuyerAgent`.

    Row tuple shape mirrors the SELECT column order in
    :class:`PgBuyerAgentRegistry`: ``(agent_url, display_name, status,
    billing_capabilities, api_key_id, default_terms, allowed_brands,
    ext)``. ``api_key_id`` is read but not surfaced on
    :class:`BuyerAgent` — the framework's typed model doesn't carry
    the bearer-table FK; adopters reading it use admin queries.
    """
    (
        agent_url,
        display_name,
        status,
        billing_capabilities,
        _api_key_id,
        default_terms,
        allowed_brands,
        ext,
    ) = row

    # billing_capabilities arrives as a Python list (psycopg auto-decodes
    # JSONB). Defensive parse if a string slips through.
    if isinstance(billing_capabilities, str):
        billing_capabilities = json.loads(billing_capabilities)

    terms: BuyerAgentDefaultTerms | None = None
    if default_terms is not None:
        terms_dict = json.loads(default_terms) if isinstance(default_terms, str) else default_terms
        terms = BuyerAgentDefaultTerms(
            rate_card=terms_dict.get("rate_card"),
            payment_terms=terms_dict.get("payment_terms"),
            credit_limit=terms_dict.get("credit_limit"),
            billing_entity=terms_dict.get("billing_entity"),
        )

    brands: frozenset[str] | None = None
    if allowed_brands is not None:
        brands_list = (
            json.loads(allowed_brands) if isinstance(allowed_brands, str) else allowed_brands
        )
        brands = frozenset(brands_list)

    if isinstance(ext, str):
        ext = json.loads(ext)

    return BuyerAgent(
        agent_url=agent_url,
        display_name=display_name,
        status=status,
        billing_capabilities=frozenset(billing_capabilities),
        default_account_terms=terms,
        allowed_brands=brands,
        ext=ext or {},
    )


def _terms_to_dict(terms: BuyerAgentDefaultTerms) -> dict[str, Any]:
    """Project :class:`BuyerAgentDefaultTerms` to a JSONB-friendly dict."""
    out: dict[str, Any] = {}
    if terms.rate_card is not None:
        out["rate_card"] = terms.rate_card
    if terms.payment_terms is not None:
        out["payment_terms"] = terms.payment_terms
    if terms.credit_limit is not None:
        out["credit_limit"] = dict(terms.credit_limit)
    if terms.billing_entity is not None:
        out["billing_entity"] = dict(terms.billing_entity)
    return out


def _is_safe_identifier(name: str) -> bool:
    """Allow only byte-ASCII lowercase identifiers for the table-name
    kwarg. Same posture as :class:`PgReplayStore` — the table name
    is static-formatted into SQL at construction; this validator is
    the sole guard against injection or homoglyph table substitution.
    """
    return _SAFE_IDENTIFIER_RE.fullmatch(name) is not None


__all__ = [
    "DEFAULT_TABLE_NAME",
    "PG_AVAILABLE",
    "PgBuyerAgentRegistry",
]
