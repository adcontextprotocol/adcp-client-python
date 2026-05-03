"""Main entrypoint — wires every Tier 2 / v3-supporting component
into one runnable adopter.

Boot sequence:

1. Connect SQLAlchemy async engine + sessionmaker.
2. Create schema (idempotent ``Base.metadata.create_all``).
3. Build the framework wiring:

   * :class:`SqlSubdomainTenantRouter` for ``Host`` → tenant
   * :class:`TenantScopedBuyerAgentRegistry` for the Tier 2 gate
   * :class:`DbAuditSink` for compliance trail
   * :class:`V3ReferenceSeller` (the platform impl)

4. ``adcp.decisioning.serve(transport="both", asgi_middleware=[...])``
   — single binary serving MCP at ``/mcp`` and A2A at ``/`` with
   :class:`SubdomainTenantMiddleware` layered on the outer HTTP app.

Adopters fork this file and replace the platform impl, the seller-
specific column populators, and the seed fixtures. Everything else
stays.

::

    cd examples/v3_reference_seller
    docker compose up -d postgres
    DATABASE_URL=postgresql+asyncpg://postgres@localhost/adcp \\
      python -m src.app
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adcp.decisioning import serve
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

    The middleware sets ``current_tenant()`` on the contextvar before
    dispatch; this factory reads it and writes ``tenant_id`` so the
    framework's idempotency middleware scopes correctly.
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
    ``create_all`` for fast iteration. The schema migration story
    is in the README.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _build_validation_config() -> ValidationHookConfig:
    """Return a :class:`ValidationHookConfig` for boot-time wiring.

    Defaults to ``strict`` on both sides so malformed requests and
    responses are rejected at the boundary — bugs like the recent
    ``pricing_options`` shape conformance issue surface immediately
    rather than at storyboard run time.

    Drops to ``warn`` when ``ADCP_ENV`` is ``prod`` or ``production``
    (the same convention :func:`adcp.validation.client_hooks._default_response_mode`
    uses on the client side).  This lets sellers running a mixed-buyer
    rollout tolerate minor out-of-spec traffic without hard-failing
    requests.  Set ``ADCP_ENV=production`` in your deployment environment
    when you need the softer mode.
    """
    adcp_env = os.environ.get("ADCP_ENV", "").strip().lower()
    mode: str = "warn" if adcp_env in {"prod", "production"} else "strict"
    return ValidationHookConfig(requests=mode, responses=mode)  # type: ignore[arg-type]


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

    engine = create_async_engine(db_url, pool_size=10, max_overflow=20)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asyncio.run(_bootstrap_schema(engine))

    router = SqlSubdomainTenantRouter(sessionmaker=sessionmaker)
    buyer_registry = make_buyer_registry(sessionmaker)
    audit_sink = make_audit_sink(sessionmaker)
    platform = V3ReferenceSeller(sessionmaker=sessionmaker)

    logger.info(
        "v3 reference seller booting on port=%d (transport=both, MCP at /mcp, A2A at /)",
        port,
    )
    logger.info("Audit sink wired: %s. Tenant router cache: 256 hosts.", type(audit_sink).__name__)

    serve(
        platform=platform,
        name="v3-reference-seller",
        port=port,
        host="0.0.0.0",
        transport="both",
        buyer_agent_registry=buyer_registry,
        context_factory=_build_context_factory(),
        validation=_build_validation_config(),
        # SubdomainTenantMiddleware reads the request Host header,
        # resolves it via the SQL router, and sets the
        # ``current_tenant()`` contextvar before the handler runs.
        # The buyer_registry, AccountStore, and audit sink all read
        # that contextvar — this is the only multi-tenant wiring
        # point.
        asgi_middleware=[
            (SubdomainTenantMiddleware, {"router": router}),
        ],
    )


if __name__ == "__main__":
    main()


__all__ = ["main"]
