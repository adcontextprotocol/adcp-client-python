"""Hello-proposal-manager — the v1 two-platform composition demo.

Wires a :class:`MockProposalManager` (proposal side) and a trivial
:class:`DecisioningPlatform` (execution side) together. The
:class:`MockProposalManager` forwards ``get_products`` requests to a
running mock-server (``bin/adcp.js mock-server sales-non-guaranteed``);
the platform handles ``create_media_buy`` directly without consulting
the mock-server.

This is the on-ramp shape for adopters whose proposal logic isn't
ready yet but who already have an adapter for their upstream:

* The mock-server provides product fixtures + stub recipes.
* The platform's ``create_media_buy`` translates buyer requests into
  upstream calls (here, it's a no-op stub).
* As the adopter writes real proposal logic, they replace
  :class:`MockProposalManager` with their own
  :class:`ProposalManager` subclass — incrementally, one slice at a
  time.

**Prerequisite:** start the mock-server before running this example::

    npx -y adcp-mock-server@latest sales-non-guaranteed --port 4500

Then::

    uv run python examples/hello_proposal_manager.py

The server answers ``get_products`` by forwarding to the mock-server
on port 4500; ``create_media_buy`` runs entirely in this process.
Tail the mock-server log to see the proposal-side traffic; tail this
process's log to see the execution-side traffic.

See ``docs/proposals/product-architecture.md`` for the full design
context — § "The two-platform composition" + § "Shape 3 — Mock-backed".
"""

from __future__ import annotations

import os
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    MockProposalManager,
    RequestContext,
    SalesPlatform,
    SingletonAccounts,
    serve,
)
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
    Does NOT implement ``get_products`` — the wired
    :class:`MockProposalManager` handles that surface.

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
    accounts = SingletonAccounts(account_id="hello-proposal-manager-acct")

    def create_media_buy(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Stub create_media_buy — returns a synthetic media_buy_id.

        Production: this is where the adopter's adapter code calls the
        upstream API (the recipe attached to the request's products
        carries the upstream-specific config the adapter consumes).
        """
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


if __name__ == "__main__":
    # Mock-server URL — adopters in production point this at the
    # appropriate `bin/adcp.js mock-server <specialism>` instance, or
    # at a fixture-server in their own infra. Override via env var
    # for CI / local dev.
    mock_url = os.environ.get(
        "ADCP_MOCK_PROPOSAL_URL",
        "http://localhost:4500",
    )

    # The proposal manager — forwards get_products to the running
    # mock-server. Adopters replace this with their own ProposalManager
    # subclass as their proposal logic comes online.
    proposal_manager = MockProposalManager(
        mock_upstream_url=mock_url,
        sales_specialism="sales-non-guaranteed",
    )

    # serve() composes the two platforms. The dispatcher routes
    # get_products to the proposal manager and create_media_buy /
    # update_media_buy / etc. to the platform.
    serve(
        HelloDecisioningPlatform(),
        proposal_manager=proposal_manager,
        name="hello-proposal-manager",
        port=3001,
        auto_emit_completion_webhooks=False,
    )
