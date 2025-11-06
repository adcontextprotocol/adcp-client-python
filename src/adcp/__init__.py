from __future__ import annotations

"""
AdCP Python Client Library

Official Python client for the Ad Context Protocol (AdCP).
Supports both A2A and MCP protocols with full type safety.
"""

from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.types.core import AgentConfig, TaskResult, WebhookMetadata

__version__ = "0.1.2"
__all__ = [
    "ADCPClient",
    "ADCPMultiAgentClient",
    "AgentConfig",
    "TaskResult",
    "WebhookMetadata",
]
