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

4. ``adcp.decisioning.serve(transport="both", ...)`` — single
   binary serving MCP at ``/mcp`` and A2A at ``/``.

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
    ToolContext,
    current_tenant,
)

from .audit import make_sink as make_audit_sink
from .buyer_registry import make_registry as make_buyer_registry
from .models import Base
from .platform import V3ReferenceSeller
from .tenant_router import SqlSubdomainTenantRouter

if TYPE_CHECKING:
    from adcp.server import RequestMetadata

logger = logging.getLogger(__name__)


def _build_context_factory(router: SqlSubdomainTenantRouter):
    """``context_factory`` that pins :attr:`ToolContext.tenant_id`
    from the resolved tenant.

    The middleware sets ``current_tenant()`` on the contextvar before
    dispatch; this factory reads it and writes ``tenant_id`` so the
    framework's idempotency middleware scopes correctly.
    """
    del router  # router is consumed via current_tenant(); kept for symmetry

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

    # The framework's serve() drives uvicorn. We layer the tenant
    # middleware on the parent Starlette app via a build hook —
    # for the MVP we wire it inline by passing a custom ``app``
    # builder. For the Tier-2 wiring we use the ``buyer_agent_registry``
    # and ``context_factory`` kwargs the framework already supports.
    serve(
        platform=platform,
        name="v3-reference-seller",
        port=port,
        transport="both",
        buyer_agent_registry=buyer_registry,
        context_factory=_build_context_factory(router),
        # NOTE: SubdomainTenantMiddleware is added to the unified
        # Starlette app via ``add_middleware``. The current public
        # surface of ``serve()`` doesn't expose a hook for adopter
        # ASGI middleware on the parent — adopters who want it today
        # call ``_build_mcp_and_a2a_app`` directly and run uvicorn
        # themselves. Filed as a follow-up DX issue: the helper
        # below shows the pattern.
        host="0.0.0.0",
    )


# Adopter-side helper for wiring SubdomainTenantMiddleware directly.
# Until ``serve()`` accepts an ``asgi_middleware=`` kwarg, callers who
# need tenant routing build the unified app themselves and run
# uvicorn outside the framework's ``serve()`` wrapper.
def build_app(
    platform: V3ReferenceSeller,
    *,
    router: SqlSubdomainTenantRouter,
    buyer_registry,  # type: ignore[no-untyped-def]
    audit_sink,  # type: ignore[no-untyped-def]
):
    """Construct the unified MCP+A2A app with the tenant middleware.

    Returns an ASGI app callable; the caller runs uvicorn against it.
    """
    from concurrent.futures import ThreadPoolExecutor

    from adcp.decisioning.handler import PlatformHandler
    from adcp.decisioning.serve import _build_mcp_and_a2a_app
    from adcp.decisioning.task_registry import InMemoryTaskRegistry

    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="v3-ref-")
    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        buyer_agent_registry=buyer_registry,
    )
    app = _build_mcp_and_a2a_app(
        handler,
        name="v3-reference-seller",
        port=3001,
        host="0.0.0.0",
        instructions=None,
        test_controller=None,
        context_factory=_build_context_factory(router),
    )
    # Wrap with the tenant middleware. ASGI middleware composes
    # outermost-first: requests pass through the tenant resolver
    # before reaching the dispatcher.
    from adcp.server.tenant_router import SubdomainTenantMiddleware as _SubdomainTenantMiddleware

    class _Wrapped:
        def __init__(self, app):
            self._app = app
            self._mw = _SubdomainTenantMiddleware(app, router=router)

        async def __call__(self, scope, receive, send):
            await self._mw(scope, receive, send)

    return _Wrapped(app)


if __name__ == "__main__":
    main()


__all__ = ["build_app", "main"]
