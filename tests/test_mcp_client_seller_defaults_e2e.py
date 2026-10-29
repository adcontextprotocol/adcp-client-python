"""An SDK client at its defaults can call an SDK seller at its defaults (#1692).

On 10.0.0b1 this failed before the handler ran: the MCP client lists tools
before every call, a pinned seller's ``tools/list`` advertised the exact
bundled wire schemas, and an eleven-tool listing of 106,250 JSON members was
over the client's own member bound. Every call raised
``ADCPResponseTooComplexError``.

Nothing is configured here on either side beyond what the issue's seller
set, so a regression on either side — the seller's listing growing, or the
client's bounds tightening — fails these tests.
"""

from __future__ import annotations

import json
import threading
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from adcp import ADCPClient, AgentConfig, Protocol
from adcp._json_bounds import MAX_JSON_MEMBERS
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.responses import capabilities_response
from adcp.types import GetProductsRequest


class _IssueSeller(ADCPHandler):
    """The seller from #1692: eleven sales tools, pinned to 3.2."""

    _adcp_version = "3.2"

    async def get_adcp_capabilities(self, params, context=None):
        # The client negotiates a version through this before its first
        # task call, so the seller must answer it; the issue's seller did.
        return capabilities_response(["media_buy"], idempotency={"supported": False})

    async def get_products(self, params, context=None):
        return {"products": []}

    async def create_media_buy(self, params, context=None): ...

    async def update_media_buy(self, params, context=None): ...

    async def get_media_buys(self, params, context=None): ...

    async def get_media_buy_delivery(self, params, context=None): ...

    async def sync_creatives(self, params, context=None): ...

    async def list_creatives(self, params, context=None): ...

    async def provide_performance_feedback(self, params, context=None): ...

    async def list_creative_formats(self, params, context=None): ...

    async def list_accounts(self, params, context=None): ...

    async def sync_accounts(self, params, context=None): ...


class _DefaultSeller(ADCPHandler):
    """No pin, no ``supported_versions``: the packaged release."""

    advertised_tools = {"get_products"}

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"], idempotency={"supported": False})

    async def get_products(self, params, context=None):
        return {"products": []}


@contextmanager
def _serving(mcp: Any) -> Iterator[str]:
    """Run the MCP app on a loopback port in a thread; yield its tool URL."""
    import uvicorn

    config = uvicorn.Config(
        mcp.streamable_http_app(streamable_http_path="/mcp"),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the MCP server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _members(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(_members(v) for v in value.values())
    if isinstance(value, list):
        return len(value) + sum(_members(v) for v in value)
    return 0


@pytest.mark.parametrize(
    "handler,server_kwargs",
    [
        pytest.param(_IssueSeller, {"supported_versions": ["3.1", "3.2"]}, id="issue-seller"),
        pytest.param(_DefaultSeller, {}, id="default-seller"),
    ],
)
async def test_default_client_calls_get_products_on_default_seller(
    handler: type[ADCPHandler], server_kwargs: dict[str, Any]
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mcp = create_mcp_server(handler(), **server_kwargs)

    with _serving(mcp) as url:
        client = ADCPClient(AgentConfig(id="seller", agent_uri=url, protocol=Protocol.MCP))
        try:
            result = await client.get_products(
                GetProductsRequest(buying_mode="brief", brief="video")
            )
            # The listing the client just read, measured as the client bounds it.
            session = await client.adapter._get_session()
            listing = await session.list_tools()
        finally:
            await client.close()

    assert result.success, (result.error, result.adcp_error)
    assert result.data is not None
    assert result.data.products == []

    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": listing.model_dump(mode="json", by_alias=True, exclude_none=True),
    }
    members = _members(frame)
    assert members <= MAX_JSON_MEMBERS // 2, members
    assert len(json.dumps(frame)) <= 2 * 1024 * 1024
