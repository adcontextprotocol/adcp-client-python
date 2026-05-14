"""Tests for A2A structured error projection (issue #530).

Mirrors :mod:`tests.test_mcp_structured_error` for the A2A surface. Verifies
that AdcpError raised from a platform method projects onto the wire as a
failed-task DataPart keyed under ``adcp_error`` with the full spec shape
— matching transport-errors.mdx §A2A Binding and PR #525's MCP fix.

Two parity gaps closed:

1. The executor catches both :class:`adcp.exceptions.ADCPError` (client-
   side) AND :class:`adcp.decisioning.types.AdcpError` (server-side
   structured error). Previously decisioning errors fell into the
   generic ``except Exception`` and rendered as plain "Skill execution
   failed" text.
2. The DataPart envelope carries ``code``, ``message``, ``recovery``,
   ``field``, ``suggestion``, ``retry_after``, ``details`` — populated
   when present, omitted when absent. Previously only ``code`` /
   ``message`` / ``recovery`` / ``suggestion`` were emitted.
"""

from __future__ import annotations

from typing import Any

import pytest
from a2a import types as pb
from a2a.server.agent_execution.context import RequestContext as _RealRequestContext
from a2a.server.events.event_queue import (
    EventQueueLegacy as EventQueue,
)  # TODO(#699): drop alias when a2aproject/a2a-python#1064 lands a type-clean EventQueue successor
from google.protobuf.json_format import MessageToDict as _MessageToDict
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value

from adcp.decisioning.types import AdcpError as DecisioningAdcpError
from adcp.exceptions import ADCPTaskError
from adcp.server import ADCPHandler
from adcp.server.a2a_server import ADCPAgentExecutor as _ADCPAgentExecutor
from adcp.types import Error


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A executor tests run outside the sandbox-authority gate."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


# ---------------------------------------------------------------------------
# Test fixtures (mirrors test_a2a_server.py shims)
# ---------------------------------------------------------------------------


def _data_part(data: dict[str, Any]) -> pb.Part:
    value = Value()
    ParseDict(data, value)
    return pb.Part(data=value)


def _make_datapart_msg(skill: str, parameters: dict[str, Any] | None = None) -> pb.Message:
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
        request=pb.SendMessageRequest(message=_make_datapart_msg(skill, parameters)),
    )


def _executor(handler: ADCPHandler) -> _ADCPAgentExecutor:
    """Build an executor with validation disabled — these tests focus on
    the error-projection contract, not wire-conformance for success
    payloads."""
    return _ADCPAgentExecutor(handler, validation=None)


def _adcp_error_data_part(task: pb.Task) -> dict[str, Any]:
    """Pull the ``adcp_error`` payload out of a failed task's DataPart."""
    assert task.artifacts, "expected at least one artifact on failed task"
    for part in task.artifacts[0].parts:
        if part.WhichOneof("content") != "data":
            continue
        payload = _MessageToDict(part.data)
        if isinstance(payload, dict) and "adcp_error" in payload:
            return payload["adcp_error"]
    raise AssertionError("no adcp_error DataPart found on task artifacts")


# ---------------------------------------------------------------------------
# Handlers that raise specific structured errors
# ---------------------------------------------------------------------------


class _AdcpCapsBase(ADCPHandler):
    """Stub ADCP capabilities — required for ADCPHandler conformance."""

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}


class _DecisioningRaiser(_AdcpCapsBase):
    """Raises a configurable decisioning ``AdcpError`` from get_products."""

    def __init__(self, exc_factory: Any) -> None:
        self._exc_factory = exc_factory

    async def get_products(self, params: Any, context: Any = None) -> Any:
        raise self._exc_factory()


class _TaskErrorRaiser(_AdcpCapsBase):
    """Raises an ADCPTaskError from get_products."""

    def __init__(self, errors: list[Error]) -> None:
        self._errors = errors

    async def get_products(self, params: Any, context: Any = None) -> Any:
        raise ADCPTaskError("get_products", self._errors)


# ============================================================================
# Decisioning AdcpError projection — the main parity gap
# ============================================================================


@pytest.mark.asyncio
async def test_decisioning_adcp_error_caught_not_dropped_to_generic_exception() -> None:
    """Pre-fix this raised through ``except Exception`` and rendered as
    "Skill execution failed: get_products" — losing the structured shape."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    assert isinstance(event, pb.Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED
    payload = _adcp_error_data_part(event)
    assert payload["code"] == "MEDIA_BUY_NOT_FOUND"
    assert payload["message"] == "no such buy"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,recovery",
    [
        ("MEDIA_BUY_NOT_FOUND", "terminal"),
        ("PACKAGE_NOT_FOUND", "terminal"),
        ("TERMS_REJECTED", "terminal"),
        ("BUDGET_TOO_LOW", "correctable"),
    ],
)
async def test_specific_codes_round_trip(code: str, recovery: str) -> None:
    """Each spec code reaches the wire with its code intact — storyboard
    runner's ``/adcp_error/code`` JSON-pointer assertion resolves to the
    actual code, not a generic fallback. Mirrors the MCP-side parametrized
    test in ``test_mcp_structured_error.py``.
    """
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError(code, message="test", recovery=recovery)  # type: ignore[arg-type]
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    assert isinstance(event, pb.Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED
    payload = _adcp_error_data_part(event)
    assert payload["code"] == code
    assert payload["recovery"] == recovery


# ============================================================================
# Optional-field gating: populate when present, omit when absent
# ============================================================================


@pytest.mark.asyncio
async def test_full_envelope_populated_when_all_fields_present() -> None:
    """Spec wire shape: ``{code, message, recovery, field, suggestion,
    retry_after, details}`` populated when the raised error supplies them."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError(
            "BUDGET_TOO_LOW",
            message="below floor",
            recovery="correctable",
            field="total_budget",
            suggestion="Increase to at least $0.50",
            retry_after=30,
            details={"floor_cpm": "0.50", "impressions": 1000},
        )
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_data_part(event)
    assert payload["code"] == "BUDGET_TOO_LOW"
    assert payload["message"] == "below floor"
    assert payload["recovery"] == "correctable"
    assert payload["field"] == "total_budget"
    assert payload["suggestion"] == "Increase to at least $0.50"
    assert payload["retry_after"] == 30
    assert payload["details"] == {"floor_cpm": "0.50", "impressions": 1000}


@pytest.mark.asyncio
async def test_optional_fields_omitted_when_absent() -> None:
    """When the raised error doesn't populate ``field`` / ``suggestion`` /
    ``retry_after`` / ``details``, those keys MUST NOT appear on the wire —
    omitted, not set to None. Buyers checking ``"field" in payload`` rely
    on this gating to distinguish "not provided" from "explicitly null"."""
    handler = _DecisioningRaiser(lambda: DecisioningAdcpError("INTERNAL_ERROR", message="oops"))
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_data_part(event)
    assert "field" not in payload
    assert "suggestion" not in payload
    assert "retry_after" not in payload
    assert "details" not in payload
    # Required fields still present.
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["message"] == "oops"
    assert "recovery" in payload


@pytest.mark.asyncio
async def test_field_populated_without_suggestion() -> None:
    """Independent gating — ``field`` populates without forcing
    ``suggestion`` to appear."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError("INVALID_REQUEST", message="bad budget", field="total_budget")
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_data_part(event)
    assert payload["field"] == "total_budget"
    assert "suggestion" not in payload


@pytest.mark.asyncio
async def test_retry_after_populated_for_transient() -> None:
    """``retry_after`` projects when present (mainly for ``transient``
    recovery codes like ``RATE_LIMITED``)."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError(
            "RATE_LIMITED",
            message="slow down",
            recovery="transient",
            retry_after=60,
        )
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_data_part(event)
    assert payload["retry_after"] == 60
    assert payload["recovery"] == "transient"


# ============================================================================
# Client-side ADCPError (e.g. ADCPTaskError) — already-supported path stays green
# ============================================================================


@pytest.mark.asyncio
async def test_adcp_task_error_still_caught_after_refactor() -> None:
    """Regression: ``ADCPTaskError`` (carrying spec codes via ``error_codes``)
    continues to project as a structured envelope after the dual-catch
    refactor. This was the only path that worked pre-fix."""
    handler = _TaskErrorRaiser([Error(code="IDEMPOTENCY_CONFLICT", message="payload differs")])
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    assert isinstance(event, pb.Task)
    assert event.status.state == pb.TaskState.TASK_STATE_FAILED
    payload = _adcp_error_data_part(event)
    assert payload["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_adcp_task_error_with_field_projects_field() -> None:
    """``ADCPTaskError`` carrying an :class:`Error` with ``field`` populates
    ``field`` on the wire (parity with MCP's ``Error`` model handling)."""
    handler = _TaskErrorRaiser(
        [
            Error(
                code="VALIDATION_ERROR",
                message="missing field",
                field="packages[0].budget",
            )
        ]
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_data_part(event)
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["field"] == "packages[0].budget"


# ============================================================================
# Issue #557: AdCP context-passthrough on the error path
# ============================================================================
#
# The success path emits the request's ``context`` extension back into
# the response. The error path must do the same so buyers retain
# correlation IDs and idempotency hints across the raise-AdcpError
# boundary.


def _adcp_error_full_payload(task: pb.Task) -> dict[str, Any]:
    """Pull the full DataPart payload (sibling fields incl. ``context``)."""
    assert task.artifacts, "expected at least one artifact on failed task"
    for part in task.artifacts[0].parts:
        if part.WhichOneof("content") != "data":
            continue
        payload = _MessageToDict(part.data)
        if isinstance(payload, dict) and "adcp_error" in payload:
            return payload
    raise AssertionError("no adcp_error DataPart found on task artifacts")


@pytest.mark.asyncio
async def test_request_context_echoes_into_error_envelope() -> None:
    """A request with a ``context`` field that triggers an AdcpError raise
    produces a failed-task DataPart with that ``context`` echoed alongside
    ``adcp_error``."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
    )
    executor = _executor(handler)
    queue = EventQueue()

    request_context = {"correlation_id": "buyer-req-42", "trace_id": "abc"}
    await executor.execute(_request_context("get_products", {"context": request_context}), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_full_payload(event)
    assert payload["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"
    assert payload.get("context") == request_context


@pytest.mark.asyncio
async def test_no_request_context_omits_context_from_error_envelope() -> None:
    """When the request carries no ``context`` field, the error DataPart
    MUST NOT synthesise one — only echo what the buyer sent."""
    handler = _DecisioningRaiser(
        lambda: DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
    )
    executor = _executor(handler)
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_full_payload(event)
    assert "context" not in payload


@pytest.mark.asyncio
async def test_echoed_context_is_sibling_of_adcp_error_not_inside() -> None:
    """``context`` lands at the DataPart top level, not inside ``adcp_error``."""
    handler = _DecisioningRaiser(lambda: DecisioningAdcpError("INTERNAL_ERROR", message="oops"))
    executor = _executor(handler)
    queue = EventQueue()

    request_context = {"correlation_id": "abc-123"}
    await executor.execute(_request_context("get_products", {"context": request_context}), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_full_payload(event)
    assert payload.get("context") == request_context
    assert "context" not in payload["adcp_error"]


@pytest.mark.asyncio
async def test_oversized_request_context_dropped_on_error_path() -> None:
    """An oversized ``context`` (>64KB) is silently dropped per the
    inject_context size cap — buyers cannot use the error envelope to
    amplify response size by stuffing the request context."""
    handler = _DecisioningRaiser(lambda: DecisioningAdcpError("INTERNAL_ERROR", message="oops"))
    executor = _executor(handler)
    queue = EventQueue()

    huge_context = {"junk": "A" * (65 * 1024)}
    await executor.execute(_request_context("get_products", {"context": huge_context}), queue)

    event = await queue.dequeue_event()
    payload = _adcp_error_full_payload(event)
    assert "context" not in payload


# ============================================================================
# A2A success path also echoes context (parity with MCP success path)
# ============================================================================


def _success_data_payload(task: pb.Task) -> dict[str, Any]:
    """Pull the DataPart payload from a completed task."""
    assert task.artifacts, "expected at least one artifact on completed task"
    for part in task.artifacts[0].parts:
        if part.WhichOneof("content") != "data":
            continue
        payload = _MessageToDict(part.data)
        if isinstance(payload, dict):
            return payload
    raise AssertionError("no DataPart found on task artifacts")


@pytest.mark.asyncio
async def test_a2a_success_path_echoes_request_context() -> None:
    """A successful A2A skill response echoes the request's ``context``
    extension, matching the MCP success path's ``inject_context`` call.
    Without this the AdCP context-passthrough contract holds on errors
    but not on successes — a strange asymmetry this PR closes."""

    class _OkHandler(_AdcpCapsBase):
        async def get_products(self, _params: Any, _context: Any = None) -> Any:
            return {"products": []}

    executor = _executor(_OkHandler())
    queue = EventQueue()

    request_context = {"correlation_id": "buyer-req-7"}
    await executor.execute(_request_context("get_products", {"context": request_context}), queue)

    event = await queue.dequeue_event()
    assert isinstance(event, pb.Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED
    payload = _success_data_payload(event)
    assert payload.get("context") == request_context
    assert payload.get("products") == []


@pytest.mark.asyncio
async def test_a2a_success_path_no_request_context_omits_echo() -> None:
    """No request-side ``context`` → no synthesized one on the success
    response either."""

    class _OkHandler(_AdcpCapsBase):
        async def get_products(self, _params: Any, _context: Any = None) -> Any:
            return {"products": []}

    executor = _executor(_OkHandler())
    queue = EventQueue()

    await executor.execute(_request_context("get_products"), queue)

    event = await queue.dequeue_event()
    payload = _success_data_payload(event)
    assert "context" not in payload
