"""Main entrypoint — wires every Tier 2 / v3-supporting component
into one runnable adopter, in the **translator pattern**: AdCP wire on
the inside, the JS mock-server (``@adcp/client adcp mock-server
sales-guaranteed``) over HTTP on the outside.

Boot sequence:

1. Connect SQLAlchemy async engine + sessionmaker.
2. Evolve schema by running ``alembic upgrade head``.
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
   * optional durable PostgreSQL task registry + atomic webhook outbox

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
import hmac
import logging
import os
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adcp.decisioning import InMemoryMockAdServer, serve
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.registry import ApiKeyCredential
from adcp.server import (
    SubdomainTenantMiddleware,
    ToolContext,
)
from adcp.server.auth import (
    ROUTED_TENANT_METADATA_KEY,
    BearerTokenAuth,
    Principal,
    auth_context_factory,
    enforce_authenticated_tenant,
)
from adcp.validation import ValidationHookConfig

from .audit import make_sink as make_audit_sink
from .buyer_registry import make_registry as make_buyer_registry
from .durable_tasks import DurableTaskWiring
from .platform import V3ReferenceSeller
from .tenant_router import SqlSubdomainTenantRouter

if TYPE_CHECKING:
    from adcp.server import RequestMetadata

logger = logging.getLogger(__name__)


def _build_context_factory():
    """``context_factory`` that pins :attr:`ToolContext.tenant_id`
    from the resolved tenant AND upgrades the bearer-flow
    ``adcp.auth_info`` with a typed :class:`ApiKeyCredential`.

    The SDK's :func:`adcp.server.auth.auth_context_factory` populates
    ``metadata["adcp.auth_info"]`` with ``credential=None`` for bearer
    flows because raw bearer tokens are server-internal (see
    :func:`auth_context_factory`'s docstring). Without a typed
    credential the framework's :class:`BuyerAgentRegistry` dispatch
    falls into the no-credential branch and returns
    ``PERMISSION_DENIED`` — so adopters that wire a registry alongside
    bearer auth MUST upgrade the ``AuthInfo`` here.

    The validator (see :func:`_make_validate_token`) stashes the raw
    bearer token in ``Principal.metadata["api_key_id"]``; this factory
    reads it back to construct the :class:`ApiKeyCredential` that
    :meth:`BuyerAgentRegistry.resolve_by_credential` matches against
    the ``api_key_id`` column.
    """
    from dataclasses import replace

    def build(meta: RequestMetadata) -> ToolContext:
        ctx = auth_context_factory(meta)
        # ``auth_context_factory`` preserves both identities in metadata.
        # Pin the business context to the routed host; the skill middleware
        # below compares it with the authenticated identity from the token at
        # a boundary where both MCP and A2A project AdcpError consistently.
        if ROUTED_TENANT_METADATA_KEY in ctx.metadata:
            ctx = replace(ctx, tenant_id=ctx.metadata[ROUTED_TENANT_METADATA_KEY])

        # Upgrade bearer-flow auth_info with a typed ApiKeyCredential
        # when the validator stashed the raw token in principal metadata.
        # ctx.metadata is a dict; mutate in place rather than rebuilding.
        api_key_id = ctx.metadata.get("api_key_id")
        existing = ctx.metadata.get("adcp.auth_info")
        if api_key_id and isinstance(existing, AuthInfo):
            ctx.metadata["adcp.auth_info"] = AuthInfo(
                kind="api_key",
                key_id=api_key_id,
                principal=existing.principal,
                credential=ApiKeyCredential(kind="api_key", key_id=api_key_id),
            )
        return ctx

    return build


async def _load_token_map(sessionmaker) -> dict[str, Principal]:
    """Eagerly load all ``BuyerAgent`` rows with a non-null
    ``api_key_id`` into a ``token → Principal`` map.

    Consumed by the sync validator returned from
    :func:`_make_validate_token`. ``BearerTokenAuth.validate_token``
    must be sync when ``transport="both"`` (the A2A leg's middleware
    cannot await an async validator), so we pay one DB scan at boot
    and serve every subsequent request from memory. The seed is small
    and stable for the reference seller; adopters with dynamic admin
    paths swap in their own validator backed by a cache with TTL-based
    reload.
    """
    from sqlalchemy import select

    from .models import BuyerAgent as BuyerAgentRow

    token_map: dict[str, Principal] = {}
    async with sessionmaker() as session:
        result = await session.execute(
            select(BuyerAgentRow).where(BuyerAgentRow.api_key_id.is_not(None))
        )
        for row in result.scalars():
            if row.api_key_id in token_map:
                raise RuntimeError(
                    "Duplicate buyer-agent api_key_id detected; bearer credentials "
                    "must identify exactly one tenant."
                )
            token_map[row.api_key_id] = Principal(
                caller_identity=row.agent_url,
                tenant_id=row.tenant_id,
                metadata={"api_key_id": row.api_key_id},
            )
    return token_map


def _make_validate_token(token_map: dict[str, Principal]):
    """Sync validator returning the pre-loaded :class:`Principal` for
    a bearer token, or ``None`` for unknown tokens.

    The returned Principal carries the raw token in metadata under
    ``api_key_id`` so :func:`_build_context_factory` can attach a
    typed :class:`ApiKeyCredential` to the dispatch context — the
    framework's :class:`BuyerAgentRegistry` then resolves
    commercially via :meth:`resolve_by_credential`.
    """

    def validate_token(token: str) -> Principal | None:
        if not token:
            return None
        return token_map.get(token)

    return validate_token


def _run_alembic_upgrade_head(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url``.

    Mirrors the production entrypoint in ``migrate.py`` so boot evolves
    the schema via the same migration scripts adopters run in CI. The
    previous ``Base.metadata.create_all`` path silently skipped column
    renames and type changes on existing tables; running migrations at
    boot eliminates that drift.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    # env.py reads DATABASE_URL from os.environ; export so the alembic
    # script picks up the same URL the app connects with.
    os.environ["DATABASE_URL"] = db_url
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")


async def _bootstrap_schema_and_load_tokens(
    db_url: str, engine, sessionmaker
) -> dict[str, Principal]:
    """Run ``alembic upgrade head`` AND load the bearer-token map in
    the same event loop, then dispose the engine before returning.

    Migrations propagate schema changes (column renames, type changes,
    new columns on existing tables) that ``create_all`` silently
    skipped. Token loading happens here (rather than separately)
    because ``BearerTokenAuth.validate_token`` must be sync for
    ``transport="both"``, so we pay one DB scan at boot and serve every
    subsequent request from memory.

    asyncpg binds connection-internal Future objects to the loop they
    were opened on. Bootstrapping via ``asyncio.run`` runs on a
    transient loop that closes when ``asyncio.run`` returns; if those
    connections stay in the pool, uvicorn's own loop trips
    ``RuntimeError: got Future attached to a different loop`` on the
    first request. Dispose before returning so uvicorn opens a fresh
    pool on its own loop.

    ``alembic.command.upgrade`` is synchronous and opens its own
    engine; ``asyncio.to_thread`` runs it off the event loop so the
    async surface stays unblocked.
    """
    await asyncio.to_thread(_run_alembic_upgrade_head, db_url)
    token_map = await _load_token_map(sessionmaker)
    await engine.dispose()
    return token_map


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

    token_map = asyncio.run(_bootstrap_schema_and_load_tokens(db_url, engine, sessionmaker))

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
    # In local development the whole durable bundle may be omitted. In
    # production DurableTaskWiring fails fast unless PostgreSQL, encryption,
    # signing, registry, and outbox configuration are all present. The web
    # process commits task terminal state + outbox rows atomically; a separate
    # ``python -m src.worker`` process owns delivery and retries.
    task_wiring = DurableTaskWiring.from_env()
    if task_wiring is not None:
        logger.info(
            "Durable task registry and webhook outbox wired: alg=%s horizon=%ds",
            task_wiring.signing_algorithm,
            task_wiring.retry_horizon_seconds,
        )

    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key=upstream_api_key,
        mock_upstream_url=upstream_url,
        mock_ad_server=mock_ad_server,
        webhook_signing_alg=(task_wiring.signing_algorithm if task_wiring else None),
        webhook_retry_horizon_seconds=(
            task_wiring.retry_horizon_seconds if task_wiring else 86_400
        ),
        idempotency=task_wiring.idempotency if task_wiring else None,
    )

    logger.info(
        "v3 reference seller booting on port=%d (transport=both, MCP at /mcp, A2A at /)",
        port,
    )
    logger.info("Mock-mode upstream: %s (api_key=%s...)", upstream_url, upstream_api_key[:4])
    logger.info("Audit sink wired: %s. Tenant router cache: 256 hosts.", type(audit_sink).__name__)
    debug_token = os.environ.get("ADCP_DEBUG_TOKEN")

    serve(
        platform=platform,
        name="v3-reference-seller",
        port=port,
        host="0.0.0.0",
        transport="both",
        buyer_agent_registry=buyer_registry,
        registry=task_wiring.registry if task_wiring else None,
        # Bearer auth wired so the framework extracts the
        # ``Authorization: Bearer <token>`` header, resolves the token
        # to a seeded BuyerAgent via api_key_id lookup, and threads the
        # raw token into the dispatch context so
        # ``BuyerAgentRegistry.resolve_by_credential`` can re-resolve
        # commercially. Without this, every dispatched skill hits the
        # registry with credential=None and returns PERMISSION_DENIED.
        auth=BearerTokenAuth(validate_token=_make_validate_token(token_map)),
        context_factory=_build_context_factory(),
        middleware=[enforce_authenticated_tenant],
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
        enable_debug_endpoints=debug_token is not None,
        debug_validate_request=(
            (
                lambda headers, token=debug_token: hmac.compare_digest(
                    headers.get("x-debug-token", ""), token
                )
            )
            if debug_token is not None
            else None
        ),
        on_startup=(task_wiring.startup,) if task_wiring else (),
        on_shutdown=((task_wiring.shutdown, engine.dispose) if task_wiring else (engine.dispose,)),
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
