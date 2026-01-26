"""Governance protocol handler.

Provides a base class for implementing Governance agents that manage
property lists for brand safety, compliance, and quality filtering.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from pydantic import ValidationError

from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext, not_supported
from adcp.types import (
    CreatePropertyListRequest,
    CreatePropertyListResponse,
    DeletePropertyListRequest,
    DeletePropertyListResponse,
    Error,
    GetPropertyListRequest,
    GetPropertyListResponse,
    ListPropertyListsRequest,
    ListPropertyListsResponse,
    UpdatePropertyListRequest,
    UpdatePropertyListResponse,
)


class GovernanceHandler(ADCPHandler):
    """Handler for Governance protocol (Property Lists).

    Subclass this to implement a Governance agent that manages property lists
    for brand safety, compliance scoring, and quality filtering.

    All property list operations must be implemented via the handle_* methods.
    The public methods (create_property_list, etc.) handle validation and
    error handling automatically.

    Non-governance operations (get_products, create_media_buy, etc.)
    return 'not supported'.

    Example:
        class MyGovernanceHandler(GovernanceHandler):
            async def handle_create_property_list(
                self,
                request: CreatePropertyListRequest,
                context: ToolContext | None = None
            ) -> CreatePropertyListResponse:
                # Store the list definition
                list_id = generate_id()
                # ...
                return CreatePropertyListResponse(list=PropertyList(...))
    """

    # ========================================================================
    # Governance Operations - Override base class with validation
    # ========================================================================

    async def create_property_list(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> CreatePropertyListResponse | NotImplementedResponse:
        """Create a property list for governance filtering.

        Validates params and delegates to handle_create_property_list.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Response with created property list metadata, or error response
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
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> GetPropertyListResponse | NotImplementedResponse:
        """Get a property list with optional resolution.

        Validates params and delegates to handle_get_property_list.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Response with list metadata and optionally resolved identifiers
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
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> ListPropertyListsResponse | NotImplementedResponse:
        """List property lists.

        Validates params and delegates to handle_list_property_lists.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Response with array of property list metadata
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
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> UpdatePropertyListResponse | NotImplementedResponse:
        """Update a property list.

        Validates params and delegates to handle_update_property_list.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Response with updated property list
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
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> DeletePropertyListResponse | NotImplementedResponse:
        """Delete a property list.

        Validates params and delegates to handle_delete_property_list.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Response confirming deletion
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
    async def handle_create_property_list(
        self,
        request: CreatePropertyListRequest,
        context: ToolContext | None = None,
    ) -> CreatePropertyListResponse:
        """Handle create property list request.

        Must be implemented by Governance agents.

        Args:
            request: Validated property list creation request
            context: Optional tool context

        Returns:
            Response with created property list metadata
        """
        ...

    @abstractmethod
    async def handle_get_property_list(
        self,
        request: GetPropertyListRequest,
        context: ToolContext | None = None,
    ) -> GetPropertyListResponse:
        """Handle get property list request.

        Must be implemented by Governance agents.

        When resolve=true, evaluates filters and returns matching property
        identifiers. Otherwise returns only metadata.

        Args:
            request: Validated request with list_id and optional resolve flag
            context: Optional tool context

        Returns:
            Response with list metadata and optionally resolved identifiers
        """
        ...

    @abstractmethod
    async def handle_list_property_lists(
        self,
        request: ListPropertyListsRequest,
        context: ToolContext | None = None,
    ) -> ListPropertyListsResponse:
        """Handle list property lists request.

        Must be implemented by Governance agents.

        Args:
            request: Validated request with optional filtering and pagination
            context: Optional tool context

        Returns:
            Response with array of property list metadata
        """
        ...

    @abstractmethod
    async def handle_update_property_list(
        self,
        request: UpdatePropertyListRequest,
        context: ToolContext | None = None,
    ) -> UpdatePropertyListResponse:
        """Handle update property list request.

        Must be implemented by Governance agents.

        Args:
            request: Validated request with list_id and updates
            context: Optional tool context

        Returns:
            Response with updated property list
        """
        ...

    @abstractmethod
    async def handle_delete_property_list(
        self,
        request: DeletePropertyListRequest,
        context: ToolContext | None = None,
    ) -> DeletePropertyListResponse:
        """Handle delete property list request.

        Must be implemented by Governance agents.

        Args:
            request: Validated request with list_id
            context: Optional tool context

        Returns:
            Response confirming deletion
        """
        ...

    # ========================================================================
    # Non-Governance Operations - Return 'not supported'
    # ========================================================================

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "get_products is not supported by Governance agents. "
            "This agent manages property lists for filtering, not product catalogs."
        )

    async def list_creative_formats(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "list_creative_formats is not supported by Governance agents."
        )

    async def list_authorized_properties(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "list_authorized_properties is not supported by Governance agents. "
            "Use get_property_list with resolve=true instead."
        )

    async def sync_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "sync_creatives is not supported by Governance agents."
        )

    async def list_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "list_creatives is not supported by Governance agents."
        )

    async def build_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "build_creative is not supported by Governance agents."
        )

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "create_media_buy is not supported by Governance agents. "
            "This agent manages property lists, not media buying."
        )

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "update_media_buy is not supported by Governance agents."
        )

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "get_media_buy_delivery is not supported by Governance agents."
        )

    async def get_signals(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "get_signals is not supported by Governance agents."
        )

    async def activate_signal(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "activate_signal is not supported by Governance agents."
        )

    async def provide_performance_feedback(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "provide_performance_feedback is not supported by Governance agents."
        )

    # ========================================================================
    # V3 Content Standards - Not supported
    # ========================================================================

    async def create_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "create_content_standards is not supported by Governance agents. "
            "Use a Content Standards agent for content calibration."
        )

    async def get_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "get_content_standards is not supported by Governance agents."
        )

    async def list_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "list_content_standards is not supported by Governance agents."
        )

    async def update_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "update_content_standards is not supported by Governance agents."
        )

    async def calibrate_content(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "calibrate_content is not supported by Governance agents."
        )

    async def validate_content_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "validate_content_delivery is not supported by Governance agents."
        )

    async def get_media_buy_artifacts(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "get_media_buy_artifacts is not supported by Governance agents."
        )

    # ========================================================================
    # V3 Sponsored Intelligence - Not supported
    # ========================================================================

    async def si_get_offering(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "si_get_offering is not supported by Governance agents. "
            "Use a Sponsored Intelligence agent for SI operations."
        )

    async def si_initiate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "si_initiate_session is not supported by Governance agents."
        )

    async def si_send_message(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "si_send_message is not supported by Governance agents."
        )

    async def si_terminate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Governance agents."""
        return not_supported(
            "si_terminate_session is not supported by Governance agents."
        )
