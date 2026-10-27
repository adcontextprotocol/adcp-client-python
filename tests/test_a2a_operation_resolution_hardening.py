"""AdCP 3.3 A2A operation-resolution hardening (adcp-client-python#1637).

security.mdx "Operation resolution over A2A" (tag v3.3.0-beta.0) makes its
hardening clauses MUST for every verifier from 3.3:

1. strict parse before classification, on unsigned bodies too (batches,
   duplicate keys after JSON string decoding, case-variant member names);
2. A2A Part shape (one content member, no FilePart, ``kind`` agreement,
   exactly one DataPart);
3. task-continuation binding (``message.taskId`` must match the task's
   operation and creating principal).

The spec's A2A conformance vectors, both the ``contradiction-resolution`` and
``hardening`` tiers, are vendored under
``tests/conformance/vectors/a2a-operation-resolution/`` (provenance in its
``UPSTREAM.json``) and driven through
:class:`~adcp.server.signed_requests.SignedRequestVerificationMiddleware`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from a2a import types as pb
from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from asgi_lifespan import LifespanManager
from google.protobuf.json_format import ParseDict

from adcp.server import ADCPHandler, RequestSignatureVerification, signed_requests
from adcp.server.a2a_server import (
    _A2A_RESOLVED_OPERATION_SCOPE_KEY,
    _TASK_BINDING_METADATA_KEY,
    A2AOperationResolutionError,
    ADCPAgentExecutor,
    _OwnerCheckedRequestHandler,
    _task_binding_metadata,
    resolve_a2a_operation,
)
from adcp.signing import InMemoryReplayStore, StaticSignerKeys
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY

VECTOR_DIR = Path(__file__).parent / "conformance" / "vectors" / "a2a-operation-resolution"
UPSTREAM = json.loads((VECTOR_DIR / "UPSTREAM.json").read_text())
KEYS = json.loads((VECTOR_DIR / "keys.json").read_text())["keys"]
BUYER = "https://buyer.example.com/"


def _vector_files() -> list[tuple[str, Path]]:
    return [
        (path.relative_to(VECTOR_DIR).as_posix(), path)
        for path in sorted(VECTOR_DIR.glob("*/*.json"))
    ]


VECTORS = _vector_files()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_vendored_vectors_match_upstream_pin() -> None:
    """Every vendored file is pinned, and nothing is missing or extra."""
    on_disk = {
        path.relative_to(VECTOR_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VECTOR_DIR.rglob("*")
        if path.is_file() and path.name != "UPSTREAM.json"
    }
    assert on_disk == UPSTREAM["sha256"]


def test_vector_set_covers_both_tiers() -> None:
    tiers = {json.loads(path.read_text())["tier"] for _, path in VECTORS}
    assert tiers == {"contradiction-resolution", "hardening"}
    assert len(VECTORS) == 33


# ---------------------------------------------------------------------------
# Conformance vectors through the signature gate
# ---------------------------------------------------------------------------


async def _run_vector(
    vector: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, Any, list[dict[str, Any]]]:
    reached: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    monkeypatch.setattr(
        signed_requests, "time", SimpleNamespace(time=lambda: float(vector["reference_now"]))
    )
    config = RequestSignatureVerification.from_capability(
        vector["verifier_capability"],
        signer_keys=StaticSignerKeys({BUYER: {"keys": KEYS}}),
        replay_store=InMemoryReplayStore(),
        signing_profile_version="3.2",
    )
    middleware = signed_requests.SignedRequestVerificationMiddleware(app, config, transport="a2a")
    request = vector["request"]
    url = urlsplit(request["url"])
    body = request["body"].encode()
    headers = [(b"host", url.netloc.encode())] + [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in request["headers"].items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": request["method"],
        "scheme": url.scheme,
        "server": (url.hostname, 443),
        "path": url.path,
        "raw_path": url.path.encode(),
        "query_string": url.query.encode(),
        "root_path": "",
        "headers": headers,
    }
    sent: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(payload), reached


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.parametrize(
    ("name", "path"),
    VECTORS,
    ids=[f"{json.loads(p.read_text())['tier']}:{n}" for n, p in VECTORS],
)
async def test_a2a_conformance_vector(
    name: str, path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vector = json.loads(path.read_text())
    expected = vector["expected_outcome"]
    status, payload, reached = await _run_vector(vector, monkeypatch)

    if not expected["success"]:
        assert status == 401, (name, payload)
        assert payload == {"error": expected["error_code"]}, name
        assert reached == [], f"{name}: rejected request reached the dispatcher"
        return

    assert status == 200, (name, payload)
    assert len(reached) == 1
    scope = reached[0]
    assert scope.get(_A2A_RESOLVED_OPERATION_SCOPE_KEY) == expected["resolved_operation"], name
    if expected["status"] == "verified":
        assert scope.get(VERIFIED_SIGNER_SCOPE_KEY) is not None, name
    else:
        assert scope.get(VERIFIED_SIGNER_SCOPE_KEY) is None, name


# ---------------------------------------------------------------------------
# Resolver edge cases beyond the static vectors
# ---------------------------------------------------------------------------


def _send(parts: list[Any], *, method: str = "SendMessage", **message: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": method,
        "params": {"message": {"messageId": "m", "role": "ROLE_USER", "parts": parts, **message}},
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"jsonrpc": "2.0", "id": "1", "Method": "SendMessage", "method": "SendMessage"},
            id="case-variant-method",
        ),
        pytest.param(
            {**_send([{"data": {"skill": "get_products"}}]), "Params": {}},
            id="case-variant-params",
        ),
        pytest.param(
            {"jsonrpc": "2.0", "method": "SendMessage", "params": {"Message": {}, "message": {}}},
            id="case-variant-message",
        ),
        pytest.param(
            _send([{"data": {"skill": "get_products"}}], Parts=[]),
            id="case-variant-parts",
        ),
        pytest.param(
            _send([{"data": {"skill": "get_products"}, "Kind": "data"}]),
            id="case-variant-kind",
        ),
        pytest.param(
            _send(
                [
                    {"kind": "file", "file": {"uri": "https://x.example/a"}},
                    {"data": {"skill": "a"}},
                ],
                method="message/send",
            ),
            id="v0.3-filepart",
        ),
        pytest.param(
            _send([{"kind": "data", "text": "get_products"}], method="message/send"),
            id="v0.3-kind-data-on-text",
        ),
        pytest.param(_send([{"data": {"skill": "a"}, "raw": "AAAA"}]), id="data-and-raw"),
        pytest.param(_send([{"metadata": {}}]), id="part-without-content"),
        pytest.param(_send(["get_products"]), id="part-not-object"),
        pytest.param(
            {"jsonrpc": "2.0", "method": "SendMessage", "params": {"message": {"parts": {}}}},
            id="parts-not-array",
        ),
        pytest.param({"jsonrpc": "2.0", "method": "SendMessage", "params": []}, id="params-array"),
        pytest.param({"jsonrpc": "2.0", "method": 1}, id="method-not-string"),
        pytest.param([], id="empty-batch"),
        pytest.param("SendMessage", id="string-body"),
        pytest.param(None, id="null-body"),
    ],
)
def test_resolver_rejects(payload: Any) -> None:
    with pytest.raises(A2AOperationResolutionError):
        resolve_a2a_operation(payload)


def test_resolver_reads_skill_exactly() -> None:
    payload = _send([{"text": "decoy"}, {"data": {"skill": "Create_Media_Buy", "input": {}}}])
    assert resolve_a2a_operation(payload) == ("SendMessage", "Create_Media_Buy")


def test_resolver_leaves_non_message_methods_to_protocol_lists() -> None:
    assert resolve_a2a_operation({"jsonrpc": "2.0", "method": "tasks/cancel"}) == (
        "tasks/cancel",
        None,
    )
    # A variant spelling is not message-carrying: it resolves to no operation
    # and the dispatcher answers method-not-found.
    assert resolve_a2a_operation(_send([], method="sendmessage")) == ("sendmessage", None)


def test_resolver_http_json_binding() -> None:
    body = {"message": {"parts": [{"data": {"skill": "create_media_buy"}}]}}
    assert resolve_a2a_operation(body, http_json=True) == (None, "create_media_buy")
    with pytest.raises(A2AOperationResolutionError):
        resolve_a2a_operation({"message": {"parts": []}}, http_json=True)


def test_resolver_accepts_v0_3_kinds() -> None:
    payload = _send(
        [{"kind": "text", "text": "hi"}, {"kind": "data", "data": {"skill": "get_products"}}],
        method="message/stream",
    )
    assert resolve_a2a_operation(payload) == ("message/stream", "get_products")


# ---------------------------------------------------------------------------
# End to end through serve()
# ---------------------------------------------------------------------------


class _Recording(ADCPHandler[Any]):
    advertised_tools: set[str] = {"get_adcp_capabilities", "get_products", "get_media_buys"}

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        self.calls.append("get_products")
        return {"products": []}

    async def get_media_buys(self, params: Any, context: Any = None) -> dict[str, Any]:
        self.calls.append("get_media_buys")
        return {"media_buys": []}


def _app(handler: ADCPHandler[Any], config: RequestSignatureVerification, **kwargs: Any) -> Any:
    from adcp.server.serve import _build_mcp_and_a2a_app

    return _build_mcp_and_a2a_app(
        handler,
        name="test-agent",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        advertise_all=False,
        stateless_http=True,
        allowed_hosts=["localhost"],
        auth=None,
        request_signature_verification=config,
        **kwargs,
    )


def _config(**overrides: Any) -> RequestSignatureVerification:
    kwargs: dict[str, Any] = {
        "signer_keys": StaticSignerKeys({BUYER: {"keys": KEYS}}),
        "required_for": frozenset({"create_media_buy"}),
    }
    kwargs.update(overrides)
    return RequestSignatureVerification(**kwargs)


@contextlib.asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            yield client


JSON = {"content-type": "application/json", "A2A-Version": "1.0"}


def _assert_malformed(response: httpx.Response) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == 'Signature error="request_body_malformed"'
    assert response.json() == {"error": "request_body_malformed"}


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_decoy_text_and_metadata_dispatch_the_datapart_skill() -> None:
    """Vector positive/004 end to end: the DataPart skill is dispatched."""
    vector = json.loads(
        (VECTOR_DIR / "positive" / "004-unsigned-decoy-text-and-metadata.json").read_text()
    )
    handler = _Recording()
    async with _client(_app(handler, _config())) as client:
        response = await client.post(
            "/", content=vector["request"]["body"].encode(), headers=vector["request"]["headers"]
        )
    assert response.status_code == 200, response.text
    assert handler.calls == [vector["expected_outcome"]["dispatched_operation"]]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_unresolvable_message_rejected_even_when_nothing_is_required() -> None:
    """Rule 1 is a precondition of the gate, not a capability-list outcome."""
    handler = _Recording()
    body = _send([{"text": '{"skill": "get_products"}'}])
    async with _client(_app(handler, _config(required_for=frozenset()))) as client:
        response = await client.post("/", content=json.dumps(body).encode(), headers=JSON)
    _assert_malformed(response)
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_mcp_duplicate_tool_name_rejected() -> None:
    raw = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        b'{"name":"get_products","n\\u0061me":"create_media_buy","arguments":{}}}'
    )
    handler = _Recording()
    async with _client(_app(handler, _config())) as client:
        response = await client.post(
            "/mcp",
            content=raw,
            headers={**JSON, "accept": "application/json, text/event-stream"},
        )
    _assert_malformed(response)
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_dispatcher_refuses_an_operation_other_than_the_gate_resolved() -> None:
    """Rule 3: a custom parser that disagrees with the gate never reaches a handler."""
    handler = _Recording()

    def parser(_context: Any) -> tuple[str | None, dict[str, Any]]:
        return "get_media_buys", {}

    body = _send([{"data": {"skill": "get_products", "input": {}}}])
    async with _client(_app(handler, _config(), message_parser=parser)) as client:
        response = await client.post("/", content=json.dumps(body).encode(), headers=JSON)
    assert response.status_code == 200, response.text
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    error = task["artifacts"][0]["parts"][0]["data"]["adcp_error"]
    assert error["code"] == "INVALID_REQUEST"
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_executor_records_the_task_binding() -> None:
    handler = _Recording()
    body = _send([{"data": {"skill": "get_products", "input": {}}}])
    async with _client(_app(handler, _config())) as client:
        response = await client.post("/", content=json.dumps(body).encode(), headers=JSON)
    task = response.json()["result"]["task"]
    # Anonymous caller: the operation is recorded, no principal, so the task
    # can never be continued.
    assert task["metadata"] == {_TASK_BINDING_METADATA_KEY: {"operation": "get_products"}}


# ---------------------------------------------------------------------------
# Task-continuation binding
# ---------------------------------------------------------------------------


class _User(User):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def is_authenticated(self) -> bool:
        return bool(self._name)

    @property
    def user_name(self) -> str:
        return self._name


def _ctx(name: str) -> ServerCallContext:
    return ServerCallContext(user=_User(name))


def _request_handler(store: InMemoryTaskStore) -> _OwnerCheckedRequestHandler:
    from adcp.server.a2a_server import _build_agent_card

    handler = _Recording()
    executor = ADCPAgentExecutor(handler, validation=None, advertise_all=True)
    card = _build_agent_card(handler, name="t", port=0, description=None, version="1")
    return _OwnerCheckedRequestHandler(agent_executor=executor, task_store=store, agent_card=card)


async def _seed(store: InMemoryTaskStore, *, operation: str, owner: str) -> str:
    ctx = _ctx(owner)
    task = pb.Task(
        id="task-1",
        context_id="ctx-1",
        status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    ParseDict(_task_binding_metadata(operation, ctx), task.metadata)
    await store.save(task, ctx)
    return task.id


def _continuation(task_id: str, skill: str) -> pb.SendMessageRequest:
    return ParseDict(
        {
            "message": {
                "messageId": "m2",
                "role": "ROLE_USER",
                "taskId": task_id,
                "parts": [{"data": {"skill": skill, "parameters": {}}}],
            }
        },
        pb.SendMessageRequest(),
    )


@pytest.mark.asyncio
async def test_continuation_with_matching_operation_and_principal_passes() -> None:
    store = InMemoryTaskStore()
    task_id = await _seed(store, operation="get_products", owner="buyer-a")
    handler = _request_handler(store)
    await handler._check_continuation_binding(
        _continuation(task_id, "get_products"), _ctx("buyer-a")
    )


@pytest.mark.asyncio
async def test_continuation_for_another_operation_rejected_before_handler_entry() -> None:
    store = InMemoryTaskStore()
    task_id = await _seed(store, operation="get_products", owner="buyer-a")
    handler = _request_handler(store)
    with pytest.raises(InvalidParamsError) as excinfo:
        await handler._check_continuation_binding(
            _continuation(task_id, "create_media_buy"), _ctx("buyer-a")
        )
    assert excinfo.value.data == {
        "adcp_error": {
            "code": "INVALID_REQUEST",
            "message": "Task continuation does not match the task's operation or principal",
            "recovery": "correctable",
        }
    }
    assert handler.agent_executor._handler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_continuation_from_another_principal_rejected() -> None:
    store = InMemoryTaskStore()
    task_id = await _seed(store, operation="get_products", owner="buyer-a")
    handler = _request_handler(store)
    # The owner-partitioned store already hides the task from buyer-b.
    with pytest.raises(TaskNotFoundError):
        await handler._check_continuation_binding(
            _continuation(task_id, "get_products"), _ctx("buyer-b")
        )


@pytest.mark.asyncio
async def test_continuation_principal_checked_even_with_a_shared_store() -> None:
    """A store that does not partition by owner still cannot transfer a task."""
    store = InMemoryTaskStore(owner_resolver=lambda _ctx: "shared")
    task_id = await _seed(store, operation="get_products", owner="buyer-a")
    handler = _request_handler(store)
    with pytest.raises(TaskNotFoundError):
        await handler._check_continuation_binding(
            _continuation(task_id, "get_products"), _ctx("buyer-b")
        )


@pytest.mark.asyncio
async def test_continuation_of_unbound_or_anonymous_task_rejected() -> None:
    store = InMemoryTaskStore(owner_resolver=lambda _ctx: "shared")
    await store.save(
        pb.Task(
            id="unbound",
            context_id="c",
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_INPUT_REQUIRED),
        ),
        _ctx("buyer-a"),
    )
    anon_id = await _seed(store, operation="get_products", owner="")
    handler = _request_handler(store)
    for task_id, caller in (("unbound", "buyer-a"), (anon_id, "")):
        with pytest.raises(TaskNotFoundError):
            await handler._check_continuation_binding(
                _continuation(task_id, "get_products"), _ctx(caller)
            )
