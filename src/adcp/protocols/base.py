from __future__ import annotations

"""Base protocol adapter interface."""

from abc import ABC, abstractmethod
from typing import Any, TypeVar

from pydantic import BaseModel

from adcp.types.core import AgentConfig, TaskResult, TaskStatus
from adcp.utils.response_parser import parse_json_or_text, parse_mcp_content

T = TypeVar("T", bound=BaseModel)


class ProtocolAdapter(ABC):
    """
    Base class for protocol adapters.

    Each adapter implements the ADCP protocol methods and handles
    protocol-specific translation (MCP/A2A) while returning properly
    typed responses.
    """

    def __init__(self, agent_config: AgentConfig):
        """Initialize adapter with agent configuration."""
        self.agent_config = agent_config

    # ========================================================================
    # Helper methods for response parsing
    # ========================================================================

    def _parse_response(
        self, raw_result: TaskResult[Any], response_type: type[T] | Any
    ) -> TaskResult[T]:
        """
        Parse raw TaskResult into typed TaskResult.

        Handles both MCP content arrays and A2A dict responses.
        Supports both single types and Union types (for oneOf discriminated unions).

        Args:
            raw_result: Raw TaskResult from adapter
            response_type: Expected Pydantic response type (can be a Union type)

        Returns:
            Typed TaskResult
        """
        # Handle failed results or interim states without data
        # For A2A: interim states (submitted/working) have data=None but success=True
        # For MCP: completed tasks always have data, missing data indicates failure
        if not raw_result.success or raw_result.data is None:
            # If already marked as unsuccessful, preserve that
            # If successful but no data (A2A interim state), preserve success=True
            return TaskResult[T](
                status=raw_result.status,
                data=None,
                message=raw_result.message,
                success=raw_result.success,  # Preserve original success state
                error=raw_result.error,  # Only use error if one was set
                metadata=raw_result.metadata,
                debug_info=raw_result.debug_info,
            )

        try:
            # Handle MCP content arrays
            if isinstance(raw_result.data, list):
                parsed_data = parse_mcp_content(raw_result.data, response_type)
            else:
                # Handle A2A or direct responses
                parsed_data = parse_json_or_text(raw_result.data, response_type)

            return TaskResult[T](
                status=raw_result.status,
                data=parsed_data,
                message=raw_result.message,  # Preserve human-readable message from protocol
                success=raw_result.success,
                error=raw_result.error,
                metadata=raw_result.metadata,
                debug_info=raw_result.debug_info,
            )
        except ValueError as e:
            # Parsing failed - return error result
            return TaskResult[T](
                status=TaskStatus.FAILED,
                error=f"Failed to parse response: {e}",
                message=raw_result.message,
                success=False,
                debug_info=raw_result.debug_info,
            )

    # ========================================================================
    # ADCP Protocol Methods - Type-safe, spec-aligned interface
    # Each adapter MUST implement these methods explicitly.
    # ========================================================================

    @abstractmethod
    async def get_products(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get advertising products."""
        pass

    @abstractmethod
    async def list_creative_formats(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List supported creative formats."""
        pass

    @abstractmethod
    async def sync_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Sync creatives."""
        pass

    @abstractmethod
    async def list_creatives(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List creatives."""
        pass

    @abstractmethod
    async def get_media_buy_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get media buy delivery."""
        pass

    @abstractmethod
    async def list_authorized_properties(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List authorized properties."""
        pass

    @abstractmethod
    async def get_signals(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get signals."""
        pass

    @abstractmethod
    async def activate_signal(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Activate signal."""
        pass

    @abstractmethod
    async def provide_performance_feedback(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Provide performance feedback."""
        pass

    @abstractmethod
    async def create_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create media buy."""
        pass

    @abstractmethod
    async def update_media_buy(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update media buy."""
        pass

    @abstractmethod
    async def build_creative(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Build creative."""
        pass

    @abstractmethod
    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        pass

    @abstractmethod
    async def get_agent_info(self) -> dict[str, Any]:
        """
        Get agent information including AdCP extension metadata.

        Returns agent card information including:
        - Agent name, description, version
        - AdCP version (from extensions.adcp.adcp_version)
        - Supported protocols (from extensions.adcp.protocols_supported)
        - Available tools/skills

        Returns:
            Dictionary with agent metadata including AdCP extension fields
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close the adapter and clean up resources.

        Implementations should close any open connections, clients, or other resources.
        """
        pass

    # ========================================================================
    # V3 Protocol Methods - Protocol Discovery
    # ========================================================================

    @abstractmethod
    async def get_adcp_capabilities(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get AdCP capabilities from the agent."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Content Standards
    # ========================================================================

    @abstractmethod
    async def create_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create content standards configuration."""
        pass

    @abstractmethod
    async def get_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get content standards configuration."""
        pass

    @abstractmethod
    async def list_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List content standards configurations."""
        pass

    @abstractmethod
    async def update_content_standards(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update content standards configuration."""
        pass

    @abstractmethod
    async def calibrate_content(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Calibrate content against standards."""
        pass

    @abstractmethod
    async def validate_content_delivery(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Validate content delivery against standards."""
        pass

    @abstractmethod
    async def get_media_buy_artifacts(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get artifacts associated with a media buy."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Sponsored Intelligence
    # ========================================================================

    @abstractmethod
    async def si_get_offering(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get sponsored intelligence offering."""
        pass

    @abstractmethod
    async def si_initiate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Initiate sponsored intelligence session."""
        pass

    @abstractmethod
    async def si_send_message(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Send message in sponsored intelligence session."""
        pass

    @abstractmethod
    async def si_terminate_session(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Terminate sponsored intelligence session."""
        pass

    # ========================================================================
    # V3 Protocol Methods - Governance (Property Lists)
    # ========================================================================

    @abstractmethod
    async def create_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Create a property list for governance."""
        pass

    @abstractmethod
    async def get_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Get a property list with optional resolution."""
        pass

    @abstractmethod
    async def list_property_lists(self, params: dict[str, Any]) -> TaskResult[Any]:
        """List property lists."""
        pass

    @abstractmethod
    async def update_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Update a property list."""
        pass

    @abstractmethod
    async def delete_property_list(self, params: dict[str, Any]) -> TaskResult[Any]:
        """Delete a property list."""
        pass
