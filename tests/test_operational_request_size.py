"""Operational dispatch shares existing body caps, outside protocol auth."""

import asyncio
import importlib
import inspect

import pytest
from mcp.server.mcpserver import MCPServer
from starlette.responses import Response
from starlette.routing import Route

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.server import ADCPHandler, RequestSignatureVerification, create_mcp_server
from adcp.server.auth import BearerTokenAuth
from adcp.signing import StaticSignerKeys
from adcp.testing import build_asgi_app

serve = importlib.import_module("adcp.server.serve")
TRANSPORTS = [
    "serve-mcp",
    "serve-sse",
    "a2a",
    "both",
    "testing-mcp",
    "testing-a2a",
    "testing-both",
    "direct-mcp",
]
if "max_request_body_size" in inspect.signature(MCPServer.sse_app).parameters:
    TRANSPORTS.append("direct-sse")


class BodyLimitSeller(ADCPHandler):
    advertised_tools = {"get_products"}

    async def get_products(self, params, context=None):
        return {"products": []}


class BodyLimitPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"], supported_billing=("operator",)
    )
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        raise NotImplementedError

    def update_media_buy(self, mid, patch, ctx):
        raise NotImplementedError

    def sync_creatives(self, req, ctx):
        raise NotImplementedError

    def get_media_buy_delivery(self, req, ctx):
        raise NotImplementedError

    def get_media_buys(self, req, ctx):
        raise NotImplementedError

    def list_creative_formats_legacy(self, req, ctx):
        return {"creative_formats": []}

    def list_creatives(self, req, ctx):
        return {"creatives": []}

    def provide_performance_feedback(self, req, ctx):
        return {"acknowledged": True}


def build(monkeypatch, transport, limit):
    calls = []
    auth_calls = []

    async def operation(request):
        calls.append(await request.body())
        return Response(calls[-1])

    def reject(token):
        auth_calls.append(token)
        return None

    routes = [Route("/ops", operation, methods=["POST"])]
    policy = {"allowed_hosts": ["seller.example"], "allowed_origins": ["https://buyer.example"]}
    kwargs = {
        **policy,
        "unauthenticated_routes": routes,
        "auth": BearerTokenAuth(validate_token=reject),
        "max_request_size": limit,
    }
    signing = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({}), required_for=frozenset({"get_products"})
    )
    if transport.startswith("direct-"):
        mcp = create_mcp_server(BodyLimitSeller(), validation=None, **policy)
        builder = mcp.sse_app if transport == "direct-sse" else mcp.streamable_http_app
        body_kwargs = {} if limit is None else {"max_request_body_size": limit}
        app = builder(unauthenticated_routes=routes, **body_kwargs)
    elif transport.startswith("testing-"):
        app = build_asgi_app(
            BodyLimitPlatform(),
            transport=transport.removeprefix("testing-"),
            validate_at_init=False,
            **kwargs,
        )
    elif transport == "a2a":
        app = serve._build_a2a_app(
            BodyLimitSeller(),
            name="body-cap",
            port=3001,
            test_controller=None,
            validation=None,
            request_signature_verification=signing,
            **kwargs,
        )
    elif transport == "both":
        app = serve._build_mcp_and_a2a_app(
            BodyLimitSeller(),
            name="body-cap",
            port=3001,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            validation=None,
            request_signature_verification=signing,
            **kwargs,
        )
    else:
        import uvicorn

        captured = []

        class Socket:
            def close(self):
                pass

        class Server:
            def __init__(self, config):
                captured.append(config.app)

            async def serve(self, sockets):
                pass

        monkeypatch.setattr(serve, "_bind_reusable_socket", lambda *args: Socket())
        monkeypatch.setattr(uvicorn, "Server", Server)
        mcp = create_mcp_server(BodyLimitSeller(), validation=None, **policy)
        serve._run_mcp_http(
            mcp,
            transport="sse" if transport == "serve-sse" else "streamable-http",
            unauthenticated_routes=routes,
            max_request_size=limit,
            auth=kwargs["auth"],
            request_signature_verification=signing,
        )
        app = captured[0]
    return app, calls, auth_calls


async def post(app, chunks, headers=()):
    sent = []
    reads = 0
    frames = iter(chunks)

    async def receive():
        nonlocal reads
        reads += 1
        try:
            return next(frames)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/ops",
        "raw_path": b"/ops",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"seller.example"),
            (b"origin", b"https://buyer.example"),
            (b"authorization", b"Bearer invalid"),
            (b"signature-input", b"invalid"),
            (b"signature", b"invalid"),
        ],
        "server": ("seller.example", 443),
        "client": ("127.0.0.1", 12345),
    }
    # Replace defaults rather than introducing duplicate Host/Origin headers.
    supplied = {key for key, _ in headers}
    scope["headers"] = [pair for pair in scope["headers"] if pair[0] not in supplied] + list(
        headers
    )
    await app(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent)
    return status, body, reads


def frames(*chunks):
    return [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.asyncio
async def test_oversized_operation_rejected_before_handler(monkeypatch, transport, chunked):
    app, calls, auth_calls = await asyncio.to_thread(build, monkeypatch, transport, 8)
    chunks = frames(b"12345", b"6789", b"unread") if chunked else frames(b"123456789")
    headers = [(b"transfer-encoding", b"chunked")] if chunked else [(b"content-length", b"9")]
    status, body, reads = await post(app, chunks, headers)
    assert status == 413
    assert b"8 bytes" in body
    assert reads == (2 if chunked else 0)
    assert calls == auth_calls == []


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.asyncio
async def test_at_cap_operation_bypasses_protocol_credentials(monkeypatch, transport):
    app, calls, auth_calls = await asyncio.to_thread(build, monkeypatch, transport, 8)
    status, body, _ = await post(app, frames(b"1234", b"5678"))
    assert status == 200
    assert body == b"12345678"
    assert calls == [body]
    assert auth_calls == []


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.asyncio
async def test_default_cap_also_bounds_operational_requests(monkeypatch, transport):
    app, calls, auth_calls = await asyncio.to_thread(build, monkeypatch, transport, None)
    limit = 4_194_304 if transport.startswith("direct-") else 10 * 1024 * 1024
    status, body, reads = await post(
        app, frames(b""), [(b"content-length", str(limit + 1).encode())]
    )
    assert status == 413
    assert str(limit).encode() in body
    assert reads == 0
    assert calls == auth_calls == []


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("header,status", [(b"host", 421), (b"origin", 403)])
@pytest.mark.asyncio
async def test_transport_policy_precedes_operation_and_body_reads(
    monkeypatch, transport, header, status
):
    app, calls, auth_calls = await asyncio.to_thread(build, monkeypatch, transport, 8)
    actual, _, reads = await post(
        app,
        frames(b"123456789"),
        [(header, b"https://evil.example" if header == b"origin" else b"evil.example")],
    )
    assert actual == status
    assert reads == 0
    assert calls == auth_calls == []


@pytest.mark.parametrize("transport", [t for t in TRANSPORTS if not t.startswith("direct-")])
@pytest.mark.asyncio
async def test_zero_disables_common_operational_cap(monkeypatch, transport):
    app, calls, auth_calls = await asyncio.to_thread(build, monkeypatch, transport, 0)
    status, body, _ = await post(app, frames(b"123456789"), [(b"content-length", b"9")])
    assert status == 200
    assert calls == [body]
    assert auth_calls == []
