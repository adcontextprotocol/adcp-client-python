"""Sponsored Intelligence protocol handler.

Provides a base class for implementing Sponsored Intelligence agents.
Non-SI operations return 'not supported' by default.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext, not_supported
from adcp.types import (
    SiGetOfferingRequest,
    SiGetOfferingResponse,
    SiInitiateSessionRequest,
    SiInitiateSessionResponse,
    SiSendMessageRequest,
    SiSendMessageResponse,
    SiTerminateSessionRequest,
    SiTerminateSessionResponse,
)


class SponsoredIntelligenceHandler(ADCPHandler):
    """Handler for Sponsored Intelligence protocol.

    Subclass this to implement a Sponsored Intelligence agent. All SI
    operations are abstract and must be implemented. Non-SI operations
    (get_products, create_media_buy, content standards, etc.) return 'not supported'.

    Example:
        class MySIHandler(SponsoredIntelligenceHandler):
            async def si_get_offering(
                self,
                request: SiGetOfferingRequest,
                context: ToolContext | None = None
            ) -> SiGetOfferingResponse:
                # Your implementation
                return SiGetOfferingResponse(...)
    """

    # ========================================================================
    # Sponsored Intelligence Operations - MUST be implemented
    # ========================================================================

    @abstractmethod
    async def si_get_offering(
        self,
        request: SiGetOfferingRequest,
        context: ToolContext | None = None,
    ) -> SiGetOfferingResponse:
        """Get sponsored intelligence offering.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: SI offering request
            context: Optional tool context

        Returns:
            SI offering response with capabilities and pricing
        """
        ...

    @abstractmethod
    async def si_initiate_session(
        self,
        request: SiInitiateSessionRequest,
        context: ToolContext | None = None,
    ) -> SiInitiateSessionResponse:
        """Initiate sponsored intelligence session.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Session initiation request
            context: Optional tool context

        Returns:
            Session initiation response with session ID
        """
        ...

    @abstractmethod
    async def si_send_message(
        self,
        request: SiSendMessageRequest,
        context: ToolContext | None = None,
    ) -> SiSendMessageResponse:
        """Send message in sponsored intelligence session.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Message request with session ID and content
            context: Optional tool context

        Returns:
            Message response with AI-generated content
        """
        ...

    @abstractmethod
    async def si_terminate_session(
        self,
        request: SiTerminateSessionRequest,
        context: ToolContext | None = None,
    ) -> SiTerminateSessionResponse:
        """Terminate sponsored intelligence session.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Session termination request
            context: Optional tool context

        Returns:
            Termination response with session summary
        """
        ...

    # ========================================================================
    # Non-SI Operations - Return 'not supported'
    # ========================================================================

    async def get_products(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_products is not supported by Sponsored Intelligence agents. "
            "This agent handles conversational AI sponsorship, not product catalog operations."
        )

    async def list_creative_formats(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "list_creative_formats is not supported by Sponsored Intelligence agents."
        )

    async def list_authorized_properties(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "list_authorized_properties is not supported by Sponsored Intelligence agents."
        )

    async def sync_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "sync_creatives is not supported by Sponsored Intelligence agents."
        )

    async def list_creatives(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "list_creatives is not supported by Sponsored Intelligence agents."
        )

    async def build_creative(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "build_creative is not supported by Sponsored Intelligence agents."
        )

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "create_media_buy is not supported by Sponsored Intelligence agents. "
            "SI sessions are initiated via si_initiate_session, not media buys."
        )

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "update_media_buy is not supported by Sponsored Intelligence agents."
        )

    async def get_media_buy_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_media_buy_delivery is not supported by Sponsored Intelligence agents."
        )

    async def get_signals(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_signals is not supported by Sponsored Intelligence agents."
        )

    async def activate_signal(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "activate_signal is not supported by Sponsored Intelligence agents."
        )

    async def provide_performance_feedback(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "provide_performance_feedback is not supported by Sponsored Intelligence agents."
        )

    # ========================================================================
    # V3 Content Standards - Not supported
    # ========================================================================

    async def create_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "create_content_standards is not supported by Sponsored Intelligence agents. "
            "Use a Content Standards agent for content calibration."
        )

    async def get_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_content_standards is not supported by Sponsored Intelligence agents."
        )

    async def list_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "list_content_standards is not supported by Sponsored Intelligence agents."
        )

    async def update_content_standards(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "update_content_standards is not supported by Sponsored Intelligence agents."
        )

    async def calibrate_content(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "calibrate_content is not supported by Sponsored Intelligence agents."
        )

    async def validate_content_delivery(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "validate_content_delivery is not supported by Sponsored Intelligence agents."
        )

    async def get_media_buy_artifacts(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_media_buy_artifacts is not supported by Sponsored Intelligence agents."
        )

    # ========================================================================
    # V3 Governance (Property Lists) - Not supported
    # ========================================================================

    async def create_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "create_property_list is not supported by Sponsored Intelligence agents. "
            "Use a Governance agent for property list operations."
        )

    async def get_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "get_property_list is not supported by Sponsored Intelligence agents."
        )

    async def list_property_lists(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "list_property_lists is not supported by Sponsored Intelligence agents."
        )

    async def update_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "update_property_list is not supported by Sponsored Intelligence agents."
        )

    async def delete_property_list(
        self, params: dict[str, Any], context: ToolContext | None = None
    ) -> NotImplementedResponse:
        """Not supported by Sponsored Intelligence agents."""
        return not_supported(
            "delete_property_list is not supported by Sponsored Intelligence agents."
        )
