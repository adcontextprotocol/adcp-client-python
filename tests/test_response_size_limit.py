"""Agent responses are capped by default, enforced while the body streams in.

Covers the transport-level limiter directly and end-to-end through both the
MCP and A2A call paths. See adcontextprotocol/adcp#8190 for the cross-SDK
contract these tests encode.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from adcp import ADCPClient, TaskOptions, ValidationHookConfig
from adcp._response_limit import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseLimitPolicy,
    _DecodedCounter,
    _EventSizeTracker,
    install_response_limit,
    reset_call_max_response_bytes,
    resolve_max_response_bytes,
    set_call_max_response_bytes,
)
from adcp.exceptions import ADCPResponseEncodingError, ADCPResponseTooLargeError
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.types import GetProductsRequest
from adcp.types.core import AgentConfig, Protocol

# ---------------------------------------------------------------------------
# Transport-level enforcement
# ---------------------------------------------------------------------------


def _limited_client(handler, limit, *, module=httpx, agent_id=None):
    client = module.AsyncClient(transport=module.MockTransport(handler))
    return install_response_limit(
        client,
        httpx_module=module,
        policy=ResponseLimitPolicy(limit_provider=lambda _long_lived: limit, agent_id=agent_id),
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
        # POST: a streamable-HTTP tool call answered as an event stream. One
        # call, so the cumulative budget applies.
        async with client.stream("POST", "https://agent.example/tool") as response:
            async for _ in response.aiter_bytes():
                pass

    # No single event came close; the per-call total is what tripped.
    assert caught.value.per_event is False
    assert caught.value.received > 1024


@pytest.mark.asyncio
async def test_session_lifetime_get_stream_is_capped_per_event_only():
    """A standing server-push channel must not fail just for running long."""
    events = [b"data: " + b"z" * 100 + b"\n\n"] * 50
    handler, _ = _streaming(events, {"content-type": "text/event-stream"})
    client = _limited_client(handler, 1024)

    async with client.stream("GET", "https://agent.example/stream") as response:
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)

    assert total > 1024

    # One oversized event on that same channel is still refused.
    handler, _ = _streaming(
        [b"data: " + b"z" * 4000 + b"\n\n"], {"content-type": "text/event-stream"}
    )
    client = _limited_client(handler, 1024)
    with pytest.raises(ADCPResponseTooLargeError) as caught:
        async with client.stream("GET", "https://agent.example/stream") as response:
            async for _ in response.aiter_bytes():
                pass
    assert caught.value.per_event is True


@pytest.mark.asyncio
async def test_padded_compressed_stream_trips_the_wire_ceiling():
    """Empty deflate blocks decode to nothing; the wire still has to end."""
    padding = b"\x00\x00\x00\xff\xff" * 40_000  # ~200 KB, decodes to 0 bytes
    handler, _ = _streaming(
        [zlib.compressobj().compress(b"") + padding], {"content-encoding": "deflate"}
    )
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get("https://agent.example/tool")

    assert caught.value.received > 1024


@pytest.mark.asyncio
async def test_bomb_measurement_does_not_allocate_a_multiple_of_the_budget():
    """The limiter must not become the allocation it is defending against."""
    payload = gzip.compress(b"0" * 64_000_000)
    handler, _ = _streaming([payload], {"content-encoding": "gzip"})
    counter = _DecodedCounter(encoding="gzip", limit=8 * 1024 * 1024, collect=False)

    returned = counter.feed(payload)

    assert counter.total > 8 * 1024 * 1024
    assert returned == b""


@pytest.mark.asyncio
async def test_multi_member_gzip_is_counted_whole():
    """A second gzip member must not hide behind the first one's size."""
    payload = gzip.compress(b"a" * 512) + gzip.compress(b"b" * 5_000_000)
    handler, _ = _streaming([payload], {"content-encoding": "gzip"})
    client = _limited_client(handler, 16_384)

    with pytest.raises(ADCPResponseTooLargeError):
        await client.get("https://agent.example/tool")


@pytest.mark.asyncio
async def test_error_message_hides_url_credentials():
    handler, _ = _streaming([b"x" * 50_000], {"content-length": "50000"})
    client = _limited_client(handler, 1024)

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get("https://agent.example/tool?api_key=s3cret")

    assert "s3cret" not in str(caught.value)


@pytest.mark.asyncio
async def test_mounted_transports_are_wrapped_too():
    handler, _ = _streaming([b"x" * 50_000])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(204)),
        mounts={"all://proxied.example": httpx.MockTransport(handler)},
    )
    install_response_limit(
        client,
        httpx_module=httpx,
        policy=ResponseLimitPolicy(limit_provider=lambda _long_lived: 1024),
    )

    with pytest.raises(ADCPResponseTooLargeError):
        await client.get("https://proxied.example/tool")


def test_install_fails_closed_on_a_client_it_cannot_meter():
    class DelegatingClient:
        """A wrapper that reads through but would absorb our assignment."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrapper = DelegatingClient(httpx.AsyncClient())

    with pytest.raises(TypeError, match="response-size budget"):
        install_response_limit(
            wrapper,
            httpx_module=httpx,
            policy=ResponseLimitPolicy(limit_provider=lambda _long_lived: 1024),
        )


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

    with pytest.raises(ADCPResponseEncodingError) as caught:
        await client.get("https://agent.example/tool")

    assert caught.value.encoding == "br"


@pytest.mark.asyncio
async def test_identity_is_not_treated_as_an_extra_coding():
    handler, _ = _streaming([b"{}"], {"content-encoding": "identity, identity"})
    client = _limited_client(handler, 1024)

    assert (await client.get("https://agent.example/tool")).text == "{}"


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
    client = _limited_client(handler, None)
    # Swap the provider for one driven by the per-call ContextVar.
    client._transport._limit_provider = lambda _long_lived: resolve_max_response_bytes(8192)

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
    padded_method = "tools/call"
    content_encoding: str | None = None
    #: Answer like a pre-2025-03-26 server: the whole AdCP payload serialized
    #: into one ``content[].text`` item, with no ``structuredContent``.
    text_fallback = False

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler override
        message = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        if "id" not in message:  # notification
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        method = message.get("method")
        padding = "x" * self.tool_payload_size if method == self.padded_method else ""
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oversize-agent", "version": padding or "1"},
            }
        elif method == "tools/call":
            if self.text_fallback:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"products": [], "padding": padding}),
                        }
                    ]
                }
            else:
                result = {
                    "content": [{"type": "text", "text": padding}],
                    "structuredContent": {"products": []},
                }
        else:
            result = {
                "tools": [{"name": padding or "get_products", "inputSchema": {"type": "object"}}]
            }
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("mcp-session-id", "session-1")
        if self.content_encoding and method == self.padded_method:
            self.send_header("content-encoding", self.content_encoding)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            # A client that aborts mid-body is this suite's happy path; let it
            # go rather than printing a socketserver traceback over the result.
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
    param = (
        request.param if isinstance(request.param, dict) else {"tool_payload_size": request.param}
    )
    handler = type("_Handler", (_MCPHandler,), param)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 400_000, "padded_method": "initialize"}],
    indirect=True,
)
async def test_mcp_handshake_over_cap_raises_typed(mcp_agent):
    """A budget abort during setup must not surface as a bare CancelledError."""
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseTooLargeError) as caught:
            await adapter._get_session()
    finally:
        await adapter.close()

    assert caught.value.limit == 50_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 400_000, "padded_method": "tools/list"}],
    indirect=True,
)
async def test_mcp_tools_list_over_cap_raises_typed(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await adapter.list_tools()
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 400_000, "padded_method": "tools/list"}],
    indirect=True,
)
async def test_mcp_get_agent_info_does_not_swallow_a_rejection(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await adapter.get_agent_info()
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [5 * 1024 * 1024], indirect=True)
async def test_mcp_four_mib_default_applies_without_configuration(mcp_agent):
    """Nothing is configured here — the default alone has to reject this."""
    adapter = _mcp_adapter(mcp_agent)
    assert adapter.agent_config.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES
    try:
        with pytest.raises(ADCPResponseTooLargeError) as caught:
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert caught.value.limit == DEFAULT_MAX_RESPONSE_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [400_000], indirect=True)
async def test_mcp_custom_httpx_client_factory_is_metered_too(mcp_agent):
    import httpx2

    adapter = MCPAdapter(
        AgentConfig(
            id="seller",
            agent_uri=mcp_agent,
            protocol=Protocol.MCP,
            max_response_bytes=50_000,
        ),
        httpx_client_factory=lambda **kwargs: httpx2.AsyncClient(**kwargs),
    )
    adapter.response_validation_mode = "warn"
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"padded_method": "initialize", "content_encoding": "br"}],
    indirect=True,
)
async def test_mcp_unmeasurable_content_encoding_raises_typed(mcp_agent):
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseEncodingError):
            await adapter._get_session()
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [{"content_encoding": "br"}], indirect=True)
async def test_mcp_tool_call_encoding_rejection_is_typed_not_a_task_result(mcp_agent):
    """The typed surface must not diverge by protocol or by coding."""
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=50_000)
    try:
        with pytest.raises(ADCPResponseEncodingError):
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_agent", [400_000], indirect=True)
async def test_fan_out_contains_one_agents_rejection(mcp_agent):
    """One hostile seller must not discard every honest seller's result."""
    import adcp.client as client_module

    honest = AgentConfig(
        id="honest", agent_uri=mcp_agent, protocol=Protocol.MCP, max_response_bytes=None
    )
    hostile = AgentConfig(
        id="hostile", agent_uri=mcp_agent, protocol=Protocol.MCP, max_response_bytes=50_000
    )
    multi = client_module.ADCPMultiAgentClient([honest, hostile])
    for agent_client in multi.agents.values():
        agent_client.adapter.response_validation_mode = "warn"
    try:
        results = await multi.get_products(GetProductsRequest(buying_mode="brief", brief="video"))
    finally:
        await multi.close()

    assert len(results) == 2
    assert results[0].success is True
    assert results[1].success is False
    assert "max_response_bytes" in (results[1].error or "")


@pytest.mark.asyncio
async def test_a2a_tool_call_recovers_the_typed_error_from_a_wrapper():
    """a2a-sdk rewraps transport failures; the budget rejection must survive."""
    adapter = A2AAdapter(
        AgentConfig(
            id="seller",
            agent_uri="https://agent.example",
            protocol=Protocol.A2A,
            max_response_bytes=50_000,
        )
    )
    adapter.response_validation_mode = "warn"
    budget_error = ADCPResponseTooLargeError(limit=50_000, received=400_000, agent_id="seller")

    async def fake_get_a2a_client():
        return object()

    async def fake_send(client, message):
        # What a2a-sdk does to a transport failure: its own type, original
        # attached as __cause__.
        raise RuntimeError("A2A client failed") from budget_error

    adapter._get_a2a_client = fake_get_a2a_client  # type: ignore[method-assign]
    adapter._send_and_aggregate = fake_send  # type: ignore[method-assign]
    with pytest.raises(ADCPResponseTooLargeError):
        await adapter.get_products({"brief": "anything"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 2 * 1024 * 1024, "text_fallback": True}],
    indirect=True,
)
async def test_text_fallback_response_under_the_default_budget_succeeds(mcp_agent):
    """A 2 MiB text-fallback response is ordinary traffic under the default.

    For a server with no ``structuredContent``, the single text item *is* the
    response. The SDK used to bound that item at a fixed 1 MiB, so 1 MiB —
    not ``max_response_bytes`` — was the effective call cap for every such
    server, and an item over it was skipped rather than reported.
    """
    adapter = _mcp_adapter(mcp_agent)
    assert adapter.agent_config.max_response_bytes == DEFAULT_MAX_RESPONSE_BYTES
    try:
        result = await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert result.success, result.error
    assert result.data is not None
    assert result.data["products"] == []
    assert len(result.data["padding"]) == 2 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 2 * 1024 * 1024, "text_fallback": True}],
    indirect=True,
)
async def test_text_fallback_response_over_the_configured_budget_raises(mcp_agent):
    """Lowering the budget rejects the same response, typed."""
    adapter = _mcp_adapter(mcp_agent, max_response_bytes=512 * 1024)
    try:
        with pytest.raises(ADCPResponseTooLargeError) as caught:
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()

    assert caught.value.limit == 512 * 1024
    # Pin which layer rejected it: the transport pre-rejects on the declared
    # Content-Length, so `declared` tells the two apart. Without this the
    # assertion above passes even if the extraction bound is deleted.
    assert caught.value.declared is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mcp_agent",
    [{"tool_payload_size": 2 * 1024 * 1024, "text_fallback": True}],
    indirect=True,
)
async def test_text_fallback_budget_follows_a_per_call_override(mcp_agent):
    """The post-parse bound is the per-call number, not a fixed one."""
    client = ADCPClient(
        AgentConfig(
            id="seller",
            agent_uri=mcp_agent,
            protocol=Protocol.MCP,
            max_response_bytes=512 * 1024,
        ),
        validation=ValidationHookConfig(responses="warn"),
    )
    request = GetProductsRequest(buying_mode="brief", brief="video")
    try:
        with pytest.raises(ADCPResponseTooLargeError):
            await client.get_products(request)

        result = await client.get_products(
            request, options=TaskOptions(max_response_bytes=4 * 1024 * 1024)
        )
        assert result.success
    finally:
        await client.close()


async def _injected_session(text: str):
    """A caller-owned MCP session, which is the shape `from_mcp_client` takes.

    This is the only path where the extraction bound is load-bearing: the
    transport is the caller's, so nothing counted those bytes on the way in.
    """

    class _Session:
        async def initialize(self):
            return None

        async def call_tool(self, name, params, **kwargs):
            return SimpleNamespace(isError=False, content=[{"type": "text", "text": text}])

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    return _Session()


@pytest.mark.asyncio
async def test_injected_session_text_is_bounded_by_the_extraction_budget():
    """`from_mcp_client` has no transport budget; this is its only bound."""
    payload = json.dumps({"products": [], "padding": "x" * 200_000})
    client = ADCPClient.from_mcp_client(
        await _injected_session(payload),
        validation=ValidationHookConfig(responses="warn"),
        max_response_bytes=4096,
    )

    with pytest.raises(ADCPResponseTooLargeError) as caught:
        await client.get_products(GetProductsRequest(buying_mode="brief", brief="video"))

    assert caught.value.limit == 4096
    # No transport ran, so this can only have come from extraction.
    assert caught.value.declared is False
    assert caught.value.url is None


@pytest.mark.asyncio
async def test_injected_session_budget_is_configurable():
    """Raising it accepts the same response; `None` removes the bound."""
    payload = json.dumps({"products": [], "padding": "x" * 200_000})

    for budget in (4 * 1024 * 1024, None):
        client = ADCPClient.from_mcp_client(
            await _injected_session(payload),
            validation=ValidationHookConfig(responses="warn"),
            max_response_bytes=budget,
        )
        result = await client.get_products(GetProductsRequest(buying_mode="brief", brief="video"))

        # The same 200 KB payload the 4 KiB budget rejected now parses.
        assert result.success, result.error
        assert result.data is not None
