from __future__ import annotations

"""Base protocol adapter interface."""

from abc import ABC, abstractmethod
from typing import Any

from adcp.types.core import AgentConfig, TaskResult


class ProtocolAdapter(ABC):
    """Base class for protocol adapters."""

    def __init__(self, agent_config: AgentConfig):
        """Initialize adapter with agent configuration."""
        self.agent_config = agent_config

    @abstractmethod
    async def call_tool(self, tool_name: str, params: dict[str, Any]) -> TaskResult[Any]:
        """
        Call a tool on the agent.

        Args:
            tool_name: Name of the tool to call
            params: Tool parameters

        Returns:
            TaskResult with the response
        """
        pass

    @abstractmethod
    async def list_tools(self) -> list[str]:
        """
        List available tools from the agent.

        Returns:
            List of tool names
        """
        pass
