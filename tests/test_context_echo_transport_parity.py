"""``context`` is echoed unchanged over MCP and A2A, through the real servers.

AdCP's ``context`` is opaque buyer data that every response echoes. These
tests drive the mounted MCP + A2A app over HTTP (``tools/call``,
``message/send``, ``SendMessage``) and compare the
echoed ``context`` byte-for-byte as JSON, so ``7`` and ``7.0`` differ.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, ClassVar

import pytest
from starlette.testclient import TestClient

from adcp.server import ADCPHandler
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_mcp_and_a2a_app

#: Integers at every depth, a fractional number, null, and a nested
#: ``context`` key. Every value here survives a protobuf ``Struct`` exactly.
CONTEXT: dict[str, Any] = {
    "count": 7,
    "list": [1, 2.5, -3, 0],
    "nil": None,
    "flag": True,
    "nested": {"context": {"retries": 3, "ratio": 0.25}},
}

REQUEST: dict[str, Any] = {"buying_mode": "brief", "brief": "premium video"}


def _json(value: Any) -> str:
    """JSON text, so ``7`` and ``7.0`` compare unequal."""
    return json.dumps(value, sort_keys=True)


class _EchoHandler(ADCPHandler[Any]):
    advertised_tools: ClassVar[set[str]] = {"get_adcp_capabilities", "get_products"}

    def __init__(self) -> None:
        self.seen: list[Any] = []

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return capabilities_response(["media_buy"])

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else params.model_dump(mode="python")
        self.seen.append(raw.get("context"))
        return {"products": []}


@pytest.fixture(scope="module")
def handler() -> _EchoHandler:
    return _EchoHandler()


@pytest.fixture(scope="module")
def client(handler: _EchoHandler) -> Iterator[TestClient]:
    app = _build_mcp_and_a2a_app(
        handler,
        name="context-echo",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        stateless_http=True,
        allowed_hosts=["testserver"],
    )
    with TestClient(app, base_url="http://localhost:3001") as test_client:
        yield test_client


def _mcp(client: TestClient, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp/",
        headers={"accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result["structuredContent"]


def _a2a_task(body: dict[str, Any]) -> dict[str, Any]:
    result = body["result"]
    return result.get("task", result)


def _a2a_data(task: dict[str, Any]) -> dict[str, Any]:
    for part in task["artifacts"][0]["parts"]:
        if "data" in part:
            data: dict[str, Any] = part["data"]
            return data
    raise AssertionError(f"no DataPart in {task}")


def _a2a(client: TestClient, wire: str, tool: str, parameters: dict[str, Any]) -> dict[str, Any]:
    invocation = {"skill": tool, "parameters": parameters}
    if wire == "v0.3":
        headers: dict[str, str] = {}
        method = "message/send"
        part: dict[str, Any] = {"kind": "data", "data": invocation}
        role = "user"
    else:
        headers = {"A2A-Version": "1.0"}
        method = "SendMessage"
        part = {"data": invocation}
        role = "ROLE_USER"
    response = client.post(
        "/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"message": {"messageId": f"m-{wire}", "role": role, "parts": [part]}},
        },
    )
    assert response.status_code == 200, response.text
    return _a2a_data(_a2a_task(response.json()))


@pytest.mark.parametrize("wire", ["v0.3", "v1.0"])
def test_a2a_echoes_integer_context_like_mcp(client: TestClient, wire: str) -> None:
    mcp = _mcp(client, "get_products", {**REQUEST, "context": CONTEXT})
    a2a = _a2a(client, wire, "get_products", {**REQUEST, "context": CONTEXT})
    assert "adcp_error" not in mcp and "adcp_error" not in a2a
    assert _json(mcp["context"]) == _json(CONTEXT)
    assert _json(a2a["context"]) == _json(CONTEXT)


@pytest.mark.parametrize("wire", ["v0.3", "v1.0"])
def test_a2a_handler_receives_integers_like_mcp(
    client: TestClient, handler: _EchoHandler, wire: str
) -> None:
    handler.seen.clear()
    _mcp(client, "get_products", {**REQUEST, "context": CONTEXT})
    _a2a(client, wire, "get_products", {**REQUEST, "context": CONTEXT})
    mcp_seen, a2a_seen = handler.seen
    assert _json(a2a_seen) == _json(mcp_seen) == _json(CONTEXT)


@pytest.mark.parametrize("wire", ["v0.3", "v1.0"])
def test_a2a_error_echo_keeps_integers(client: TestClient, wire: str) -> None:
    """The structured-error envelope crosses the same ``Struct`` boundary."""
    # ``brief`` must be a string: the request fails validation before the handler.
    data = _a2a(client, wire, "get_products", {**REQUEST, "brief": 5, "context": CONTEXT})
    assert "adcp_error" in data
    assert _json(data["context"]) == _json(CONTEXT)


@pytest.mark.parametrize("wire", ["v0.3", "v1.0"])
def test_struct_cannot_carry_an_integral_float(client: TestClient, wire: str) -> None:
    """Documented tradeoff: a ``Struct`` holds ``7`` and ``7.0`` as one double.

    MCP echoes ``7.0`` as sent. A2A echoes the JSON-equal ``7``: the protobuf
    layer has already erased the spelling before the SDK sees the request.
    """
    context = {"whole": 7.0, "half": 7.5}
    mcp = _mcp(client, "get_products", {**REQUEST, "context": context})
    a2a = _a2a(client, wire, "get_products", {**REQUEST, "context": context})
    assert _json(mcp["context"]) == _json({"whole": 7.0, "half": 7.5})
    assert _json(a2a["context"]) == _json({"whole": 7, "half": 7.5})
    assert a2a["context"] == mcp["context"]


def test_text_part_json_reaches_the_handler_exactly(
    client: TestClient, handler: _EchoHandler
) -> None:
    """TextPart JSON never enters a ``Struct``, so ``7.0`` reaches the handler as sent."""
    handler.seen.clear()
    invocation = {"skill": "get_products", "parameters": {**REQUEST, "context": {"v": 7.0}}}
    response = client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": "m-text",
                    "role": "user",
                    "parts": [{"kind": "text", "text": json.dumps(invocation)}],
                }
            },
        },
    )
    assert response.status_code == 200, response.text
    assert _json(handler.seen) == _json([{"v": 7.0}])


@pytest.mark.asyncio
async def test_sse_rewrite_handles_events_split_across_chunks() -> None:
    from adcp._a2a_numbers import A2AStructNumberMiddleware

    event = b'data: {"context":{"n":7.0,"f":1.5}}\r\n\r\n'

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        for chunk in (event[:9], event[9:20], event[20:]):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    middleware = A2AStructNumberMiddleware(app)
    await middleware({"type": "http", "method": "POST", "path": "/"}, None, send)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert body == b'data: {"context": {"n": 7, "f": 1.5}}\r\n\r\n'


def test_client_reads_integers_from_a_struct_response() -> None:
    """The SDK's own A2A client restores integers when reading a result."""
    from a2a import types as pb
    from google.protobuf.json_format import ParseDict
    from google.protobuf.struct_pb2 import Value

    from adcp.protocols.a2a import A2AAdapter
    from adcp.types.core import AgentConfig, Protocol

    value = Value()
    ParseDict({"products": [], "context": CONTEXT}, value)
    task = pb.Task(
        id="t",
        context_id="c",
        status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_COMPLETED),
        artifacts=[pb.Artifact(artifact_id="a", parts=[pb.Part(data=value)])],
    )
    adapter = A2AAdapter(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    result = adapter._extract_result_from_task(task)
    assert _json(result["context"]) == _json(CONTEXT)
