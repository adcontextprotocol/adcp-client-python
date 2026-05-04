"""Example: multi-tenant MCP server with bearer-token auth.

Wires :class:`~adcp.server.BearerTokenAuthMiddleware` +
:func:`~adcp.server.auth_context_factory` onto a multi-tenant sales
agent. The SDK owns the security-critical plumbing (constant-time
token compare, discovery bypass, ``ContextVar`` reset-in-finally);
the seller supplies only the token → principal map and the handler
logic.

Run::

    uv run python examples/mcp_with_auth_middleware.py
    # → server on http://localhost:3001/mcp/
    # curl -H 'Authorization: Bearer token-acme' ...

Production note: ``mcp.run()`` is used here for brevity. Real
deployments should mount the Starlette app behind uvicorn + a reverse
proxy that terminates TLS and handles rate limiting. Production
agents also typically load tokens from a database — swap
``validator_from_token_map`` for an ``async def validate_token`` that
hits your token store.

For advanced production patterns — subdomain-based tenant routing
(Pattern 2b), ResolvedIdentity DB enrichment, idempotency wiring, and
error classification — see ``docs/handler-authoring.md``.
"""

from __future__ import annotations

from typing import Any

from adcp.server import (
    ADCPHandler,
    BearerTokenAuthMiddleware,
    Principal,
    ToolContext,
    auth_context_factory,
    create_mcp_server,
    validator_from_token_map,
)
from adcp.server.responses import capabilities_response, products_response

# Real agents look tokens up in Postgres / Vault / an identity provider.
# ``validator_from_token_map`` hashes the raw tokens at construction and
# does ``hmac.compare_digest`` lookups — same security properties as a
# hand-rolled validator, one line instead of a dozen.
validate_token = validator_from_token_map(
    {
        "token-acme": Principal(caller_identity="principal-acme-ops", tenant_id="tenant-acme"),
        "token-globex": Principal(
            caller_identity="principal-globex-ops", tenant_id="tenant-globex"
        ),
    }
)


class MultiTenantSalesAgent(ADCPHandler):
    _agent_type = "demo multi-tenant sales agent"

    async def get_adcp_capabilities(
        self, params: Any, context: ToolContext | None = None
    ) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: ToolContext | None = None) -> dict[str, Any]:
        # On the dispatch path the handler receives a decisioning
        # ``RequestContext``; read ``ctx.auth_principal`` (not
        # ``ctx.caller_identity``) for "who's calling?". The
        # framework mutates ``caller_identity`` downstream into a
        # composite cache scope key for idempotency — it is not a
        # principal label by the time a handler sees it. On bearer
        # flows like this one ``auth_principal`` is sourced from the
        # :data:`adcp.server.auth.current_principal` ContextVar that
        # the middleware populates.
        tenant = context.tenant_id if context is not None else None
        return products_response(_products_for_tenant(tenant))


def _products_for_tenant(tenant_id: str | None) -> list[dict[str, Any]]:
    if tenant_id == "tenant-acme":
        return [{"product_id": "acme_display_1", "name": "Acme homepage display"}]
    if tenant_id == "tenant-globex":
        return [{"product_id": "globex_video_1", "name": "Globex CTV video"}]
    return []


def main() -> None:
    mcp = create_mcp_server(
        MultiTenantSalesAgent(),
        name="multi-tenant-sales-agent",
        context_factory=auth_context_factory,
    )
    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
