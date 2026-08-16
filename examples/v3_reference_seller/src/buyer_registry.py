"""Tenant-scoped :class:`adcp.decisioning.BuyerAgentRegistry`.

The framework's :class:`adcp.decisioning.PgBuyerAgentRegistry` looks
up buyer agents by ``agent_url`` only — a single global table. For
multi-tenant deployments where the same ``agent_url`` may have
different commercial postures across tenants (active under one,
suspended under another), this adopter wraps the
``buyer_agents`` SQL table and scopes resolution by the current
tenant from :func:`adcp.server.current_tenant`.

The registry's ``resolve_*`` methods read the tenant-scoped row
without explicit kwarg threading — the contextvar set by the
:class:`adcp.server.SubdomainTenantMiddleware` propagates into the
async dispatch the framework calls.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import select

from adcp.decisioning import (
    ApiKeyCredential,
    AuditingBuyerAgentRegistry,
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentRegistry,
    CachingBuyerAgentRegistry,
    OAuthCredential,
    RateLimitedBuyerAgentRegistry,
)
from adcp.server import current_tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from adcp.audit_sink import AuditSink

from .models import BuyerAgent as BuyerAgentRow

logger = logging.getLogger(__name__)


class TenantScopedBuyerAgentRegistry:
    """SQLAlchemy + tenant-scoped lookups against the
    ``buyer_agents`` table.

    Resolves to ``None`` when the request has no tenant context
    (i.e., :func:`current_tenant` returns ``None`` because the
    middleware bypassed routing or the host wasn't registered) — the
    framework's dispatch then rejects with ``PERMISSION_DENIED``
    (with ``details`` omitted so the unrecognized-agent path is
    wire-indistinguishable from a recognized-but-denied response).

    Implements both methods of the
    :class:`adcp.decisioning.BuyerAgentRegistry` Protocol so the
    seller can run the full mixed (signing + bearer) posture against
    the same backing table.
    """

    def __init__(self, *, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        """Verified-signed-request path.

        The framework has already validated the RFC 9421 signature;
        this method's only job is the commercial lookup.
        """
        tenant = current_tenant()
        if tenant is None:
            logger.debug(
                "resolve_by_agent_url called without a tenant context (agent_url=%s)",
                agent_url,
            )
            return None
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.agent_url == agent_url,
                )
            )
            row = result.scalar_one_or_none()
        return _row_to_agent(row) if row else None

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        """Pre-trust beta path — bearer / OAuth lookup against the
        seller's existing key column."""
        tenant = current_tenant()
        if tenant is None:
            return None
        if isinstance(credential, ApiKeyCredential):
            key = credential.key_id
        else:
            key = credential.client_id
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(BuyerAgentRow)
                .where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.api_key_id == key,
                )
                .limit(2)
            )
            rows = list(result.scalars().all())
        if len(rows) > 1:
            logger.error(
                "ambiguous buyer credential within tenant; denying lookup "
                "(tenant_id=%s, credential_kind=%s)",
                tenant.id,
                credential.kind,
            )
            return None
        return _row_to_agent(rows[0]) if rows else None


def _row_to_agent(row: BuyerAgentRow) -> BuyerAgent:
    """Project ORM row → framework typed :class:`BuyerAgent`."""
    capabilities = frozenset(row.billing_capabilities or ())
    terms: BuyerAgentDefaultTerms | None = None
    if row.default_terms:
        d = row.default_terms
        terms = BuyerAgentDefaultTerms(
            rate_card=d.get("rate_card"),
            payment_terms=d.get("payment_terms"),
            credit_limit=d.get("credit_limit"),
            billing_entity=d.get("billing_entity"),
        )
    brands: frozenset[str] | None = None
    if row.allowed_brands:
        brands = frozenset(row.allowed_brands)
    return BuyerAgent(
        agent_url=row.agent_url,
        display_name=row.display_name,
        status=row.status,  # type: ignore[arg-type]
        billing_capabilities=capabilities,
        default_account_terms=terms,
        allowed_brands=brands,
        ext=dict(row.ext or {}),
    )


def make_registry(
    sessionmaker: async_sessionmaker,
    *,
    audit_sink: AuditSink | None = None,
    ttl_seconds: float = 60.0,
    rps_per_tenant: float = 100.0,
    max_entries: int = 4096,
) -> BuyerAgentRegistry:
    """Factory for the tenant-scoped registry. Returns a
    Protocol-typed handle — adopters wire it into
    :func:`adcp.decisioning.serve` via ``buyer_agent_registry=``.

    Composes three wrappers around the SQL-backed registry so the
    Tier 2 commercial-identity gate has production-grade properties:

    * **Cache** (outermost) — TTL + LRU. Both positive and negative
      resolutions cached so an enumeration probe at the lookup
      endpoint hits the DB at most once per ``(tenant, key)`` per
      ``ttl_seconds`` window.
    * **Rate limit** (middle) — token bucket per
      ``(tenant, lookup_key)``. On exhaustion raises
      ``PERMISSION_DENIED`` with no ``details`` so the wire shape
      matches the registry-miss path (no enumeration oracle).
    * **Audit** (innermost) — emits one
      :class:`~adcp.audit_sink.AuditEvent` per DB outcome
      (``resolved`` / ``miss``); the cache and rate-limit layers
      add ``cached_hit`` / ``cached_miss`` / ``rate_limited``
      events to the same sink so compliance teams reconstruct
      every resolve attempt.

    Adopters needing different defaults pass ``ttl_seconds`` /
    ``rps_per_tenant`` / ``max_entries`` overrides.
    """
    sql_backed = TenantScopedBuyerAgentRegistry(sessionmaker=sessionmaker)
    audited = AuditingBuyerAgentRegistry(sql_backed, audit_sink=audit_sink)
    rate_limited = RateLimitedBuyerAgentRegistry(
        audited,
        rps_per_tenant=rps_per_tenant,
        audit_sink=audit_sink,
    )
    return CachingBuyerAgentRegistry(
        rate_limited,
        ttl_seconds=ttl_seconds,
        max_entries=max_entries,
        audit_sink=audit_sink,
    )


__all__ = ["TenantScopedBuyerAgentRegistry", "make_registry"]
