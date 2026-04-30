#!/usr/bin/env python3
"""Reference ADCPHandler seller agent.

A complete, runnable seller for the AdCP media_buy_seller storyboard
(9 steps, all core tools). Used as the reference for the seller,
generative-seller, and retail-media skills.

Run:
    python examples/seller_agent.py

Validate:
    npx -y -p @adcp/client adcp storyboard run \\
        http://localhost:3001/mcp media_buy_seller --json
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from adcp.server import (
    ADCPHandler,
    adcp_error,
    cancel_media_buy_response,
    serve,
)
from adcp.server.responses import (
    capabilities_response,
    creative_formats_response,
    delivery_response,
    media_buy_response,
    media_buys_response,
    products_response,
    sync_accounts_response,
    sync_creatives_response,
    sync_governance_response,
    update_media_buy_response,
)
from adcp.server.test_controller import TestControllerError, TestControllerStore

PORT = int(os.environ.get("ADCP_PORT") or os.environ.get("PORT") or 3001)
AGENT_URL = f"http://localhost:{PORT}/mcp"

accounts: dict[str, dict[str, Any]] = {}
media_buys: dict[str, dict[str, Any]] = {}
creatives: dict[str, dict[str, Any]] = {}
proposals: dict[str, dict[str, Any]] = {}

PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "premium-homepage",
        "name": "Homepage Takeover",
        "description": "Full-page homepage placement with 100% SOV",
        "delivery_type": "guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": AGENT_URL, "id": "display_970x250"}],
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-homepage",
                "pricing_model": "cpm",
                "floor_price": 15.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
    {
        "product_id": "run-of-site",
        "name": "Run of Site Display",
        "description": "300x250 display ads across example.com",
        "delivery_type": "non_guaranteed",
        "publisher_properties": [{"publisher_domain": "example.com", "selection_type": "all"}],
        "format_ids": [{"agent_url": AGENT_URL, "id": "display_300x250"}],
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-ros",
                "pricing_model": "cpm",
                "floor_price": 5.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],
            "date_range_support": "date_range",
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {"provider": "internal"},
    },
]


class DemoSeller(ADCPHandler):
    async def get_adcp_capabilities(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        return capabilities_response(
            ["media_buy"],
            compliance_testing={
                "scenarios": [
                    "force_account_status",
                    "force_media_buy_status",
                    "force_creative_status",
                    "simulate_delivery",
                    "simulate_budget_spend",
                ],
            },
            # adcp.idempotency is required by spec (get-adcp-capabilities-response.json)
            idempotency={"supported": False},
        )

    async def sync_accounts(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for acct in params.get("accounts", []):
            account_id = f"acct-{uuid.uuid4().hex[:8]}"
            accounts[account_id] = {
                "status": "active",
                "brand": acct.get("brand"),
                "operator": acct.get("operator"),
            }
            results.append(
                {
                    "account_id": account_id,
                    "brand": acct.get("brand"),
                    "operator": acct.get("operator"),
                    "action": "created",
                    "status": "active",
                    "account_scope": "operator_brand",
                }
            )
        return sync_accounts_response(results)

    async def sync_governance(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for entry in params.get("accounts", []):
            acct_ref = entry.get("account", {})
            agents = entry.get("governance_agents", [])
            results.append(
                {
                    "account": acct_ref,
                    "status": "synced",
                    "governance_agents": [
                        {"url": a.get("url"), "categories": a.get("categories", [])} for a in agents
                    ],
                }
            )
        return sync_governance_response(results)

    async def get_products(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        if params.get("buying_mode") == "refine":
            proposal = params.get("proposal", {}) or {}
            proposal_id = proposal.get("proposal_id") or f"prop-{uuid.uuid4().hex[:8]}"
            incoming_packages = proposal.get("packages", []) or []
            proposals[proposal_id] = {
                "status": "draft",
                "packages": incoming_packages,
            }
            # proposal.json requires: proposal_id, name, allocations (minItems: 1).
            # Each allocation requires product_id + allocation_percentage (sum to 100).
            if incoming_packages:
                even_split = round(100 / len(incoming_packages), 2)
                allocations = [
                    {
                        "product_id": p["product_id"],
                        "allocation_percentage": even_split,
                    }
                    for p in incoming_packages
                ]
            else:
                allocations = [
                    {
                        "product_id": PRODUCTS[0]["product_id"],
                        "allocation_percentage": 100.0,
                    }
                ]
            return {
                **products_response(PRODUCTS),
                "proposals": [
                    {
                        "proposal_id": proposal_id,
                        "name": proposal.get("name", "Draft proposal"),
                        "proposal_status": "draft",
                        "allocations": allocations,
                    }
                ],
            }
        return products_response(PRODUCTS)

    async def create_media_buy(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        if not params.get("packages"):
            return adcp_error(
                "INVALID_REQUEST",
                "At least one package required",
                field="packages",
            )

        valid_ids = {p["product_id"] for p in PRODUCTS}
        packages = []
        for pkg in params["packages"]:
            product_id = pkg.get("product_id")
            if product_id not in valid_ids:
                return adcp_error(
                    "PRODUCT_NOT_FOUND",
                    f"Product '{product_id}' not found",
                    field="product_id",
                    suggestion="Use get_products to discover available products",
                )
            packages.append(
                {
                    "package_id": f"pkg-{uuid.uuid4().hex[:8]}",
                    "product_id": product_id,
                    "pricing_option_id": pkg.get("pricing_option_id"),
                    "budget": pkg.get("budget"),
                }
            )

        has_creatives = any(
            pkg.get("creatives") or pkg.get("creative_assignments")
            for pkg in params["packages"]
        )
        status = "active" if has_creatives else "pending_creatives"
        mb_id = f"mb-{uuid.uuid4().hex[:8]}"
        media_buys[mb_id] = {
            "status": status,
            "currency": "USD",
            "packages": packages,
            "revision": 1,
        }
        return media_buy_response(mb_id, packages, status=status)

    async def get_media_buys(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        requested_ids = params.get("media_buy_ids")
        results = []
        for mb_id, mb in media_buys.items():
            if requested_ids and mb_id not in requested_ids:
                continue
            pkgs = mb.get("packages", [])
            results.append(
                {
                    "media_buy_id": mb_id,
                    "status": mb["status"],
                    "currency": mb.get("currency", "USD"),
                    "total_budget": sum(pkg.get("budget") or 0 for pkg in pkgs),  # None for flat-rate pkgs
                    "packages": pkgs,
                }
            )
        return media_buys_response(results)

    async def update_media_buy(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        mb_id = params.get("media_buy_id")
        mb = media_buys.get(mb_id) if mb_id else None
        if not mb or not mb_id:
            return adcp_error("MEDIA_BUY_NOT_FOUND", f"Media buy {mb_id} not found")

        if params.get("revision") and params["revision"] != mb.get("revision", 1):
            return adcp_error("CONFLICT", "Revision mismatch - refetch and retry")

        if params.get("packages"):
            existing_pkg_ids = {pkg["package_id"] for pkg in mb.get("packages", [])}
            for pkg_update in params["packages"]:
                if pkg_update.get("package_id") not in existing_pkg_ids:
                    return adcp_error(
                        "PACKAGE_NOT_FOUND",
                        f"Package {pkg_update.get('package_id')!r} not found in media buy {mb_id}",
                        field="packages",
                    )

        status = mb["status"]
        if params.get("paused") is True and status == "active":
            mb["status"] = "paused"
        elif params.get("paused") is False and status == "paused":
            mb["status"] = "active"
        elif params.get("canceled") is True:
            if status in ("completed", "rejected", "canceled"):
                return adcp_error("NOT_CANCELLABLE", f"Cannot cancel a {status} media buy")
            mb["status"] = "canceled"
            return cancel_media_buy_response(mb_id, "buyer")

        mb["revision"] = mb.get("revision", 1) + 1
        return update_media_buy_response(mb_id, status=mb["status"], revision=mb["revision"])

    async def list_creative_formats(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        all_formats: list[dict[str, Any]] = [
            {
                "format_id": {
                    "agent_url": AGENT_URL,
                    "id": "display_300x250",
                },
                "name": "Display 300x250",
                # role is required; width/height must be nested under dimensions
                "renders": [{"role": "primary", "dimensions": {"width": 300, "height": 250}}],
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "image",
                        "asset_type": "image",
                        "required": True,
                        "accepted_media_types": [
                            "image/png",
                            "image/jpeg",
                        ],
                    }
                ],
            },
            {
                "format_id": {
                    "agent_url": AGENT_URL,
                    "id": "display_970x250",
                },
                "name": "Display 970x250",
                # role is required; width/height must be nested under dimensions
                "renders": [{"role": "primary", "dimensions": {"width": 970, "height": 250}}],
                "assets": [
                    {
                        "item_type": "individual",
                        "asset_id": "image",
                        "asset_type": "image",
                        "required": True,
                        "accepted_media_types": [
                            "image/png",
                            "image/jpeg",
                        ],
                    }
                ],
            },
        ]
        # format_ids is optional per spec: absent means return all formats
        # match on compound (agent_url, id) key — correct for multi-agent deployments
        requested = params.get("format_ids")
        if requested:
            keys = {(r["agent_url"], r["id"]) for r in requested}
            all_formats = [
                fmt for fmt in all_formats
                if (fmt["format_id"]["agent_url"], fmt["format_id"]["id"]) in keys
            ]
        return creative_formats_response(all_formats)

    async def sync_creatives(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        results = []
        for c in params.get("creatives", []):
            creative_id = c.get("creative_id") or f"c-{uuid.uuid4().hex[:8]}"
            creatives[creative_id] = {**c, "status": "approved"}
            results.append(
                {
                    "creative_id": creative_id,
                    "action": "created",
                    "status": "approved",
                }
            )
        return sync_creatives_response(results)

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: Any = None
    ) -> dict[str, Any]:
        requested_ids = params.get("media_buy_ids", [])
        deliveries = []
        for mb_id in requested_ids:
            if mb_id in media_buys:
                deliveries.append(
                    {
                        "media_buy_id": mb_id,
                        "status": "active",
                        "totals": {
                            "impressions": 45000,
                            "clicks": 680,
                            "spend": 540.00,
                        },
                        "by_package": [],
                    }
                )
        return delivery_response(
            deliveries,
            reporting_period={
                "start": "2026-04-01T00:00:00Z",
                "end": "2026-04-09T23:59:59Z",
            },
        )


class DemoStore(TestControllerStore):
    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        acct = accounts.get(account_id)
        if not acct:
            raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")
        prev = acct["status"]
        acct["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_media_buy_status(
        self,
        media_buy_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        mb = media_buys.get(media_buy_id)
        if not mb:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        prev = mb["status"]
        if prev in ("completed", "rejected", "canceled"):
            raise TestControllerError(
                "INVALID_TRANSITION",
                f"Cannot transition from {prev}",
                current_state=prev,
            )
        mb["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_creative_status(
        self,
        creative_id: str,
        status: str,
        rejection_reason: str | None = None,
    ) -> dict[str, Any]:
        c = creatives.get(creative_id)
        if not c:
            raise TestControllerError("NOT_FOUND", f"Creative {creative_id} not found")
        prev = c.get("status", "unknown")
        if prev == "archived":
            raise TestControllerError(
                "INVALID_TRANSITION",
                "Cannot transition from archived",
                current_state=prev,
            )
        c["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def simulate_delivery(
        self,
        media_buy_id: str,
        impressions: int | None = None,
        clicks: int | None = None,
        conversions: int | None = None,
        reported_spend: float | None = None,
    ) -> dict[str, Any]:
        if media_buy_id not in media_buys:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        simulated: dict[str, Any] = {"media_buy_id": media_buy_id}
        if impressions is not None:
            simulated["impressions"] = impressions
        if clicks is not None:
            simulated["clicks"] = clicks
        if conversions is not None:
            simulated["conversions"] = conversions
        if reported_spend is not None:
            simulated["reported_spend"] = reported_spend
        return {"simulated": simulated, "cumulative": simulated}

    async def simulate_budget_spend(
        self,
        spend_percentage: float,
        account_id: str | None = None,
        media_buy_id: str | None = None,
    ) -> dict[str, Any]:
        return {"simulated": {"spend_percentage": spend_percentage}}


if __name__ == "__main__":
    serve(
        DemoSeller(),
        name="demo-seller",
        port=PORT,
        test_controller=DemoStore(),
    )
