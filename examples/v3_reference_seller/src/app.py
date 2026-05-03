"""Main entrypoint — wires every Tier 2 / v3-supporting component
into one runnable adopter, in the **translator pattern**: AdCP wire on
the inside, the JS mock-server (``@adcp/client adcp mock-server
sales-guaranteed``) over HTTP on the outside.

Boot sequence:

1. Connect SQLAlchemy async engine + sessionmaker.
2. Create schema (idempotent ``Base.metadata.create_all``).
3. Connect the upstream HTTP client (:class:`MockUpstreamClient`).
4. Build the framework wiring:

   * :class:`SqlSubdomainTenantRouter` for ``Host`` → tenant
   * :class:`TenantScopedBuyerAgentRegistry` for the Tier 2 gate
   * :class:`DbAuditSink` for compliance trail
   * :class:`V3ReferenceSeller` (the platform impl)

5. ``adcp.decisioning.serve(transport="both", asgi_middleware=[...])``
   — single binary serving MCP at ``/mcp`` and A2A at ``/`` with
   :class:`SubdomainTenantMiddleware` layered on the outer HTTP app.

Adopters fork this file and replace :class:`MockUpstreamClient` with
their own ad-server HTTP client. Everything else stays.

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
from .upstream import MockUpstreamClient

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

    upstream = MockUpstreamClient(
        base_url=upstream_url,
        api_key=upstream_api_key,
    )

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
    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream=upstream,
        mock_ad_server=mock_ad_server,
    )

    logger.info(
        "v3 reference seller booting on port=%d (transport=both, MCP at /mcp, A2A at /)",
        port,
    )
    logger.info("Translator upstream: %s (api_key=%s...)", upstream_url, upstream_api_key[:4])
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
    )


if __name__ == "__main__":
    main()


__all__ = ["main"]
