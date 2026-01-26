"""MCP server integration helpers.

Provides utilities for registering ADCP handlers with MCP servers.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from adcp.server.base import ADCPHandler, ToolContext

if TYPE_CHECKING:
    pass


# Tool definitions for all ADCP operations
ADCP_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    # Core Catalog Operations
    {
        "name": "get_products",
        "description": "Get advertising products from the catalog",
        "inputSchema": {
            "type": "object",
            "properties": {
                "context": {"type": "object"},
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "list_creative_formats",
        "description": "List supported creative formats",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "format_id": {"type": "string"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "list_authorized_properties",
        "description": "List properties authorized for ad placement",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
            },
        },
    },
    # Creative Operations
    {
        "name": "sync_creatives",
        "description": "Sync creatives to the agent",
        "inputSchema": {
            "type": "object",
            "properties": {
                "creatives": {"type": "array"},
            },
            "required": ["creatives"],
        },
    },
    {
        "name": "list_creatives",
        "description": "List synced creatives",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "build_creative",
        "description": "Build a creative from assets",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format_id": {"type": "string"},
                "assets": {"type": "array"},
            },
            "required": ["format_id", "assets"],
        },
    },
    # Media Buy Operations
    {
        "name": "create_media_buy",
        "description": "Create a media buy from products",
        "inputSchema": {
            "type": "object",
            "properties": {
                "packages": {"type": "array"},
                "proposal_id": {"type": "string"},
            },
        },
    },
    {
        "name": "update_media_buy",
        "description": "Update an existing media buy",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "packages": {"type": "array"},
            },
            "required": ["media_buy_id"],
        },
    },
    {
        "name": "get_media_buy_delivery",
        "description": "Get delivery metrics for a media buy",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["media_buy_id"],
        },
    },
    # Signal Operations
    {
        "name": "get_signals",
        "description": "Get available signals",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filters": {"type": "object"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "activate_signal",
        "description": "Activate a signal for use",
        "inputSchema": {
            "type": "object",
            "properties": {
                "signal_id": {"type": "string"},
                "activation_key": {"type": "string"},
            },
            "required": ["signal_id"],
        },
    },
    # Feedback Operations
    {
        "name": "provide_performance_feedback",
        "description": "Provide performance feedback",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
                "feedback": {"type": "object"},
            },
            "required": ["media_buy_id", "feedback"],
        },
    },
    # V3 Protocol Discovery
    {
        "name": "get_adcp_capabilities",
        "description": "Get ADCP capabilities supported by this agent",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # V3 Content Standards
    {
        "name": "create_content_standards",
        "description": "Create content standards configuration",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rules": {"type": "array"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_content_standards",
        "description": "Get content standards configuration",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
            },
            "required": ["content_standards_id"],
        },
    },
    {
        "name": "list_content_standards",
        "description": "List content standards configurations",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "update_content_standards",
        "description": "Update content standards configuration",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "rules": {"type": "array"},
            },
            "required": ["content_standards_id"],
        },
    },
    {
        "name": "calibrate_content",
        "description": "Calibrate content against standards",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "content": {"type": "object"},
            },
            "required": ["content_standards_id", "content"],
        },
    },
    {
        "name": "validate_content_delivery",
        "description": "Validate content delivery against standards",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content_standards_id": {"type": "string"},
                "delivery": {"type": "object"},
            },
            "required": ["content_standards_id", "delivery"],
        },
    },
    {
        "name": "get_media_buy_artifacts",
        "description": "Get artifacts associated with a media buy",
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_buy_id": {"type": "string"},
            },
            "required": ["media_buy_id"],
        },
    },
    # V3 Sponsored Intelligence
    {
        "name": "si_get_offering",
        "description": "Get sponsored intelligence offering",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "si_initiate_session",
        "description": "Initiate sponsored intelligence session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "budget": {"type": "number"},
            },
        },
    },
    {
        "name": "si_send_message",
        "description": "Send message in sponsored intelligence session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "si_terminate_session",
        "description": "Terminate sponsored intelligence session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    },
    # V3 Governance (Property Lists)
    {
        "name": "create_property_list",
        "description": "Create a property list for governance filtering",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "base_properties": {"type": "array"},
                "filters": {"type": "object"},
                "brand_manifest": {"type": "object"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_property_list",
        "description": "Get a property list with optional resolution",
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "resolve": {"type": "boolean"},
                "pagination": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "list_property_lists",
        "description": "List property lists",
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal": {"type": "string"},
                "pagination": {"type": "object"},
            },
        },
    },
    {
        "name": "update_property_list",
        "description": "Update a property list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "filters": {"type": "object"},
                "brand_manifest": {"type": "object"},
            },
            "required": ["list_id"],
        },
    },
    {
        "name": "delete_property_list",
        "description": "Delete a property list",
        "inputSchema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
            },
            "required": ["list_id"],
        },
    },
]


def create_tool_caller(
    handler: ADCPHandler,
    method_name: str,
) -> Callable[[dict[str, Any]], Any]:
    """Create a tool caller function for an ADCP handler method.

    Args:
        handler: The ADCP handler instance
        method_name: Name of the method to call

    Returns:
        Async callable that invokes the handler method
    """
    method = getattr(handler, method_name)

    async def call_tool(params: dict[str, Any]) -> Any:
        context = ToolContext()
        result = await method(params, context)
        # Convert Pydantic models to dicts for MCP serialization
        if hasattr(result, "model_dump"):
            return result.model_dump(exclude_none=True)
        return result

    return call_tool


class MCPToolSet:
    """Collection of MCP tools from an ADCP handler.

    Provides tool definitions and handlers for registering with an MCP server.
    """

    def __init__(self, handler: ADCPHandler):
        """Create tool set from handler.

        Args:
            handler: ADCP handler instance
        """
        self.handler = handler
        self._tools: dict[str, Callable[[dict[str, Any]], Any]] = {}

        # Create tool callers for all methods
        for tool_def in ADCP_TOOL_DEFINITIONS:
            name = tool_def["name"]
            self._tools[name] = create_tool_caller(handler, name)

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        """Get MCP tool definitions."""
        return ADCP_TOOL_DEFINITIONS.copy()

    async def call_tool(self, name: str, params: dict[str, Any]) -> Any:
        """Call a tool by name.

        Args:
            name: Tool name
            params: Tool parameters

        Returns:
            Tool result

        Raises:
            KeyError: If tool not found
        """
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return await self._tools[name](params)

    def get_tool_names(self) -> list[str]:
        """Get list of available tool names."""
        return list(self._tools.keys())


def create_mcp_tools(handler: ADCPHandler) -> MCPToolSet:
    """Create MCP tools from an ADCP handler.

    This is the main entry point for MCP server integration.

    Example with mcp library:
        from mcp.server import Server
        from adcp.server import ContentStandardsHandler, create_mcp_tools

        class MyHandler(ContentStandardsHandler):
            # ... implement methods

        handler = MyHandler()
        tools = create_mcp_tools(handler)

        server = Server("my-content-agent")

        @server.list_tools()
        async def list_tools():
            return tools.tool_definitions

        @server.call_tool()
        async def call_tool(name: str, arguments: dict):
            return await tools.call_tool(name, arguments)

    Args:
        handler: ADCP handler instance

    Returns:
        MCPToolSet with tool definitions and handlers
    """
    return MCPToolSet(handler)
