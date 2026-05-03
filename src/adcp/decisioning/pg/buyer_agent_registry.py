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
import re
from typing import TYPE_CHECKING, Any

from adcp.decisioning.registry import (
    ApiKeyCredential,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentStatus,
    OAuthCredential,
)

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

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
                "table_name must match [a-z_][a-z0-9_]* (ASCII only), " f"got {table_name!r}"
            )
        self._pool = pool
        self._table = table_name

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
            f"WHERE api_key_id = %s"
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
        ``table_name``. Idempotent via ``CREATE ... IF NOT EXISTS``;
        safe to call on every app boot.

        The equivalent raw DDL ships at
        :file:`src/adcp/decisioning/pg/buyer_agent_registry.sql` for
        adopters using a migration tool (Alembic, Flyway, psql) —
        that file uses the canonical ``adcp_buyer_agents`` name.
        """
        table = self._table  # already validated at __init__
        ddl = (
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
            f"CREATE INDEX IF NOT EXISTS {table}_api_key_id_idx "  # noqa: S608
            f"    ON {table} (api_key_id) WHERE api_key_id IS NOT NULL;"
            f"CREATE INDEX IF NOT EXISTS {table}_status_idx "  # noqa: S608
            f"    ON {table} (status) WHERE status <> 'active';"
        )
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(ddl)

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
        credential: ApiKeyCredential | OAuthCredential,
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

    def set_status(self, agent_url: str, status: BuyerAgentStatus) -> None:
        """Update an agent's lifecycle status. Use to suspend / block
        / reactivate without rewriting the full row."""
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)!r}, got {status!r}")
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_set_status, (status, agent_url))

    def delete(self, agent_url: str) -> None:
        """Remove an agent from the registry.

        Hard delete — no row history. Adopters needing audit retention
        keep the row and set ``status='blocked'`` instead.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_delete, (agent_url,))

    # ----- sync helpers (called via asyncio.to_thread) ----------------

    def _sync_lookup_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_select_by_agent_url, (agent_url,))
            row = cur.fetchone()
            return _row_to_agent(row) if row else None

    def _sync_lookup_by_api_key_id(self, key: str) -> BuyerAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(self._sql_select_by_api_key_id, (key,))
            row = cur.fetchone()
            return _row_to_agent(row) if row else None


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
