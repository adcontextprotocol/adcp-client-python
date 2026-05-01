"""Hello-seller — the smallest possible v6.0 DecisioningPlatform.

A minimal :class:`SalesPlatform` adopter showing the canonical surface:

* :class:`DecisioningCapabilities` declared on the class body
* :class:`SingletonAccounts` for the dev/single-tenant case
* Three platform methods (``get_products``, ``create_media_buy``,
  ``get_media_buy_delivery``) — all sync, sync return path

Run::

    uv run python examples/hello_seller.py

Then:

* MCP discovery: connect with any AdCP MCP buyer
* List tools: should advertise just the 3 implemented + the
  framework's protocol tools
* Call ``get_products``: returns one product
* Call ``create_media_buy``: returns the success envelope
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloSeller(DecisioningPlatform):
    """The canonical minimal v6.0 sales-non-guaranteed adopter.

    Implements the three sync methods every sales-* specialism
    requires for a buyer to discover, transact, and read delivery.
    Production sellers would add ``update_media_buy`` and
    ``sync_creatives`` to satisfy the full sales-non-guaranteed
    contract; this example focuses on the common-path subset that
    fits in one screen.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Return a single example product. Sync — no HITL."""
        return {
            "products": [
                {
                    "product_id": "display-rotation",
                    "name": "Display Rotation",
                    "description": "300x250 banner across our example properties",
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
                        "available_metrics": ["impressions", "spend"],
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
        """Sync fast path — accept the request and return a media_buy_id.

        Production sellers branch on a budget/policy check here and
        return :meth:`ctx.handoff_to_task(fn)` for HITL review (see
        ``examples/hello_seller_async_handoff.py``). Hello-seller
        accepts everything; reject obviously-broken budgets via
        :class:`AdcpError`.
        """
        # Pre-flight: reject zero-budget requests with a structured
        # error so buyers get a clear correction signal. Real sellers
        # check against a published floor; this just demonstrates the
        # AdcpError raise-and-project pattern.
        packages = self._get_packages(req)
        if not packages:
            raise AdcpError(
                "INVALID_REQUEST",
                message="At least one package is required",
                field="packages",
                recovery="correctable",
            )

        return {
            "media_buy_id": f"mb_{ctx.account.id}_{len(packages)}",
            "status": "active",
            "packages": [
                {
                    "package_id": f"pkg_{i}",
                    "product_id": pkg.get("product_id", "display-rotation"),
                    "pricing_option_id": pkg.get("pricing_option_id", "po-cpm-default"),
                }
                for i, pkg in enumerate(packages)
            ],
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync update — accept any patch as a no-op echo. The
        ``(media_buy_id, patch, ctx)`` signature mirrors the
        :class:`SalesPlatform` Protocol (D1 arg-projection — the
        framework's handler.py shim splits the wire request shape
        into separate kwargs)."""
        return {
            "media_buy_id": media_buy_id,
            "status": "active",
            "packages": [],
        }

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync creative review — auto-approve every submitted
        creative. Production sellers run S&P review here and either
        return mixed approved/pending rows, or hand off the entire
        batch via :meth:`ctx.handoff_to_task` for trafficker
        review (see the async-handoff example)."""
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
        """Stub delivery snapshot — flat zeros."""
        return {
            "deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }

    @staticmethod
    def _get_packages(req: Any) -> list[dict[str, Any]]:
        """Pull the wire ``packages`` array from the request, tolerating
        both Pydantic and dict shapes (the framework's typed dispatch
        gives Pydantic; tests / scripts may pass dicts)."""
        if hasattr(req, "packages"):
            packages = req.packages or []
            return [p.model_dump() if hasattr(p, "model_dump") else dict(p) for p in packages]
        if isinstance(req, dict):
            return list(req.get("packages") or [])
        return []


if __name__ == "__main__":
    # serve() builds the PlatformHandler, allocates the executor +
    # registry, validates the platform at boot, and starts the MCP
    # server. Default port 3001 over streamable-http; override via
    # ``serve(seller, port=...)``.
    #
    # ``auto_emit_completion_webhooks=False`` opts out of the F12
    # sync-completion webhook auto-emit so the example boots without
    # a ``webhook_sender``. Wire ``webhook_sender=`` in production so
    # buyers who register ``push_notification_config.url`` get
    # notifications.
    serve(HelloSeller(), name="hello-seller", auto_emit_completion_webhooks=False)
