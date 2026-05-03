"""Hello-proposal-manager — the v1 two-platform composition demo.

Shows the per-tenant ProposalManager binding via :class:`PlatformRouter`.
Multi-tenant deployments need different proposal logic per tenant — a
GAM tenant's products differ from a Kevel tenant's; a Meta tenant's
proposal assembly differs from a TikTok tenant's. The router binds
``proposal_managers={tenant_id: ProposalManager}`` per tenant; tenants
without an entry fall through to the tenant's
:meth:`DecisioningPlatform.get_products` (back-compat per tenant).

Single-tenant adopters use a one-entry router with
``platforms={"default": ...}`` and
``proposal_managers={"default": ...}`` — same code path, no branching.

This demo wires two tenants:

* ``tenant_acme`` — has a :class:`MockProposalManager` wired. Forwards
  ``get_products`` to a running mock-server.
* ``tenant_globex`` — no proposal_manager. Falls through to the
  tenant's :meth:`HelloDecisioningPlatform.get_products` (which here
  returns a static stub catalogue).

Both tenants share the same :class:`HelloDecisioningPlatform` shape
for ``create_media_buy`` / ``update_media_buy`` / etc.

**Prerequisite:** start the mock-server before running this example::

    npx -y adcp-mock-server@latest sales-non-guaranteed --port 4500

Then::

    uv run python examples/hello_proposal_manager.py

See ``docs/proposals/product-architecture.md`` for the full design
context — § "The two-platform composition" + § "Tenant binding model".
"""

from __future__ import annotations

import os
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    MockProposalManager,
    PlatformRouter,
    RequestContext,
    SalesPlatform,
    serve,
)
from adcp.decisioning.accounts import Account, AuthInfo
from adcp.decisioning.capabilities import (
    Account as CapabilitiesAccount,
)
from adcp.decisioning.capabilities import (
    Adcp,
    IdempotencySupported,
    MediaBuy,
)


class HelloDecisioningPlatform(DecisioningPlatform, SalesPlatform):
    """Trivial execution-side platform.

    Handles ``create_media_buy`` / ``update_media_buy`` /
    ``sync_creatives`` / ``get_media_buy_delivery`` with stub responses.

    Also implements ``get_products`` so tenants WITHOUT a wired
    ProposalManager (here: ``tenant_globex``) have a working product
    catalog. Tenants WITH a wired ProposalManager (here:
    ``tenant_acme``) bypass this — the router routes to the manager
    instead.

    In production, this is where the adopter's adapter code lives —
    translating wire ``CreateMediaBuyRequest`` payloads into upstream
    calls (GAM, Kevel, Meta, etc.) and projecting the results back
    onto the wire response shape.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        adcp=Adcp(
            major_versions=[3],
            idempotency=IdempotencySupported(
                supported=True,
                replay_ttl_seconds=86400,
            ),
        ),
        account=CapabilitiesAccount(supported_billing=["operator"]),
        media_buy=MediaBuy(supported_pricing_models=["cpm"]),
    )
    # ``accounts`` is required on the Protocol but the router's
    # dispatch path does NOT consult per-platform AccountStores —
    # the router's own ``accounts`` does the resolution. Stub here.
    accounts = None  # type: ignore[assignment]

    def get_products(self, req: Any, ctx: RequestContext[Any]) -> dict[str, Any]:
        """Static stub catalogue for tenants without a ProposalManager."""
        del req, ctx
        return {
            "products": [
                {
                    "product_id": "globex-static-catalog-product",
                    "name": "Globex catalog stub",
                    "description": "Static catalog product served by the platform.",
                    "delivery_type": "non_guaranteed",
                    "publisher_properties": [
                        {"publisher_domain": "globex.example", "selection_type": "all"},
                    ],
                    "format_ids": [
                        {
                            "agent_url": "https://creative.adcontextprotocol.org/",
                            "id": "display_300x250",
                        },
                    ],
                    "pricing_options": [
                        {
                            "pricing_option_id": "po-cpm-default",
                            "pricing_model": "cpm",
                            "floor_price": 5.0,
                            "currency": "USD",
                        },
                    ],
                    "reporting_capabilities": {
                        "available_metrics": ["impressions"],
                        "available_reporting_frequencies": ["daily"],
                        "date_range_support": "date_range",
                        "supports_webhooks": False,
                        "expected_delay_minutes": 60,
                        "timezone": "UTC",
                    },
                    "delivery_measurement": {"provider": "internal"},
                },
            ],
        }

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del ctx
        idem_key = getattr(req, "idempotency_key", "unknown")
        return {
            "media_buy_id": f"mb_demo_{idem_key}",
            "status": "active",
            "packages": [],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del patch, ctx
        return {"media_buy_id": media_buy_id, "status": "active", "packages": []}

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del ctx
        creatives = getattr(req, "creatives", None) or []
        return {
            "creatives": [
                {
                    "creative_id": (
                        c.creative_id if hasattr(c, "creative_id") else c.get("creative_id")
                    ),
                    "approval_status": "approved",
                }
                for c in creatives
            ],
        }

    def get_media_buy_delivery(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        del ctx
        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }


class _DemoMultiTenantAccounts:
    """Demo AccountStore that maps the wire ``account_id`` to a tenant.

    Real adopters back this with a database lookup or read
    :func:`adcp.server.tenant_router.current_tenant` (set by
    :class:`SubdomainTenantMiddleware`). Kept intentionally simple here
    so the example focuses on the router wiring.
    """

    resolution = "explicit"

    def resolve(
        self,
        ref: dict[str, Any] | None = None,
        auth_info: AuthInfo | None = None,
    ) -> Account[Any]:
        ref = ref or {}
        account_id = str(ref.get("account_id", "tenant_acme:default"))
        # Convention: ``"<tenant_id>:<rest>"`` — extract the tenant.
        tenant_id = account_id.split(":", 1)[0]
        return Account(
            id=account_id,
            name=account_id,
            status="active",
            metadata={"tenant_id": tenant_id},
            auth_info=None,
        )


if __name__ == "__main__":
    # Mock-server URL — tenant_acme's MockProposalManager forwards to
    # this URL. Override via env for CI / local dev.
    mock_url = os.environ.get(
        "ADCP_MOCK_PROPOSAL_URL",
        "http://localhost:4500",
    )

    # Per-tenant ProposalManager mapping. tenant_acme gets a mock-
    # backed ProposalManager; tenant_globex falls through to its
    # platform's own ``get_products`` — both shapes coexist behind
    # one router.
    proposal_managers = {
        "tenant_acme": MockProposalManager(
            mock_upstream_url=mock_url,
            sales_specialism="sales-non-guaranteed",
        ),
        # tenant_globex: deliberately omitted — fall-through to
        # platform.get_products demonstrates per-tenant back-compat.
    }

    router = PlatformRouter(
        accounts=_DemoMultiTenantAccounts(),
        platforms={
            "tenant_acme": HelloDecisioningPlatform(),
            "tenant_globex": HelloDecisioningPlatform(),
        },
        proposal_managers=proposal_managers,
        capabilities=DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"],
            adcp=Adcp(
                major_versions=[3],
                idempotency=IdempotencySupported(
                    supported=True,
                    replay_ttl_seconds=86400,
                ),
            ),
            account=CapabilitiesAccount(supported_billing=["operator"]),
            media_buy=MediaBuy(supported_pricing_models=["cpm"]),
        ),
    )

    # Single-tenant adopters use the same shape:
    #
    #     PlatformRouter(
    #         accounts=...,
    #         platforms={"default": MyPlatform()},
    #         proposal_managers={"default": MyProposalManager(...)},
    #         capabilities=...,
    #     )
    serve(
        router,
        name="hello-proposal-manager",
        port=3001,
        auto_emit_completion_webhooks=False,
    )
