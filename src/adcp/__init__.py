from __future__ import annotations

"""
AdCP Python Client Library

Official Python client for the Ad Context Protocol (AdCP).
Supports both A2A and MCP protocols with full type safety.
"""

from adcp.client import ADCPClient, ADCPMultiAgentClient
from adcp.exceptions import (
    ADCPAuthenticationError,
    ADCPConnectionError,
    ADCPError,
    ADCPProtocolError,
    ADCPTimeoutError,
    ADCPToolNotFoundError,
    ADCPWebhookError,
    ADCPWebhookSignatureError,
)
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus, WebhookMetadata

__version__ = "0.1.3"

__all__ = [
    "ADCPClient",
    "ADCPMultiAgentClient",
    "AgentConfig",
    "Protocol",
    "TaskResult",
    "TaskStatus",
    "WebhookMetadata",
    "ADCPError",
    "ADCPConnectionError",
    "ADCPAuthenticationError",
    "ADCPTimeoutError",
    "ADCPProtocolError",
    "ADCPToolNotFoundError",
    "ADCPWebhookError",
    "ADCPWebhookSignatureError",
]
