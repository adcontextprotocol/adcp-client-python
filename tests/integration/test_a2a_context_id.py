"""End-to-end HTTP integration tests for A2A contextId / taskId handling.

Spins up a real A2A Starlette app on localhost via uvicorn and drives
it with the SDK's ``ADCPClient``. These tests prove the wire-level
behavior — they are the counterpart to the mocked adapter tests in
``tests/test_protocols.py`` and the only thing that would catch
regressions in how the client actually serializes ``context_id`` /
``task_id`` onto the JSON-RPC ``message/send`` request.

Also guards the server-side session-scoping claim: a handler keyed on
``context_id`` would start seeing fresh buckets on every call if the
client stopped echoing the id — these tests would fire first.

Note on what the observer can see: the a2a-sdk's ``RequestContext``
auto-populates ``context_id`` / ``task_id`` server-side — it mints
one when the client sent nothing, so a server-side observer cannot
tell "client sent None" from "client sent X" by reading
``RequestContext.context_id`` alone. What it *can* prove is the
higher-value contract — that two turns on one ``ADCPClient`` land on
the *same* server-observed id, and that ``reset_context()`` produces
a *different* one. That's the buyer-visible semantic; the None-on-
first-wire detail is covered by the unit tests in
``tests/test_protocols.py``.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from a2a import types as pb
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value
from starlette.applications import Starlette

from adcp import ADCPClient
from adcp.server import ADCPHandler
from adcp.server.a2a_server import create_a2a_server
from adcp.types import AgentConfig, Protocol

# Starlette/uvicorn A2A integration requires Python 3.11+.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a2a-sdk starlette integration requires Python 3.11+",
)


class _EchoHandler(ADCPHandler):
    """Minimal handler for the happy-path tests — the assertions are at
    the protocol layer, not the handler. Returns spec-compliant empty
    payloads so the client's strict response validator passes."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": []}

    async def create_media_buy(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {
            "media_buy_id": "mb-1",
            "confirmed_at": "2026-05-01T00:00:00Z",
            "revision": 1,
            "packages": [],
        }


def _part_data_dict(part: pb.Part) -> dict[str, Any] | None:
    """Return the dict payload of a Part if it carries a ``data`` oneof."""
    if part.WhichOneof("content") != "data":
        return None
    value = MessageToDict(part.data)
    return value if isinstance(value, dict) else None


class _Observer:
    """Captures the (context_id, task_id) the server saw on each
    incoming A2A message.

    Installed via the ``message_parser`` hook — the parser is invoked
    on every ``message/send`` with the full RequestContext. See the
    module docstring for what this observation point can and cannot
    prove.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, str | None]] = []

    def parser(self, context: RequestContext) -> tuple[str | None, dict[str, Any]]:
        self.calls.append({"context_id": context.context_id, "task_id": context.task_id})
        # Reimplement the default skill-DataPart parse inline so we
        # don't reach into executor internals.
        msg = context.message
        if msg is None:
            return None, {}
        for part in msg.parts:
            data = _part_data_dict(part)
            if data is None:
                continue
            skill = data.get("skill")
            params = data.get("parameters") or {}
            if skill and isinstance(params, dict):
                return str(skill), params
        return None, {}


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def _running_server(handler: ADCPHandler, observer: _Observer) -> AsyncIterator[str]:
    """Start an in-process uvicorn serving the A2A app, yield its base URL."""
    port = _pick_free_port()
    app = create_a2a_server(
        handler,
        name="integration-test-agent",
        port=port,
        message_parser=observer.parser,
        # Stub handler that returns synthetic responses; the test
        # exercises A2A context-id wire plumbing, not spec-shape
        # request payloads. Opt out of the strict server default.
        validation=None,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):  # ~10s ceiling
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn failed to start within timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_two_calls_share_server_assigned_context_id():
    """Core contract: two sequential calls on one ADCPClient land on
    the server under the same context_id. If the client stopped
    echoing it, turn 2 would get a different server-minted id — the
    assertion that calls[0] == calls[1] catches that regression."""
    observer = _Observer()
    async with _running_server(_EchoHandler(), observer) as base_url:
        config = AgentConfig(
            id="ctx-test-agent",
            agent_uri=base_url,
            protocol=Protocol.A2A,
            auth_token="test",
        )
        async with ADCPClient(config) as client:
            assert client.context_id is None
            r1 = await client.adapter.get_products({"brief": "x"})
            assert r1.success, r1.error
            # After turn 1 the client has captured the server's id.
            assert client.context_id is not None
            captured_after_turn_1 = client.context_id

            r2 = await client.adapter.create_media_buy({"budget": 1000})
            assert r2.success, r2.error

    assert len(observer.calls) == 2
    # The server saw the same context_id on both turns — this proves
    # session continuity end-to-end. The server is authoritative, so
    # this value is what any handler keyed on context_id would scope to.
    assert observer.calls[0]["context_id"] == observer.calls[1]["context_id"]
    # And that server-observed id matches what the client captured.
    assert observer.calls[1]["context_id"] == captured_after_turn_1


@pytest.mark.asyncio
async def test_reset_context_produces_new_server_side_session():
    """After ``reset_context()``, the next call must land on a
    different server-side context than the one before. If reset were a
    no-op the two server-observed ids would match and this would fire."""
    observer = _Observer()
    async with _running_server(_EchoHandler(), observer) as base_url:
        config = AgentConfig(
            id="ctx-test-agent",
            agent_uri=base_url,
            protocol=Protocol.A2A,
            auth_token="test",
        )
        async with ADCPClient(config) as client:
            await client.adapter.get_products({"brief": "x"})
            client.reset_context()
            assert client.context_id is None
            await client.adapter.get_products({"brief": "y"})

    # Two distinct server-side sessions.
    assert observer.calls[0]["context_id"] != observer.calls[1]["context_id"]


@pytest.mark.asyncio
async def test_seeded_context_id_reaches_server_on_first_call():
    """Constructor seeding (``ADCPClient(context_id=...)``) — the
    resume-across-restart use case. The server sees the seeded id on
    turn 1 with no round-trip, so a buyer rehydrating from persisted
    state lands on the same server-side session."""
    seed = f"buyer-seeded-{uuid4()}"
    observer = _Observer()
    async with _running_server(_EchoHandler(), observer) as base_url:
        config = AgentConfig(
            id="ctx-test-agent",
            agent_uri=base_url,
            protocol=Protocol.A2A,
            auth_token="test",
        )
        async with ADCPClient(config, context_id=seed) as client:
            assert client.context_id == seed
            r1 = await client.adapter.get_products({"brief": "x"})
            assert r1.success, r1.error

    assert observer.calls[0]["context_id"] == seed


# ---------------------------------------------------------------------------
# HITL / input-required resume — requires a custom AgentExecutor that emits
# a non-terminal task on the first call. ADCPAgentExecutor always emits
# terminal (completed/failed), so we drop down to the raw a2a-sdk here.
# ---------------------------------------------------------------------------


class _HitlExecutor(AgentExecutor):
    """Emits an ``input-required`` task on the first call, then a
    ``completed`` task on the second. Records what came in on the wire.
    """

    def __init__(self) -> None:
        self.observations: list[dict[str, str | None]] = []
        self._served = 0

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.observations.append(
            {
                "context_id": context.context_id,
                "task_id": context.task_id,
                # In 1.0 a Message carries task_id/context_id as string
                # fields on the proto; empty string means "not set" (we
                # convert to None for test ergonomics).
                "message_task_id": (context.message.task_id or None) if context.message else None,
                "message_context_id": (
                    (context.message.context_id or None) if context.message else None
                ),
            }
        )
        self._served += 1
        if self._served == 1:
            state = pb.TaskState.TASK_STATE_INPUT_REQUIRED
            text = "manager approval needed"
        else:
            state = pb.TaskState.TASK_STATE_COMPLETED
            text = "approved"

        # The completion turn must carry a spec-compliant ``create_media_buy``
        # response so the client's strict schema validator passes — this
        # test is exercising task_id echo semantics, not the response
        # payload shape. Turn 1's ``input-required`` state has no data
        # requirement (it's an interim state). The ``approved`` flag is
        # a test-only marker and rides as an additional property, which
        # the schema permits (``additionalProperties: true``).
        if state == pb.TaskState.TASK_STATE_COMPLETED:
            data: dict[str, Any] = {
                "media_buy_id": "mb-1",
                "confirmed_at": "2026-05-01T00:00:00Z",
                "revision": 1,
                "packages": [],
                "approved": True,
            }
        else:
            data = {"approved": False}
        data_value = Value()
        ParseDict(data, data_value)
        task = pb.Task(
            id=context.task_id or str(uuid4()),
            context_id=context.context_id or str(uuid4()),
            status=pb.TaskStatus(state=state),
            artifacts=[
                pb.Artifact(
                    artifact_id=str(uuid4()),
                    parts=[
                        pb.Part(text=text),
                        pb.Part(data=data_value),
                    ],
                )
            ],
        )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = pb.Task(
            id=context.task_id or str(uuid4()),
            context_id=context.context_id or str(uuid4()),
            status=pb.TaskStatus(state=pb.TaskState.TASK_STATE_CANCELED),
        )
        await event_queue.enqueue_event(task)


def _make_hitl_app(executor: _HitlExecutor, port: int) -> Any:
    """Build a raw A2A Starlette app around the custom executor.

    The agent card's ``supported_interfaces`` must include the serving
    port — the client routes JSON-RPC POSTs to the advertised interface
    URL, not to the base_url it passed to the resolver.
    """
    url = f"http://127.0.0.1:{port}/"
    card = pb.AgentCard(
        name="hitl-test-agent",
        description="non-terminal-state test",
        version="1.0.0",
        supported_interfaces=[
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="0.3"),
            pb.AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=pb.AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            pb.AgentSkill(
                id="create_media_buy",
                name="create_media_buy",
                description="create_media_buy",
                tags=["adcp"],
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    routes = list(create_agent_card_routes(agent_card=card)) + list(
        create_jsonrpc_routes(
            request_handler=handler,
            rpc_url="/",
            enable_v0_3_compat=True,
        )
    )
    return Starlette(routes=routes)


@asynccontextmanager
async def _running_raw_server(
    executor: _HitlExecutor,
) -> AsyncIterator[str]:
    port = _pick_free_port()
    app = _make_hitl_app(executor, port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn failed to start within timeout")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_task_id_echoed_on_resume_after_input_required():
    """HITL flow: server returns ``input-required`` on turn 1 → client
    auto-retains task_id → turn 2 carries both context_id and task_id
    on the Message. This test focuses on the client-side contract: the
    echoed ids travel on the wire so a server that supports task resume
    can reattach.

    The a2a-sdk 1.0 ``ActiveTaskManager`` declines to replace an
    in-flight task's status event when the executor re-emits with a
    terminal state on the same task_id (it logs "Task already exists.
    Ignoring task replacement."). That behavior is a server-side policy
    decision; the load-bearing buyer contract being asserted here is
    that turn 2 carries the right ids on the wire, not that the server
    advances state on a second send. The adapter's state-commit
    semantics on terminal responses are covered by the unit tests in
    ``tests/test_protocols.py``.
    """
    executor = _HitlExecutor()
    async with _running_raw_server(executor) as base_url:
        config = AgentConfig(
            id="hitl-agent",
            agent_uri=base_url,
            protocol=Protocol.A2A,
            auth_token="test",
        )
        async with ADCPClient(config) as client:
            r1 = await client.adapter.create_media_buy({"budget": 1000})
            # After an input-required response the adapter stashed both ids.
            assert client.context_id is not None
            assert client.active_task_id is not None
            retained_task_id = client.active_task_id
            retained_context_id = client.context_id

            await client.adapter.create_media_buy({"approval": "yes"})

    # The executor recorded both calls; turn 2 must carry the task_id
    # and context_id from turn 1 so a resume-supporting server can
    # reattach to the in-flight task.
    assert len(executor.observations) == 2
    assert executor.observations[1]["message_task_id"] == retained_task_id
    assert executor.observations[1]["message_context_id"] == retained_context_id
    _ = r1


@pytest.mark.asyncio
async def test_resume_across_simulated_restart_lands_on_same_session():
    """Persistence-across-restart story: client A establishes a
    session, persists its context_id, dies. Client B spins up, seeds
    with the persisted id, and its first call must carry that id so
    the server can reattach it to the original session."""
    observer = _Observer()
    async with _running_server(_EchoHandler(), observer) as base_url:
        config = AgentConfig(
            id="ctx-test-agent",
            agent_uri=base_url,
            protocol=Protocol.A2A,
            auth_token="test",
        )
        # Client A — establishes the session and "persists" the id.
        async with ADCPClient(config) as client_a:
            await client_a.adapter.get_products({"brief": "x"})
            persisted_context_id = client_a.context_id
            assert persisted_context_id is not None

        # Client B — different instance, seeds from persisted state.
        async with ADCPClient(config, context_id=persisted_context_id) as client_b:
            await client_b.adapter.create_media_buy({"budget": 1000})

    # Both server-observed calls share the same context_id.
    assert observer.calls[0]["context_id"] == observer.calls[1]["context_id"]
    assert observer.calls[1]["context_id"] == persisted_context_id
