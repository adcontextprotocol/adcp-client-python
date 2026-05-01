"""Hello-seller-governance — minimal CampaignGovernancePlatform adopter.

The smallest possible ``governance-spend-authority`` (or
``governance-delivery-monitor``) seller. Four required methods:
``check_governance``, ``sync_plans``, ``report_plan_outcome``,
``get_plan_audit_logs``.

This is the template for spend-authority / delivery-monitor agents.
Note: ``governance_aware=True`` MUST be declared on capabilities —
the framework's D15 round-4 fail-fast catches missing opt-in at
server boot.

Run::

    uv run python examples/hello_seller_governance.py
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    RequestContext,
    SingletonAccounts,
    serve,
)


class HelloGovernanceSeller(DecisioningPlatform):
    """The canonical minimal ``governance-spend-authority`` adopter."""

    capabilities = DecisioningCapabilities(
        specialisms=["governance-spend-authority"],
        governance_aware=True,
    )
    accounts = SingletonAccounts(account_id="hello-governance")

    def check_governance(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Approve / deny / require conditions. Sync — buyer waits for
        the decision before proceeding to ``create_media_buy``."""
        return {
            "decision": "approved",
            "policy_id": "policy-default",
            "audit_id": f"audit-{getattr(req, 'plan_id', 'unknown')}",
        }

    def sync_plans(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """CRUD with delta upsert into the governance agent's plan
        store."""
        return {"plans": [], "applied_changes": 0}

    def report_plan_outcome(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Outcome reporting from sellers (delivery actuals)."""
        return {"acknowledged": True}

    def get_plan_audit_logs(
        self,
        req: Any,
        ctx: RequestContext[Any],
    ) -> dict[str, Any]:
        """Audit log read for governance decisions + outcomes."""
        return {"audit_logs": []}


def main() -> None:
    """Boot the seller on http://localhost:3001/mcp."""
    serve(HelloGovernanceSeller(), auto_emit_completion_webhooks=False)


if __name__ == "__main__":
    main()
