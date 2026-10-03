"""Actual MCP requests across repeated lifespans and failed/cancelled startup."""

import asyncio
import importlib
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.testclient import TestClient

from adcp.server import ADCPHandler, create_mcp_server, get_mcp_session_stats

serve_module = importlib.import_module("adcp.server.serve")


class Seller(ADCPHandler):
    advertised_tools = {"get_products"}

    async def get_products(self, params, context=None):
        return {"products": []}


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "repeat-test", "version": "1"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream"}


def request_cycle(client, stateless):
    initialized = client.post("/mcp", json=INIT, headers=HEADERS)
    assert initialized.status_code == 200
    session = initialized.headers.get("mcp-session-id")
    headers = {**HEADERS, "mcp-protocol-version": initialized.json()["result"]["protocolVersion"]}
    if not stateless:
        assert session
        headers["mcp-session-id"] = session
        assert (
            client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            ).status_code
            == 202
        )
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_products",
                "arguments": {
                    "buying_mode": "brief",
                    "brief": "video",
                },
            },
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["result"]["structuredContent"]["products"] == []
    return session


@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("combined", [False, True])
def test_two_full_start_request_stop_cycles(monkeypatch, stateless, combined):
    mcp = create_mcp_server(Seller(), validation=None, stateless_http=stateless)
    assert mcp._session_manager is None
    events = []
    if combined:
        original_a2a = importlib.import_module("adcp.server.a2a_server").create_a2a_server
        monkeypatch.setattr(serve_module, "create_mcp_server", lambda *args, **kwargs: mcp)

        def create_a2a(*args, **kwargs):
            app = original_a2a(*args, **kwargs)
            inner_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(app):
                async with inner_lifespan(app):
                    events.append("a2a-start")
                    try:
                        yield
                    finally:
                        events.append("a2a-stop")

            app.router.lifespan_context = lifespan
            return app

        monkeypatch.setattr("adcp.server.a2a_server.create_a2a_server", create_a2a)

        async def startup():
            assert mcp._session_manager._task_group is not None
            events.append("user-start")

        async def shutdown():
            assert mcp._session_manager._task_group is not None
            events.append("user-stop")

        app = serve_module._build_mcp_and_a2a_app(
            Seller(),
            name="repeat",
            port=3001,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            validation=None,
            on_startup=[startup],
            on_shutdown=[shutdown],
        )
    else:
        app = mcp.streamable_http_app()
    managers = []
    prior_session = None
    for _ in range(2):
        with TestClient(app, base_url="http://localhost:3001") as client:
            manager = mcp._session_manager
            assert manager is not None
            assert all(manager is not old for old in managers)
            managers.append(manager)
            if prior_session:
                assert (
                    client.post(
                        "/mcp",
                        headers={**HEADERS, "mcp-session-id": prior_session},
                        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                    ).status_code
                    == 404
                )
            prior_session = request_cycle(client, stateless)
            assert get_mcp_session_stats(mcp).active_sessions == (0 if stateless else 1)
        assert mcp._session_manager is None
        assert manager._task_group is None
        assert not manager._server_instances
        assert not manager._session_bindings
        assert get_mcp_session_stats(mcp).active_sessions == 0
    if combined:
        assert events == ["a2a-start", "user-start", "user-stop", "a2a-stop"] * 2


@pytest.mark.asyncio
async def test_requests_outside_lifespan_return_unavailable():
    mcp = create_mcp_server(Seller(), validation=None)
    app = mcp.streamable_http_app()
    for _ in range(2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:3001"
        ) as client:
            assert (await client.post("/mcp", headers=HEADERS, json=INIT)).status_code == 503


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.asyncio
async def test_cancellation_or_failure_releases_binding(cancelled):
    mcp = create_mcp_server(Seller(), validation=None)
    app = mcp.streamable_http_app()
    manager = None
    exception = asyncio.CancelledError if cancelled else RuntimeError
    with pytest.raises(BaseException) as caught:
        async with app.router.lifespan_context(app):
            manager = mcp._session_manager
            raise exception()
    leaf = getattr(caught.value, "exceptions", (caught.value,))[0]
    assert isinstance(leaf, exception)
    assert manager is not None
    assert mcp._session_manager is None
    assert manager._task_group is None
    async with app.router.lifespan_context(app):
        assert mcp._session_manager is not manager


@pytest.mark.asyncio
async def test_external_cancellation_and_startup_failure_clear_live_endpoint():
    mcp = create_mcp_server(Seller(), validation=None)
    app = mcp.streamable_http_app()
    ready = asyncio.Event()
    managers = []

    async def owner():
        async with app.router.lifespan_context(app):
            managers.append(mcp._session_manager)
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert mcp._session_manager is None
    assert managers[0]._task_group is None
    original = mcp._lowlevel_server.lifespan

    @asynccontextmanager
    async def fail_startup(server):
        raise RuntimeError("startup failed")
        yield

    mcp._lowlevel_server.lifespan = fail_startup
    with pytest.raises(RuntimeError, match="startup failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup succeeded")
    assert mcp._session_manager is None
    mcp._lowlevel_server.lifespan = original
    async with app.router.lifespan_context(app):
        assert mcp._session_manager is not managers[0]


@pytest.mark.parametrize("sse_path,message_path", [("/sse", "/messages/"), ("/events", "/inbox/")])
@pytest.mark.asyncio
async def test_sse_stream_enters_lifecycle_and_disconnects(sse_path, message_path):
    """Drive a real stream without buffering its unbounded response in httpx."""
    mcp = create_mcp_server(Seller(), validation=None)
    app = mcp.sse_app(sse_path=sse_path, message_path=message_path)
    original = mcp._lowlevel_server.lifespan
    events = []
    started = asyncio.Event()

    @asynccontextmanager
    async def lifecycle(server):
        async with original(server) as state:
            events.append("start")
            started.set()
            try:
                yield state
            finally:
                events.append("stop")

    mcp._lowlevel_server.lifespan = lifecycle
    for _ in range(2):
        started.clear()
        endpoint = asyncio.Event()
        messages = []

        async def receive():
            await endpoint.wait()
            await started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if b"event: endpoint" in message.get("body", b""):
                endpoint.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": sse_path,
            "raw_path": sse_path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"localhost:3001"), (b"accept", b"text/event-stream")],
            "server": ("localhost", 3001),
            "client": ("127.0.0.1", 12345),
        }
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(app(scope, receive, send), timeout=5)
        start = next(message for message in messages if message["type"] == "http.response.start")
        assert start["status"] == 200
        assert (b"content-type", b"text/event-stream; charset=utf-8") in start["headers"]
        body = b"".join(message.get("body", b"") for message in messages)
        assert b"event: endpoint" in body
        assert message_path.encode() + b"?session_id=" in body
    assert events == ["start", "stop"] * 2
