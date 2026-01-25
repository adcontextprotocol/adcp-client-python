"""Content Standards protocol handler.

Provides a base class for implementing Content Standards agents.
Non-Content-Standards operations return 'not supported' by default.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext, not_supported
from adcp.types import (
    CalibrateContentRequest,
    CalibrateContentResponse,
    ContentStandards,
    CreateContentStandardsRequest,
    CreateContentStandardsResponse,
    GetContentStandardsRequest,
    GetContentStandardsResponse,
    GetMediaBuyArtifactsRequest,
    GetMediaBuyArtifactsResponse,
    ListContentStandardsRequest,
    ListContentStandardsResponse,
    UpdateContentStandardsRequest,
    UpdateContentStandardsResponse,
    ValidateContentDeliveryRequest,
    ValidateContentDeliveryResponse,
)


class ContentStandardsHandler(ADCPHandler):
    """Handler for Content Standards protocol.

    Subclass this to implement a Content Standards agent. All Content Standards
    operations are abstract and must be implemented. Non-Content-Standards
    operations (get_products, create_media_buy, etc.) return 'not supported'.

    Example:
        class MyContentStandardsHandler(ContentStandardsHandler):
            async def create_content_standards(
                self,
                request: CreateContentStandardsRequest,
                context: ToolContext | None = None
            ) -> CreateContentStandardsResponse:
                # Your implementation
                return CreateContentStandardsResponse(...)
    """

    # ========================================================================
    # Content Standards Operations - MUST be implemented
    # ========================================================================

    @abstractmethod
    async def create_content_standards(
        self,
        request: CreateContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> CreateContentStandardsResponse:
        """Create content standards configuration.

        Must be implemented by Content Standards agents.

        Args:
            request: Content standards creation request
            context: Optional tool context

        Returns:
            Content standards creation response
        """
        ...

    @abstractmethod
    async def get_content_standards(
        self,
        request: GetContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> GetContentStandardsResponse:
        """Get content standards configuration.

        Must be implemented by Content Standards agents.

        Args:
            request: Content standards retrieval request
            context: Optional tool context

        Returns:
            Content standards response
        """
        ...

    @abstractmethod
    async def list_content_standards(
        self,
        request: ListContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> ListContentStandardsResponse:
        """List content standards configurations.

        Must be implemented by Content Standards agents.

        Args:
            request: List content standards request
            context: Optional tool context

        Returns:
            List of content standards
        """
        ...

    @abstractmethod
    async def update_content_standards(
        self,
        request: UpdateContentStandardsRequest,
        context: ToolContext | None = None,
    ) -> UpdateContentStandardsResponse:
        """Update content standards configuration.

        Must be implemented by Content Standards agents.

        Args:
            request: Content standards update request
            context: Optional tool context

        Returns:
            Updated content standards response
        """
        ...

    @abstractmethod
    async def calibrate_content(
        self,
        request: CalibrateContentRequest,
        context: ToolContext | None = None,
    ) -> CalibrateContentResponse:
        """Calibrate content against standards.

        Must be implemented by Content Standards agents.

        Args:
            request: Calibration request with content to evaluate
            context: Optional tool context

        Returns:
            Calibration response with scores and feedback
        """
        ...

    @abstractmethod
    async def validate_content_delivery(
        self,
        request: ValidateContentDeliveryRequest,
        context: ToolContext | None = None,
    ) -> ValidateContentDeliveryResponse:
        """Validate content delivery against standards.

        Must be implemented by Content Standards agents.

        Args:
            request: Validation request with delivery data
            context: Optional tool context

        Returns:
            Validation response
        """
        ...

    @abstractmethod
    async def get_media_buy_artifacts(
        self,
        request: GetMediaBuyArtifactsRequest,
        context: ToolContext | None = None,
    ) -> GetMediaBuyArtifactsResponse:
        """Get artifacts associated with a media buy.

        Must be implemented by Content Standards agents.

        Args:
            request: Artifacts retrieval request
            context: Optional tool context

        Returns:
            Media buy artifacts response
        """
        ...

    # ========================================================================
    # Non-Content-Standards Operations - Return 'not supported'
    # ========================================================================

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "get_products is not supported by Content Standards agents. "
            "This agent handles content calibration and validation, not product catalog operations."
        )

    async def list_creative_formats(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "list_creative_formats is not supported by Content Standards agents."
        )

    async def list_authorized_properties(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "list_authorized_properties is not supported by Content Standards agents."
        )

    async def sync_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "sync_creatives is not supported by Content Standards agents."
        )

    async def list_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "list_creatives is not supported by Content Standards agents."
        )

    async def build_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "build_creative is not supported by Content Standards agents."
        )

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "create_media_buy is not supported by Content Standards agents. "
            "This agent handles content calibration and validation, not media buying."
        )

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "update_media_buy is not supported by Content Standards agents."
        )

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "get_media_buy_delivery is not supported by Content Standards agents."
        )

    async def get_signals(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "get_signals is not supported by Content Standards agents."
        )

    async def activate_signal(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "activate_signal is not supported by Content Standards agents."
        )

    async def provide_performance_feedback(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "provide_performance_feedback is not supported by Content Standards agents."
        )

    # ========================================================================
    # V3 Sponsored Intelligence - Not supported
    # ========================================================================

    async def si_get_offering(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "si_get_offering is not supported by Content Standards agents. "
            "Use a Sponsored Intelligence agent for SI operations."
        )

    async def si_initiate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "si_initiate_session is not supported by Content Standards agents."
        )

    async def si_send_message(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "si_send_message is not supported by Content Standards agents."
        )

    async def si_terminate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "si_terminate_session is not supported by Content Standards agents."
        )

    # ========================================================================
    # V3 Governance (Property Lists) - Not supported
    # ========================================================================

    async def create_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "create_property_list is not supported by Content Standards agents. "
            "Use a Governance agent for property list operations."
        )

    async def get_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "get_property_list is not supported by Content Standards agents."
        )

    async def list_property_lists(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "list_property_lists is not supported by Content Standards agents."
        )

    async def update_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "update_property_list is not supported by Content Standards agents."
        )

    async def delete_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Content Standards agents."""
        return not_supported(
            "delete_property_list is not supported by Content Standards agents."
        )
