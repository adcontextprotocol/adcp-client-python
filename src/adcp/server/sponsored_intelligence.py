"""Sponsored Intelligence protocol handler.

Provides a base class for implementing Sponsored Intelligence agents.
Non-SI operations return 'not supported' by default.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any

from pydantic import ValidationError

from adcp.server.base import ADCPHandler, NotImplementedResponse, ToolContext, not_supported
from adcp.types import (
    Error,
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
    operations must be implemented via the handle_* methods.
    The public methods (si_get_offering, etc.) handle validation and
    error handling automatically.

    Non-SI operations (get_products, create_media_buy, content standards, etc.)
    return 'not supported'.

    Example:
        class MySIHandler(SponsoredIntelligenceHandler):
            async def handle_si_get_offering(
                self,
                request: SiGetOfferingRequest,
                context: ToolContext | None = None
            ) -> SiGetOfferingResponse:
                # Your implementation
                return SiGetOfferingResponse(...)
    """

    # ========================================================================
    # Sponsored Intelligence Operations - Override base class with validation
    # ========================================================================

    async def si_get_offering(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> SiGetOfferingResponse | NotImplementedResponse:
        """Get sponsored intelligence offering.

        Validates params and delegates to handle_si_get_offering.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            SI offering response with capabilities and pricing, or error response
        """
        try:
            request = SiGetOfferingRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_si_get_offering(request, context)

    async def si_initiate_session(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> SiInitiateSessionResponse | NotImplementedResponse:
        """Initiate sponsored intelligence session.

        Validates params and delegates to handle_si_initiate_session.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Session initiation response with session ID, or error response
        """
        try:
            request = SiInitiateSessionRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_si_initiate_session(request, context)

    async def si_send_message(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> SiSendMessageResponse | NotImplementedResponse:
        """Send message in sponsored intelligence session.

        Validates params and delegates to handle_si_send_message.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Message response with AI-generated content, or error response
        """
        try:
            request = SiSendMessageRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_si_send_message(request, context)

    async def si_terminate_session(
        self,
        params: dict[str, Any],
        context: ToolContext | None = None,
    ) -> SiTerminateSessionResponse | NotImplementedResponse:
        """Terminate sponsored intelligence session.

        Validates params and delegates to handle_si_terminate_session.

        Args:
            params: Request parameters as dict
            context: Optional tool context

        Returns:
            Termination response with session summary, or error response
        """
        try:
            request = SiTerminateSessionRequest.model_validate(params)
        except ValidationError as e:
            return NotImplementedResponse(
                supported=False,
                reason=f"Invalid request: {e}",
                error=Error(code="VALIDATION_ERROR", message=str(e)),
            )
        return await self.handle_si_terminate_session(request, context)

    # ========================================================================
    # Abstract handlers - Implement these in subclasses
    # ========================================================================

    @abstractmethod
    async def handle_si_get_offering(
        self,
        request: SiGetOfferingRequest,
        context: ToolContext | None = None,
    ) -> SiGetOfferingResponse:
        """Handle get offering request.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Validated SI offering request
            context: Optional tool context

        Returns:
            SI offering response with capabilities and pricing
        """
        ...

    @abstractmethod
    async def handle_si_initiate_session(
        self,
        request: SiInitiateSessionRequest,
        context: ToolContext | None = None,
    ) -> SiInitiateSessionResponse:
        """Handle initiate session request.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Validated session initiation request
            context: Optional tool context

        Returns:
            Session initiation response with session ID
        """
        ...

    @abstractmethod
    async def handle_si_send_message(
        self,
        request: SiSendMessageRequest,
        context: ToolContext | None = None,
    ) -> SiSendMessageResponse:
        """Handle send message request.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Validated message request with session ID and content
            context: Optional tool context

        Returns:
            Message response with AI-generated content
        """
        ...

    @abstractmethod
    async def handle_si_terminate_session(
        self,
        request: SiTerminateSessionRequest,
        context: ToolContext | None = None,
    ) -> SiTerminateSessionResponse:
        """Handle terminate session request.

        Must be implemented by Sponsored Intelligence agents.

        Args:
            request: Validated session termination request
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
