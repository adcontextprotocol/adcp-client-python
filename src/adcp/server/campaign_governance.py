"""Campaign governance protocol handler.

Provides a base class for implementing campaign governance agents that validate
media buying actions against authorized plans, budgets, and policies.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from pydantic import ValidationError

from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext, not_supported
from adcp.types import Error
from adcp.types.campaign_governance import (
    CheckGovernanceRequest,
    CheckGovernanceResponse,
    GetPlanAuditLogsRequest,
    GetPlanAuditLogsResponse,
    ReportPlanOutcomeRequest,
    ReportPlanOutcomeResponse,
    SyncPlansRequest,
    SyncPlansResponse,
)


class CampaignGovernanceHandler(ADCPHandler):
    """Handler for campaign governance protocol.

    Subclass this to implement a campaign governance agent that validates
    media buying actions against authorized campaign plans, budgets, and policies.

    Four operations must be implemented via the handle_* methods:
    - sync_plans: Receive and store campaign plans
    - check_governance: Validate proposed or committed actions
    - report_plan_outcome: Close the governance loop after seller response
    - get_plan_audit_logs: Provide audit trail and metrics

    Non-governance operations (get_products, create_media_buy, etc.)
    return 'not supported'.

    Example:
        class MyGovernanceAgent(CampaignGovernanceHandler):
            async def handle_check_governance(
                self,
                request: CheckGovernanceRequest,
                context: ToolContext | None = None,
            ) -> CheckGovernanceResponse:
                plan = self.plans[request.plan_id]
                # ... validate request against plan ...
                return CheckGovernanceResponse(
                    check_id=generate_id(),
                    status="approved",
                    binding=request.binding,
                    plan_id=request.plan_id,
                    buyer_campaign_ref=request.buyer_campaign_ref,
                    explanation="All checks passed",
                    mode="enforce",
                )
    """

    # ========================================================================
    # Campaign Governance Operations - Override base class with validation
    # ========================================================================

    async def sync_plans(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> SyncPlansResponse | NotImplementedResponse:
        """Push campaign plans to this governance agent.

        Validates params and delegates to handle_sync_plans.
        """
        try:
            request = SyncPlansRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_sync_plans(request, context)

    async def check_governance(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> CheckGovernanceResponse | NotImplementedResponse:
        """Validate a campaign action against governance policies.

        Validates params and delegates to handle_check_governance.
        """
        try:
            request = CheckGovernanceRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_check_governance(request, context)

    async def report_plan_outcome(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> ReportPlanOutcomeResponse | NotImplementedResponse:
        """Close the governance loop after seller response.

        Validates params and delegates to handle_report_plan_outcome.
        """
        try:
            request = ReportPlanOutcomeRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_report_plan_outcome(request, context)

    async def get_plan_audit_logs(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> GetPlanAuditLogsResponse | NotImplementedResponse:
        """Get audit trail for campaign plans.

        Validates params and delegates to handle_get_plan_audit_logs.
        """
        try:
            request = GetPlanAuditLogsRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_get_plan_audit_logs(request, context)

    # ========================================================================
    # Abstract handlers - Implement these in subclasses
    # ========================================================================

    @abstractmethod
    async def handle_sync_plans(
        self,
        request: SyncPlansRequest,
        context: ToolContext | None = None,
    ) -> SyncPlansResponse:
        """Handle sync_plans request.

        Receive campaign plans, resolve applicable policies from the registry,
        and return plan status with resolved policies.
        """
        ...

    @abstractmethod
    async def handle_check_governance(
        self,
        request: CheckGovernanceRequest,
        context: ToolContext | None = None,
    ) -> CheckGovernanceResponse:
        """Handle check_governance request.

        Validate a proposed or committed action against the campaign plan,
        budget limits, and applicable policies.
        """
        ...

    @abstractmethod
    async def handle_report_plan_outcome(
        self,
        request: ReportPlanOutcomeRequest,
        context: ToolContext | None = None,
    ) -> ReportPlanOutcomeResponse:
        """Handle report_plan_outcome request.

        Record the outcome of a governance-checked action and update
        plan budget tracking.
        """
        ...

    @abstractmethod
    async def handle_get_plan_audit_logs(
        self,
        request: GetPlanAuditLogsRequest,
        context: ToolContext | None = None,
    ) -> GetPlanAuditLogsResponse:
        """Handle get_plan_audit_logs request.

        Return audit trail, budget status, and drift metrics for
        the requested plans.
        """
        ...

    # ========================================================================
    # Non-Campaign-Governance Operations - Return 'not supported'
    # ========================================================================

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "get_products is not supported by Campaign Governance agents. "
            "This agent validates campaign actions, not product catalogs."
        )

    async def list_creative_formats(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "list_creative_formats is not supported by Campaign Governance agents."
        )

    async def sync_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "sync_creatives is not supported by Campaign Governance agents."
        )

    async def list_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "list_creatives is not supported by Campaign Governance agents."
        )

    async def build_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "build_creative is not supported by Campaign Governance agents."
        )

    async def preview_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "preview_creative is not supported by Campaign Governance agents."
        )

    async def get_creative_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "get_creative_delivery is not supported by Campaign Governance agents."
        )

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "create_media_buy is not supported by Campaign Governance agents. "
            "This agent validates actions, not executes them."
        )

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "update_media_buy is not supported by Campaign Governance agents."
        )

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "get_media_buy_delivery is not supported by Campaign Governance agents."
        )

    async def get_signals(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "get_signals is not supported by Campaign Governance agents."
        )

    async def activate_signal(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "activate_signal is not supported by Campaign Governance agents."
        )

    async def provide_performance_feedback(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "provide_performance_feedback is not supported by Campaign Governance agents."
        )

    async def list_accounts(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "list_accounts is not supported by Campaign Governance agents."
        )

    async def sync_accounts(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "sync_accounts is not supported by Campaign Governance agents."
        )

    async def log_event(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "log_event is not supported by Campaign Governance agents."
        )

    async def sync_event_sources(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Campaign Governance agents."""
        return not_supported(
            "sync_event_sources is not supported by Campaign Governance agents."
        )
