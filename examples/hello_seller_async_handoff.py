"""Hybrid v6.0 DecisioningPlatform — sync fast path + HITL handoff
+ AdcpError correctable rejection.

Demonstrates the three return shapes a single ``create_media_buy``
method can produce:

1. **Sync success** — `return CreateMediaBuySuccessResponse(...)` /
   ``dict``. Framework projects to the wire success envelope.
2. **AdcpError raise** — `raise AdcpError("BUDGET_TOO_LOW", ...)`.
   Framework projects to the wire ``adcp_error`` envelope with
   ``recovery: 'correctable'`` so the buyer retries with the fixed
   field.
3. **TaskHandoff** — `return ctx.handoff_to_task(fn)`. Framework
   allocates a task_id, returns the wire ``Submitted`` envelope to
   the buyer immediately, runs ``fn`` in the background, persists
   the terminal artifact via the registry. Buyer polls
   ``tasks/get`` (or receives the webhook).

Branch per-call: programmatic remnant goes sync, guaranteed inventory
goes through trafficker review, aggressive budgets get rejected.

Run::

    uv run python examples/hello_seller_async_handoff.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SalesResult,
    SingletonAccounts,
    TaskHandoffContext,
    serve,
)

# Tunable thresholds — keep at top-level so the demo is easy to tweak.
_MIN_VIABLE_BUDGET_CPM = 0.50  # USD per thousand
_HITL_REVIEW_THRESHOLD = 50_000.0  # buys above this go through review


class HelloSellerHybrid(DecisioningPlatform):
    """Adopter that mixes sync fast-path, AdcpError rejection, and
    TaskHandoff in a single ``create_media_buy`` body.

    The sync methods (``get_products``, ``get_media_buy_delivery``)
    stay sync. The hybrid path is on the mutating tools that may
    need HITL review.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        channels=["display"],
        pricing_models=["cpm"],
    )
    accounts = SingletonAccounts(account_id="hello-hybrid")

    def get_products(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        return {
            "products": [
                {
                    "product_id": "display-rotation",
                    "name": "Display Rotation",
                    "description": (
                        "Hybrid product — small budgets accept sync, " "large budgets go to review"
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
                            "floor_price": _MIN_VIABLE_BUDGET_CPM,
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
    ) -> SalesResult[dict[str, Any]]:
        """Hybrid: branch per-call between sync, AdcpError, and handoff.

        :returns: Either a :class:`dict` (sync success), or
            :class:`TaskHandoff` returned from
            :meth:`ctx.handoff_to_task`. Type alias
            :data:`SalesResult` covers both arms.

        :raises AdcpError: when the budget is below the seller's
            minimum viable threshold. Buyer fixes ``total_budget``
            and retries (``recovery='correctable'``).
        """
        total_budget = self._extract_total_budget(req)

        # Arm 1: budget below floor → AdcpError correctable rejection.
        if total_budget < _MIN_VIABLE_BUDGET_CPM:
            raise AdcpError(
                "BUDGET_TOO_LOW",
                message=(
                    f"total_budget {total_budget} USD below minimum "
                    f"viable {_MIN_VIABLE_BUDGET_CPM} USD"
                ),
                field="total_budget",
                recovery="correctable",
                suggestion=(
                    f"Increase total_budget to at least "
                    f"{_MIN_VIABLE_BUDGET_CPM} USD to engage trafficking."
                ),
            )

        # Arm 2: large buy → handoff for trafficker review.
        if total_budget >= _HITL_REVIEW_THRESHOLD:
            return ctx.handoff_to_task(
                self._async_trafficker_review,
            )

        # Arm 3: small/medium buy → sync acceptance.
        return {
            "media_buy_id": f"mb_sync_{ctx.account.id}_{int(total_budget)}",
            "status": "active",
            "packages": self._echo_packages(req),
        }

    def update_media_buy(
        self,
        media_buy_id: str,
        patch: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync update — accept any patch."""
        return {"media_buy_id": media_buy_id, "status": "active", "packages": []}

    def sync_creatives(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Sync auto-approve — production would handoff for S&P review
        when a new buyer submits unfamiliar creative."""
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
            "deliveries": [
                {
                    "media_buy_id": getattr(req, "media_buy_id", "mb_unknown"),
                    "totals": {"impressions": 0, "spend": 0.0},
                },
            ],
        }

    # ---- Handoff fn ----

    async def _async_trafficker_review(
        self,
        task_ctx: TaskHandoffContext,
    ) -> dict[str, Any]:
        """Background fn the framework runs after the Submitted
        envelope returns. Adopters wire this to their own queue /
        Slack / approval system; here we simulate a brief review and
        return the success.

        ``task_ctx.id`` is framework-allocated BEFORE this fn runs —
        adopters persist it to their queue so the trafficker's
        approve/reject action can call back into the registry.

        ``task_ctx.update(progress)`` writes the progress payload AND
        transitions the task to ``working`` state on first call.
        Registry write failures are suppressed (logged at WARNING with
        traceback) so a transient registry hiccup doesn't abort the
        handoff fn — buyer-facing impact is a missed progress event,
        not a failed task.
        """
        await task_ctx.update({"step": "queued for trafficker review"})
        # Simulate review latency. Real adopters wait on an external
        # signal (Slack approval, queue message, etc.).
        await asyncio.sleep(0.05)
        await task_ctx.update({"step": "trafficker approved"})
        # Adopter media_buy_id allocation — DON'T leak the framework's
        # task_id namespace here. Buyers reading
        # ``media_buy_id.startswith("task_")`` would conflate the two
        # IDs. Real adopters mint media_buy_id from their own backend
        # store; the example just synthesizes a stable string.
        import uuid

        return {
            "media_buy_id": f"mb_reviewed_{uuid.uuid4().hex[:8]}",
            "status": "active",
            "packages": [],
        }

    # ---- Helpers ----

    @staticmethod
    def _extract_total_budget(req: Any) -> float:
        """Coerce ``total_budget`` from the typed Pydantic model OR
        a raw dict. The wire shape is
        ``{currency, amount}`` per ``money.json``."""
        raw = (
            req.total_budget
            if hasattr(req, "total_budget")
            else (req.get("total_budget") if isinstance(req, dict) else None)
        )
        if raw is None:
            return 0.0
        if hasattr(raw, "amount"):
            return float(raw.amount or 0.0)
        if isinstance(raw, dict):
            return float(raw.get("amount") or 0.0)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _echo_packages(req: Any) -> list[dict[str, Any]]:
        packages = getattr(req, "packages", None) or []
        return [
            {
                "package_id": f"pkg_{i}",
                "product_id": (
                    p.product_id
                    if hasattr(p, "product_id")
                    else p.get("product_id", "display-rotation")
                ),
                "pricing_option_id": (
                    p.pricing_option_id
                    if hasattr(p, "pricing_option_id")
                    else p.get("pricing_option_id", "po-cpm-default")
                ),
            }
            for i, p in enumerate(packages)
        ]


if __name__ == "__main__":
    # Same serve(...) call as the sync example. The HITL flow needs
    # a TaskRegistry; serve() wires InMemoryTaskRegistry by default
    # for local dev. In production, set
    # ADCP_DECISIONING_ALLOW_INMEMORY_TASKS=1 (single-process pilot)
    # OR pass registry= a durable impl (Postgres-backed v6.1).
    serve(HelloSellerHybrid(), name="hello-seller-hybrid")
