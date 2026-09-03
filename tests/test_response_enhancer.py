"""Server-wide ``response_enhancer`` callback (issue #926, JS #2161 parity).

The enhancer stamps cross-cutting fields on every response class — framework
tool successes, custom-tool successes (``get_task_status`` / ``list_tasks``),
the pre-auth ``get_adcp_capabilities`` discovery response, and structured
``adcp_error`` responses — uniformly across the MCP and A2A transports.

These tests exercise the real wire dicts the dispatcher produces (not the
helper in isolation, except for the pure arity/throw unit tests) so a
serialization or seam regression is caught:

- success path via ``create_tool_caller`` (the MCP + A2A shared seam),
- the MCP error envelope via ``build_mcp_error_result``,
- the A2A success + error envelopes via ``ADCPAgentExecutor.execute``,
- the A2A ``comply_test_controller`` bypass closure.
"""

from __future__ import annotations

from typing import Any

import pytest
from a2a import types as pb
from a2a.server.agent_execution.context import RequestContext as _RealRequestContext
from a2a.server.events.event_queue import (
    EventQueueLegacy as EventQueue,
)
from google.protobuf.json_format import MessageToDict as _MessageToDict
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from adcp.decisioning.types import AdcpError as DecisioningAdcpError
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler, ToolContext
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.helpers import _apply_response_enhancer, _enhancer_is_context_aware
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import products_response
from adcp.server.test_controller import TestControllerStore
from adcp.server.translate import build_mcp_error_result
from adcp.types import Error
from adcp.validation import ValidationHookConfig


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A executor tests run outside the sandbox-authority gate."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


# ---------------------------------------------------------------------------
# Handlers covering the four response classes
# ---------------------------------------------------------------------------


class _Handler(ADCPHandler):
    """Implements every tool the four response-class tests need."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        # Real, spec-conformant wire shape so before-validate tests can
        # rely on validation passing for the un-enhanced response.
        return products_response([])

    async def get_task_status(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"task_id": "task_1", "status": "completed"}

    async def list_tasks(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"tasks": []}


class _ErrorHandler(_Handler):
    """get_products raises a credential-policy-style structured error."""

    async def get_products(self, params: Any, context: Any = None) -> Any:
        raise DecisioningAdcpError(
            "PERMISSION_DENIED",
            message="credential policy denied this request",
            recovery="terminal",
        )


# ---------------------------------------------------------------------------
# A2A wire helpers (mirror the shims in test_a2a_server.py)
# ---------------------------------------------------------------------------


def _data_part(data: dict[str, Any]) -> pb.Part:
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def _datapart_msg(skill: str, parameters: dict[str, Any] | None = None) -> pb.Message:
    return pb.Message(
        message_id="msg-1",
        role=pb.Role.ROLE_USER,
        parts=[_data_part({"skill": skill, "parameters": parameters or {}})],
    )


def _empty_call_context() -> Any:
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext

    return ServerCallContext(user=UnauthenticatedUser())


def _request_context(skill: str, parameters: dict[str, Any] | None = None) -> _RealRequestContext:
    return _RealRequestContext(
        call_context=_empty_call_context(),
        request=pb.SendMessageRequest(message=_datapart_msg(skill, parameters)),
    )


def _first_data_part(task: pb.Task) -> dict[str, Any]:
    assert task.artifacts, "expected at least one artifact"
    for part in task.artifacts[0].parts:
        if part.WhichOneof("content") == "data":
            return _MessageToDict(part.data)
    raise AssertionError("task has no DataPart")


def _stamp(result: dict[str, Any]) -> None:
    """Context-blind enhancer that stamps a sentinel field."""
    result["enhanced"] = True


# ===========================================================================
# Pure unit: arity dispatch + throw + None
# ===========================================================================


class TestEnhancerHelper:
    def test_one_arg_is_context_blind(self) -> None:
        assert _enhancer_is_context_aware(lambda d: None) is False

    def test_three_arg_is_context_aware(self) -> None:
        assert _enhancer_is_context_aware(lambda name, d, ctx: None) is True

    def test_var_positional_is_context_aware(self) -> None:
        assert _enhancer_is_context_aware(lambda *args: None) is True

    def test_context_blind_invoked_with_dict(self) -> None:
        result: dict[str, Any] = {}
        _apply_response_enhancer(lambda d: d.__setitem__("blind", 1), "m", result, None)
        assert result == {"blind": 1}

    def test_context_aware_receives_method_and_context(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["name"] = name
            seen["ctx"] = ctx

        ctx = ToolContext(caller_identity="buyer")
        _apply_response_enhancer(enhancer, "get_products", {}, ctx)
        assert seen == {"name": "get_products", "ctx": ctx}

    def test_throwing_enhancer_logs_warning_and_returns_unenhanced(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(d: dict[str, Any]) -> None:
            raise RuntimeError("kaboom")

        original = {"orig": 1}
        with caplog.at_level("WARNING", logger="adcp.server"):
            out = _apply_response_enhancer(boom, "get_products", original, None)
        assert out is original
        assert original == {"orig": 1}
        assert any(
            "response_enhancer raised for get_products" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_none_enhancer_returns_same_reference_unchanged(self) -> None:
        original = {"a": 1}
        assert _apply_response_enhancer(None, "m", original, None) is original
        assert original == {"a": 1}

    def test_return_value_is_ignored(self) -> None:
        # Enhancer that returns a brand-new dict — the framework must keep
        # the mutated original, not the returned value.
        def replacing(d: dict[str, Any]) -> dict[str, Any]:
            d["mutated"] = True
            return {"this": "is ignored"}

        original: dict[str, Any] = {}
        out = _apply_response_enhancer(replacing, "m", original, None)
        assert out is original
        assert original == {"mutated": True}


# ===========================================================================
# Success path — create_tool_caller (the shared MCP + A2A seam)
# ===========================================================================


class TestSuccessPathCaller:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,params,assertion_key",
        [
            ("get_products", {"brief": "x", "promoted_offering": "y"}, "products"),
            ("get_task_status", {"task_id": "task_1"}, "task_id"),
            ("list_tasks", {}, "tasks"),
            ("get_adcp_capabilities", {}, "adcp"),
        ],
    )
    async def test_enhancer_mutation_reaches_each_response_class(
        self, method: str, params: dict[str, Any], assertion_key: str
    ) -> None:
        caller = create_tool_caller(_Handler(), method, response_enhancer=_stamp)
        result = await caller(params)
        assert result["enhanced"] is True
        assert assertion_key in result  # original payload preserved

    @pytest.mark.asyncio
    async def test_capabilities_runs_with_unauthenticated_none_context(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["ctx_is_none"] = ctx is None
            d["enhanced"] = True

        # Pre-auth discovery: no context passed (bare ToolContext synthesised
        # by the caller). The enhancer must tolerate it.
        caller = create_tool_caller(_Handler(), "get_adcp_capabilities", response_enhancer=enhancer)
        result = await caller({})
        assert result["enhanced"] is True
        # The caller synthesises a bare ToolContext when none is passed —
        # the context-aware enhancer receives that object, never crashes.
        assert seen["ctx_is_none"] is False

    @pytest.mark.asyncio
    async def test_context_aware_arity_sees_real_caller_identity(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["name"] = name
            seen["identity"] = ctx.caller_identity if ctx else None
            d["enhanced"] = True

        caller = create_tool_caller(_Handler(), "get_products", response_enhancer=enhancer)
        result = await caller(
            {"brief": "x", "promoted_offering": "y"},
            ToolContext(caller_identity="buyer-acme"),
        )
        assert result["enhanced"] is True
        assert seen == {"name": "get_products", "identity": "buyer-acme"}

    @pytest.mark.asyncio
    async def test_no_enhancer_leaves_response_unchanged(self) -> None:
        caller = create_tool_caller(_Handler(), "get_products")
        result = await caller({"brief": "x", "promoted_offering": "y"})
        assert "enhanced" not in result
        assert "products" in result

    @pytest.mark.asyncio
    async def test_before_validate_conformance_break_surfaces_validation_error(self) -> None:
        """An enhancer injecting a non-conformant field is conformance-checked
        — proving the enhancer runs BEFORE response validation. The bad
        mutation surfaces as VALIDATION_ERROR rather than shipping malformed."""

        def break_conformance(d: dict[str, Any]) -> None:
            # ``products`` must be a list per the get-products schema.
            d["products"] = "not-a-list"

        caller = create_tool_caller(
            _Handler(),
            "get_products",
            validation=ValidationHookConfig(responses="strict"),
            response_enhancer=break_conformance,
        )
        with pytest.raises(ADCPTaskError) as info:
            await caller({"brief": "x", "promoted_offering": "y"})
        first = info.value.errors[0]
        assert first.code == "VALIDATION_ERROR"
        assert first.details["side"] == "response"

    @pytest.mark.asyncio
    async def test_throwing_enhancer_is_not_a_transport_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A buggy enhancer must NOT become a transport error — the
        un-enhanced response ships and a WARNING is logged."""

        def boom(d: dict[str, Any]) -> None:
            raise RuntimeError("enhancer bug")

        baseline = await create_tool_caller(_Handler(), "get_products")(
            {"brief": "x", "promoted_offering": "y"}
        )
        caller = create_tool_caller(_Handler(), "get_products", response_enhancer=boom)
        with caplog.at_level("WARNING", logger="adcp.server"):
            result = await caller({"brief": "x", "promoted_offering": "y"})
        assert result == baseline  # identical to the un-enhanced response
        assert any("response_enhancer raised for get_products" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_enhancer_runs_after_context_echo(self) -> None:
        """Credential-echo contract: the enhancer runs AFTER ``inject_context``,
        so it observes the already-assembled (credential-stripped) echo
        envelope and cannot precede the strip."""
        seen: dict[str, Any] = {}

        def enhancer(d: dict[str, Any]) -> None:
            # The wire ``context`` echo is already present when the enhancer
            # runs — proving ordering: inject_context -> enhancer.
            seen["context_present_at_enhance_time"] = "context" in d

        caller = create_tool_caller(_Handler(), "get_products", response_enhancer=enhancer)
        await caller({"brief": "x", "promoted_offering": "y", "context": {"correlation_id": "abc"}})
        assert seen["context_present_at_enhance_time"] is True

    @pytest.mark.asyncio
    async def test_error_envelope_from_handler_skips_success_enhancer(self) -> None:
        """When the handler returns an ``{"adcp_error": ...}`` envelope, the
        success-path enhancer is skipped — the L3 error envelope is enhanced
        on the dedicated error path instead, so there is no double pass."""

        class _L3(_Handler):
            async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
                return {"adcp_error": {"code": "PERMISSION_DENIED", "message": "no"}}

        caller = create_tool_caller(_L3(), "get_products", response_enhancer=_stamp)
        result = await caller({"brief": "x", "promoted_offering": "y"})
        assert "enhanced" not in result
        assert "adcp_error" in result


# ===========================================================================
# MCP error path — build_mcp_error_result
# ===========================================================================


class TestMcpErrorPath:
    def test_enhancer_stamps_mcp_error_envelope(self) -> None:
        exc = DecisioningAdcpError(
            "PERMISSION_DENIED", message="credential policy denied", recovery="terminal"
        )
        result = build_mcp_error_result(
            exc,
            params={"context": {"correlation_id": "abc"}},
            method_name="get_products",
            response_enhancer=_stamp,
        )
        assert result.structured_content is not None
        assert result.structured_content["adcp_error"]["code"] == "PERMISSION_DENIED"
        assert result.structured_content["enhanced"] is True

    def test_enhancer_runs_after_context_echo_on_error(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["context_present"] = "context" in d
            seen["name"] = name
            d["enhanced"] = True

        exc = Error(code="PERMISSION_DENIED", message="no")
        build_mcp_error_result(
            exc,
            params={"context": {"correlation_id": "abc"}},
            method_name="get_products",
            response_enhancer=enhancer,
        )
        assert seen == {"context_present": True, "name": "get_products"}

    def test_throwing_enhancer_does_not_break_error_envelope(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(d: dict[str, Any]) -> None:
            raise RuntimeError("enhancer bug")

        exc = Error(code="PERMISSION_DENIED", message="no")
        with caplog.at_level("WARNING", logger="adcp.server"):
            result = build_mcp_error_result(exc, method_name="get_products", response_enhancer=boom)
        assert result.structured_content is not None
        assert result.structured_content["adcp_error"]["code"] == "PERMISSION_DENIED"
        assert "enhanced" not in result.structured_content

    def test_no_enhancer_leaves_error_envelope_unchanged(self) -> None:
        exc = Error(code="PERMISSION_DENIED", message="no")
        result = build_mcp_error_result(exc, method_name="get_products")
        assert result.structured_content is not None
        assert "enhanced" not in result.structured_content


# ===========================================================================
# A2A end-to-end — success, capabilities, error, comply bypass
# ===========================================================================


class TestA2aTransport:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "skill,assertion_key",
        [
            ("get_products", "products"),
            ("get_task_status", "task_id"),
            ("list_tasks", "tasks"),
            ("get_adcp_capabilities", "adcp"),
        ],
    )
    async def test_success_enhanced_on_a2a(self, skill: str, assertion_key: str) -> None:
        executor = ADCPAgentExecutor(_Handler(), validation=None, response_enhancer=_stamp)
        queue = EventQueue()
        params = {"task_id": "task_1"} if skill == "get_task_status" else {}
        await executor.execute(_request_context(skill, params), queue)
        event = await queue.dequeue_event()
        assert isinstance(event, pb.Task)
        assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
        data = _first_data_part(event)
        assert data["enhanced"] is True
        assert assertion_key in data

    @pytest.mark.asyncio
    async def test_capabilities_enhanced_with_unauthenticated_context_on_a2a(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["name"] = name
            d["enhanced"] = True

        executor = ADCPAgentExecutor(_Handler(), validation=None, response_enhancer=enhancer)
        queue = EventQueue()
        await executor.execute(_request_context("get_adcp_capabilities"), queue)
        event = await queue.dequeue_event()
        data = _first_data_part(event)
        assert data["enhanced"] is True
        assert seen["name"] == "get_adcp_capabilities"

    @pytest.mark.asyncio
    async def test_error_envelope_enhanced_on_a2a(self) -> None:
        executor = ADCPAgentExecutor(_ErrorHandler(), validation=None, response_enhancer=_stamp)
        queue = EventQueue()
        await executor.execute(_request_context("get_products"), queue)
        event = await queue.dequeue_event()
        assert isinstance(event, pb.Task)
        assert event.status.state == pb.TaskState.TASK_STATE_FAILED
        data = _first_data_part(event)
        assert data["adcp_error"]["code"] == "PERMISSION_DENIED"
        assert data["enhanced"] is True

    @pytest.mark.asyncio
    async def test_error_enhancer_runs_after_context_echo_on_a2a(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["context_present"] = "context" in d
            d["enhanced"] = True

        executor = ADCPAgentExecutor(_ErrorHandler(), validation=None, response_enhancer=enhancer)
        queue = EventQueue()
        await executor.execute(
            _request_context("get_products", {"context": {"correlation_id": "abc"}}),
            queue,
        )
        await queue.dequeue_event()
        assert seen["context_present"] is True

    @pytest.mark.asyncio
    async def test_no_enhancer_leaves_a2a_success_unchanged(self) -> None:
        executor = ADCPAgentExecutor(_Handler(), validation=None)
        queue = EventQueue()
        await executor.execute(_request_context("get_products"), queue)
        event = await queue.dequeue_event()
        data = _first_data_part(event)
        assert "enhanced" not in data
        assert "products" in data

    @pytest.mark.asyncio
    async def test_comply_test_controller_bypass_is_enhanced(self) -> None:
        """The A2A ``comply_test_controller`` skill bypasses
        ``create_tool_caller`` (its own dispatch closure). It must still run
        through the enhancer — otherwise comply responses silently skip the
        seller's cross-cutting stamp."""

        class _Store(TestControllerStore):
            pass

        executor = ADCPAgentExecutor(
            _Handler(),
            test_controller=_Store(),
            validation=None,
            response_enhancer=_stamp,
        )
        # ``list_scenarios`` is the gate-exempt capability probe — exercises
        # the bypass closure without needing a resolved sandbox account.
        result = await executor._tool_callers["comply_test_controller"](
            {"scenario": "list_scenarios", "account": {"sandbox": True}}
        )
        assert result["enhanced"] is True
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_comply_bypass_enhancer_runs_after_context_echo(self) -> None:
        seen: dict[str, Any] = {}

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            seen["name"] = name
            seen["context_present"] = "context" in d
            d["enhanced"] = True

        class _Store(TestControllerStore):
            pass

        executor = ADCPAgentExecutor(
            _Handler(),
            test_controller=_Store(),
            validation=None,
            response_enhancer=enhancer,
        )
        result = await executor._tool_callers["comply_test_controller"](
            {
                "scenario": "list_scenarios",
                "account": {"sandbox": True},
                "context": {"correlation_id": "abc"},
            }
        )
        assert result["enhanced"] is True
        assert seen == {"name": "comply_test_controller", "context_present": True}


# ===========================================================================
# Config threading — response_enhancer reaches both transports
# ===========================================================================


class TestConfigThreading:
    def test_serve_config_carries_response_enhancer(self) -> None:
        from adcp.server import ServeConfig

        cfg = ServeConfig(response_enhancer=_stamp)
        assert cfg.response_enhancer is _stamp

    def test_create_a2a_server_threads_enhancer_to_executor(self) -> None:
        """``create_a2a_server(response_enhancer=...)`` constructs the executor
        that runs every A2A dispatch with the enhancer — the same constructor
        path ``serve(transport="a2a")`` uses, so the A2A leg is not silently
        dropped. Asserting at the constructor keeps the test off a2a-sdk
        request-handler internals."""
        import inspect

        from adcp.server.a2a_server import create_a2a_server

        # Smoke: the public factory accepts and forwards the kwarg.
        sig = inspect.signature(create_a2a_server)
        assert "response_enhancer" in sig.parameters
        # The executor — the object that actually runs the enhancer — stores
        # it. ``create_a2a_server`` builds exactly this executor.
        executor = ADCPAgentExecutor(_Handler(), validation=None, response_enhancer=_stamp)
        assert executor._response_enhancer is _stamp
        # And the factory call itself does not raise with the kwarg set.
        create_a2a_server(_Handler(), advertise_all=True, response_enhancer=_stamp)

    @pytest.mark.asyncio
    async def test_create_mcp_server_threads_enhancer_to_wire_dispatch(self) -> None:
        """End-to-end MCP through ``create_mcp_server(response_enhancer=...)`` —
        the enhancer threads create_mcp_server -> _register_handler_tools ->
        create_tool_caller, and its stamp reaches the dispatched wire envelope
        the FastMCP tool function returns. This is the same registration path
        ``serve(transport="streamable-http")`` uses, so the MCP leg is proven
        plumbed without re-running the streamable-http session handshake."""
        from adcp.server import create_mcp_server

        def enhancer(name: str, d: dict[str, Any], ctx: ToolContext | None) -> None:
            d["x_enhanced_tool"] = name

        mcp = create_mcp_server(
            _Handler(), advertise_all=True, validation=None, response_enhancer=enhancer
        )
        tool_fn = mcp._tool_manager._tools["get_products"].fn
        result = await tool_fn(brief="x", promoted_offering="y")
        # FastMCP success path returns the plain dict the caller produced.
        assert result["x_enhanced_tool"] == "get_products"
        assert "products" in result

    @pytest.mark.asyncio
    async def test_create_mcp_server_threads_enhancer_to_error_wire(self) -> None:
        """The same ``create_mcp_server`` threading reaches the MCP *error*
        envelope (``build_mcp_error_result``) — a credential-policy error
        dispatched through the registered FastMCP tool carries the stamp."""
        from mcp.types import CallToolResult

        from adcp.server import create_mcp_server

        mcp = create_mcp_server(
            _ErrorHandler(), advertise_all=True, validation=None, response_enhancer=_stamp
        )
        tool_fn = mcp._tool_manager._tools["get_products"].fn
        result = await tool_fn(brief="x", promoted_offering="y")
        assert isinstance(result, CallToolResult)
        assert result.structured_content is not None
        assert result.structured_content["adcp_error"]["code"] == "PERMISSION_DENIED"
        assert result.structured_content["enhanced"] is True
