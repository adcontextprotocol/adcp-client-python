"""Identity discovery against the SDK's real MCP server and tool schemas."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.server import ADCPHandler, ToolContext, create_mcp_server
from adcp.signing.agent_resolver import (
    DEFAULT_MAX_CAPABILITIES_BYTES,
    _fetch_capabilities,
)


class _IdentityHandler(ADCPHandler[Any]):
    advertised_tools: set[str] = {"get_adcp_capabilities"}

    async def get_adcp_capabilities(
        self, params: Any, context: ToolContext | None = None
    ) -> dict[str, Any]:
        return {"identity": {"brand_json_url": "https://example.com/.well-known/brand.json"}}


@pytest.mark.parametrize("streaming_responses", [False, True])
async def test_discovery_calls_capabilities_without_fetching_tool_inventory(
    streaming_responses: bool,
) -> None:
    # The SDK's capabilities tool schema exceeds the 64 KiB budget. Discovery
    # must call the tool directly rather than fetching an unrelated inventory
    # or validating the identity payload against a full capabilities schema.
    mcp = create_mcp_server(
        _IdentityHandler(),
        name="resolver-integration",
        validation=None,
        stateless_http=True,
        streaming_responses=streaming_responses,
        enable_dns_rebinding_protection=False,
    )
    app = mcp.streamable_http_app()
    methods: list[str] = []

    async def record_request(request: httpx.Request) -> None:
        if request.method == "POST":
            payload = json.loads(await request.aread())
            methods.append(payload["method"])

    async with LifespanManager(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            event_hooks={"request": [record_request]},
        )
        result = await _fetch_capabilities(
            "https://agent.example.com/mcp",
            protocol="mcp",
            allow_private=False,
            max_body_bytes=DEFAULT_MAX_CAPABILITIES_BYTES,
            max_redirects=0,
            timeout_seconds=5,
            client_factory=lambda _: client,
        )

    assert result.body["identity"] == {
        "brand_json_url": "https://example.com/.well-known/brand.json"
    }
    assert "tools/call" in methods
    assert "tools/list" not in methods
    assert client.is_closed
