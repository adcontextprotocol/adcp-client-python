"""Hello-mock-seller — mock-mode upstream URL routing demo.

The shape adopters use to enable spec-conformance / agent-development
without standing up a real upstream:

- ``mode='live'`` / ``mode='sandbox'``: requests go to the adopter's
  production URL (``HelloMockPlatform.upstream_url``). Sandbox is the
  adopter's own test infra at the same URL with different credentials —
  the adopter's ``DecisioningPlatform`` code path runs end-to-end.
- ``mode='mock'``: requests go to ``account.metadata['mock_upstream_url']``.
  The adapter code is unchanged; only the upstream URL changes per
  request. Adopters point this at a per-specialism mock-server fixture
  (e.g. ``bin/adcp.js mock-server sales-non-guaranteed`` on port 4500)
  for spec-compliance storyboards or local agent development.

The mock-server lifecycle is **not** managed by the SDK. Adopters or
CI start it as needed (``bin/adcp.js mock-server <specialism>``) and
populate the URL on the account's metadata in ``AccountStore.resolve``.

Run::

    uv run python examples/hello_mock_seller.py

The example boots without hitting any of the four URLs — it's a code-
shape demo. Production adapters call ``client.get(...)`` /
``client.post(...)`` after :meth:`upstream_for`; here we just inspect
the URL the framework selected so the four-account routing is visible
on stdout.

See ``docs/proposals/lifecycle-state-and-sandbox-authority.md`` for
the three-mode design.
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    ExplicitAccounts,
    RequestContext,
    SalesPlatform,
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
from adcp.decisioning.types import Account

# Pre-built account roster. Each Account demonstrates one routing case
# the framework's upstream_for() enforces. Adopters in production load
# accounts from a DB / config service via their AccountStore.resolve;
# ExplicitAccounts(loader=...) is the simplest fit for a fixed roster.
_ACCOUNTS: dict[str, Account[Any]] = {
    "acct_live": Account(
        id="acct_live",
        mode="live",
        name="Live tenant",
        status="active",
    ),
    "acct_sandbox": Account(
        id="acct_sandbox",
        mode="sandbox",
        name="Sandbox tenant",
        status="active",
    ),
    "acct_mock_a": Account(
        id="acct_mock_a",
        mode="mock",
        name="Mock fixture A",
        status="active",
        metadata={"mock_upstream_url": "http://localhost:4500"},
    ),
    "acct_mock_b": Account(
        id="acct_mock_b",
        mode="mock",
        name="Mock fixture B",
        status="active",
        metadata={"mock_upstream_url": "http://localhost:4501"},
    ),
}


def _load_account(account_id: str) -> Account[Any]:
    from adcp.decisioning import AdcpError

    if account_id not in _ACCOUNTS:
        raise AdcpError(
            "ACCOUNT_NOT_FOUND",
            message=f"unknown account_id={account_id!r}",
            recovery="terminal",
            field="account.account_id",
        )
    return _ACCOUNTS[account_id]


class HelloMockPlatform(DecisioningPlatform, SalesPlatform):
    """Single platform demonstrating the four mock-mode routing cases.

    All four accounts run through the same adapter code; the framework
    selects the upstream URL at :meth:`upstream_for` time based on
    ``ctx.account.mode``:

    - acct_live    → ``HelloMockPlatform.upstream_url``
    - acct_sandbox → ``HelloMockPlatform.upstream_url``
    - acct_mock_a  → ``http://localhost:4500``
    - acct_mock_b  → ``http://localhost:4501``

    Production adapters (GAM, Kevel, FreeWheel, Prebid) declare
    ``upstream_url`` as the canonical production URL — fixed per
    platform. Per-tenant routing flows through ``ctx.auth_info`` and
    ``ctx.account.metadata``, never through ``upstream_url``.
    """

    upstream_url = "https://example-ad-server.invalid/api"

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
    accounts = ExplicitAccounts(loader=_load_account)

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        # The adapter's view of the world is identical across all four
        # accounts — same upstream_for() call, same client API, same
        # business logic. Only the resolved base_url differs.
        client = self.upstream_for(ctx)

        # Demo-only: surface the routed URL so the four-account routing
        # is visible from a single tool call. Real adapters call
        # ``await client.get('/v1/products', ...)`` here and project the
        # upstream JSON onto the ``products`` schema.
        return {
            "products": [
                {
                    "product_id": "demo-product",
                    "name": f"Mock-routed for {ctx.account.id}",
                    "description": (
                        f"Adapter pointed at {client._base_url} "
                        f"(account.mode={ctx.account.mode})"
                    ),
                    "delivery_type": "non_guaranteed",
                    "publisher_properties": [
                        {"publisher_domain": "example.com", "selection_type": "all"},
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
        # Same routing behavior — the client points at the live URL for
        # acct_live / acct_sandbox, and at the per-account mock URL
        # for acct_mock_a / acct_mock_b.
        _ = self.upstream_for(ctx)
        return {
            "media_buy_id": f"mb_{ctx.account.id}_demo",
            "status": "active",
            "packages": [],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        return {"media_buy_id": media_buy_id, "status": "active", "packages": []}

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
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
        return {
            "media_buy_deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }


if __name__ == "__main__":
    # Default port 3001. Buyers send ``account.account_id`` ∈
    # {acct_live, acct_sandbox, acct_mock_a, acct_mock_b} on requests;
    # the framework resolves through ExplicitAccounts.loader and threads
    # the account onto ctx so upstream_for() picks the right URL.
    serve(
        HelloMockPlatform(),
        name="hello-mock-seller",
        port=3001,
        auto_emit_completion_webhooks=False,
    )
