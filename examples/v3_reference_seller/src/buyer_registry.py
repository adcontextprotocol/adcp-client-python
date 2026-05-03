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
    BuyerAgent,
    BuyerAgentDefaultTerms,
    BuyerAgentRegistry,
    OAuthCredential,
)
from adcp.server import current_tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

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
                select(BuyerAgentRow).where(
                    BuyerAgentRow.tenant_id == tenant.id,
                    BuyerAgentRow.api_key_id == key,
                )
            )
            row = result.scalar_one_or_none()
        return _row_to_agent(row) if row else None


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


def make_registry(sessionmaker: async_sessionmaker) -> BuyerAgentRegistry:
    """Factory for the tenant-scoped registry. Returns a
    Protocol-typed handle — adopters wire it into
    :func:`adcp.decisioning.serve` via ``buyer_agent_registry=``."""
    return TenantScopedBuyerAgentRegistry(sessionmaker=sessionmaker)


__all__ = ["TenantScopedBuyerAgentRegistry", "make_registry"]
