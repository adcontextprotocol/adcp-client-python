"""Real MCP HTTP failures retain status and WWW-Authenticate diagnostics."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adcp import ADCPClient
from adcp.exceptions import ADCPAuthenticationError
from adcp.protocols.mcp import MCPAdapter
from adcp.types import GetProductsRequest
from adcp.types.core import AgentConfig, Protocol


@pytest.fixture
def rejecting_agent(request):
    status = request.param
    calls = []

    class Unauthorized(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler override
            calls.append(self.path)
            body = b'{"error":"unauthenticated"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", 'Bearer realm="adcp", error="invalid_token"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler override
            self.do_POST()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Unauthorized)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp/", status, calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejecting_agent", [401, 403], indirect=True)
@pytest.mark.parametrize("transport", ["streamable_http", "sse"])
@pytest.mark.parametrize("custom_factory", [False, True])
@pytest.mark.parametrize("raise_on_response", [False, True])
async def test_auth_status_is_typed_and_stops_url_probes(
    rejecting_agent, transport, custom_factory, raise_on_response
):
    import httpx2

    uri, status, calls = rejecting_agent
    seen_responses = []

    async def observe_response(response):
        seen_responses.append(response.status_code)
        if raise_on_response:
            response.raise_for_status()

    def factory(**kwargs):
        return httpx2.AsyncClient(**kwargs, event_hooks={"response": [observe_response]})

    adapter = MCPAdapter(
        AgentConfig(id="seller", agent_uri=uri, protocol=Protocol.MCP, mcp_transport=transport),
        httpx_client_factory=factory if custom_factory else None,
    )
    with pytest.raises(ADCPAuthenticationError) as caught:
        await adapter._get_session()
    assert caught.value.status_code == status
    assert 'error="invalid_token"' in caught.value.www_authenticate
    assert f"HTTP {status}" in str(caught.value)
    assert "auth_token" in str(caught.value)
    assert calls == ["/mcp/"]
    if custom_factory:
        assert seen_responses == [status]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejecting_agent", [401], indirect=True)
async def test_client_result_identifies_bad_token(rejecting_agent):
    uri, _, _ = rejecting_agent
    client = ADCPClient(
        AgentConfig(
            id="seller",
            agent_uri=uri,
            protocol=Protocol.MCP,
            auth_token="wrong-token",
            requires_auth=True,
        )
    )
    result = await client.get_products(GetProductsRequest(buying_mode="brief", brief="video"))
    assert not result.success
    assert "HTTP 401" in result.error
    assert "invalid_token" in result.error
    assert "auth_token" in result.error
    assert "Check that the agent URI is correct" not in result.error
    assert "wrong-token" not in result.error


@pytest.fixture
def initialized_agent(request):
    status = request.param

    class InitializedAgent(BaseHTTPRequestHandler):
        def send_json(self, payload, http_status=200):
            body = json.dumps(payload).encode()
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if http_status in (401, 403):
                self.send_header("WWW-Authenticate", 'Bearer error="invalid_token"')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler override
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if message["method"] == "initialize":
                self.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "protocolVersion": message["params"]["protocolVersion"],
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "auth-agent", "version": "1"},
                        },
                    }
                )
            elif message["method"].startswith("notifications/"):
                self.send_response(202)
                self.end_headers()
            elif message["method"] == "tools/list":
                self.send_json({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}})
            elif message["params"]["arguments"]["brief"] == "allowed":
                payload = {"products": [], "cache_scope": "public", "status": "completed"}
                self.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(payload)}],
                            "structuredContent": payload,
                        },
                    }
                )
            else:
                self.send_json({"error": "unauthenticated"}, status)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler override
            self.send_response(405)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), InitializedAgent)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp/", status
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized_agent", [401, 403], indirect=True)
@pytest.mark.parametrize("raising_hook", [False, True])
async def test_reused_session_auth_failure_is_scoped_to_its_call(initialized_agent, raising_hook):
    import httpx2

    from adcp.protocols.mcp import _MCP_HTTP_AUTH_ERRORS

    uri, status = initialized_agent

    async def check_status(response):
        response.raise_for_status()

    def factory(**kwargs):
        return httpx2.AsyncClient(**kwargs, event_hooks={"response": [check_status]})

    adapter = MCPAdapter(
        AgentConfig(id="seller", agent_uri=uri, protocol=Protocol.MCP),
        httpx_client_factory=factory if raising_hook else None,
    )
    try:
        await adapter._get_session()
        denied, allowed = await asyncio.gather(
            adapter._call_mcp_tool("get_products", {"buying_mode": "brief", "brief": "denied"}),
            adapter._call_mcp_tool("get_products", {"buying_mode": "brief", "brief": "allowed"}),
        )
        assert not denied.success
        assert f"HTTP {status}" in denied.error
        assert "invalid_token" in denied.error
        assert "auth_token" in denied.error
        assert allowed.success
        assert _MCP_HTTP_AUTH_ERRORS.get() is None
        followup = await adapter._call_mcp_tool(
            "get_products", {"buying_mode": "brief", "brief": "allowed"}
        )
        assert followup.success
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_non_auth_response_hook_errors_still_propagate():
    import httpx2

    from adcp.protocols.mcp import _preserve_http_auth_response

    async def check_status(response):
        response.raise_for_status()

    response = httpx2.Response(500, request=httpx2.Request("POST", "http://seller.example/mcp/"))
    with pytest.raises(httpx2.HTTPStatusError):
        await _preserve_http_auth_response(check_status)(response)


@pytest.mark.asyncio
async def test_caller_cancellation_resets_auth_diagnostics():
    from adcp.protocols.mcp import _MCP_HTTP_AUTH_ERRORS

    started = asyncio.Event()
    after_call = []

    class PendingSession:
        async def call_tool(self, *args):
            errors = _MCP_HTTP_AUTH_ERRORS.get()
            assert errors is not None
            errors.append(ADCPAuthenticationError("HTTP 401", status_code=401))
            started.set()
            await asyncio.Event().wait()

    adapter = MCPAdapter(
        AgentConfig(id="seller", agent_uri="http://seller.example/mcp/", protocol=Protocol.MCP)
    )
    adapter._inject_session(PendingSession())

    async def call():
        try:
            return await adapter._call_mcp_tool(
                "get_products", {"buying_mode": "brief", "brief": "video"}
            )
        finally:
            after_call.append(_MCP_HTTP_AUTH_ERRORS.get())

    pending = asyncio.create_task(call())
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert after_call == [None]
