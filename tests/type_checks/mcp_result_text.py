"""Typed sync/async MCP-only summary callbacks and field shorthand."""

from typing import Any

from adcp.decisioning import DecisioningPlatform
from adcp.server import (
    ADCPHandler,
    MCPResultText,
    ServeConfig,
    ToolContext,
    create_mcp_server,
    serve,
)
from adcp.testing import build_asgi_app, build_test_client


def summarize(name: str, result: dict[str, Any], context: ToolContext | None) -> str | None:
    return f"{name}: {len(result.get('products', []))} products"


async def async_summarize(
    name: str, result: dict[str, Any], context: ToolContext | None
) -> str | None:
    return summarize(name, result, context)


def configure(handler: ADCPHandler[ToolContext], platform: DecisioningPlatform) -> None:
    hook: MCPResultText = summarize
    serve(handler, config=ServeConfig(transport="both", mcp_result_text=hook))
    create_mcp_server(handler, mcp_result_text=async_summarize)
    create_mcp_server(handler, mcp_result_text="message")
    build_asgi_app(platform, mcp_result_text=hook)


async def check(platform: DecisioningPlatform) -> None:
    async with build_test_client(
        platform, transport="both", mcp_result_text=async_summarize
    ) as client:
        response = await client.get("/.well-known/agent-card.json")
        assert response.status_code == 200
