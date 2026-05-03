"""ADCP Server Framework.

Build an AdCP agent in minutes. The framework handles the protocol
plumbing so you focus on business logic.

Quickstart (class-based)::

    from adcp.server import ADCPHandler, serve
    from adcp.server.responses import capabilities_response, products_response

    class MySeller(ADCPHandler):
        async def get_adcp_capabilities(self, params, context=None):
            return capabilities_response(["media_buy"])

        async def get_products(self, params, context=None):
            return products_response(MY_PRODUCTS)

    serve(MySeller(), name="my-seller")

Quickstart (decorator-based)::

    from adcp.server import adcp_server, serve
    from adcp.server.responses import products_response

    server = adcp_server("my-seller")

    @server.get_products
    async def get_products(params, context=None):
        return products_response(MY_PRODUCTS)

    serve(server, name="my-seller")  # capabilities auto-generated

What the framework does automatically:

- **Error responses**: ``adcp_error("BUDGET_TOO_LOW")`` auto-populates
  recovery classification (transient/correctable/terminal) from 20+
  standard codes.
- **State transitions**: ``media_buy_response(..., status="active")``
  auto-populates ``valid_actions`` from the status. No manual mapping.
- **Account resolution**: ``resolve_account(params, my_resolver)``
  auto-resolves AccountReference and returns ACCOUNT_NOT_FOUND errors.
- **Context passthrough**: ``inject_context(params, response)`` echoes
  the request context field back in the response (ADCP requirement).
- **Cancellation**: ``cancel_media_buy_response(id, "buyer")``
  auto-sets canceled_at, status, and valid_actions=[].
- **Capabilities**: The decorator builder auto-generates
  ``get_adcp_capabilities`` from which handlers you register.
- **Validation**: GovernanceHandler and ContentStandardsHandler
  auto-validate request dicts into Pydantic models before your
  handler code runs.
"""

from __future__ import annotations

from adcp.capabilities import validate_capabilities
from adcp.server.a2a_server import ADCPAgentExecutor, MessageParser, create_a2a_server
from adcp.server.auth import (
    A2ABearerAuthMiddleware,
    AsyncTokenValidator,
    BearerTokenAuth,
    BearerTokenAuthMiddleware,
    Principal,
    SyncTokenValidator,
    TokenValidator,
    auth_context_factory,
    constant_time_token_match,
    validator_from_token_map,
)
from adcp.server.base import (
    AccountAwareToolContext,
    ADCPHandler,
    NotImplementedResponse,
    TContext,
    ToolContext,
    not_supported,
)
from adcp.server.brand import BrandHandler
from adcp.server.builder import ADCPServerBuilder, adcp_server
from adcp.server.compliance import ComplianceHandler
from adcp.server.content_standards import ContentStandardsHandler
from adcp.server.discovery import (
    DISCOVERY_PATH,
    build_manifest,
    make_discovery_route,
)
from adcp.server.governance import GovernanceHandler
from adcp.server.helpers import (  # noqa: F401
    CORRECTABLE_CODES,
    MEDIA_BUY_STATE_MACHINE,
    STANDARD_ERROR_CODES,
    TERMINAL_CODES,
    TRANSIENT_CODES,
    AccountError,
    adcp_error,
    cancel_media_buy_response,
    inject_context,
    is_terminal_status,
    resolve_account,
    resolve_account_into_context,
    valid_actions_for_status,
)
from adcp.server.idempotency import IdempotencyStore, MemoryBackend
from adcp.server.mcp_tools import (
    DISCOVERY_METHODS,
    DISCOVERY_TOOLS,
    MCPToolSet,
    create_mcp_tools,
    get_tools_for_handler,
    register_handler_tools,
    validate_discovery_set,
)
from adcp.server.proposal import ProposalBuilder, ProposalNotSupported
from adcp.server.responses import (
    activate_signal_response,
    build_creative_response,
    capabilities_response,
    creative_formats_response,
    delivery_response,
    error_response,
    list_creatives_response,
    log_event_response,
    media_buy_error_response,
    media_buy_response,
    media_buys_response,
    preview_creative_response,
    products_response,
    signals_response,
    sync_accounts_response,
    sync_catalogs_response,
    sync_creatives_response,
    sync_governance_response,
    update_media_buy_response,
)
from adcp.server.serve import (
    ASGIMiddlewareEntry,
    ContextFactory,
    RequestMetadata,
    ServeConfig,
    SkillMiddleware,
    create_mcp_server,
    serve,
)
from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler
from adcp.server.tenant_router import (
    CallableSubdomainTenantRouter,
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    SubdomainTenantRouter,
    Tenant,
    TenantResolver,
    current_tenant,
)
from adcp.server.test_controller import (
    INSECURE_ALLOW_ALL,
    TestControllerError,
    TestControllerStore,
    register_test_controller,
)
from adcp.server.tmp import TmpHandler

__all__ = [
    # Base classes
    "AccountAwareToolContext",
    "ADCPHandler",
    "BrandHandler",
    "ComplianceHandler",
    "TContext",
    "TmpHandler",
    "ToolContext",
    "NotImplementedResponse",
    "not_supported",
    # Capability validation
    "validate_capabilities",
    # Protocol handlers
    "ContentStandardsHandler",
    "GovernanceHandler",
    "SponsoredIntelligenceHandler",
    # Proposal helpers
    "ProposalBuilder",
    "ProposalNotSupported",
    # MCP integration
    "ContextFactory",
    "DISCOVERY_METHODS",
    "DISCOVERY_TOOLS",
    "MCPToolSet",
    "RequestMetadata",
    "ServeConfig",
    "create_mcp_tools",
    "create_mcp_server",
    "get_tools_for_handler",
    "register_handler_tools",
    "serve",
    "validate_discovery_set",
    # A2A integration
    "ADCPAgentExecutor",
    "MessageParser",
    "ASGIMiddlewareEntry",
    "SkillMiddleware",
    "create_a2a_server",
    # Bearer-token auth middleware (seller-facing recipe)
    "A2ABearerAuthMiddleware",
    "AsyncTokenValidator",
    "BearerTokenAuth",
    "BearerTokenAuthMiddleware",
    "Principal",
    "SyncTokenValidator",
    "TokenValidator",
    "auth_context_factory",
    "constant_time_token_match",
    "validator_from_token_map",
    # Idempotency middleware (AdCP #2315 seller side)
    "IdempotencyStore",
    "MemoryBackend",
    # Subdomain tenant routing
    "CallableSubdomainTenantRouter",
    "InMemorySubdomainTenantRouter",
    "SubdomainTenantMiddleware",
    "SubdomainTenantRouter",
    "Tenant",
    "TenantResolver",
    "current_tenant",
    # Multi-agent discovery manifest (/.well-known/adcp-agents.json)
    "DISCOVERY_PATH",
    "build_manifest",
    "make_discovery_route",
    # Test controller
    "INSECURE_ALLOW_ALL",
    "TestControllerStore",
    "TestControllerError",
    "register_test_controller",
    # DX helpers
    "AccountError",
    "STANDARD_ERROR_CODES",
    "adcp_error",
    "adcp_server",
    "ADCPServerBuilder",
    "cancel_media_buy_response",
    "inject_context",
    "is_terminal_status",
    "resolve_account",
    "resolve_account_into_context",
    "valid_actions_for_status",
    # Response builders
    "activate_signal_response",
    "build_creative_response",
    "capabilities_response",
    "creative_formats_response",
    "delivery_response",
    "error_response",
    "list_creatives_response",
    "log_event_response",
    "media_buy_error_response",
    "media_buy_response",
    "media_buys_response",
    "preview_creative_response",
    "products_response",
    "signals_response",
    "sync_accounts_response",
    "sync_catalogs_response",
    "sync_creatives_response",
    "sync_governance_response",
    "update_media_buy_response",
]
