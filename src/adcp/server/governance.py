"""Governance protocol handler.

Provides a base class for implementing Governance agents that manage
property lists for brand safety, compliance, and quality filtering.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Generic

from pydantic import ValidationError

from adcp.governance import (
    governance_request_check_type,
    validate_governance_outcome_request,
    validate_governance_request,
    validate_governance_verdict,
)
from adcp.server.base import ADCPHandler, NotImplementedResponse, TContext
from adcp.types import (
    CheckGovernanceRequest,
    CheckGovernanceResponse,
    CreatePropertyListRequest,
    CreatePropertyListResponse,
    DeletePropertyListRequest,
    DeletePropertyListResponse,
    Error,
    GetCreativeFeaturesRequest,
    GetCreativeFeaturesResponse,
    GetPlanAuditLogsRequest,
    GetPlanAuditLogsResponse,
    GetPropertyListRequest,
    GetPropertyListResponse,
    ListPropertyListsRequest,
    ListPropertyListsResponse,
    ReportPlanOutcomeRequest,
    ReportPlanOutcomeResponse,
    SyncPlansRequest,
    SyncPlansResponse,
    UpdatePropertyListRequest,
    UpdatePropertyListResponse,
)


class GovernanceHandler(ADCPHandler[TContext], Generic[TContext]):
    """Handler for Governance protocol (Property Lists).

    Subclass this to implement a Governance agent that manages property lists
    for brand safety, compliance scoring, and quality filtering.

    All property list operations must be implemented via the handle_* methods.
    The public methods (create_property_list, etc.) handle validation and
    error handling automatically.

    Non-governance operations (get_products, create_media_buy, etc.)
    return 'not supported' via the base class.

    Example:
        class MyGovernanceHandler(GovernanceHandler):
            async def handle_create_property_list(
                self,
                request: CreatePropertyListRequest,
                context: TContext | None = None
            ) -> CreatePropertyListResponse:
                # Store the list definition
                list_id = generate_id()
                # ...
                return CreatePropertyListResponse(list=PropertyList(...))
    """

    _agent_type: str = "Governance agents"

    # ========================================================================
    # Governance Operations - Override base class with validation
    # ========================================================================

    async def get_creative_features(
        self,
        params: GetCreativeFeaturesRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> GetCreativeFeaturesResponse | NotImplementedResponse:
        """Evaluate governance features for a creative manifest."""
        try:
            request = GetCreativeFeaturesRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_get_creative_features(request, context)

    async def sync_plans(
        self,
        params: SyncPlansRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> SyncPlansResponse | NotImplementedResponse:
        """Sync campaign governance plans to the agent."""
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
        params: CheckGovernanceRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> CheckGovernanceResponse | NotImplementedResponse:
        """Check whether a proposed or committed action complies with plan governance."""
        try:
            request = CheckGovernanceRequest.model_validate(params)
            expected_check_type = governance_request_check_type(
                request,
                resolved_version=getattr(context, "resolved_adcp_version", None),
            )
            if expected_check_type is not None:
                validate_governance_request(request)
        except (ValidationError, ValueError) as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        response = await self.handle_check_governance(request, context)
        if not isinstance(response, NotImplementedResponse) and expected_check_type is not None:
            validate_governance_verdict(
                response,
                expected_check_type=expected_check_type,
            )
        return response

    async def report_plan_outcome(
        self,
        params: ReportPlanOutcomeRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> ReportPlanOutcomeResponse | NotImplementedResponse:
        """Report the outcome of a previously governed action."""
        try:
            request = ReportPlanOutcomeRequest.model_validate(params)
            validate_governance_outcome_request(
                request,
                allow_legacy_delivery=True,
                resolved_version=getattr(context, "resolved_adcp_version", None),
            )
        except (ValidationError, ValueError) as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_report_plan_outcome(request, context)

    async def get_plan_audit_logs(
        self,
        params: GetPlanAuditLogsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> GetPlanAuditLogsResponse | NotImplementedResponse:
        """Retrieve governance audit logs for one or more plans."""
        try:
            request = GetPlanAuditLogsRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_get_plan_audit_logs(request, context)

    async def create_property_list(
        self,
        params: CreatePropertyListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> CreatePropertyListResponse | NotImplementedResponse:
        """Create a property list for governance filtering.

        Validates params and delegates to handle_create_property_list.
        """
        try:
            request = CreatePropertyListRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_create_property_list(request, context)

    async def get_property_list(
        self,
        params: GetPropertyListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> GetPropertyListResponse | NotImplementedResponse:
        """Get a property list with optional resolution.

        Validates params and delegates to handle_get_property_list.
        """
        try:
            request = GetPropertyListRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_get_property_list(request, context)

    async def list_property_lists(
        self,
        params: ListPropertyListsRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> ListPropertyListsResponse | NotImplementedResponse:
        """List property lists.

        Validates params and delegates to handle_list_property_lists.
        """
        try:
            request = ListPropertyListsRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_list_property_lists(request, context)

    async def update_property_list(
        self,
        params: UpdatePropertyListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> UpdatePropertyListResponse | NotImplementedResponse:
        """Update a property list.

        Validates params and delegates to handle_update_property_list.
        """
        try:
            request = UpdatePropertyListRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_update_property_list(request, context)

    async def delete_property_list(
        self,
        params: DeletePropertyListRequest | dict[str, Any],
        context: TContext | None = None,
    ) -> DeletePropertyListResponse | NotImplementedResponse:
        """Delete a property list.

        Validates params and delegates to handle_delete_property_list.
        """
        try:
            request = DeletePropertyListRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_delete_property_list(request, context)

    # ========================================================================
    # Abstract handlers - Implement these in subclasses
    # ========================================================================

    @abstractmethod
    async def handle_get_creative_features(
        self,
        request: GetCreativeFeaturesRequest,
        context: TContext | None = None,
    ) -> GetCreativeFeaturesResponse:
        """Handle creative feature evaluation."""
        ...

    @abstractmethod
    async def handle_sync_plans(
        self,
        request: SyncPlansRequest,
        context: TContext | None = None,
    ) -> SyncPlansResponse:
        """Handle campaign governance plan sync."""
        ...

    @abstractmethod
    async def handle_check_governance(
        self,
        request: CheckGovernanceRequest,
        context: TContext | None = None,
    ) -> CheckGovernanceResponse:
        """Handle a governance check request."""
        ...

    @abstractmethod
    async def handle_report_plan_outcome(
        self,
        request: ReportPlanOutcomeRequest,
        context: TContext | None = None,
    ) -> ReportPlanOutcomeResponse:
        """Handle reporting of a governed action outcome."""
        ...

    @abstractmethod
    async def handle_get_plan_audit_logs(
        self,
        request: GetPlanAuditLogsRequest,
        context: TContext | None = None,
    ) -> GetPlanAuditLogsResponse:
        """Handle retrieval of governance audit logs."""
        ...

    @abstractmethod
    async def handle_create_property_list(
        self,
        request: CreatePropertyListRequest,
        context: TContext | None = None,
    ) -> CreatePropertyListResponse:
        """Handle create property list request."""
        ...

    @abstractmethod
    async def handle_get_property_list(
        self,
        request: GetPropertyListRequest,
        context: TContext | None = None,
    ) -> GetPropertyListResponse:
        """Handle get property list request."""
        ...

    @abstractmethod
    async def handle_list_property_lists(
        self,
        request: ListPropertyListsRequest,
        context: TContext | None = None,
    ) -> ListPropertyListsResponse:
        """Handle list property lists request."""
        ...

    @abstractmethod
    async def handle_update_property_list(
        self,
        request: UpdatePropertyListRequest,
        context: TContext | None = None,
    ) -> UpdatePropertyListResponse:
        """Handle update property list request."""
        ...

    @abstractmethod
    async def handle_delete_property_list(
        self,
        request: DeletePropertyListRequest,
        context: TContext | None = None,
    ) -> DeletePropertyListResponse:
        """Handle delete property list request."""
        ...
