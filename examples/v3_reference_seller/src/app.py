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

from adcp.decisioning import AdcpError, InMemoryMockAdServer, serve
from adcp.decisioning.context import AuthInfo
from adcp.decisioning.registry import ApiKeyCredential
from adcp.server import (
    SubdomainTenantMiddleware,
    ToolContext,
    current_tenant,
)
from adcp.server.auth import BearerTokenAuth, Principal, auth_context_factory
from adcp.validation import ValidationHookConfig
from adcp.webhook_sender import WebhookSender
from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor

from .audit import make_sink as make_audit_sink
from .buyer_registry import make_registry as make_buyer_registry
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
        # Pin tenant from SubdomainTenantMiddleware. Subdomain wins for
        # tenant routing; the validator's tenant_id is only the token's
        # home tenant and may not match the host the request came in on.
        tenant = current_tenant()
        if tenant is not None:
            ctx = replace(ctx, tenant_id=tenant.id)

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

    # Webhook-signing wiring (#384). Loaded from env so the PEM stays
    # off the process command line and out of any os.environ dump that
    # would otherwise surface a raw JWK scalar. The key is a separate
    # ed25519/es256 keypair from the request-signing key — AdCP requires
    # webhook-signing material be distinct so a signature from one
    # surface cannot be replayed on the other.
    #
    # Generate with:
    #   adcp-keygen --alg ed25519 --purpose webhook-signing \
    #     --out /etc/adcp/webhook-signing.pem
    # Then publish the printed public JWK at the seller's jwks_uri.
    signing_pem_path = os.environ.get("ADCP_WEBHOOK_SIGNING_KEY_PATH")
    signing_key_id = os.environ.get("ADCP_WEBHOOK_SIGNING_KEY_ID")
    signing_alg = os.environ.get("ADCP_WEBHOOK_SIGNING_ALG", "ed25519")

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
    # Wire the webhook supervisor iff signing material is present. When
    # the env vars are unset, the seller falls back to the
    # ``auto_emit_completion_webhooks=False`` posture below — a buyer
    # registering ``push_notification_config.url`` will not receive
    # auto-emitted completion webhooks, but boot succeeds without a key.
    # The framework's #384 validator binds these two posture knobs
    # together: capabilities advertise signing iff the supervisor is
    # wired with an RFC 9421 key.
    webhook_supervisor: InMemoryWebhookDeliverySupervisor | None = None
    if signing_pem_path and signing_key_id:
        webhook_sender = WebhookSender.from_pem(
            signing_pem_path,
            key_id=signing_key_id,
            alg=signing_alg,
        )
        webhook_supervisor = InMemoryWebhookDeliverySupervisor(sender=webhook_sender)
        logger.info(
            "Webhook signing wired: key_id=%s alg=%s pem=%s",
            signing_key_id,
            signing_alg,
            signing_pem_path,
        )
    elif signing_pem_path or signing_key_id:
        # Partial config is operator error — both env vars must be set
        # together, or both omitted. Raise AdcpError (terminal) so an
        # adopter wrapping main() in ``except AdcpError`` catches all
        # boot misconfigs uniformly, matching the sibling validators.
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "ADCP_WEBHOOK_SIGNING_KEY_PATH and "
                "ADCP_WEBHOOK_SIGNING_KEY_ID must be set together — got "
                f"path={signing_pem_path!r}, key_id={signing_key_id!r}"
            ),
            recovery="terminal",
            details={
                "missing": "webhook_signing_env_pair",
                "ADCP_WEBHOOK_SIGNING_KEY_PATH_set": bool(signing_pem_path),
                "ADCP_WEBHOOK_SIGNING_KEY_ID_set": bool(signing_key_id),
            },
        )

    platform = V3ReferenceSeller(
        sessionmaker=sessionmaker,
        upstream_api_key=upstream_api_key,
        mock_upstream_url=upstream_url,
        mock_ad_server=mock_ad_server,
        webhook_signing_alg=signing_alg if webhook_supervisor is not None else None,
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
        # Bearer auth wired so the framework extracts the
        # ``Authorization: Bearer <token>`` header, resolves the token
        # to a seeded BuyerAgent via api_key_id lookup, and threads the
        # raw token into the dispatch context so
        # ``BuyerAgentRegistry.resolve_by_credential`` can re-resolve
        # commercially. Without this, every dispatched skill hits the
        # registry with credential=None and returns PERMISSION_DENIED.
        auth=BearerTokenAuth(validate_token=_make_validate_token(token_map)),
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
        # Auto-emit binds to the supervisor: when a webhook-signing PEM
        # is wired via the ADCP_WEBHOOK_SIGNING_KEY_PATH env var, the
        # supervisor signs every auto-emitted completion webhook per
        # RFC 9421 and the seller advertises the matching capability.
        # When unwired, auto-emit stays off so the F12 boot gate doesn't
        # trip on the missing sender (no silent webhook drops).
        webhook_supervisor=webhook_supervisor,
        auto_emit_completion_webhooks=webhook_supervisor is not None,
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
