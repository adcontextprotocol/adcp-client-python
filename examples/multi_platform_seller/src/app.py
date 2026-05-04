"""Boot a multi-platform seller — N tenants behind one process.

Wires:

* :class:`MockGuaranteedPlatform` for ``tenant-a`` (sales-guaranteed).
* :class:`MockNonGuaranteedPlatform` for ``tenant-b`` (sales-non-guaranteed).
* :class:`PlatformRouter` over both, with a union capabilities
  declaration so ``tools/list`` advertises every method either platform
  serves.
* :class:`SubdomainTenantMiddleware` so requests to ``tenant-a.localhost``
  vs ``tenant-b.localhost`` resolve to the right tenant via the
  ``Host`` header.

Run::

    python -m examples.multi_platform_seller.src.app

For local subdomain routing, add to ``/etc/hosts``::

    127.0.0.1 tenant-a.localhost tenant-b.localhost

Then connect any AdCP MCP buyer to::

    http://tenant-a.localhost:3001/mcp
    http://tenant-b.localhost:3001/mcp

The storyboard runner sends explicit ``account.account_id`` like
``tenant-a:storyboard-account``; both routing paths work against the
same boot.
"""

from __future__ import annotations

import os

from adcp.decisioning import (
    DecisioningCapabilities,
    PlatformRouter,
    serve,
)
from adcp.decisioning.capabilities import Account as CapabilitiesAccount
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencyUnsupported,
    MediaBuy,
    SupportedProtocol,
)
from adcp.server import (
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    Tenant,
)

# Examples are not a package on PYTHONPATH; the ``-m`` invocation
# requires this module to live under ``examples.multi_platform_seller.src``
# which already gives the right qualified imports.
from examples.multi_platform_seller.src.account_store import MultiTenantAccountStore
from examples.multi_platform_seller.src.mock_guaranteed import MockGuaranteedPlatform
from examples.multi_platform_seller.src.mock_non_guaranteed import (
    MockNonGuaranteedPlatform,
)

PORT = int(os.environ.get("ADCP_PORT") or os.environ.get("PORT") or 3001)


def build_router() -> PlatformRouter:
    """Construct the :class:`PlatformRouter` over the two mock tenants."""
    tenants = frozenset({"tenant-a", "tenant-b"})
    accounts = MultiTenantAccountStore(tenants=tenants)

    # Union capabilities: the router advertises every specialism either
    # platform serves. Per-tool dispatch goes to whichever tenant the
    # request resolves to; calls that the resolved tenant doesn't
    # implement raise UNSUPPORTED_FEATURE.
    capabilities = DecisioningCapabilities(
        specialisms=["sales-guaranteed", "sales-non-guaranteed"],
        adcp=Adcp(
            major_versions=[3],
            # Router union over two mock platforms — neither wires
            # in-memory dedup, so the union honestly advertises
            # unsupported. Real adopters wrap mutating handlers with
            # @IdempotencyStore.wrap and declare supported=True.
            idempotency=IdempotencyUnsupported(supported=False),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        supported_protocols=[SupportedProtocol.media_buy],
    )

    return PlatformRouter(
        accounts=accounts,
        platforms={
            "tenant-a": MockGuaranteedPlatform(),
            "tenant-b": MockNonGuaranteedPlatform(),
        },
        capabilities=capabilities,
    )


def build_subdomain_middleware() -> tuple[type, dict[str, object]]:
    """Build the ``SubdomainTenantMiddleware`` registration tuple.

    Returns ``(middleware_class, kwargs)`` matching the framework's
    ``asgi_middleware=`` shape so ``serve()`` adds it to the Starlette
    stack with the right router.
    """
    # The router's ``_normalize_host`` (see
    # ``adcp.server.tenant_router._normalize_host``) lower-cases the
    # host and strips any ``:port`` suffix at construction AND at
    # lookup, so ``tenant-a.localhost:3001`` and ``tenant-a.localhost``
    # resolve identically. Register the bare host once.
    subdomain_router = InMemorySubdomainTenantRouter(
        tenants={
            "tenant-a.localhost": Tenant(id="tenant-a", display_name="Mock Guaranteed Tenant"),
            "tenant-b.localhost": Tenant(id="tenant-b", display_name="Mock Non-Guaranteed Tenant"),
        }
    )
    return SubdomainTenantMiddleware, {"router": subdomain_router}


def _allowed_hosts() -> list[str]:
    """The TransportSecurityMiddleware host allowlist.

    FastMCP's DNS-rebinding-protection default only accepts loopback
    patterns; the tenant subdomains have to be added explicitly. Both
    bare hosts and ``host:*`` (any-port) wildcards work; using
    ``host:*`` keeps the example port-agnostic for adopters who change
    ``ADCP_PORT``.
    """
    return [
        "tenant-a.localhost",
        "tenant-a.localhost:*",
        "tenant-b.localhost",
        "tenant-b.localhost:*",
    ]


if __name__ == "__main__":
    router = build_router()
    middleware_class, middleware_kwargs = build_subdomain_middleware()

    serve(
        router,
        name="multi-platform-seller",
        port=PORT,
        auto_emit_completion_webhooks=False,
        # ``serve()`` forwards extra kwargs to ``adcp.server.serve``;
        # the underlying transport accepts a Starlette middleware list.
        asgi_middleware=[(middleware_class, middleware_kwargs)],
        # Extend FastMCP's host allowlist to include the tenant
        # subdomains. Without this the transport returns 421 on every
        # ``Host: tenant-x.localhost`` request before the subdomain
        # router gets a chance to resolve.
        allowed_hosts=_allowed_hosts(),
    )
