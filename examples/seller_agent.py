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
from adcp.server.helpers import valid_actions_for_status
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
# Used when no account_id is present; single-tenant demo shortcut.
# Real sellers must scope directives and tasks by account_id.
_DEFAULT_ACCOUNT_ID = "__default__"

# Test-controller state (force_*/seed_* scenarios only)
plans: dict[str, dict[str, Any]] = {}
# Seeded creative formats keyed by the string format ID the storyboard supplies.
# list_creative_formats merges these in so storyboard references resolve.
seeded_creative_formats: dict[str, dict[str, Any]] = {}
# Single-shot directives registered by force_create_media_buy_arm; keyed by account_id.
pending_directives: dict[str, dict[str, Any]] = {}
# Tasks registered when create_media_buy consumes a 'submitted' directive; keyed by task_id.
pending_task_completions: dict[str, dict[str, Any]] = {}

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
            idempotency={"supported": False},
            compliance_testing={
                # AdCP 3.0.1's capabilities-response schema constrains this
                # enum to the original six scenarios. The new force_* and
                # seed_* scenarios (added to comply-test-controller-request
                # in 3.0.1) live on the dynamic list_scenarios response and
                # are reported there — not advertised here. Once the
                # capabilities schema's enum catches up, the rest land too.
                # force_session_status is schema-allowed even for media_buy
                # sellers; DemoStore provides a stub so list_scenarios
                # includes it and the storyboard runner's controller
                # detection check succeeds.
                "scenarios": [
                    "force_account_status",
                    "force_media_buy_status",
                    "force_creative_status",
                    "force_session_status",
                    "simulate_delivery",
                    "simulate_budget_spend",
                ],
            },
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
        account_id = (params.get("account") or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        directive = pending_directives.pop(account_id, None)
        if directive:
            arm = directive.get("arm")
            if arm == "input-required":
                # CreateMediaBuyInputRequired shape per AdCP spec.
                return {"reason": "APPROVAL_REQUIRED"}
            if arm == "submitted":
                # CreateMediaBuyResponse (submitted-task envelope) per AdCP spec.
                task_id = directive.get("task_id")
                if task_id:
                    pending_task_completions[task_id] = {
                        "state": "submitted",
                        "account_id": account_id,
                    }
                resp: dict[str, Any] = {"status": "submitted"}
                if task_id:
                    resp["task_id"] = task_id
                if directive.get("message"):
                    resp["message"] = directive["message"]
                return resp

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
            # Inspect per-package measurement_terms for aggressive viewability.
            # The storyboard's create_media_buy_aggressive_terms step sends
            # measurement_terms.viewability_threshold > 80 per package and expects
            # TERMS_REJECTED. viewability_threshold is a storyboard demo convention
            # (additionalProperties on measurement-terms.json); real sellers should
            # map their own rejection criteria here.
            pkg_terms = pkg.get("measurement_terms") or {}
            if (pkg_terms.get("viewability_threshold") or 0) > 80:
                return adcp_error(
                    "TERMS_REJECTED",
                    "Viewability threshold exceeds maximum supported value of 80%",
                    field="measurement_terms.viewability_threshold",
                    recovery="correctable",
                )

            pkg_obj: dict[str, Any] = {
                "package_id": f"pkg-{uuid.uuid4().hex[:8]}",
                "product_id": product_id,
                "pricing_option_id": pkg.get("pricing_option_id"),
                "budget": pkg.get("budget"),
            }
            # Persist overlay and creative fields so get_media_buys can round-trip them.
            for field in ("targeting_overlay", "creative_assignments", "creatives"):
                if pkg.get(field) is not None:
                    pkg_obj[field] = pkg[field]
            packages.append(pkg_obj)

        has_creatives = any(
            pkg.get("creative_assignments") or pkg.get("creatives") for pkg in params["packages"]
        )
        status = "active" if has_creatives else "pending_creatives"

        mb_id = f"mb-{uuid.uuid4().hex[:8]}"
        media_buys[mb_id] = {
            "status": status,
            "currency": "USD",
            "packages": packages,
            "revision": 1,
        }
        # Pull valid_actions from the SDK's authoritative state machine —
        # tracks any future spec churn without manual list maintenance.
        return media_buy_response(
            mb_id,
            packages,
            status=status,
            valid_actions=valid_actions_for_status(status) or None,
        )

    async def get_media_buys(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        requested_ids = params.get("media_buy_ids")
        results = []
        for mb_id, mb in media_buys.items():
            if requested_ids and mb_id not in requested_ids:
                continue
            total_budget = sum((pkg.get("budget") or 0) for pkg in mb.get("packages", []))
            results.append(
                {
                    "media_buy_id": mb_id,
                    "status": mb["status"],
                    "currency": mb.get("currency", "USD"),
                    "packages": mb.get("packages", []),
                    "total_budget": total_budget,
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
            existing_pkgs = {p["package_id"]: p for p in mb.get("packages", [])}
            existing_pkg_ids = set(existing_pkgs.keys())
            for pkg_update in params["packages"]:
                pkg_id = pkg_update.get("package_id")
                if pkg_id and pkg_id not in existing_pkg_ids:
                    return adcp_error(
                        "PACKAGE_NOT_FOUND",
                        f"Package '{pkg_id}' not found in media buy {mb_id}",
                        field="package_id",
                    )
                # Apply targeting and creative field deltas to persisted package state
                # so get_media_buys can round-trip property_list and overlay updates.
                if pkg_id and pkg_id in existing_pkgs:
                    persisted = existing_pkgs[pkg_id]
                    for field in ("targeting_overlay", "creative_assignments", "creatives"):
                        if field in pkg_update:
                            persisted[field] = pkg_update[field]

        status = mb["status"]
        if status == "pending_creatives" and params.get("packages"):
            if any(
                pkg.get("creative_assignments") or pkg.get("creatives")
                for pkg in params["packages"]
            ):
                mb["status"] = "active"
                status = "active"
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
        return update_media_buy_response(
            mb_id,
            status=mb["status"],
            revision=mb["revision"],
            valid_actions=valid_actions_for_status(mb["status"]) or None,
        )

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
        all_formats = all_formats + list(seeded_creative_formats.values())
        filter_ids = params.get("format_ids")
        if filter_ids:
            wanted = {(fid.get("agent_url"), fid["id"]) for fid in filter_ids if "id" in fid}
            formats = [
                f
                for f in all_formats
                if (f["format_id"].get("agent_url"), f["format_id"]["id"]) in wanted
            ]
        else:
            formats = all_formats
        return creative_formats_response(formats)

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
        # Transition any media buys waiting on creatives to pending_start
        # now that creatives are approved (storyboard creative_fate_after_sync
        # asserts this). Real sellers would scope by media_buy_id linkage —
        # the example uses a single-tenant simplification.
        for mb in media_buys.values():
            if mb.get("status") == "pending_creatives":
                mb["status"] = "pending_start"
                mb["revision"] = mb.get("revision", 1) + 1
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
        reported_spend: dict[str, Any] | None = None,
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

    async def force_session_status(
        self,
        session_id: str,
        status: str,
        termination_reason: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        # DemoSeller has no SI session state; return a canned transition so
        # the storyboard runner's controller-detection probe succeeds and the
        # force_session_status storyboard can run (it will simply report the
        # canned previous_state).
        return {"previous_state": "active", "current_state": status}

    async def force_create_media_buy_arm(
        self,
        arm: str,
        task_id: str | None = None,
        message: str | None = None,
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        account_id = (account or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        pending_directives[account_id] = {"arm": arm, "task_id": task_id, "message": message}
        forced: dict[str, Any] = {"arm": arm}
        if arm == "submitted" and task_id:
            forced["task_id"] = task_id
        return {"success": True, "forced": forced}

    async def force_task_completion(
        self,
        task_id: str,
        result: dict[str, Any],
        *,
        account: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        task = pending_task_completions.get(task_id)
        if task is None:
            raise TestControllerError("NOT_FOUND", f"Task {task_id} not found")
        caller_id = (account or {}).get("account_id") or _DEFAULT_ACCOUNT_ID
        if task.get("account_id", _DEFAULT_ACCOUNT_ID) != caller_id:
            raise TestControllerError("NOT_FOUND", f"Task {task_id} not found")
        prev = task.get("state", "submitted")
        if prev == "completed":
            if task.get("result") != result:
                raise TestControllerError(
                    "INVALID_TRANSITION",
                    "Task already completed with different result",
                    current_state="completed",
                )
            return {
                "success": True,
                "previous_state": task.get("previous_state", "submitted"),
                "current_state": "completed",
            }
        pending_task_completions[task_id] = {
            **task,
            "state": "completed",
            "result": result,
            "previous_state": prev,
        }
        return {"success": True, "previous_state": prev, "current_state": "completed"}

    async def seed_product(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        pid = product_id or data.get("product_id") or f"seeded-{uuid.uuid4().hex[:8]}"
        data["product_id"] = pid
        # Ensure schema-required fields are present so downstream validation passes
        # even when the runner sends a minimal fixture with only product_id.
        data.setdefault("name", pid)
        data.setdefault("description", "")
        data.setdefault("delivery_type", "non_guaranteed")
        data.setdefault("publisher_properties", [])
        data.setdefault("format_ids", [])
        data.setdefault("pricing_options", [])
        data.setdefault(
            "reporting_capabilities",
            {
                "available_metrics": [],
                "available_reporting_frequencies": [],
                "date_range_support": "date_range",
                "supports_webhooks": False,
                "expected_delay_minutes": 0,
                "timezone": "UTC",
            },
        )
        data.setdefault("delivery_measurement", {"provider": "internal"})
        for i, p in enumerate(PRODUCTS):
            if p.get("product_id") == pid:
                PRODUCTS[i] = data
                return {"product_id": pid}
        PRODUCTS.append(data)
        return {"product_id": pid}

    async def seed_pricing_option(
        self,
        fixture: dict[str, Any] | None = None,
        product_id: str | None = None,
        pricing_option_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        po_id = (
            pricing_option_id
            or data.get("pricing_option_id")
            or f"po-seeded-{uuid.uuid4().hex[:8]}"
        )
        data["pricing_option_id"] = po_id
        for prod in PRODUCTS:
            if product_id and prod.get("product_id") != product_id:
                continue
            options: list[dict[str, Any]] = prod.setdefault("pricing_options", [])
            for i, opt in enumerate(options):
                if opt.get("pricing_option_id") == po_id:
                    options[i] = data
                    return {"pricing_option_id": po_id}
            options.append(data)
            return {"pricing_option_id": po_id}
        raise TestControllerError("NOT_FOUND", f"Product '{product_id}' not found")

    async def seed_creative(
        self,
        fixture: dict[str, Any] | None = None,
        creative_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        cid = creative_id or data.get("creative_id") or f"c-seeded-{uuid.uuid4().hex[:8]}"
        data["creative_id"] = cid
        creatives[cid] = data
        return {"creative_id": cid}

    async def seed_plan(
        self,
        fixture: dict[str, Any] | None = None,
        plan_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        pid = plan_id or data.get("plan_id") or f"plan-seeded-{uuid.uuid4().hex[:8]}"
        data["plan_id"] = pid
        plans[pid] = data
        return {"plan_id": pid}

    async def seed_media_buy(
        self,
        fixture: dict[str, Any] | None = None,
        media_buy_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        mb_id = media_buy_id or data.get("media_buy_id") or f"mb-seeded-{uuid.uuid4().hex[:8]}"
        data["media_buy_id"] = mb_id
        data.setdefault("status", "active")
        data.setdefault("currency", "USD")
        data.setdefault("packages", [])
        media_buys[mb_id] = data
        return {"media_buy_id": mb_id}

    async def seed_creative_format(
        self,
        fixture: dict[str, Any] | None = None,
        format_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        data = dict(fixture or {})
        fid = format_id or (data.get("format_id") or {}).get("id") or f"fmt-seeded-{uuid.uuid4().hex[:8]}"
        data.setdefault("format_id", {"agent_url": AGENT_URL, "id": fid})
        data.setdefault("name", fid)
        data.setdefault("renders", [])
        data.setdefault("assets", [])
        seeded_creative_formats[fid] = data
        return {"format_id": fid}


if __name__ == "__main__":
    serve(
        DemoSeller(),
        name="demo-seller",
        port=PORT,
        test_controller=DemoStore(),
    )
