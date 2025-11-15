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
        # Handle failed results or missing data
        if not raw_result.success or raw_result.data is None:
            # Explicitly construct typed result to satisfy type checker
            return TaskResult[T](
                status=raw_result.status,
                data=None,
                message=raw_result.message,
                success=False,
                error=raw_result.error or "No data returned from adapter",
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
    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Close the adapter and clean up resources.

        Implementations should close any open connections, clients, or other resources.
        """
        pass
