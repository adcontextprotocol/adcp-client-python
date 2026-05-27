"""Tests for A2A server support: ADCPAgentExecutor, create_a2a_server."""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any

import pytest
from a2a import types as pb
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import (
    EventQueueLegacy as EventQueue,
)  # TODO(#699): drop alias when a2aproject/a2a-python#1064 lands a type-clean EventQueue successor
from google.protobuf.json_format import MessageToDict as _MessageToDict
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from adcp.server import ADCPHandler, ToolContext
from adcp.server.a2a_server import (
    ADCPAgentExecutor as _ADCPAgentExecutor,
)
from adcp.server.a2a_server import (
    _build_agent_card,
    _part_data_dict,
    create_a2a_server,
)
from adcp.server.test_controller import TestControllerError, TestControllerStore


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A executor / agent-card tests cover transport + skill
    advertisement, not the sandbox-authority gate. Set the legacy env
    opt-in so the gate admits without requiring per-call resolver
    wiring. The gate's own behavior is exercised in
    ``test_account_mode_gate.py``."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


def ADCPAgentExecutor(*args: Any, **kwargs: Any) -> _ADCPAgentExecutor:  # noqa: N802
    """Test wrapper that defaults ``validation=None``.

    The framework defaults to strict-by-default wire-conformance
    validation; this test module focuses on transport plumbing
    (dispatch, middleware composition, parser hooks, context echo)
    and uses minimal stub handlers that do not return fully
    spec-conformant responses. Opting out of validation here keeps the
    transport contract under test without forcing every stub to grow
    full ``Product`` / ``adcp.idempotency`` payloads. Tests that
    specifically want to assert validation behavior pass an explicit
    ``validation=`` kwarg, which overrides this default.
    """
    kwargs.setdefault("validation", None)
    return _ADCPAgentExecutor(*args, **kwargs)


# Backwards-compat fixture aliases: tests construct these at the
# 0.3-era Pydantic call sites (``DataPart(data=...)``, ``TextPart(text=...)``,
# ``Part(root=data_part)``). In 1.0 everything is a proto ``Part`` with a
# ``content`` oneof; these helpers produce that shape while keeping
# the old factory call signatures readable.


def DataPart(data: dict) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def ScalarDataPart(value: str) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    data = Value()
    data.string_value = value
    return pb.Part(data=data)


def TextPart(text: str) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    return pb.Part(text=text)


def Part(root: pb.Part) -> pb.Part:  # noqa: N802 (0.3 fixture shim)
    """Identity wrapper: the 1.0 ``Part`` has no ``root`` indirection."""
    return root


def Message(  # noqa: N802 (0.3 fixture shim)
    *, message_id: str, role: pb.Role.ValueType, parts: list
) -> pb.Message:
    return pb.Message(message_id=message_id, role=role, parts=parts)


# Shim the ``Role.user`` / ``Role.agent`` attribute access the 0.3
# Pydantic enum exposed onto the 1.0 proto enum. Monkey-patching here
# keeps every ``Role.user`` call site in the suite untouched.
pb.Role.user = pb.Role.ROLE_USER  # type: ignore[attr-defined]
pb.Role.agent = pb.Role.ROLE_AGENT  # type: ignore[attr-defined]


# Expose the 1.0 proto types under the 0.3 names the suite uses.
Task = pb.Task
Role = pb.Role


def MessageSendParams(  # noqa: N802 (0.3 fixture shim)
    *, message: pb.Message
) -> pb.SendMessageRequest:
    """Build a ``SendMessageRequest`` carrying the given message.

    0.3 tests passed ``MessageSendParams(message=msg)`` to
    :class:`RequestContext`; in 1.0 :class:`RequestContext` accepts a
    ``SendMessageRequest`` under the ``request=`` kwarg directly. The
    shim keeps every call site readable while translating to the 1.0
    shape.
    """
    return pb.SendMessageRequest(message=message)


# Build a ``ServerCallContext`` once so RequestContext(call_context=...)
# has something to accept — the tests never read off it, they just need
# the constructor to succeed.
def _empty_call_context():
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext

    return ServerCallContext(user=UnauthenticatedUser())


# 1.0 RequestContext __init__ uses positional call_context as arg 1.
# Shadow it with a helper that auto-injects an empty call_context so
# existing test constructions work without the extra keyword noise.
_RealRequestContext = RequestContext


def RequestContext(*args, **kwargs):  # noqa: N802
    if "call_context" not in kwargs and not args:
        kwargs["call_context"] = _empty_call_context()
    return _RealRequestContext(*args, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _TestHandler(ADCPHandler):
    """Minimal handler that supports get_adcp_capabilities and get_products."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {
            "products": [{"id": "p1", "name": "Display"}],
            "sandbox": True,
        }


class _MediaBuyVersionHandler(ADCPHandler):
    def __init__(self) -> None:
        self.contexts: list[ToolContext | None] = []

    async def create_media_buy(
        self,
        params: Any,
        context: ToolContext | None = None,
    ) -> dict[str, Any]:
        self.contexts.append(context)
        return {
            "media_buy_id": "mb_1",
            "packages": [],
            "status": "completed",
            "media_buy_status": "pending_creatives",
        }


def _make_datapart_msg(skill: str, parameters: dict[str, Any] | None = None) -> Message:
    return Message(
        message_id="msg-1",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": skill, "parameters": parameters or {}}))],
    )


def _make_text_msg(text: str) -> Message:
    return Message(
        message_id="msg-1",
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
    )


def _first_data_part(task: pb.Task) -> dict[str, Any]:
    assert task.artifacts
    for part in task.artifacts[0].parts:
        if part.WhichOneof("content") == "data":
            return _MessageToDict(part.data)
    raise AssertionError("task has no DataPart")


# ---------------------------------------------------------------------------
# ADCPAgentExecutor — sync tests
# ---------------------------------------------------------------------------


def test_executor_supported_skills():
    executor = ADCPAgentExecutor(_TestHandler())
    skills = executor.supported_skills
    assert "get_adcp_capabilities" in skills
    assert "get_products" in skills


def test_part_data_dict_ignores_scalar_value_payloads():
    assert _part_data_dict(ScalarDataPart("not an object")) is None


# ---------------------------------------------------------------------------
# ADCPAgentExecutor — async tests
# ---------------------------------------------------------------------------


async def test_execute_with_datapart():
    """Executor dispatches DataPart skill invocation to handler."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED

    # Verify the result data is in the artifact
    assert event.artifacts
    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    assert len(data_parts) >= 1
    result = data_parts[0]
    assert "products" in result
    assert result["products"][0]["id"] == "p1"


async def test_execute_skips_scalar_datapart_and_uses_text_fallback():
    """Scalar protobuf Value payloads are not AdCP DataPart objects."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(
        request=MessageSendParams(
            message=Message(
                message_id="msg-1",
                role=Role.user,
                parts=[
                    Part(root=ScalarDataPart("not an object")),
                    Part(root=TextPart('{"skill": "get_products", "parameters": {}}')),
                ],
            )
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


async def test_pre_validation_hook_chain_runs_through_a2a_executor():
    """A2A executor applies the same ordered hook-chain behavior as MCP."""

    class _HookHandler(ADCPHandler):
        async def get_products(self, params: dict[str, Any], context: Any = None):
            return {"params_received": dict(params)}

    calls: list[str] = []

    def first(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(f"first:{tool_name}")
        return {**args, "first": True}

    def second(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append(f"second:{tool_name}:{args['first']}")
        return {**args, "second": True}

    executor = ADCPAgentExecutor(
        _HookHandler(),
        pre_validation_hooks={"get_products": [first, second]},
    )
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg("get_products", {"buying_mode": "brief"})
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
    result = _first_data_part(event)
    assert calls == ["first:get_products", "second:get_products:True"]
    assert result["params_received"]["first"] is True
    assert result["params_received"]["second"] is True


async def test_context_auto_injected():
    """Context from request is automatically echoed in response."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg(
                "get_products",
                {"context": {"correlation_id": "test-ctx-123"}},
            )
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result["context"]["correlation_id"] == "test-ctx-123"


async def test_a2a_omitted_version_keeps_current_media_buy_envelope():
    handler = _MediaBuyVersionHandler()
    executor = ADCPAgentExecutor(handler)
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("create_media_buy")))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    data = _first_data_part(event)
    assert data["status"] == "completed"
    assert data["media_buy_status"] == "pending_creatives"
    assert handler.contexts[0] is not None
    assert handler.contexts[0].resolved_adcp_version is None


async def test_a2a_explicit_major_version_projects_30_media_buy_shape():
    handler = _MediaBuyVersionHandler()
    executor = ADCPAgentExecutor(handler)
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg("create_media_buy", {"adcp_major_version": 3})
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    data = _first_data_part(event)
    assert data["status"] == "pending_creatives"
    assert "media_buy_status" not in data
    assert handler.contexts[0] is not None
    assert handler.contexts[0].resolved_adcp_version == "3.0"


async def test_execute_unknown_skill():
    """Executor returns failed task for unknown skills."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("nonexistent_skill")))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED


async def test_execute_no_skill_in_message():
    """Executor returns failed task when message has no parseable skill."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(request=MessageSendParams(message=_make_text_msg("hello")))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED


async def test_execute_json_text_fallback():
    """Executor parses JSON text as skill invocation."""
    executor = ADCPAgentExecutor(_TestHandler())
    payload = json.dumps({"skill": "get_products", "parameters": {}})
    ctx = RequestContext(request=MessageSendParams(message=_make_text_msg(payload)))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


async def test_execute_handler_exception():
    """Handler exception returns failed task without leaking details."""

    class _BrokenHandler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params: Any, context: Any = None) -> Any:
            raise RuntimeError("secret database connection string leaked")

    executor = ADCPAgentExecutor(_BrokenHandler())
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED

    # Verify exception details are NOT in the error message
    text_parts = [p.text for p in event.artifacts[0].parts if p.WhichOneof("content") == "text"]
    error_text = text_parts[0]
    assert "secret database" not in error_text
    assert "get_products" in error_text


async def test_cancel():
    """Cancel returns a canceled task."""
    executor = ADCPAgentExecutor(_TestHandler())
    ctx = RequestContext(task_id="t1", context_id="c1")
    queue = EventQueue()

    await executor.cancel(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_CANCELED


# ---------------------------------------------------------------------------
# Agent card builder
# ---------------------------------------------------------------------------


def test_build_agent_card_with_skills():
    card = _build_agent_card(_TestHandler(), name="test-agent", port=3001)
    assert card.name == "test-agent"
    assert card.supported_interfaces[0].url == "http://localhost:3001/"
    skill_ids = [s.id for s in card.skills]
    assert "get_adcp_capabilities" in skill_ids
    assert "get_products" in skill_ids


def test_build_agent_card_skills_tagged_adcp():
    card = _build_agent_card(_TestHandler(), name="test", port=8080)
    for skill in card.skills:
        assert "adcp" in skill.tags


def test_build_agent_card_public_url_overrides_localhost():
    card = _build_agent_card(
        _TestHandler(),
        name="test",
        port=8080,
        public_url="https://agent.example.com/",
    )
    for iface in card.supported_interfaces:
        assert iface.url == "https://agent.example.com/"


def test_build_agent_card_public_url_trailing_slash_normalised():
    card = _build_agent_card(
        _TestHandler(),
        name="test",
        port=8080,
        public_url="https://agent.example.com",
    )
    for iface in card.supported_interfaces:
        assert iface.url == "https://agent.example.com/"


def test_build_agent_card_public_url_none_uses_localhost():
    card = _build_agent_card(_TestHandler(), name="test", port=8080, public_url=None)
    for iface in card.supported_interfaces:
        assert iface.url == "http://localhost:8080/"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_public_url_in_card(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    app = create_a2a_server(
        _TestHandler(),
        name="test-agent",
        public_url="https://agent.example.com/",
    )
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card_json = resp.json()
    for iface in card_json.get("supportedInterfaces", []):
        assert iface["url"] == "https://agent.example.com/"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_no_public_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    app = create_a2a_server(_TestHandler(), name="test-agent", port=9000)
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card_json = resp.json()
    for iface in card_json.get("supportedInterfaces", []):
        assert iface["url"] == "http://localhost:9000/"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_public_url_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PUBLIC_URL", "https://env.example.com/")
    app = create_a2a_server(_TestHandler(), name="test-agent")
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card_json = resp.json()
    for iface in card_json.get("supportedInterfaces", []):
        assert iface["url"] == "https://env.example.com/"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_public_url_kwarg_takes_precedence_over_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PUBLIC_URL", "https://env.example.com/")
    app = create_a2a_server(
        _TestHandler(),
        name="test-agent",
        public_url="https://explicit.example.com/",
    )
    from starlette.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card_json = resp.json()
    for iface in card_json.get("supportedInterfaces", []):
        assert iface["url"] == "https://explicit.example.com/"


# ---------------------------------------------------------------------------
# create_a2a_server
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_creates_starlette_app():
    app = create_a2a_server(_TestHandler(), name="test-agent")
    # Starlette app has .routes
    assert hasattr(app, "routes")
    route_paths = [r.path for r in app.routes]
    # Both the 1.0 canonical path and the 0.3 alias must be registered.
    assert any(
        p.startswith("/.well-known/agent-card") for p in route_paths
    ), "canonical /.well-known/agent-card.json route missing"
    assert (
        "/.well-known/agent.json" in route_paths
    ), "0.3 alias /.well-known/agent.json route missing from create_a2a_server"


# ---------------------------------------------------------------------------
# TestControllerStore integration
# ---------------------------------------------------------------------------


class _TestStore(TestControllerStore):
    def __init__(self) -> None:
        self.accounts: dict[str, str] = {"acct-1": "active"}

    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        if account_id not in self.accounts:
            raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")
        prev = self.accounts[account_id]
        self.accounts[account_id] = status
        return {"previous_state": prev, "current_state": status}


def test_executor_with_test_controller_has_skill():
    """Test controller registers comply_test_controller as a skill."""
    executor = ADCPAgentExecutor(_TestHandler(), test_controller=_TestStore())
    assert "comply_test_controller" in executor.supported_skills


async def test_execute_test_controller_list_scenarios():
    """comply_test_controller list_scenarios works via A2A."""
    executor = ADCPAgentExecutor(_TestHandler(), test_controller=_TestStore())
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg(
                "comply_test_controller",
                {"scenario": "list_scenarios"},
            )
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED

    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result["success"] is True
    assert "force_account_status" in result["scenarios"]


async def test_execute_test_controller_force_account_status():
    """comply_test_controller dispatches force_account_status correctly."""
    executor = ADCPAgentExecutor(_TestHandler(), test_controller=_TestStore())
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg(
                "comply_test_controller",
                {
                    "scenario": "force_account_status",
                    "params": {"account_id": "acct-1", "status": "suspended"},
                },
            )
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED

    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result["success"] is True
    assert result["previous_state"] == "active"
    assert result["current_state"] == "suspended"


async def test_execute_test_controller_error():
    """comply_test_controller handles TestControllerError."""
    executor = ADCPAgentExecutor(_TestHandler(), test_controller=_TestStore())
    ctx = RequestContext(
        request=MessageSendParams(
            message=_make_datapart_msg(
                "comply_test_controller",
                {
                    "scenario": "force_account_status",
                    "params": {"account_id": "nonexistent", "status": "active"},
                },
            )
        )
    )
    queue = EventQueue()

    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert (
        event.status.state == pb.TaskState.TASK_STATE_COMPLETED
    )  # A2A task succeeds; error is in data

    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result["success"] is False
    assert result["error"] == "NOT_FOUND"


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_with_test_controller():
    """create_a2a_server includes comply_test_controller in agent card."""
    app = create_a2a_server(_TestHandler(), name="test-agent", test_controller=_TestStore())
    assert hasattr(app, "routes")


# ---------------------------------------------------------------------------
# Pluggable TaskStore (issue #224)
# ---------------------------------------------------------------------------


class _RecordingTaskStore:
    """TaskStore that records every save/get/delete for test assertions.

    Implements the a2a-sdk ``TaskStore`` protocol via duck-typing. Tests
    inject this to prove ``create_a2a_server(task_store=...)`` actually
    threads the store through to ``DefaultRequestHandler`` — the whole
    point of the hook.
    """

    def __init__(self) -> None:
        self.saves: list[str] = []
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self._store: dict[str, Any] = {}

    async def save(self, task: Any, context: Any = None) -> None:
        self.saves.append(task.id)
        self._store[task.id] = task

    async def get(self, task_id: str, context: Any = None) -> Any | None:
        self.gets.append(task_id)
        return self._store.get(task_id)

    async def delete(self, task_id: str, context: Any = None) -> None:
        self.deletes.append(task_id)
        self._store.pop(task_id, None)

    async def list(self, params: Any = None, context: Any = None) -> Any:
        """New 1.0 abstract method; return an empty ListTasksResponse."""
        return pb.ListTasksResponse(tasks=list(self._store.values()))


def _extract_default_request_handler(app: Any) -> Any:
    """Walk the a2a-sdk Starlette app graph to the DefaultRequestHandler.

    Structure is ``Starlette.routes[*].endpoint.__self__ →
    A2AStarletteApplication.handler (JSONRPCHandler) → .request_handler``.
    Touching this indirection in one place localises the blast radius if
    a2a-sdk changes its internals.
    """
    from a2a.server.request_handlers import DefaultRequestHandler

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        dispatcher = getattr(endpoint, "__self__", None) if endpoint else None
        if dispatcher is None:
            continue
        request_handler = getattr(dispatcher, "request_handler", None)
        if isinstance(request_handler, DefaultRequestHandler):
            return request_handler
    raise AssertionError(
        "Could not locate the DefaultRequestHandler on the A2A app — "
        "a2a-sdk internals likely changed. Update _extract_default_request_handler "
        "but keep the contract: task_store= on create_a2a_server must thread "
        "through to DefaultRequestHandler.task_store."
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_defaults_to_in_memory_task_store():
    """Default behavior preserved: omitting task_store falls back to
    InMemoryTaskStore, so existing adopters see no change."""
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore

    app = create_a2a_server(_TestHandler(), name="test-agent")
    handler = _extract_default_request_handler(app)
    assert isinstance(handler.task_store, InMemoryTaskStore), (
        "Default task_store should be InMemoryTaskStore when no override "
        "is provided, preserving pre-#224 behavior."
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_accepts_custom_task_store():
    """Custom TaskStore instance must be threaded through to the A2A
    DefaultRequestHandler — the whole point of the hook."""
    store = _RecordingTaskStore()
    app = create_a2a_server(_TestHandler(), name="test-agent", task_store=store)
    handler = _extract_default_request_handler(app)
    assert handler.task_store is store, (
        "create_a2a_server(task_store=...) dropped the custom store. "
        "DefaultRequestHandler.task_store is instead "
        f"{type(handler.task_store).__name__}."
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
async def test_custom_task_store_receives_saves_from_skill_dispatch():
    """Behavioral test: a skill call through the A2A executor actually
    produces ``save()`` traffic on the pluggable store.

    The attribute-identity check in the previous test proves the hook is
    wired at construction time; this one proves the hook is *used* at
    runtime — the failure mode it defends against is a2a-sdk version
    changes that rename or sidestep ``DefaultRequestHandler.task_store``
    while the attribute reference stays intact.

    We drive the executor directly (no HTTP) and observe the recording
    store. Exercising via ``DefaultRequestHandler`` would be closer to
    production but pulls in message-send request construction that
    a2a-sdk keeps in flux; this level is the stable behavioral contract.
    """
    store = _RecordingTaskStore()
    # The executor itself doesn't touch the store — DefaultRequestHandler
    # does. But routing an end-to-end message through the full JSON-RPC
    # path via httpx is a lot of scaffolding for a single-store
    # assertion, and the store's ABC is the stable surface. Go through
    # DefaultRequestHandler.on_get_task instead: if the handler asks
    # the store anything, the recording store records it.
    app = create_a2a_server(_TestHandler(), name="behavioral-test", task_store=store)
    handler = _extract_default_request_handler(app)

    # A get for a non-existent task should route through our store.
    # ``on_get_task`` raises :class:`TaskNotFoundError` once the store
    # returns None; that's fine — what we care about is that the store
    # *was queried*. If the handler bypassed our store and went somewhere
    # else, the recording set stays empty. In 1.0, handler methods take
    # the request as a proto and a :class:`ServerCallContext`.
    from a2a.utils.errors import A2AError

    with contextlib.suppress(A2AError):
        await handler.on_get_task(pb.GetTaskRequest(id="does-not-exist"), _empty_call_context())
    assert "does-not-exist" in store.gets, (
        "DefaultRequestHandler did not route the get_task call through our "
        "custom store. The kwarg is wired but not exercised."
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
async def test_task_store_persists_across_app_recreation():
    """A shared ``TaskStore`` instance is reusable across multiple
    ``create_a2a_server`` calls — the "restart" property durable stores
    actually need. This test deliberately uses direct store access on
    both sides of the 'restart' because it's proving persistence of
    the store's own state, not a claim about the new server using it
    (that's the previous test's job)."""
    store = _RecordingTaskStore()

    task_1 = Task(
        id="task-persistence-1",
        context_id="ctx-1",
        status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_COMPLETED),
    )
    await store.save(task_1)

    # Recreate the server. In production this is a process restart; here
    # it's just a second create_a2a_server call reusing the store.
    create_a2a_server(_TestHandler(), name="test-agent-v2", task_store=store)

    retrieved = await store.get("task-persistence-1")
    assert retrieved is not None
    assert retrieved.id == "task-persistence-1"
    assert "task-persistence-1" in store.gets


async def test_sqlite_task_store_isolates_scopes_by_context():
    """Reference ``SqliteTaskStore`` filters reads and writes by the
    authenticated principal derived from ``context.user.user_name``.
    Cross-tenant task lookups must not succeed — the whole point of
    carrying `context` through the TaskStore ABC."""
    # Import the reference impl from the example file. Keeping the test
    # close to the example guards the security claim in the example's
    # docstring.
    import importlib.util
    import tempfile
    from pathlib import Path

    from a2a.auth.user import User
    from a2a.server.context import ServerCallContext
    from a2a.types import TaskStatus

    example_path = Path(__file__).parent.parent / "examples" / "a2a_db_tasks.py"
    spec = importlib.util.spec_from_file_location("_a2a_db_tasks_example", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _TestUser(User):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def is_authenticated(self) -> bool:
            return True

        @property
        def user_name(self) -> str:
            return self._name

    def _ctx(name: str) -> ServerCallContext:
        return ServerCallContext(user=_TestUser(name))

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "isolation.db"
        store = mod.SqliteTaskStore(db_path=db)

        task = Task(id="shared-task-id", context_id="c1", status=TaskStatus(state="completed"))
        await store.save(task, context=_ctx("tenant-a-principal"))

        # Same task id, different principal → must not surface tenant
        # A's task to tenant B. The scope column is the whole isolation
        # mechanism; if this ever returns the saved task, the example
        # just taught a cross-tenant data leak.
        got_b = await store.get("shared-task-id", context=_ctx("tenant-b-principal"))
        assert got_b is None, (
            "SqliteTaskStore returned tenant A's task to tenant B — the "
            "reference impl is leaking across principals."
        )

        # Same principal returns the task.
        got_a = await store.get("shared-task-id", context=_ctx("tenant-a-principal"))
        assert got_a is not None and got_a.id == "shared-task-id"

        # Delete from tenant B's scope must not delete tenant A's row.
        await store.delete("shared-task-id", context=_ctx("tenant-b-principal"))
        still_a = await store.get("shared-task-id", context=_ctx("tenant-a-principal"))
        assert still_a is not None, "SqliteTaskStore cross-scope delete removed tenant A's task."


# ---------------------------------------------------------------------------
# Pluggable PushNotificationConfigStore (issue #225)
# ---------------------------------------------------------------------------


class _RecordingPushConfigStore:
    """Duck-typed PushNotificationConfigStore — records every call for
    test assertions. Same shape/role as ``_RecordingTaskStore`` above."""

    def __init__(self) -> None:
        self.sets: list[tuple[str, str]] = []  # (task_id, config_id)
        self.gets: list[str] = []
        self.deletes: list[tuple[str, str | None]] = []
        self._store: dict[tuple[str, str], Any] = {}

    async def set_info(
        self,
        task_id: str,
        notification_config: Any,
        context: Any = None,
    ) -> None:
        config_id = getattr(notification_config, "id", None) or task_id
        self.sets.append((task_id, config_id))
        self._store[(task_id, config_id)] = notification_config

    async def get_info(self, task_id: str, context: Any = None) -> list[Any]:
        self.gets.append(task_id)
        return [v for (tid, _cid), v in self._store.items() if tid == task_id]

    async def delete_info(
        self,
        task_id: str,
        context: Any = None,
        config_id: str | None = None,
    ) -> None:
        self.deletes.append((task_id, config_id))
        if config_id is None:
            keys = [k for k in self._store if k[0] == task_id]
            for k in keys:
                del self._store[k]
        else:
            self._store.pop((task_id, config_id), None)


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_omits_push_config_store_by_default():
    """Omitting ``push_config_store`` preserves a2a-sdk's default:
    ``DefaultRequestHandler._push_config_store`` stays ``None`` and
    push-notif endpoints surface as ``UnsupportedOperationError``.
    Sellers opt-in to the feature by wiring a store."""
    app = create_a2a_server(_TestHandler(), name="test-agent")
    handler = _extract_default_request_handler(app)
    assert handler._push_config_store is None, (
        "push_config_store should default to None so push-notif endpoints "
        "remain unsupported until the seller explicitly opts in. Instead "
        f"got {type(handler._push_config_store).__name__}."
    )


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_accepts_custom_push_config_store():
    """Custom store must thread through to
    ``DefaultRequestHandler.push_config_store`` — the contract of the
    hook."""
    store = _RecordingPushConfigStore()
    app = create_a2a_server(_TestHandler(), name="test-agent", push_config_store=store)
    handler = _extract_default_request_handler(app)
    assert handler._push_config_store is store, (
        "create_a2a_server(push_config_store=...) dropped the custom store; "
        f"handler._push_config_store is {type(handler._push_config_store).__name__}."
    )


async def test_sqlite_push_config_store_isolates_scopes_by_contextvar():
    """Reference ``SqlitePushNotificationConfigStore`` scopes reads and
    writes by the ContextVar the seller's auth middleware populates.
    Cross-tenant registration must never surface another tenant's
    push-notif callback URL."""
    import importlib.util
    import tempfile
    from pathlib import Path

    from a2a.types import TaskPushNotificationConfig as PushNotificationConfig

    example_path = Path(__file__).parent.parent / "examples" / "a2a_db_tasks.py"
    spec = importlib.util.spec_from_file_location("_a2a_db_tasks_example", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "push.db"
        store = mod.SqlitePushNotificationConfigStore(db_path=db)
        scope_var = mod._current_push_config_scope

        cfg = PushNotificationConfig(id="cfg-1", url="https://callback.tenant-a.example/webhook")

        # Tenant A sets a config on task-shared.
        tok_a = scope_var.set("tenant-a")
        try:
            await store.set_info("task-shared", cfg)
            got_a = await store.get_info("task-shared")
            assert len(got_a) == 1 and str(got_a[0].url) == str(cfg.url)
        finally:
            scope_var.reset(tok_a)

        # Tenant B queries the same task — must see nothing.
        tok_b = scope_var.set("tenant-b")
        try:
            got_b = await store.get_info("task-shared")
            assert got_b == [], (
                "SqlitePushNotificationConfigStore returned tenant A's "
                "push-notif config to tenant B — the reference impl is "
                "leaking callback URLs across principals."
            )

            # And tenant B's delete must not affect tenant A.
            await store.delete_info("task-shared")
        finally:
            scope_var.reset(tok_b)

        tok_a2 = scope_var.set("tenant-a")
        try:
            still_a = await store.get_info("task-shared")
            assert len(still_a) == 1, (
                "SqlitePushNotificationConfigStore cross-scope delete " "removed tenant A's config."
            )
        finally:
            scope_var.reset(tok_a2)


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
async def test_custom_push_config_store_receives_sets_from_handler():
    """Behavioral test (parallel to ``test_custom_task_store_receives_saves_from_skill_dispatch``).
    If a2a-sdk ever renames or bypasses ``DefaultRequestHandler._push_config_store``
    while leaving the attribute intact, the attribute-identity check in
    the earlier test passes while production calls skip our store
    entirely. This asserts the handler's
    ``on_set_task_push_notification_config`` actually routes set-info
    through our store."""
    import contextlib as _ctxlib

    from a2a.utils.errors import A2AError

    push_store = _RecordingPushConfigStore()
    # Need a populated TaskStore because on_set validates the task exists
    # before forwarding to push_config_store.set_info. Pre-seed a task.
    task_store = _RecordingTaskStore()

    await task_store.save(
        Task(id="task-1", context_id="ctx-1", status=pb.TaskStatus(state="working")),
        _empty_call_context(),
    )

    app = create_a2a_server(
        _TestHandler(),
        name="behavioral-push",
        task_store=task_store,
        push_config_store=push_store,
    )
    handler = _extract_default_request_handler(app)

    # 1.0 folded :class:`PushNotificationConfig` into
    # :class:`TaskPushNotificationConfig` — all fields now sit directly
    # on the outer message.
    params = pb.TaskPushNotificationConfig(
        id="cfg-1",
        task_id="task-1",
        url="https://callback.example/hook",
    )
    with _ctxlib.suppress(A2AError):
        await handler.on_create_task_push_notification_config(params, _empty_call_context())

    assert ("task-1", "cfg-1") in push_store.sets, (
        "DefaultRequestHandler.on_set_task_push_notification_config did not "
        "route to our custom push_config_store. The kwarg is wired but not "
        "exercised at runtime."
    )


async def test_sqlite_push_config_store_warns_once_on_anonymous_scope():
    """Reference impl must fail LOUD when the scope_provider returns
    None — silent fall-through to the anonymous bucket is the
    multi-tenant footgun security review flagged. Warning fires once
    per store instance (not per call) so operators notice without
    flooding logs."""
    import importlib.util
    import tempfile
    import warnings as _warnings
    from pathlib import Path

    from a2a.types import TaskPushNotificationConfig as PushNotificationConfig

    example_path = Path(__file__).parent.parent / "examples" / "a2a_db_tasks.py"
    spec = importlib.util.spec_from_file_location("_a2a_db_tasks_ex_warn", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "anon.db"
        # Force the anonymous path by supplying a provider that always
        # returns None.
        store = mod.SqlitePushNotificationConfigStore(db_path=db, scope_provider=lambda: None)

        cfg = PushNotificationConfig(url="https://x.example/hook")
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            await store.set_info("task-1", cfg)
            await store.set_info("task-2", cfg)
            await store.set_info("task-3", cfg)

        anon_warnings = [w for w in caught if "SqlitePushNotificationConfigStore" in str(w.message)]
        assert len(anon_warnings) == 1, (
            "Anonymous-scope warning should fire exactly once per store "
            f"instance, got {len(anon_warnings)}."
        )


async def test_sqlite_push_config_store_synthesises_config_id_when_omitted():
    """Client not supplying ``PushNotificationConfig.id`` must not cause
    two registrations on the same task to overwrite each other via
    INSERT OR REPLACE collision on the composite PK. Reference impl
    synthesises a UUID; two sets should produce two rows."""
    import importlib.util
    import tempfile
    from pathlib import Path

    from a2a.types import TaskPushNotificationConfig as PushNotificationConfig

    example_path = Path(__file__).parent.parent / "examples" / "a2a_db_tasks.py"
    spec = importlib.util.spec_from_file_location("_a2a_db_tasks_ex_uuid", example_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "uuid.db"
        store = mod.SqlitePushNotificationConfigStore(db_path=db, scope_provider=lambda: "tenant-a")

        await store.set_info(
            "shared-task",
            PushNotificationConfig(url="https://first.example/hook"),
        )
        await store.set_info(
            "shared-task",
            PushNotificationConfig(url="https://second.example/hook"),
        )

        configs = await store.get_info("shared-task")
        assert len(configs) == 2, (
            "Two set_info calls with id=None collapsed into one row — the "
            "fallback config_id must synthesise a unique value to prevent "
            "silent overwrite."
        )


# ---------------------------------------------------------------------------
# Per-skill middleware hook (issue #226)
# ---------------------------------------------------------------------------


async def test_middleware_runs_and_sees_skill_context_and_result():
    """Single middleware observes the skill name, params, ToolContext,
    and the handler's return value. This is the audit/activity-feed
    happy path that closes #226."""
    from adcp.server import SkillMiddleware  # noqa: F401 (type import)

    observed: list[dict[str, Any]] = []

    async def audit_middleware(
        skill_name: str,
        params: dict[str, Any],
        context: Any,
        call_next: Any,
    ) -> Any:
        observed.append(
            {
                "phase": "before",
                "skill_name": skill_name,
                "params": params,
                "caller_identity": getattr(context, "caller_identity", None),
            }
        )
        result = await call_next()
        observed.append({"phase": "after", "skill_name": skill_name, "result": result})
        return result

    executor = ADCPAgentExecutor(_TestHandler(), middleware=[audit_middleware])
    ctx = RequestContext(
        request=MessageSendParams(message=_make_datapart_msg("get_products", {"brief": "coffee"}))
    )
    queue = EventQueue()
    await executor.execute(ctx, queue)

    assert len(observed) == 2, f"expected before+after, got {observed}"
    assert observed[0]["phase"] == "before"
    assert observed[0]["skill_name"] == "get_products"
    assert observed[0]["params"] == {"brief": "coffee"}
    assert observed[1]["phase"] == "after"
    assert "products" in observed[1]["result"]


async def test_middleware_composes_outermost_first():
    """Multiple middlewares compose in order: the first entry wraps
    everything later. Matches Starlette/ASGI semantics — the contract
    documented in SkillMiddleware's docstring."""
    call_order: list[str] = []

    def _mw(name: str) -> Any:
        async def middleware(skill_name, params, context, call_next):
            call_order.append(f"{name}-enter")
            try:
                return await call_next()
            finally:
                call_order.append(f"{name}-exit")

        return middleware

    executor = ADCPAgentExecutor(
        _TestHandler(), middleware=[_mw("outer"), _mw("middle"), _mw("inner")]
    )
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    assert call_order == [
        "outer-enter",
        "middle-enter",
        "inner-enter",
        "inner-exit",
        "middle-exit",
        "outer-exit",
    ], call_order


async def test_middleware_can_short_circuit_without_invoking_handler():
    """Middleware that returns without calling ``call_next`` stops the
    chain and its return value becomes the dispatch result. Rate
    limiters and feature flags rely on this."""
    handler_called = False

    class _TrackingHandler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {"adcp": {"major_versions": [3]}}

        async def get_products(self, params: Any, context: Any = None) -> Any:
            nonlocal handler_called
            handler_called = True
            return {"products": []}

    async def rate_limit_middleware(skill_name, params, context, call_next):
        # Don't call call_next — short-circuit.
        return {"products": [], "sandbox": True, "rate_limited": True}

    executor = ADCPAgentExecutor(_TrackingHandler(), middleware=[rate_limit_middleware])
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result.get("rate_limited") is True
    assert handler_called is False, (
        "middleware short-circuited but the handler still ran — call_next "
        "was invoked despite the middleware not calling it"
    )


async def test_middleware_observes_handler_exceptions():
    """Audit middleware needs to see failures, not just successes. The
    issue's leaning-toward-option-A reasoning cited this explicitly."""
    captured_exceptions: list[Exception] = []

    async def audit_middleware(skill_name, params, context, call_next):
        try:
            return await call_next()
        except Exception as exc:
            captured_exceptions.append(exc)
            raise

    class _FailingHandler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {}

        async def get_products(self, params: Any, context: Any = None) -> Any:
            raise RuntimeError("deliberate handler failure")

    executor = ADCPAgentExecutor(_FailingHandler(), middleware=[audit_middleware])
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    assert len(captured_exceptions) == 1
    assert isinstance(captured_exceptions[0], RuntimeError)
    # And the executor's normal failure path still runs — the client
    # gets a failed task, not a 500, because middleware re-raised.
    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED


async def test_no_middleware_preserves_direct_dispatch():
    """Sellers who don't pass ``middleware`` see zero behavior change —
    the dispatch chain short-circuits to direct handler invocation,
    and nothing in the chain allocates per-call middleware state."""
    executor = ADCPAgentExecutor(_TestHandler())
    assert executor._middleware == ()

    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)
    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_threads_middleware_into_executor():
    """Kwarg on ``create_a2a_server`` reaches the executor. Paranoid
    contract test: if a refactor accidentally drops the kwarg from the
    ``ADCPAgentExecutor(...)`` construction, this fires."""

    async def noop_mw(skill_name, params, context, call_next):
        return await call_next()

    app = create_a2a_server(_TestHandler(), name="mw-test", middleware=[noop_mw])
    handler = _extract_default_request_handler(app)
    executor = handler.agent_executor
    assert isinstance(executor, _ADCPAgentExecutor)
    assert executor._middleware == (noop_mw,)


async def test_middleware_can_invoke_call_next_multiple_times_for_retry():
    """Retry-on-transient-error middleware calls ``call_next()`` more
    than once — each call builds a fresh inner chain. This locks the
    re-entrant composition contract a naive loop-variable closure would
    break."""
    call_counts = {"mw": 0, "handler": 0}

    async def retry_middleware(skill_name, params, context, call_next):
        last_exc: Exception | None = None
        for _ in range(3):
            call_counts["mw"] += 1
            try:
                return await call_next()
            except RuntimeError as exc:
                last_exc = exc
        raise last_exc if last_exc else RuntimeError("unreachable")

    class _TransientFailHandler(ADCPHandler):
        async def get_adcp_capabilities(self, params: Any, context: Any = None) -> Any:
            return {}

        async def get_products(self, params: Any, context: Any = None) -> Any:
            call_counts["handler"] += 1
            if call_counts["handler"] < 3:
                raise RuntimeError("transient")
            return {"products": [{"id": "finally"}]}

    executor = ADCPAgentExecutor(_TransientFailHandler(), middleware=[retry_middleware])
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    assert call_counts["mw"] == 3
    assert call_counts["handler"] == 3
    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


async def test_middleware_can_transform_result_on_return_side():
    """Middleware can mutate or replace the value of ``call_next()``
    before returning it. The transformed value is what the client
    sees — covers the annotation / enrichment use case distinct from
    short-circuiting."""

    async def enriching_middleware(skill_name, params, context, call_next):
        result = await call_next()
        # Wrap handler's return with a marker the test observes.
        if isinstance(result, dict):
            return {**result, "middleware_marker": "wrapped"}
        return result

    executor = ADCPAgentExecutor(_TestHandler(), middleware=[enriching_middleware])
    ctx = RequestContext(request=MessageSendParams(message=_make_datapart_msg("get_products")))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
    data_parts = [
        _MessageToDict(p.data)
        for p in event.artifacts[0].parts
        if p.WhichOneof("content") == "data"
    ]
    result = data_parts[0]
    assert result["middleware_marker"] == "wrapped"
    # And the handler's original payload is still there.
    assert result["products"][0]["id"] == "p1"


# --------------------------------------------------------------------
# Custom message_parser hook (alternative A2A wire formats)
# --------------------------------------------------------------------


async def test_custom_message_parser_receives_request_context():
    """A custom parser is called with the RequestContext and owns the
    (skill_name, params) extraction — enabling JSON-RPC, bare-text, or
    vendor-specific DataPart layouts without subclassing the executor."""

    class _ParserHandler(ADCPHandler):
        async def get_products(self, params, context=None):
            return {"products": [{"id": params.get("id", "?")}]}

    received: list[Any] = []

    def my_parser(ctx: RequestContext) -> tuple[str | None, dict[str, Any]]:
        received.append(ctx)
        # Pretend the client sends ``{"operation": "get_products", "body": {...}}``.
        msg = ctx.message
        assert msg is not None
        for part in msg.parts:
            if part.WhichOneof("content") != "data":
                continue
            data = _MessageToDict(part.data)
            if isinstance(data, dict):
                op = data.get("operation")
                body = data.get("body") or {}
                if op:
                    return str(op), body if isinstance(body, dict) else {}
        return None, {}

    executor = ADCPAgentExecutor(_ParserHandler(), message_parser=my_parser)
    msg = Message(
        message_id="m-custom",
        role=Role.user,
        parts=[Part(root=DataPart(data={"operation": "get_products", "body": {"id": "p42"}}))],
    )
    ctx = RequestContext(request=MessageSendParams(message=msg))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    assert len(received) == 1
    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


async def test_custom_parser_returning_none_yields_error_task():
    """A parser that returns (None, {}) must surface as an error Task
    the same way an unparseable default message does."""

    def bad_parser(ctx: RequestContext) -> tuple[str | None, dict[str, Any]]:
        return None, {}

    class _Handler(ADCPHandler):
        async def get_products(self, params, context=None):
            return {"products": []}

    executor = ADCPAgentExecutor(_Handler(), message_parser=bad_parser)
    msg = Message(
        message_id="m-none",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": "get_products", "parameters": {}}))],
    )
    ctx = RequestContext(request=MessageSendParams(message=msg))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED


async def test_default_parser_runs_when_no_message_parser_configured():
    """No ``message_parser=`` → the built-in ``_default_parse_request``
    runs. Pins backwards-compat for sellers who don't opt in."""

    class _Handler(ADCPHandler):
        async def get_products(self, params, context=None):
            return {"products": [{"id": "default-path"}]}

    executor = ADCPAgentExecutor(_Handler())
    msg = Message(
        message_id="m-default",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": "get_products", "parameters": {}}))],
    )
    ctx = RequestContext(request=MessageSendParams(message=msg))
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)
def test_create_a2a_server_threads_message_parser_into_executor():
    """The kwarg propagates from ``create_a2a_server`` → executor."""

    def my_parser(ctx: RequestContext) -> tuple[str | None, dict[str, Any]]:
        return None, {}

    app = create_a2a_server(_TestHandler(), name="parser-test", message_parser=my_parser)
    handler = _extract_default_request_handler(app)
    executor = handler.agent_executor
    assert isinstance(executor, _ADCPAgentExecutor)
    assert executor._message_parser is my_parser


async def test_custom_parser_can_compose_with_default():
    """Typical pattern: seller's parser tries a custom shape first,
    then falls through to the default parser for legacy clients."""

    class _Handler(ADCPHandler):
        async def get_products(self, params, context=None):
            return {"products": [{"from_params": params.get("source", "unknown")}]}

    executor = ADCPAgentExecutor(_Handler())

    def composed(ctx: RequestContext) -> tuple[str | None, dict[str, Any]]:
        # Seller's custom shape: a Part carrying
        # ``{"operation": ..., "body": ...}``.
        msg = ctx.message
        if msg is not None:
            for part in msg.parts:
                if part.WhichOneof("content") != "data":
                    continue
                data = _MessageToDict(part.data)
                if isinstance(data, dict) and "operation" in data:
                    return str(data["operation"]), {
                        "source": "custom",
                        **(data.get("body") or {}),
                    }
        # Fall through to the default for legacy clients.
        return executor._default_parse_request(ctx)

    executor2 = ADCPAgentExecutor(_Handler(), message_parser=composed)

    # Legacy shape → default parser catches it.
    legacy_msg = Message(
        message_id="m-legacy",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": "get_products", "parameters": {}}))],
    )
    legacy_ctx = RequestContext(request=MessageSendParams(message=legacy_msg))
    queue = EventQueue()
    await executor2.execute(legacy_ctx, queue)
    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
