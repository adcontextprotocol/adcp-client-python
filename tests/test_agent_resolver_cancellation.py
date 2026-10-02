"""Untrusted stateful MCP peers cannot prolong resolver cancellation."""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Literal

import httpx
import pytest

from adcp.signing.agent_resolver import AgentResolverError, _fetch_capabilities


@pytest.mark.parametrize("cancel", [False, True], ids=["deadline", "caller-cancel"])
@pytest.mark.parametrize("stall_at", ["capabilities", "delete"])
async def test_stateful_bootstrap_closes_without_waiting_for_peer(
    cancel: bool, stall_at: Literal["capabilities", "delete"]
) -> None:
    # Complete lazy imports before measuring the network deadline.
    importlib.import_module("adcp.protocols.mcp")
    stalled = asyncio.Event()
    delete_requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            delete_requests.append(request)
            stalled.set()
            await asyncio.Event().wait()
            raise AssertionError("DELETE must be cancelled")

        message = json.loads(request.content)
        method = message["method"]
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unresponsive-peer", "version": "1"},
            }
        elif method.startswith("notifications/"):
            return httpx.Response(202)
        elif method == "tools/list":
            result = {
                "tools": [{"name": "get_adcp_capabilities", "inputSchema": {"type": "object"}}]
            }
        elif method == "tools/call":
            if stall_at == "capabilities":
                stalled.set()
                await asyncio.Event().wait()
                raise AssertionError("capabilities call must be cancelled")
            result = {
                "content": [],
                "structuredContent": {
                    "identity": {"brand_json_url": "https://example.com/brand.json"}
                },
            }
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "session"},
            json={"jsonrpc": "2.0", "id": message["id"], "result": result},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    task = asyncio.create_task(
        _fetch_capabilities(
            "https://example.com/mcp",
            protocol="mcp",
            allow_private=False,
            max_body_bytes=65536,
            max_redirects=0,
            timeout_seconds=0.2,
            client_factory=lambda _: client,
        )
    )
    try:
        await asyncio.wait_for(stalled.wait(), timeout=1)
        if cancel:
            task.cancel()
        # asyncio.wait leaves the task alone: another cancellation would mask
        # the bug where cleanup waits for DELETE after the first cancellation.
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done, "resolver cancellation waited for the unresponsive peer"
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(AgentResolverError) as exc:
                await task
            assert exc.value.code == "capabilities_unreachable"
        assert client.is_closed
        assert len(delete_requests) == (1 if stall_at == "delete" else 0)
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, AgentResolverError):
            pass
        await client.aclose()
