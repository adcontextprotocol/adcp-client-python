"""JSON depth and member bounds (AdCP 3.3 ``security.mdx`` §Response Size Bounds
item 6, §Inbound body bounds item 3).

Covers the counting scanner in :mod:`adcp._json_bounds` directly, then its use
on the client: every agent response body (and every SSE event) streamed
through the response limiter, and the MCP ``content[].text`` fallback.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from adcp import ADCPClient, ValidationHookConfig
from adcp._json_bounds import (
    MAX_JSON_DEPTH,
    MAX_JSON_MEMBERS,
    JsonBoundsError,
    JsonBoundsScanner,
    SseJsonBoundsScanner,
    bounded_json_loads,
    check_json_bounds,
)
from adcp._response_limit import (
    ADCPResponseTooComplexError,
    ResponseLimitPolicy,
    install_response_limit,
)
from adcp.exceptions import ADCPResponseTooLargeError
from adcp.protocols.mcp import extract_adcp_error, extract_adcp_success
from adcp.types import GetProductsRequest
from tests.test_response_size_limit import _mcp_adapter, _MCPHandler


def _nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


def _wide(members: int) -> bytes:
    return b"[" + b",".join(b"0" for _ in range(members)) + b"]"


def _scan(data: bytes, **bounds: int) -> JsonBoundsScanner:
    scanner = JsonBoundsScanner(**bounds)
    scanner.feed(data)
    return scanner


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def test_spec_values():
    assert MAX_JSON_DEPTH == 64
    assert MAX_JSON_MEMBERS == 100_000


def test_depth_64_passes_and_65_is_rejected():
    check_json_bounds(_nested(64))
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(_nested(65))
    assert caught.value.kind == "depth"
    assert caught.value.limit == 64
    assert caught.value.observed == 65


def test_object_nesting_counts_the_same_as_arrays():
    doc = b'{"a":' * 65 + b"1" + b"}" * 65
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(doc)
    assert caught.value.kind == "depth"
    check_json_bounds(b'{"a":' * 64 + b"1" + b"}" * 64)


def test_members_100000_pass_and_100001_are_rejected():
    check_json_bounds(_wide(100_000))
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(_wide(100_001))
    assert caught.value.kind == "members"
    assert caught.value.observed == 100_001


def test_members_are_aggregated_across_containers():
    # 50,001 objects of one member each, plus the 50,001 array elements that
    # hold them: 100,002 members, though no container is near the bound.
    doc = json.dumps([{"k": 0} for _ in range(50_001)]).encode()
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(doc)
    assert caught.value.kind == "members"


def test_object_members_count_once_not_per_key_and_value():
    scanner = _scan(b'{"a": 1, "b": [true, null], "c": {}}', max_depth=10, max_members=100)
    # a, b, c, plus the two array elements.
    assert scanner.members == 5
    assert scanner.depth == 0


def test_root_scalar_is_not_a_member():
    assert _scan(b'"just a string"').members == 0


def test_structure_inside_strings_is_ignored():
    deep_text = json.dumps({"s": "[" * 500 + '\\"{' * 500, "t": "\\\\"}).encode()
    scanner = _scan(deep_text)
    assert scanner.members == 2
    assert not scanner.stopped


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64])
def test_result_is_invariant_to_chunk_boundaries(chunk_size):
    doc = json.dumps(
        {'a\\"b': ["x\\", {"y": [1, 2.5e3, None, True]}, "é\n"], "z": {"q": "]}"}}
    ).encode()
    whole = _scan(doc, max_depth=10, max_members=100)
    chunked = JsonBoundsScanner(max_depth=10, max_members=100)
    for start in range(0, len(doc), chunk_size):
        chunked.feed(doc[start : start + chunk_size])
    assert chunked.members == whole.members == 11
    assert chunked.depth == whole.depth == 0


def test_violation_is_raised_from_the_chunk_that_crosses_it():
    scanner = JsonBoundsScanner()
    scanner.feed(_nested(64)[:64])
    with pytest.raises(JsonBoundsError):
        scanner.feed(b"[")


def test_non_json_body_passes_and_is_left_to_the_parser():
    # An HTML error page full of brackets must not read as a bounds violation.
    page = b"<html><body>" + b"[" * 500 + b"</body></html>"
    check_json_bounds(page)
    assert _scan(page).stopped


def test_scanning_stops_at_the_first_grammar_error():
    # After the root value closes, nothing the parser would accept follows,
    # so the bytes after it are never counted.
    scanner = _scan(b"[1]" + _nested(100))
    assert scanner.stopped
    with pytest.raises(json.JSONDecodeError):
        bounded_json_loads(b"[1]" + _nested(100))


def test_reset_starts_a_new_document():
    scanner = JsonBoundsScanner(max_depth=2, max_members=3)
    scanner.feed(b"[[1, 2]]")
    assert scanner.members == 3
    scanner.reset()
    # Counted afresh: a second identical document does not accumulate.
    scanner.feed(b"[[1, 2]]")
    assert scanner.members == 3
    assert scanner.depth == 0


def test_utf8_bom_is_skipped_as_json_loads_does():
    body = b"\xef\xbb\xbf" + _nested(65)
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(body)
    assert caught.value.kind == "depth"


@pytest.mark.parametrize("split", [1, 2, 3, 4])
def test_utf8_bom_split_across_chunks_is_still_recognized(split):
    body = b"\xef\xbb\xbf" + _nested(65)
    scanner = JsonBoundsScanner()
    with pytest.raises(JsonBoundsError) as caught:
        scanner.feed(body[:split])
        scanner.feed(body[split:])
    assert caught.value.kind == "depth"


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le"])
def test_wide_encodings_are_refused(codec):
    # json.loads(bytes) would decode these and parse a structure the scanner
    # cannot follow, so they are refused rather than waved through.
    body = ("  " + "[" * 70 + "]" * 70).encode(codec)
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(body)
    assert caught.value.kind == "encoding"
    with pytest.raises(JsonBoundsError):
        _scan(body)


def test_short_wide_body_is_refused_too():
    with pytest.raises(JsonBoundsError):
        check_json_bounds("[1]".encode("utf-16-le"))


def test_str_input_is_checked_as_utf8():
    check_json_bounds("[" * 64 + "]" * 64)
    with pytest.raises(JsonBoundsError):
        check_json_bounds("[" * 65 + "]" * 65)


def test_bounded_json_loads_parses_within_bounds():
    assert bounded_json_loads(b'{"a": [1, 2]}') == {"a": [1, 2]}
    with pytest.raises(JsonBoundsError):
        bounded_json_loads(_nested(65))


def test_error_message_never_quotes_the_document():
    secret = b'{"token": "sk_live_SECRET", "x": ' + _nested(65) + b"}"
    with pytest.raises(JsonBoundsError) as caught:
        check_json_bounds(secret)
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "doc",
    [
        b'{"s": "' + b"[" * 200 + b'", "n": [1]}',
        b'[{"a": "\\"[[["}, [[], {}], "x"]',
        json.dumps([{"k": [i, {"j": i}]} for i in range(40)]).encode(),
        _nested(30) + b" ",
    ],
)
@pytest.mark.parametrize("bounds", [(3, 5), (5, 50), (100, 10), (100, 10_000)])
def test_fast_path_agrees_with_the_scanner(doc, bounds):
    max_depth, max_members = bounds

    def verdict(fn):
        try:
            fn()
        except JsonBoundsError as exc:
            return exc.kind
        return None

    assert verdict(
        lambda: check_json_bounds(doc, max_depth=max_depth, max_members=max_members)
    ) == verdict(lambda: _scan(doc, max_depth=max_depth, max_members=max_members))


def test_rejects_non_positive_bounds():
    with pytest.raises(ValueError):
        JsonBoundsScanner(max_depth=0)
    with pytest.raises(ValueError):
        JsonBoundsScanner(max_members=0)


# ---------------------------------------------------------------------------
# SSE scanner
# ---------------------------------------------------------------------------


def _sse_feed(stream: bytes, chunk_size: int | None = None, **bounds: int) -> None:
    scanner = SseJsonBoundsScanner(**bounds)
    if chunk_size is None:
        scanner.feed(stream)
        return
    for start in range(0, len(stream), chunk_size):
        scanner.feed(stream[start : start + chunk_size])


@pytest.mark.parametrize("chunk_size", [None, 1, 5])
def test_sse_each_event_is_its_own_document(chunk_size):
    # Many events each under the bound pass: the count resets per event.
    event = b"event: message\ndata: " + _wide(60) + b"\n\n"
    _sse_feed(event * 10, chunk_size, max_members=100)
    with pytest.raises(JsonBoundsError):
        _sse_feed(b"data: " + _wide(101) + b"\n\n", chunk_size, max_members=100)


@pytest.mark.parametrize("chunk_size", [None, 1, 3])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_sse_multi_line_data_is_joined(chunk_size, newline):
    lines = [b"data: " + b"[" * 40, b"data:" + b"[" * 25, b"data: " + b"]" * 65]
    stream = newline.join(lines) + newline + newline
    with pytest.raises(JsonBoundsError) as caught:
        _sse_feed(stream, chunk_size)
    assert caught.value.kind == "depth"


def test_sse_other_fields_and_comments_are_not_scanned():
    stream = (
        b": " + _nested(200) + b"\n"
        b"id: " + _nested(200) + b"\n"
        b"event: " + _nested(200) + b"\n"
        b"data: {}\n\n"
    )
    _sse_feed(stream)


def test_sse_leading_bom_is_stripped():
    with pytest.raises(JsonBoundsError):
        _sse_feed(b"\xef\xbb\xbfdata: " + _nested(65) + b"\n\n", 1)


def test_sse_data_split_inside_the_field_name():
    scanner = SseJsonBoundsScanner()
    scanner.feed(b"da")
    with pytest.raises(JsonBoundsError):
        scanner.feed(b"ta: " + _nested(65) + b"\n\n")


# ---------------------------------------------------------------------------
# Client transport
# ---------------------------------------------------------------------------


def _limited_client(handler, limit=4 * 1024 * 1024):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return install_response_limit(
        client,
        httpx_module=httpx,
        policy=ResponseLimitPolicy(limit_provider=lambda _long_lived: limit, agent_id="seller"),
    )


def _respond(body: bytes, headers: dict[str, str] | None = None):
    def handler(request):
        async def stream():
            for start in range(0, len(body), 4096):
                yield body[start : start + 4096]

        return httpx.Response(
            200, content=stream(), headers=headers or {"content-type": "application/json"}
        )

    return handler


async def test_deep_json_response_is_rejected_typed():
    client = _limited_client(_respond(b'{"result": ' + _nested(65) + b"}"))

    with pytest.raises(ADCPResponseTooComplexError) as caught:
        await client.post("https://agent.example/mcp")

    assert isinstance(caught.value, ADCPResponseTooLargeError)
    assert caught.value.bound == "depth"
    assert caught.value.limit == 64
    assert caught.value.agent_id == "seller"
    assert caught.value.url == "https://agent.example/mcp"


async def test_wide_json_response_is_rejected_while_streaming():
    reads: list[int] = []
    body = _wide(400_000)

    def handler(request):
        async def stream():
            for start in range(0, len(body), 4096):
                reads.append(start)
                yield body[start : start + 4096]

        return httpx.Response(200, content=stream(), headers={"content-type": "application/json"})

    client = _limited_client(handler)
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        await client.post("https://agent.example/mcp")

    assert caught.value.bound == "members"
    # Aborted near member 100,001 (~200 KB in), not after the whole 800 KB body.
    assert len(reads) * 4096 < len(body) // 2


async def test_compressed_deep_response_is_scanned_decompressed():
    body = gzip.compress(_nested(65))
    client = _limited_client(
        _respond(body, {"content-type": "application/json", "content-encoding": "gzip"})
    )
    with pytest.raises(ADCPResponseTooComplexError):
        await client.post("https://agent.example/mcp")


async def test_ordinary_response_passes_untouched():
    payload = {"products": [{"id": str(i), "tags": ["a", "b"]} for i in range(500)]}
    client = _limited_client(_respond(json.dumps(payload).encode()))
    response = await client.post("https://agent.example/mcp")
    assert response.json() == payload


async def test_non_json_error_page_passes_through():
    page = b"<html>" + b"[" * 200 + b"</html>"
    client = _limited_client(_respond(page, {"content-type": "text/html"}))
    response = await client.get("https://agent.example/")
    assert response.content == page


async def test_utf16_json_response_is_refused():
    client = _limited_client(_respond('{"a": [1]}'.encode("utf-16")))
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        await client.post("https://agent.example/mcp")
    assert caught.value.bound == "encoding"


async def test_sse_event_over_the_bound_is_rejected_per_event():
    stream = b"event: message\ndata: " + _nested(65) + b"\n\n"
    client = _limited_client(_respond(stream, {"content-type": "text/event-stream"}))
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        async with client.stream("POST", "https://agent.example/mcp") as response:
            async for _ in response.aiter_bytes():
                pass
    assert caught.value.per_event is True
    assert caught.value.bound == "depth"


async def test_many_small_sse_events_pass():
    event = b"data: " + json.dumps({"jsonrpc": "2.0", "result": list(range(50))}).encode()
    stream = (event + b"\n\n") * 5_000  # 250,000 members across the stream
    client = _limited_client(_respond(stream, {"content-type": "text/event-stream"}))
    async with client.stream("POST", "https://agent.example/mcp") as response:
        async for _ in response.aiter_bytes():
            pass


async def test_sse_in_a_non_utf8_charset_is_refused():
    stream = "data: {}\n\n".encode("utf-16")
    client = _limited_client(
        _respond(stream, {"content-type": "text/event-stream; charset=utf-16"})
    )
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        await client.post("https://agent.example/mcp")
    assert caught.value.bound == "encoding"
    assert "utf-16" in str(caught.value)


async def test_sse_utf8_charset_is_accepted():
    client = _limited_client(
        _respond(b"data: {}\n\n", {"content-type": 'text/event-stream; charset="UTF-8"'})
    )
    async with client.stream("POST", "https://agent.example/mcp") as response:
        async for _ in response.aiter_bytes():
            pass


async def test_disabling_the_budget_disables_the_parse_bounds_too():
    client = _limited_client(_respond(_nested(200)), limit=None)
    response = await client.post("https://agent.example/mcp")
    assert response.content == _nested(200)


# ---------------------------------------------------------------------------
# MCP content[].text fallback
# ---------------------------------------------------------------------------


def _text_result(text: str, *, is_error: bool = False):
    return SimpleNamespace(isError=is_error, content=[{"type": "text", "text": text}])


def test_text_fallback_is_bounded_before_json_loads():
    text = json.dumps({"products": [], "deep": json.loads(_nested(70))})
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        extract_adcp_success(_text_result(text), agent_id="seller")
    assert caught.value.bound == "depth"
    assert caught.value.agent_id == "seller"


def test_text_fallback_error_path_is_bounded_too():
    text = json.dumps({"adcp_error": {"code": "X"}, "pad": list(range(100_001))})
    with pytest.raises(ADCPResponseTooComplexError) as caught:
        extract_adcp_error(_text_result(text, is_error=True))
    assert caught.value.bound == "members"


def test_text_fallback_within_bounds_still_parses():
    assert extract_adcp_success(_text_result('{"products": []}')) == {"products": []}


def test_text_fallback_bounds_follow_the_budget_opt_out():
    text = json.dumps({"products": [], "deep": json.loads(_nested(70))})
    assert extract_adcp_success(_text_result(text), max_response_bytes=None) is not None


async def test_injected_session_text_is_bounded_end_to_end():
    """`from_mcp_client` has no transport scan; extraction is the only bound."""
    text = json.dumps({"products": [], "deep": json.loads(_nested(70))})

    class _Session:
        async def initialize(self):
            return None

        async def call_tool(self, name, params, **kwargs):
            return _text_result(text)

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    client = ADCPClient.from_mcp_client(
        _Session(), validation=ValidationHookConfig(responses="warn")
    )
    with pytest.raises(ADCPResponseTooComplexError):
        await client.get_products(GetProductsRequest(buying_mode="brief", brief="video"))


# ---------------------------------------------------------------------------
# MCP end to end over HTTP
# ---------------------------------------------------------------------------


class _DeepStructuredHandler(_MCPHandler):
    """Answers tools/call with structuredContent nested past the bound."""

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler override
        message = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        if "id" not in message:  # notification
            self.send_response(202)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        method = message.get("method")
        if method == "initialize":
            result: dict = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "deep-agent", "version": "1"},
            }
        elif method == "tools/call":
            deep: object = {}
            for _ in range(70):
                deep = [deep]
            result = {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"products": [], "ext": deep},
            }
        else:
            result = {"tools": [{"name": "get_products", "inputSchema": {"type": "object"}}]}
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("mcp-session-id", "session-1")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


@pytest.fixture
def deep_mcp_agent():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeepStructuredHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp/"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


async def test_mcp_tool_call_with_over_deep_structured_content_raises_typed(deep_mcp_agent):
    adapter = _mcp_adapter(deep_mcp_agent)
    try:
        with pytest.raises(ADCPResponseTooComplexError) as caught:
            await adapter.get_products({"brief": "anything"})
    finally:
        await adapter.close()
    assert caught.value.bound == "depth"
    assert caught.value.agent_id == "seller"
