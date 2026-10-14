"""Agent responses are capped by default, enforced while the body streams in.

Covers the transport-level limiter directly and end-to-end through both the
MCP and A2A call paths. See adcontextprotocol/adcp#8190 for the cross-SDK
contract these tests encode.
"""

from __future__ import annotations

import gzip
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from adcp import ADCPClient, TaskOptions, ValidationHookConfig
from adcp._response_limit import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseLimitPolicy,
    _EventSizeTracker,
    install_response_limit,
    reset_call_max_response_bytes,
    resolve_max_response_bytes,
    set_call_max_response_bytes,
)
from adcp.exceptions import ADCPProtocolError, ADCPResponseTooLargeError
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types import GetProductsRequest
from adcp.types.core import AgentConfig, Protocol

# ---------------------------------------------------------------------------
# Transport-level enforcement
# ---------------------------------------------------------------------------


def _limited_client(handler, limit, *, module=httpx):
    client = module.AsyncClient(transport=module.MockTransport(handler))
    return install_response_limit(
        client,
        httpx_module=module,
        policy=ResponseLimitPolicy(limit_provider=lambda: limit),
    )


def _raw_deflate(data: bytes) -> bytes:
    """Compress without the zlib wrapper — the other `deflate` seen in the wild."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _streaming(chunks, headers=None, *, module=httpx):
    """Build a handler returning a body that is only produced as it is read."""
    reads: list[int] = []

    def handler(request):
        async def body():
            for chunk in chunks:
                reads.append(len(chunk))
                yield chunk

        return module.Response(200, content=body(), headers=headers or {})

    return handler, reads


@pytest.mark.asyncio
async def test_streamed_body_over_cap_is_rejected_without_content_length():
    handler, reads = _streaming([b"x" * 128] * 64, {"content-type": "application/json"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get("https://agent.example/tool")

    assert caught.value.limit == 1024
    assert caught.value.received > 1024
    assert caught.value.declared is False
    # Enforced while reading: the body was abandoned partway, not buffered.
    assert sum(reads) <= 1024 + 128


@pytest.mark.asyncio
async def test_declared_content_length_is_rejected_before_any_body_byte():
    handler, reads = _streaming([b"x" * 50_000], {"content-length": "50000"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get("https://agent.example/tool")

    assert caught.value.declared is True
    assert caught.value.received == 50_000
    assert reads == []


@pytest.mark.asyncio
async def test_under_cap_response_passes_through_untouched():
    payload = json.dumps({"products": ["a", "b"]}).encode()
    handler, _ = _streaming([payload], {"content-type": "application/json"})
    client = _limited_client(handler, 1024)

    response = await client.get("https://agent.example/tool")

    assert response.json() == {"products": ["a", "b"]}


@pytest.mark.asyncio
async def test_single_oversized_sse_event_is_rejected():
    event = b"data: " + b"z" * 4000 + b"\n\n"
    handler, _ = _streaming([event], {"content-type": "text/event-stream"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        async with client.stream("GET", "https://agent.example/tool") as response:
            async for _ in response.aiter_bytes():
                pass

    assert caught.value.per_event is True


@pytest.mark.asyncio
async def test_cumulative_sse_events_over_cap_are_rejected():
    events = [b"data: " + b"z" * 100 + b"\n\n"] * 50
    handler, _ = _streaming(events, {"content-type": "text/event-stream"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        async with client.stream("GET", "https://agent.example/tool") as response:
            async for _ in response.aiter_bytes():
                pass

    # No single event came close; the per-call total is what tripped.
    assert caught.value.per_event is False
    assert caught.value.received > 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding,compress",
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("deflate", _raw_deflate),
    ],
)
async def test_compressed_bomb_is_measured_decompressed(encoding, compress):
    payload = compress(b"0" * 5_000_000)
    assert len(payload) < 16_384, "the bomb must fit under the cap on the wire"
    handler, _ = _streaming([payload], {"content-encoding": encoding})
    client = _limited_client(handler, 16_384)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get("https://agent.example/tool")

    assert caught.value.received > 16_384


@pytest.mark.asyncio
async def test_under_cap_compressed_response_still_decodes():
    handler, _ = _streaming([gzip.compress(b'{"ok":true}')], {"content-encoding": "gzip"})
    client = _limited_client(handler, 1024)

    response = await client.get("https://agent.example/tool")

    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_uncountable_content_encoding_is_refused():
    handler, _ = _streaming([b"whatever"], {"content-encoding": "br"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPProtocolError) as caught:
        await client.get("https://agent.example/tool")

    assert "br" in str(caught.value)


@pytest.mark.asyncio
async def test_accept_encoding_is_pinned_to_countable_codings():
    handler, _ = _streaming([b"{}"])
    client = _limited_client(handler, 1024)

    assert client.headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.asyncio
async def test_none_limit_opts_out_entirely():
    handler, _ = _streaming([b"x" * 100_000])
    client = _limited_client(handler, None)

    response = await client.get("https://agent.example/tool")

    assert len(response.content) == 100_000


@pytest.mark.asyncio
async def test_limit_is_re_read_per_request():
    handler, _ = _streaming([b"x" * 2048])
    client = _limited_client(handler, lambda: None)
    # Swap the provider for one driven by the per-call ContextVar.
    client._transport._limit_provider = lambda: resolve_max_response_bytes(8192)

    assert len((await client.get("https://agent.example/tool")).content) == 2048

    token = set_call_max_response_bytes(1024)
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await client.get("https://agent.example/tool")
    finally:
        reset_call_max_response_bytes(token)


def test_event_size_tracker_is_invariant_to_chunk_splits():
    stream = b"data: aaa\n\ndata: bbbb\r\n\r\ndata: ccccc\n\ndata: partial"
    for size in range(1, 13):
        tracker = _EventSizeTracker()
        sizes = []
        for offset in range(0, len(stream), size):
            sizes.extend(tracker.feed(stream[offset : offset + size]))
        assert sizes == [9, 10, 11], f"chunk size {size}"
        assert tracker.pending == 13, f"chunk size {size}"


# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------


def test_cap_is_on_by_default_at_four_mib():
    config = AgentConfig(id="s", agent_uri="https://agent.example", protocol=Protocol.MCP)

    assert DEFAULT_MAX_RESPONSE_BYTES == 4 * 1024 * 1024
    assert config.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES


@pytest.mark.parametrize("value", [0, -1, True])
def test_agent_config_rejects_a_non_positive_cap(value):
    with pytest.raises(ValueError, match="max_response_bytes"):
        AgentConfig(
            id="s",
            agent_uri="https://agent.example",
            protocol=Protocol.MCP,
            max_response_bytes=value,
        )


def test_agent_config_accepts_an_explicit_opt_out():
    config = AgentConfig(
        id="s",
        agent_uri="https://agent.example",
        protocol=Protocol.MCP,
        max_response_bytes=None,
    )

    assert config.max_response_bytes is None


@pytest.mark.parametrize("value", [0, -5, True])
def test_task_options_rejects_a_non_positive_cap(value):
    with pytest.raises(ValueError, match="max_response_bytes"):
        TaskOptions(max_response_bytes=value)


def test_task_options_default_inherits_the_agent_budget():
    assert TaskOptions().max_response_bytes is None
    assert resolve_max_response_bytes(1234) == 1234


# ---------------------------------------------------------------------------
# MCP end to end
# ---------------------------------------------------------------------------


class _MCPHandler(BaseHTTPRequestHandler):
    """Smallest streamable-HTTP MCP server that can answer one tool call."""

    protocol_version = "HTTP/1.1"
    tool_payload_size = 0

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler override
        message = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        if "id" not in message:  # notification
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oversize-agent", "version": "1"},
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": "x" * self.tool_payload_size}],
                "structuredContent": {"products": []},
            }
        else:
            result = {"tools": []}
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("mcp-session-id", "session-1")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler override
        self.send_response(405)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_DELETE(self):  # noqa: N802 - BaseHTTPRequestHandler override
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def mcp_agent(request):
    handler = type("_Handler", (_MCPHandler,), {"tool_payload_size": request.param})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp/"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def _mcp_adapter(uri, **config):
    adapter = MCPAdapter(AgentConfig(id="seller", agent_uri=uri, protocol=Protocol.MCP, **config))
    adapter.response_validation_mode = "warn"
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [400_000], indirect=True)
async def test_mcp_tool_call_over_cap_raises_typed(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseTooLargeError) as caught:
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert caught.value.limit == 50_000
    assert caught.value.agent_id == "seller"


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [1_000], indirect=True)
async def test_mcp_tool_call_under_cap_succeeds(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        result = await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert result.data == {"products": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [400_000], indirect=True)
async def test_mcp_per_call_override_raises_the_budget(mcp_agent):
    client = ADCPClient(
        AgentConfig(
            id="seller",
            agent_uri=mcp_agent,
            protocol=Protocol.MCP,
            max_response_bytes=50_000,
        ),
        validation=ValidationHookConfig(responses="warn"),
    )
    try:
        request = GetProductsRequest(buying_mode="brief", brief="video")
        with pytest.raises(ADCPResponseTooLargeError):
            await client.get_products(request)

        result = await client.get_products(
            request,
            options=TaskOptions(max_response_bytes=4 * 1024 * 1024),
        )
        assert result.success
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [400_000], indirect=True)
async def test_mcp_opt_out_disables_the_budget(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=None)
    try:
        result = await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert result.data == {"products": []}


# ---------------------------------------------------------------------------
# A2A end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def oversized_http_agent():
    class Oversized(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler override
            body = b"y" * 400_000
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Oversized)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.asyncio
async def test_a2a_client_meters_every_response(oversized_http_agent):
    adapter = A2AAdapter(
        AgentConfig(
            id="seller",
            agent_uri=oversized_http_agent,
            protocol=Protocol.A2A,
            max_response_bytes=50_000,
        )
    )
    try:
        client = await adapter._get_httpx_client()
        with pytest.raises(ADCPResponseTooLargeError) as caught:
            await client.get(f"{oversized_http_agent}/anything")
    finally:
        await adapter.close()

    assert caught.value.limit == 50_000
    assert caught.value.agent_id == "seller"


@pytest.mark.asyncio
async def test_a2a_agent_card_fetch_is_capped(oversized_http_agent):
    adapter = A2AAdapter(
        AgentConfig(
            id="seller",
            agent_uri=oversized_http_agent,
            protocol=Protocol.A2A,
            max_response_bytes=50_000,
        )
    )
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await adapter._get_a2a_client()
    finally:
        await adapter.close()
