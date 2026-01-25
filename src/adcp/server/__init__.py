"""ADCP Server Framework.

Provides base classes and adapters for building ADCP-compliant servers/agents.
Supports selective protocol implementation via protocol-specific adapters.

Examples:
    # Content Standards agent (stubs media buy operations)
    from adcp.server import ContentStandardsHandler, create_mcp_tools

    class MyContentHandler(ContentStandardsHandler):
        async def create_content_standards(self, request):
            # Implement your logic
            pass

    # Register with MCP server
    tools = create_mcp_tools(MyContentHandler())
"""

from __future__ import annotations

from adcp.server.base import (
    ADCPHandler,
    NotImplementedResponse,
    ToolContext,
    not_supported,
)
from adcp.server.content_standards import ContentStandardsHandler
from adcp.server.governance import GovernanceHandler
from adcp.server.mcp_tools import MCPToolSet, create_mcp_tools
from adcp.server.proposal import ProposalBuilder, ProposalNotSupported
from adcp.server.sponsored_intelligence import SponsoredIntelligenceHandler

__all__ = [
    # Base classes
    "ADCPHandler",
    "ToolContext",
    "NotImplementedResponse",
    "not_supported",
    # Protocol handlers
    "ContentStandardsHandler",
    "GovernanceHandler",
    "SponsoredIntelligenceHandler",
    # Proposal helpers
    "ProposalBuilder",
    "ProposalNotSupported",
    # MCP integration
    "MCPToolSet",
    "create_mcp_tools",
]
