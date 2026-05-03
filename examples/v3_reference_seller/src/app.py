"""Main entrypoint — wires every Tier 2 / v3-supporting component
into one runnable adopter, in the **translator pattern**: AdCP wire on
the inside, the JS mock-server (``@adcp/client adcp mock-server
sales-guaranteed``) over HTTP on the outside.

Boot sequence:

1. Connect SQLAlchemy async engine + sessionmaker.
2. Create schema (idempotent ``Base.metadata.create_all``).
3. Wire the upstream HTTP client. The platform calls
   :meth:`adcp.decisioning.DecisioningPlatform.upstream_for` per
   request, which builds a pooled :class:`UpstreamHttpClient` from the
   resolved account's ``mode`` (``mock`` here) +
   ``metadata['mock_upstream_url']`` (sourced from
   ``MOCK_AD_SERVER_URL``).
4. Build the framework wiring:

   * :class:`SqlSubdomainTenantRouter` for ``Host`` → tenant
   * :class:`TenantScopedBuyerAgentRegistry` for the Tier 2 gate
   * :class:`DbAuditSink` for compliance trail
   * :class:`V3ReferenceSeller` (the platform impl)

5. ``adcp.decisioning.serve(transport="both", asgi_middleware=[...])``
   — single binary serving MCP at ``/mcp`` and A2A at ``/`` with
   :class:`SubdomainTenantMiddleware` layered on the outer HTTP app.

Adopters fork this file and replace the per-specialism mock-server
boot with their own ad-server URL: declare ``upstream_url`` on the
:class:`V3ReferenceSeller` subclass and have ``AccountStore.resolve``
return ``mode='live'`` (or ``'sandbox'``) accounts. Everything else
stays — the framework's ``upstream_for`` routes the same adapter
code path against either URL.

::

    # Boot the upstream first
    npx -y -p @adcp/client@latest \\
        adcp mock-server sales-guaranteed --port 4503 --api-key test-key &

    # Then boot the seller
    cd examples/v3_reference_seller
    docker compose up -d postgres
    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \\
      MOCK_AD_SERVER_URL=http://127.0.0.1:4503 \\
      MOCK_AD_SERVER_API_KEY=test-key \\
      python -m src.app
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adcp.decisioning import InMemoryMockAdServer, serve
from adcp.server import (
    SubdomainTenantMiddleware,
    ToolContext,
    current_tenant,
)
from adcp.validation import ValidationHookConfig

from .audit import make_sink as make_audit_sink
from .buyer_registry import make_registry as make_buyer_registry
from .models import Base
from .platform import V3ReferenceSeller
from .tenant_router import SqlSubdomainTenantRouter

if TYPE_CHECKING:
    from adcp.server import RequestMetadata

logger = logging.getLogger(__name__)


def _build_context_factory():
    """``context_factory`` that pins :attr:`ToolContext.tenant_id`
    from the resolved tenant.
    """

    def build(meta: RequestMetadata) -> ToolContext:
        tenant = current_tenant()
        return ToolContext(
            request_id=meta.request_id,
            tenant_id=tenant.id if tenant else None,
        )

    return build


async def _bootstrap_schema(engine) -> None:
    """Create all tables. Idempotent (CREATE TABLE IF NOT EXISTS).

    Production adopters use Alembic — this entrypoint sticks with
    ``create_all`` for fast iteration.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # asyncpg binds connection-internal Future objects to the loop
    # they were opened on. Bootstrapping via ``asyncio.run`` runs on
    # a transient loop that closes when ``asyncio.run`` returns; if
    # those connections stay in the pool, uvicorn's own loop trips
    # ``RuntimeError: got Future attached to a different loop`` on
    # the first request. Dispose so uvicorn opens a fresh pool on
    # its own loop.
    await engine.dispose()


def main() -> None:
    """Entrypoint — boot the seller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres@localhost/adcp",
    )
    port = int(os.environ.get("PORT", "3001"))
    upstream_url = os.environ.get("MOCK_AD_SERVER_URL", "http://127.0.0.1:4503")
    upstream_api_key = os.environ.get(
        "MOCK_AD_SERVER_API_KEY",
        "mock_sales_guaranteed_key_do_not_use_in_prod",
    )

    engine = create_async_engine(db_url, pool_size=10, max_overflow=20)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asyncio.run(_bootstrap_schema(engine))

    router = SqlSubdomainTenantRouter(sessionmaker=sessionmaker)
    audit_sink = make_audit_sink(sessionmaker)
    # The buyer registry composes cache + rate-limit + audit around
    # the SQL-backed lookup. Wiring the same audit_sink at every
    # layer means cached_hit / cached_miss / rate_limited / resolved
    # / miss outcomes ALL land in the audit trail; SecOps can
    # reconstruct every resolve attempt.
    buyer_registry = make_buyer_registry(sessionmaker, audit_sink=audit_sink)
    # Anti-façade traffic recorder. The reference seller is a dev /
    # storyboard target, so we wire the in-memory recorder and flip
    # ``enable_debug_endpoints=True`` below to expose
    # ``GET /_debug/traffic``. Production adopters omit both kwargs;
    # the endpoint stays closed and the recorder is a no-op.
    mock_ad_server = InMemoryMockAdServer()
    # The reference seller is mock-mode by design: every Account its
    # ``AccountStore.resolve`` returns is ``mode='mock'`` and carries
    # ``MOCK_AD_SERVER_URL`` in ``account.metadata['mock_upstream_url']``.
    # The framework's ``upstream_for(ctx)`` reads that URL to point
    # the pooled :class:`UpstreamHttpClient` at the JS mock-server.
    # Adopters with a real production upstream replace ``mode='mock'``
    # with ``mode='live'`` in their ``AccountStore.resolve`` and declare
    # :attr:`V3ReferenceSeller.upstream_url` to their production URL.
    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key=upstream_api_key,
        mock_upstream_url=upstream_url,
        mock_ad_server=mock_ad_server,
    )

    logger.info(
        "v3 reference seller booting on port=%d (transport=both, MCP at /mcp, A2A at /)",
        port,
    )
    logger.info("Mock-mode upstream: %s (api_key=%s...)", upstream_url, upstream_api_key[:4])
    logger.info("Audit sink wired: %s. Tenant router cache: 256 hosts.", type(audit_sink).__name__)

    serve(
        platform=platform,
        name="v3-reference-seller",
        port=port,
        host="0.0.0.0",
        transport="both",
        buyer_agent_registry=buyer_registry,
        context_factory=_build_context_factory(),
        asgi_middleware=[
            (SubdomainTenantMiddleware, {"router": router}),
        ],
        # Schema-driven validation in strict mode on both sides.
        # This is the framework default since DX#8 (strict by default
        # to catch ``pricing_options``-class bugs that ``extra="allow"``
        # Pydantic models silently swallow), but pinned explicitly here
        # so the reference seller's posture is self-evident from the
        # serve call. Adopters forking this entrypoint can drop to
        # ``responses="warn"`` if they have a deliberate reason to
        # ship spec-divergent responses; they cannot escape detection
        # by simply omitting the kwarg.
        validation=ValidationHookConfig(requests="strict", responses="strict"),
        mock_ad_server=mock_ad_server,
        enable_debug_endpoints=True,
        # The reference platform doesn't emit completion webhooks —
        # turn off the F12 auto-emit gate so server boot doesn't trip
        # ``validate_webhook_sender_for_platform``. Adopters whose
        # platforms need webhook delivery wire a
        # :class:`WebhookSender` (or
        # :class:`InMemoryWebhookDeliverySupervisor`) and remove this
        # kwarg — see the webhook_supervisor module for the wiring
        # pattern.
        auto_emit_completion_webhooks=False,
        # FastMCP's TransportSecurityMiddleware enforces DNS-rebinding
        # protection: its default ``allowed_hosts`` accepts only
        # loopback (``127.0.0.1:*``, ``localhost:*``, ``[::1]:*``), so
        # subdomain hosts like ``acme.localhost:3001`` are rejected
        # with ``421 Misdirected Request``. ``SubdomainTenantMiddleware``
        # above already validates the Host header against the seeded
        # tenant table — that's the load-bearing host check for this
        # seller. Disabling the MCP-layer check avoids duplicating
        # the same validation against a static, hard-to-extend list.
        # Adopters that don't run a tenant-aware ASGI middleware leave
        # this kwarg unset to keep the FastMCP defaults active.
        enable_dns_rebinding_protection=False,
    )


if __name__ == "__main__":
    main()


__all__ = ["main"]
