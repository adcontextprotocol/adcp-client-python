"""Tests for AgentConfig.extra_headers pass-through to protocol adapters."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types.core import AgentConfig, Protocol


def _make_mcp_adapter(**overrides: Any) -> MCPAdapter:
    config = AgentConfig(
        id="mcp-seller",
        agent_uri="https://seller.example.com/mcp",
        protocol=Protocol.MCP,
        **overrides,
    )
    return MCPAdapter(config)


def _make_a2a_adapter(**overrides: Any) -> A2AAdapter:
    config = AgentConfig(
        id="a2a-seller",
        agent_uri="https://seller.example.com/a2a",
        protocol=Protocol.A2A,
        **overrides,
    )
    return A2AAdapter(config)


@pytest.mark.asyncio
async def test_mcp_extra_headers_forwarded_to_streamable_http_transport() -> None:
    adapter = _make_mcp_adapter(
        auth_token="tok",
        extra_headers={"x-adcp-tenant": "acme", "x-trace-id": "abc"},
    )

    captured: dict[str, Any] = {}

    def _fake(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    with patch("adcp.protocols.mcp.streamablehttp_client", side_effect=_fake):
        with pytest.raises(Exception):
            await adapter._get_session()

    headers = captured["headers"]
    assert headers["x-adcp-auth"] == "tok"
    assert headers["x-adcp-tenant"] == "acme"
    assert headers["x-trace-id"] == "abc"


@pytest.mark.asyncio
async def test_mcp_extra_headers_forwarded_to_sse_transport() -> None:
    adapter = _make_mcp_adapter(
        mcp_transport="sse",
        auth_token="tok",
        extra_headers={"x-adcp-tenant": "acme"},
    )

    captured: dict[str, Any] = {}

    def _fake(*args: Any, **kwargs: Any) -> Any:
        # sse_client is called positionally for url, then headers kwarg
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise RuntimeError("stop")

    with patch("adcp.protocols.mcp.sse_client", side_effect=_fake):
        with pytest.raises(Exception):
            await adapter._get_session()

    headers = captured["kwargs"]["headers"]
    assert headers["x-adcp-auth"] == "tok"
    assert headers["x-adcp-tenant"] == "acme"


@pytest.mark.asyncio
async def test_mcp_no_extra_headers_passes_only_auth() -> None:
    adapter = _make_mcp_adapter(auth_token="tok")

    captured: dict[str, Any] = {}

    def _fake(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    with patch("adcp.protocols.mcp.streamablehttp_client", side_effect=_fake):
        with pytest.raises(Exception):
            await adapter._get_session()

    assert captured["headers"] == {"x-adcp-auth": "tok"}


@pytest.mark.asyncio
async def test_a2a_extra_headers_attached_to_httpx_client() -> None:
    adapter = _make_a2a_adapter(
        auth_token="tok",
        extra_headers={"x-adcp-tenant": "acme", "x-trace-id": "abc"},
    )

    client = await adapter._get_httpx_client()
    try:
        assert client.headers["x-adcp-auth"] == "tok"
        assert client.headers["x-adcp-tenant"] == "acme"
        assert client.headers["x-trace-id"] == "abc"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a2a_no_extra_headers_only_auth() -> None:
    adapter = _make_a2a_adapter(auth_token="tok")

    client = await adapter._get_httpx_client()
    try:
        assert client.headers["x-adcp-auth"] == "tok"
        assert "x-adcp-tenant" not in client.headers
    finally:
        await client.aclose()
