"""Hello-seller-signals — minimal SignalsPlatform adopter.

The smallest possible ``signal-marketplace`` seller. Two methods:
``get_signals`` for catalog discovery and ``activate_signal`` for
provisioning to destination platforms. ``signal-owned`` sellers can be
discovery-only when their owned signals are already usable on seller
inventory.

This is the template for signal-marketplace adopters (LiveRamp,
Nielsen, 1P data providers).

The activate_signal method is also the canonical example for the
TaskHandoff primitive — long-running deployments hand off to a
background fn and return a Submitted envelope synchronously while
the framework polls/completes the task in the background.

Run::

    uv run python examples/hello_seller_signals.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloSignalsSeller(DecisioningPlatform):
    """The canonical minimal ``signal-marketplace`` adopter.

    Catalog returns three signals (one demographic, one in-market,
    one purchase-intent) and activate_signal demonstrates BOTH the
    sync-success path AND the TaskHandoff path for long-running
    activations.
    """

    capabilities = DecisioningCapabilities(
        specialisms=["signal-marketplace"],
        channels=["display", "video"],
    )
    accounts = SingletonAccounts(account_id="hello-signals")

    def get_signals(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Return the catalog. Sync — no HITL."""
        return {
            "signals": [
                {
                    "signal_id": "demo-female-25-44",
                    "name": "Female, 25-44",
                    "category": "demographic",
                    "estimated_size": 4_500_000,
                    "pricing_model": "cpm_uplift",
                    "cpm_uplift": 0.50,
                },
                {
                    "signal_id": "in-market-auto",
                    "name": "In-Market: Auto",
                    "category": "in_market",
                    "estimated_size": 1_200_000,
                    "pricing_model": "cpm_uplift",
                    "cpm_uplift": 1.20,
                },
                {
                    "signal_id": "purchase-intent-luxury",
                    "name": "Purchase Intent: Luxury Goods",
                    "category": "purchase_intent",
                    "estimated_size": 350_000,
                    "pricing_model": "cpm_uplift",
                    "cpm_uplift": 2.10,
                },
            ]
        }

    def activate_signal(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> Any:
        """Provision a signal onto destination platforms.

        Sync-success path returns immediately with deployment confirmation.
        For long-running activations (large segments, multiple destinations),
        switch to the TaskHandoff path:

            return ctx.handoff_to_task(self._async_activation)

        The framework allocates a task_id, returns the
        ``{task_id, status: "submitted"}`` envelope synchronously, and
        runs ``_async_activation`` in the background.
        """
        # ``ActivateSignalRequest`` carries the signal reference on
        # ``signal_agent_segment_id`` (the canonical spec field — the
        # segment ID a buyer activates against). The catalog returned
        # by ``get_signals`` may use ``signal_id`` for the buyer-facing
        # name, but the activation request keys on
        # ``signal_agent_segment_id``.
        segment_id = getattr(req, "signal_agent_segment_id", "unknown")
        # Buyer-supplied destinations list — required (min_length=1)
        # and unbounded; we echo back one deployment per destination.
        destinations = getattr(req, "destinations", []) or []
        return {
            "deployments": [
                {
                    "destination_platform": (
                        getattr(d, "platform", None)
                        or (d.get("platform") if isinstance(d, dict) else None)
                        or "the-trade-desk"
                    ),
                    "deployment_id": f"dep-{segment_id}",
                    "status": "active",
                }
                for d in (destinations or [{"platform": "the-trade-desk"}])
            ]
        }

    async def _async_activation(self, task_ctx: Any) -> dict[str, Any]:
        """Background activation handler — invoked by the framework
        when a buyer's activate_signal call routes via TaskHandoff.
        Realistic adopters poll the destination platform's API here;
        the stub returns immediately for the example."""
        await asyncio.sleep(0)
        return {"deployments": [{"deployment_id": "dep-async", "status": "active"}]}


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp.

    Synchronous terminal responses remain inline-only by default. In
    production, wire ``webhook_sender=`` so
    buyers who register ``push_notification_config.url`` on
    ``activate_signal`` get notifications when a TaskHandoff
    completes.
    """
    serve(HelloSignalsSeller())


if __name__ == "__main__":
    main()
