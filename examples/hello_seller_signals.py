"""Hello-seller-signals — minimal SignalsPlatform adopter.

The smallest possible ``signal-marketplace`` (or ``signal-owned``)
seller. Two methods: ``get_signals`` for catalog discovery and
``activate_signal`` for provisioning to destination platforms.

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
        return {
            "deployments": [
                {
                    "destination_platform": getattr(req, "destination_platform", "the-trade-desk"),
                    "deployment_id": f"dep-{getattr(req, 'signal_id', 'unknown')}",
                    "status": "active",
                }
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

    ``auto_emit_completion_webhooks=False`` opts out of the sync
    completion-webhook auto-emit so this example boots without a
    ``webhook_sender``. In production, wire ``webhook_sender=`` so
    buyers who register ``push_notification_config.url`` on
    ``activate_signal`` get notifications when a TaskHandoff
    completes.
    """
    serve(HelloSignalsSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
