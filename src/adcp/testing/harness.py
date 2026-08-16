"""SellerTestClient / SellerA2AClient — in-process AdCP harnesses for unit tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from adcp.decisioning import DecisioningPlatform
    from adcp.validation.client_hooks import ValidationHookConfig


@dataclass(frozen=True)
class AdcpErrorPayload:
    """Typed container for a wire ``adcp_error`` dict.

    Maps to the AdCP transport-errors spec §MCP Binding fields.
    """

    code: str
    message: str
    recovery: str | None = None
    field: str | None = None
    suggestion: str | None = None
    retry_after: int | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolInvokeResult:
    """Result of a :meth:`SellerTestClient.invoke` call."""

    data: dict[str, Any] | None
    adcp_error: AdcpErrorPayload | None
    structured_content: dict[str, Any]

    @property
    def ok(self) -> bool:
        """True when the tool returned a success envelope (no adcp_error)."""
        return self.adcp_error is None


class SellerTestClient:
    """In-process MCP test client for AdCP seller implementations.

    Wraps a :class:`~adcp.decisioning.DecisioningPlatform` with
    :meth:`invoke` — a one-call helper that handles tool dispatch and
    ``adcp_error`` extraction from ``structuredContent``, eliminating
    the boilerplate every adopter previously rewrote in each test file.

    Usage::

        @pytest.fixture
        def seller():
            return SellerTestClient(MySeller())

        async def test_buy_not_found(seller):
            result = await seller.invoke(
                "update_media_buy", {"media_buy_id": "missing", ...}
            )
            assert not result.ok
            assert result.adcp_error.code == "MEDIA_BUY_NOT_FOUND"

        async def test_get_products_success(seller):
            result = await seller.invoke("get_products", {})
            assert result.ok
            assert "products" in result.data

    The harness calls :meth:`mcp.call_tool` in-process — it exercises
    the full handler dispatch and error-translation stack without HTTP
    or SSE framing. For HTTP-level tests (auth middleware, CORS, size
    limits), use :func:`adcp.testing.build_test_client` directly.

    For A2A transport, use :class:`SellerA2AClient` — same call
    shape (:meth:`SellerA2AClient.invoke`), different in-process
    dispatch path (executor + event queue rather than MCP tool call).

    This client covers per-handler unit and integration tests. Full
    compliance scenario grading runs through the ``@adcp/sdk``
    storyboard runner (``adcp storyboard run``), whose canonical scenarios
    and grader are the single source of truth. See
    ``docs/testing-your-adcp-server.md`` for both paths.
    """

    def __init__(
        self,
        platform: DecisioningPlatform,
        *,
        name: str | None = None,
        validation: ValidationHookConfig | None = None,
    ) -> None:
        """
        Args:
            platform: The :class:`~adcp.decisioning.DecisioningPlatform`
                instance under test.
            name: Server name forwarded to :func:`~adcp.server.serve.create_mcp_server`.
                Defaults to ``type(platform).__name__``.
            validation: Schema validation config. ``None`` (default) disables
                validation so tests focus on handler behavior, not schema
                conformance. Pass
                :data:`~adcp.validation.client_hooks.SERVER_DEFAULT_VALIDATION`
                to match production behavior.
        """
        self._platform = platform
        self._name = name
        self._validation = validation
        self._mcp: Any | None = None
        self._mcp_lock = asyncio.Lock()

    def _build_mcp_sync(self) -> Any:
        from adcp.decisioning.serve import create_adcp_server_from_platform
        from adcp.server.serve import create_mcp_server

        handler, _executor, _registry = create_adcp_server_from_platform(
            self._platform,
            auto_emit_completion_webhooks=False,
        )
        return create_mcp_server(
            handler,
            name=self._name or type(self._platform).__name__,
            validation=self._validation,
        )

    async def _ensure_mcp(self) -> Any:
        async with self._mcp_lock:
            if self._mcp is None:
                # create_adcp_server_from_platform calls asyncio.run() internally
                # (via validate_capabilities_response_shape) — must run in a thread
                # to avoid "cannot be called from a running event loop".
                self._mcp = await asyncio.to_thread(self._build_mcp_sync)
        return self._mcp

    async def invoke(
        self,
        tool: str,
        payload: dict[str, Any] | None = None,
    ) -> ToolInvokeResult:
        """Invoke a tool and return the result with adcp_error extraction.

        Args:
            tool: AdCP tool name (e.g. ``"update_media_buy"``).
            payload: Arguments forwarded to the tool. ``None`` → empty dict.

        Returns:
            :class:`ToolInvokeResult` — check :attr:`~ToolInvokeResult.ok`
            for success/failure and :attr:`~ToolInvokeResult.adcp_error` for
            error details.
        """
        from mcp.types import CallToolResult

        mcp = await self._ensure_mcp()
        kwargs = payload or {}
        raw_result = await mcp.call_tool(tool, kwargs)

        # Success shape comes from this SDK's `_AdcpFuncMetadata.convert_result`
        # (`src/adcp/server/serve.py`), which yields a 2-tuple of
        # `(list[ContentBlock], dict | None)` — that's a coupling to our internal
        # FuncMetadata override, not the public FastMCP.call_tool contract.
        # Error shape (AdcpError raised by handler) lands as `CallToolResult`
        # with `isError=True` and `structuredContent={"adcp_error": {...}}`.
        # The plain-dict branch is defensive against FastMCP flattening the
        # success return in a future version.
        if isinstance(raw_result, CallToolResult):
            structured: dict[str, Any] = raw_result.structured_content or {}
        elif isinstance(raw_result, (tuple, list)) and len(raw_result) == 2:
            success_dict: Any = raw_result[1]
            structured = dict(success_dict) if success_dict is not None else {}
        elif isinstance(raw_result, dict):
            structured = dict(raw_result)
        else:
            raise RuntimeError(
                f"FastMCP.call_tool returned unexpected shape {type(raw_result)!r}; "
                "harness needs updating for this MCP version"
            )

        raw_error = structured.get("adcp_error")

        adcp_error: AdcpErrorPayload | None = None
        if raw_error is not None:
            code = raw_error.get("code")
            message = raw_error.get("message")
            if not code:
                raise RuntimeError(
                    "adcp_error envelope is missing required 'code' field; "
                    "server is non-conformant to AdCP transport-errors spec"
                )
            if not message:
                raise RuntimeError(
                    "adcp_error envelope is missing required 'message' field; "
                    "server is non-conformant to AdCP transport-errors spec"
                )
            adcp_error = AdcpErrorPayload(
                code=code,
                message=message,
                recovery=raw_error.get("recovery"),
                field=raw_error.get("field"),
                suggestion=raw_error.get("suggestion"),
                retry_after=raw_error.get("retry_after"),
                details=raw_error.get("details"),
            )

        data: dict[str, Any] | None = None
        if raw_error is None and structured:
            data = dict(structured)

        return ToolInvokeResult(data=data, adcp_error=adcp_error, structured_content=structured)


class SellerA2AClient:
    """In-process A2A test client for AdCP seller implementations.

    The A2A sibling to :class:`SellerTestClient`. Same call shape
    (``await client.invoke(skill, payload)``), same return type
    (:class:`ToolInvokeResult`), but routes through the A2A executor
    + event-queue dispatch path instead of MCP's tool call.

    Usage::

        @pytest.fixture
        def seller_a2a():
            return SellerA2AClient(MySeller())

        async def test_buy_not_found_a2a(seller_a2a):
            result = await seller_a2a.invoke(
                "update_media_buy", {"media_buy_id": "missing", ...}
            )
            assert not result.ok
            assert result.adcp_error.code == "MEDIA_BUY_NOT_FOUND"

    The harness constructs an :class:`~adcp.server.a2a_server.ADCPAgentExecutor`,
    builds a minimal A2A :class:`~a2a.server.agent_execution.context.RequestContext`
    carrying a ``DataPart`` with ``{"skill": ..., "parameters": ...}``,
    runs the executor against a fresh event queue, and drains the queue
    until a terminal Task arrives. Success payloads come from the Task's
    first DataPart artifact; structured errors land in that same DataPart
    keyed under ``adcp_error`` per transport-errors.mdx §A2A Binding.

    Limitations (each tracked as a follow-up on #678):

    * **No push-notification capture.** Adopters who need to assert on
      outbound signed webhook delivery need a sink primitive this
      client doesn't yet provide.
    * **No intermediate state observation.** :meth:`invoke` drains to
      the terminal Task and returns. Tasks that pass through
      ``working`` / ``input_required`` are observed only at their final
      state.
    * **No task cancellation harness.** Once :meth:`invoke` is awaited
      there is no handle to cancel a long-running task.
    """

    def __init__(
        self,
        platform: DecisioningPlatform,
        *,
        validation: ValidationHookConfig | None = None,
    ) -> None:
        """
        Args:
            platform: The :class:`~adcp.decisioning.DecisioningPlatform`
                instance under test.
            validation: Schema validation config. ``None`` (default) disables
                validation so tests focus on handler behavior, not schema
                conformance. Pass
                :data:`~adcp.validation.client_hooks.SERVER_DEFAULT_VALIDATION`
                to match production behavior.
        """
        self._platform = platform
        self._validation = validation
        self._executor: Any | None = None
        self._executor_lock = asyncio.Lock()

    def _build_executor_sync(self) -> Any:
        from adcp.decisioning.serve import create_adcp_server_from_platform
        from adcp.server.a2a_server import ADCPAgentExecutor

        handler, _executor, _registry = create_adcp_server_from_platform(
            self._platform,
            auto_emit_completion_webhooks=False,
        )
        return ADCPAgentExecutor(handler, validation=self._validation)

    async def _ensure_executor(self) -> Any:
        async with self._executor_lock:
            if self._executor is None:
                # create_adcp_server_from_platform calls asyncio.run() internally
                # (via validate_capabilities_response_shape) — must run in a thread
                # to avoid "cannot be called from a running event loop".
                self._executor = await asyncio.to_thread(self._build_executor_sync)
        return self._executor

    async def invoke(
        self,
        skill: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 5.0,
        max_events: int = 32,
    ) -> ToolInvokeResult:
        """Invoke a skill and return the terminal-state result.

        Args:
            skill: AdCP skill name (e.g. ``"update_media_buy"``).
            payload: Arguments forwarded to the skill. ``None`` → empty dict.
            timeout_seconds: Per-event dequeue timeout. The harness drains
                the event queue waiting for a terminal Task; this caps how
                long any single dequeue waits. Default 5s.
            max_events: Maximum number of A2A events to drain while waiting
                for a terminal Task. Default 32 preserves the previous hard cap.

        Returns:
            :class:`ToolInvokeResult` — ``ok`` reflects whether the terminal
            Task state was ``COMPLETED``; ``adcp_error`` carries the structured
            error from a failed Task's DataPart per the A2A binding.
        """
        # TODO(#699): Migrate off EventQueueLegacy when a2a-sdk ships a
        # type-clean successor. a2aproject/a2a-python#944 split the class
        # into an abstract EventQueue base + concrete EventQueueLegacy; the
        # base's __new__ redirects EventQueue() to EventQueueLegacy() at
        # runtime, but mypy correctly flags abstract instantiation and
        # a2a-sdk's own internals still use EventQueueLegacy. Tracked
        # upstream at a2aproject/a2a-python#1064.
        from a2a import types as pb
        from a2a.auth.user import UnauthenticatedUser
        from a2a.server.agent_execution.context import (
            RequestContext as A2ARequestContext,
        )
        from a2a.server.context import ServerCallContext
        from a2a.server.events.event_queue import EventQueueLegacy
        from google.protobuf.json_format import MessageToDict, ParseDict
        from google.protobuf.struct_pb2 import Value

        executor = await self._ensure_executor()
        kwargs = payload or {}

        # Build the DataPart-shaped skill invocation that ADCPAgentExecutor's
        # default parser accepts: {"skill": "...", "parameters": {...}}.
        value = Value()
        ParseDict({"skill": skill, "parameters": kwargs}, value)
        msg = pb.Message(
            message_id=str(uuid4()),
            role="ROLE_USER",
            parts=[pb.Part(data=value)],
        )
        call_ctx = ServerCallContext(user=UnauthenticatedUser())
        request_ctx = A2ARequestContext(
            call_context=call_ctx,
            request=pb.SendMessageRequest(message=msg),
        )
        queue = EventQueueLegacy()
        await executor.execute(request_ctx, queue)

        # Drain the event queue until a terminal Task arrives. Bounded so a
        # buggy handler that never publishes a terminal event can't hang the
        # test runner — each dequeue carries `timeout_seconds`, and the loop
        # is bounded to `max_events` intermediate state events.
        terminal_task: Any = None
        for _ in range(max_events):
            try:
                event = await asyncio.wait_for(queue.dequeue_event(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                break
            if isinstance(event, pb.Task) and event.status.state in (
                pb.TaskState.TASK_STATE_COMPLETED,
                pb.TaskState.TASK_STATE_FAILED,
                pb.TaskState.TASK_STATE_CANCELED,
            ):
                terminal_task = event
                break

        if terminal_task is None:
            raise RuntimeError(
                f"A2A executor for skill={skill!r} produced no terminal Task "
                f"within {timeout_seconds}s x {max_events} events — "
                "check executor middleware for hangs"
            )

        # Project the terminal Task's first DataPart artifact to a dict.
        structured: dict[str, Any] = {}
        if terminal_task.artifacts:
            for part in terminal_task.artifacts[0].parts:
                if part.WhichOneof("content") == "data":
                    projected = MessageToDict(part.data)
                    if isinstance(projected, dict):
                        structured = projected
                    break

        raw_error = structured.get("adcp_error")

        adcp_error: AdcpErrorPayload | None = None
        if raw_error is not None:
            code = raw_error.get("code")
            message = raw_error.get("message")
            if not code:
                raise RuntimeError(
                    "adcp_error envelope is missing required 'code' field; "
                    "server is non-conformant to AdCP transport-errors spec"
                )
            if not message:
                raise RuntimeError(
                    "adcp_error envelope is missing required 'message' field; "
                    "server is non-conformant to AdCP transport-errors spec"
                )
            adcp_error = AdcpErrorPayload(
                code=code,
                message=message,
                recovery=raw_error.get("recovery"),
                field=raw_error.get("field"),
                suggestion=raw_error.get("suggestion"),
                retry_after=raw_error.get("retry_after"),
                details=raw_error.get("details"),
            )
        elif terminal_task.status.state == pb.TaskState.TASK_STATE_FAILED:
            # A2A unstructured-error path: failed Task without an `adcp_error`
            # DataPart (unknown skill, no parseable skill, transport-layer
            # rejection). Synthesize an envelope so callers can assert on
            # ``result.adcp_error`` uniformly across structured/unstructured
            # failures.
            status_msg = ""
            if terminal_task.status.HasField("message"):
                for part in terminal_task.status.message.parts:
                    if part.WhichOneof("content") == "text":
                        status_msg = part.text
                        break
            adcp_error = AdcpErrorPayload(
                code="INTERNAL_ERROR",
                message=status_msg or "A2A task failed without structured adcp_error envelope",
            )

        data: dict[str, Any] | None = None
        if raw_error is None and structured:
            data = dict(structured)

        return ToolInvokeResult(data=data, adcp_error=adcp_error, structured_content=structured)


__all__ = [
    "AdcpErrorPayload",
    "SellerA2AClient",
    "SellerTestClient",
    "ToolInvokeResult",
]
