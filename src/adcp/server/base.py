"""Base classes for ADCP server implementations.

Defines the ADCPHandler base class and utilities for building ADCP-compliant agents.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from adcp.types import Error


@dataclass
class ToolContext:
    """Context passed to tool handlers.

    Contains metadata about the current request that may be useful
    for logging, authorization, or other cross-cutting concerns.
    """

    request_id: str | None = None
    caller_identity: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class NotImplementedResponse(BaseModel):
    """Standard response for operations not supported by this handler."""

    supported: bool = False
    reason: str = "This operation is not supported by this agent"
    error: Error | None = None


def not_supported(
    reason: str = "This operation is not supported by this agent",
) -> NotImplementedResponse:
    """Create a standard 'not supported' response.

    Use this to return from operations that your agent does not implement.

    Args:
        reason: Human-readable explanation of why the operation is not supported

    Returns:
        NotImplementedResponse with supported=False
    """
    return NotImplementedResponse(
        supported=False,
        reason=reason,
        error=Error(
            code="NOT_SUPPORTED",
            message=reason,
        ),
    )


class ADCPHandler(ABC):
    """Base class for ADCP operation handlers.

    Subclass this to implement ADCP operations. All operations have default
    implementations that return 'not supported', allowing you to implement
    only the operations your agent supports.

    For protocol-specific handlers, use:
    - ContentStandardsHandler: For content standards agents
    - SponsoredIntelligenceHandler: For sponsored intelligence agents
    """

    # ========================================================================
    # Core Catalog Operations
    # ========================================================================

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get advertising products.

        Override this to provide product catalog functionality.
        """
        return not_supported("get_products is not implemented by this agent")

    async def list_creative_formats(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """List supported creative formats.

        Override this to provide creative format information.
        """
        return not_supported("list_creative_formats is not implemented by this agent")

    async def list_authorized_properties(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """List authorized properties.

        Override this to provide property authorization information.
        """
        return not_supported("list_authorized_properties is not implemented by this agent")

    # ========================================================================
    # Creative Operations
    # ========================================================================

    async def sync_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Sync creatives.

        Override this to handle creative synchronization.
        """
        return not_supported("sync_creatives is not implemented by this agent")

    async def list_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """List creatives.

        Override this to list synced creatives.
        """
        return not_supported("list_creatives is not implemented by this agent")

    async def build_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Build a creative.

        Override this to build creatives from assets.
        """
        return not_supported("build_creative is not implemented by this agent")

    # ========================================================================
    # Media Buy Operations
    # ========================================================================

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Create a media buy.

        Override this to handle media buy creation.
        """
        return not_supported("create_media_buy is not implemented by this agent")

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Update a media buy.

        Override this to handle media buy updates.
        """
        return not_supported("update_media_buy is not implemented by this agent")

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get media buy delivery metrics.

        Override this to provide delivery reporting.
        """
        return not_supported("get_media_buy_delivery is not implemented by this agent")

    # ========================================================================
    # Signal Operations
    # ========================================================================

    async def get_signals(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get available signals.

        Override this to provide signal catalog.
        """
        return not_supported("get_signals is not implemented by this agent")

    async def activate_signal(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Activate a signal.

        Override this to handle signal activation.
        """
        return not_supported("activate_signal is not implemented by this agent")

    # ========================================================================
    # Feedback Operations
    # ========================================================================

    async def provide_performance_feedback(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Provide performance feedback.

        Override this to handle performance feedback ingestion.
        """
        return not_supported("provide_performance_feedback is not implemented by this agent")

    # ========================================================================
    # V3 Protocol Discovery
    # ========================================================================

    async def get_adcp_capabilities(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get ADCP capabilities.

        Override this to advertise your agent's capabilities.
        """
        return not_supported("get_adcp_capabilities is not implemented by this agent")

    # ========================================================================
    # V3 Content Standards Operations
    # ========================================================================

    async def create_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Create content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("create_content_standards is not implemented by this agent")

    async def get_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("get_content_standards is not implemented by this agent")

    async def list_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """List content standards configurations.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("list_content_standards is not implemented by this agent")

    async def update_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Update content standards configuration.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("update_content_standards is not implemented by this agent")

    async def calibrate_content(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Calibrate content against standards.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("calibrate_content is not implemented by this agent")

    async def validate_content_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Validate content delivery against standards.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("validate_content_delivery is not implemented by this agent")

    async def get_media_buy_artifacts(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get artifacts associated with a media buy.

        Override this in ContentStandardsHandler subclasses.
        """
        return not_supported("get_media_buy_artifacts is not implemented by this agent")

    # ========================================================================
    # V3 Sponsored Intelligence Operations
    # ========================================================================

    async def si_get_offering(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get sponsored intelligence offering.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return not_supported("si_get_offering is not implemented by this agent")

    async def si_initiate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Initiate sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return not_supported("si_initiate_session is not implemented by this agent")

    async def si_send_message(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Send message in sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return not_supported("si_send_message is not implemented by this agent")

    async def si_terminate_session(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Terminate sponsored intelligence session.

        Override this in SponsoredIntelligenceHandler subclasses.
        """
        return not_supported("si_terminate_session is not implemented by this agent")

    # ========================================================================
    # V3 Governance (Property Lists) Operations
    # ========================================================================

    async def create_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Create a property list for governance filtering.

        Override this in GovernanceHandler subclasses.
        """
        return not_supported("create_property_list is not implemented by this agent")

    async def get_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Get a property list with optional resolution.

        Override this in GovernanceHandler subclasses.
        """
        return not_supported("get_property_list is not implemented by this agent")

    async def list_property_lists(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """List property lists.

        Override this in GovernanceHandler subclasses.
        """
        return not_supported("list_property_lists is not implemented by this agent")

    async def update_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Update a property list.

        Override this in GovernanceHandler subclasses.
        """
        return not_supported("update_property_list is not implemented by this agent")

    async def delete_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> Any:
        """Delete a property list.

        Override this in GovernanceHandler subclasses.
        """
        return not_supported("delete_property_list is not implemented by this agent")
