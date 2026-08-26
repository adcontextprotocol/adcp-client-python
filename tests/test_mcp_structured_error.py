"""Tests for MCP structured error projection (issue #509).

Verifies that AdcpError raised from a platform method projects onto the
wire as a ``CallToolResult`` with ``isError=True`` AND
``structuredContent.adcp_error`` populated — matching transport-errors.mdx
§MCP Binding and the storyboard runner's ``/adcp_error/code`` JSON-pointer
assertion.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from adcp.decisioning.types import AdcpError as DecisioningAdcpError
from adcp.exceptions import ADCPError, ADCPTaskError
from adcp.server.translate import build_mcp_error_result
from adcp.types import Error

# ============================================================================
# Unit tests: build_mcp_error_result shape
# ============================================================================


class TestBuildMcpErrorResultShape:
    """The structured envelope shape matches the spec."""

    def test_returns_call_tool_result_with_is_error_true(self):
        exc = DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
        result = build_mcp_error_result(exc)
        assert isinstance(result, CallToolResult)
        assert result.is_error is True

    def test_structured_content_keyed_under_adcp_error(self):
        exc = DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
        result = build_mcp_error_result(exc)
        assert result.structured_content is not None
        assert "adcp_error" in result.structured_content

    def test_structured_content_carries_code(self):
        exc = DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"

    def test_structured_content_carries_message(self):
        exc = DecisioningAdcpError("PACKAGE_NOT_FOUND", message="package gone")
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["message"] == "package gone"

    def test_structured_content_carries_recovery(self):
        exc = DecisioningAdcpError("BUDGET_TOO_LOW", message="under floor", recovery="correctable")
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["recovery"] == "correctable"

    def test_text_fallback_present_in_content(self):
        exc = DecisioningAdcpError(
            "MEDIA_BUY_NOT_FOUND", message="no such buy", field="media_buy_id"
        )
        result = build_mcp_error_result(exc)
        assert len(result.content) == 1
        assert isinstance(result.content[0], TextContent)
        assert "MEDIA_BUY_NOT_FOUND" in result.content[0].text
        assert "no such buy" in result.content[0].text


class TestBuildMcpErrorResultOptionalFields:
    """Optional fields populate when present, omit when absent."""

    def test_field_populated_when_present(self):
        exc = DecisioningAdcpError("INVALID_REQUEST", message="bad budget", field="total_budget")
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["field"] == "total_budget"

    def test_field_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "field" not in result.structured_content["adcp_error"]

    def test_suggestion_populated_when_present(self):
        exc = DecisioningAdcpError(
            "BUDGET_TOO_LOW",
            message="too low",
            suggestion="Increase to at least $0.50",
        )
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["suggestion"] == "Increase to at least $0.50"

    def test_suggestion_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "suggestion" not in result.structured_content["adcp_error"]

    def test_details_populated_when_present(self):
        details: dict[str, Any] = {"validation_errors": [{"path": "x", "msg": "bad"}]}
        exc = DecisioningAdcpError("VALIDATION_ERROR", message="bad fields", details=details)
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["details"] == details

    def test_details_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "details" not in result.structured_content["adcp_error"]

    def test_retry_after_populated_when_present(self):
        exc = DecisioningAdcpError(
            "RATE_LIMITED",
            message="slow down",
            recovery="transient",
            retry_after=30,
        )
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["retry_after"] == 30


class TestBuildMcpErrorResultExceptionTypes:
    """Handles all three input shapes: ADCPError, decisioning AdcpError, Error model."""

    def test_handles_adcp_task_error(self):
        err = Error(code="TERMS_REJECTED", message="terms unacceptable")
        exc = ADCPTaskError("create_media_buy", [err])
        result = build_mcp_error_result(exc)
        assert result.is_error is True
        assert result.structured_content["adcp_error"]["code"] == "TERMS_REJECTED"
        # ADCPTaskError prefixes the operation; the original Error.message
        # text is preserved in the projected message.
        assert "terms unacceptable" in result.structured_content["adcp_error"]["message"]

    def test_handles_error_model(self):
        err = Error(
            code="VALIDATION_ERROR",
            message="missing field",
            field="packages[0].budget",
        )
        result = build_mcp_error_result(err)
        assert result.structured_content["adcp_error"]["code"] == "VALIDATION_ERROR"
        assert result.structured_content["adcp_error"]["field"] == "packages[0].budget"

    def test_handles_plain_adcp_error_falls_back_to_internal(self):
        exc = ADCPError("unexpected")
        result = build_mcp_error_result(exc)
        assert result.structured_content["adcp_error"]["code"] == "INTERNAL_ERROR"

    def test_rejects_unknown_type(self):
        with pytest.raises(TypeError):
            build_mcp_error_result(ValueError("not an adcp error"))


# ============================================================================
# Integration: through the FastMCP tool registration path
# ============================================================================


@pytest.mark.asyncio
async def test_adcp_error_from_handler_projects_to_structured_content():
    """End-to-end: AdcpError raised from a handler reaches the wire as
    ``CallToolResult(isError=True, structuredContent={"adcp_error": {...}})``.

    Exercises the full FastMCP path: tool dispatch, fn_metadata.convert_result,
    lowlevel handler short-circuit on CallToolResult instances.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(
            "MEDIA_BUY_NOT_FOUND",
            message="No media buy with id mb-404",
            recovery="terminal",
            field="media_buy_id",
            details={"media_buy_id": "mb-404"},
        )

    mcp = MCPServer("test-structured-error")
    _register_tool(
        mcp,
        "get_media_buy_delivery",
        "test description",
        {"type": "object", "properties": {"media_buy_id": {"type": "string"}}},
        caller,
    )

    result = await mcp.call_tool("get_media_buy_delivery", {"media_buy_id": "mb-404"})

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"
    assert result.structured_content["adcp_error"]["message"] == "No media buy with id mb-404"
    assert result.structured_content["adcp_error"]["recovery"] == "terminal"
    assert result.structured_content["adcp_error"]["field"] == "media_buy_id"
    assert result.structured_content["adcp_error"]["details"] == {"media_buy_id": "mb-404"}
    # Text fallback preserved.
    assert any(
        "MEDIA_BUY_NOT_FOUND" in c.text for c in result.content if isinstance(c, TextContent)
    )


@pytest.mark.asyncio
async def test_transient_decisioning_import_during_registration_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.server import MCPServer

    from adcp.server import translate
    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError("INVALID_REQUEST", message="retry import")

    translate._load_decisioning_adcp_error_types.cache_clear()
    real_import = builtins.__import__

    def transient_import_error(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "adcp.decisioning.types":
            raise ImportError("simulated circular import")
        return real_import(name, *args, **kwargs)

    mcp = MCPServer("test-transient-decisioning-import")
    with monkeypatch.context() as import_patch:
        import_patch.setattr(builtins, "__import__", transient_import_error)
        _register_tool(mcp, "test_tool", "test", {"type": "object"}, caller)

    result = await mcp.call_tool("test_tool", {})
    assert isinstance(result, CallToolResult)
    assert result.structured_content["adcp_error"]["code"] == "INVALID_REQUEST"


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
async def test_specific_codes_round_trip(code: str, recovery: str):
    """Each spec code reaches the wire with its code intact —
    storyboard runner's ``/adcp_error/code`` JSON-pointer assertion
    resolves to the actual code, not ``mcp_error``.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(code, message="test", recovery=recovery)

    mcp = MCPServer("test-codes")
    _register_tool(
        mcp,
        "test_tool",
        "test",
        {"type": "object"},
        caller,
    )

    result = await mcp.call_tool("test_tool", {})
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content["adcp_error"]["code"] == code
    assert result.structured_content["adcp_error"]["recovery"] == recovery


@pytest.mark.asyncio
async def test_adcp_task_error_round_trips_through_register_tool():
    """ADCPError subclasses (ADCPTaskError, IdempotencyConflictError, etc.)
    also reach the wire as structured envelopes, not ToolError text.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        err = Error(code="IDEMPOTENCY_CONFLICT", message="payload differs")
        raise ADCPTaskError("create_media_buy", [err])

    mcp = MCPServer("test-task-error")
    _register_tool(
        mcp,
        "create_media_buy",
        "test",
        {"type": "object"},
        caller,
    )

    result = await mcp.call_tool("create_media_buy", {})
    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content["adcp_error"]["code"] == "IDEMPOTENCY_CONFLICT"


class TestBuildMcpErrorResultContextEcho:
    """Issue #557: AdCP context-passthrough contract on the error path.

    The success path runs ``inject_context(raw_params, response)`` so a
    request's ``context`` extension echoes back to the buyer. The error
    path must do the same — without it, buyers lose correlation IDs and
    idempotency hints across the raise-AdcpError boundary.
    """

    def test_no_params_omits_context_from_envelope(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "context" not in result.structured_content

    def test_params_without_context_omits_context_from_envelope(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc, params={"media_buy_id": "mb-1"})
        assert "context" not in result.structured_content

    def test_params_with_context_echoes_into_envelope(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        ctx = {"correlation_id": "abc-123", "buyer_trace": "trace-xyz"}
        result = build_mcp_error_result(exc, params={"media_buy_id": "mb-1", "context": ctx})
        assert result.structured_content.get("context") == ctx

    def test_echoed_context_is_sibling_of_adcp_error_not_inside_it(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        ctx = {"correlation_id": "abc-123"}
        result = build_mcp_error_result(exc, params={"context": ctx})
        assert "context" in result.structured_content
        assert "context" not in result.structured_content["adcp_error"]

    def test_oversized_context_silently_dropped(self):
        """``inject_context``'s 64KB cap applies on the error path too —
        prevents response-size amplification via buyer-controlled context."""
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        huge = {"junk": "A" * (65 * 1024)}
        result = build_mcp_error_result(exc, params={"context": huge})
        assert "context" not in result.structured_content


@pytest.mark.asyncio
async def test_context_echo_round_trips_through_register_tool():
    """End-to-end: a request with a ``context`` field that triggers an
    AdcpError raise produces a wire response with that same ``context``
    echoed alongside ``adcp_error`` in structuredContent.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(
            "MEDIA_BUY_NOT_FOUND",
            message="No media buy with id mb-404",
            recovery="terminal",
        )

    mcp = MCPServer("test-context-echo")
    _register_tool(
        mcp,
        "get_media_buy_delivery",
        "test description",
        {"type": "object"},
        caller,
    )

    request_context = {"correlation_id": "buyer-req-42"}
    result = await mcp.call_tool(
        "get_media_buy_delivery",
        {"media_buy_id": "mb-404", "context": request_context},
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"
    assert result.structured_content.get("context") == request_context


@pytest.mark.asyncio
async def test_dispatcher_wrap_to_internal_error_preserves_context_echo():
    """Pin the chain: a non-AdcpError raised from a DecisioningPlatform
    method is wrapped to ``AdcpError("INTERNAL_ERROR")`` by
    ``_invoke_platform_method``, then projected through ``serve.py``'s
    decisioning branch via ``build_mcp_error_result``, with the
    request's ``context`` field echoed onto the wire envelope.

    The test asserts both halves:
    1. The wrap actually ran — ``details.caused_by`` carries the
       original :class:`ValueError` class name (set by
       ``_internal_error_details``).
    2. The request context survived the wrap and lands as a sibling
       of ``adcp_error`` in ``structuredContent``.

    Without (1) the test would pass even if the wrap step were
    skipped — we'd be re-asserting #560's coverage of an explicit
    AdcpError raise. The ``caused_by`` check pins the wrap path
    specifically.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        from concurrent.futures import ThreadPoolExecutor

        from pydantic import BaseModel

        from adcp.decisioning.dispatch import _build_request_context, _invoke_platform_method
        from adcp.decisioning.task_registry import InMemoryTaskRegistry
        from adcp.decisioning.types import Account
        from adcp.server.base import ToolContext

        class _CrashingPlatform:
            async def get_products(self, params, ctx):
                raise ValueError("oops, internal-state bug")

        class _Req(BaseModel):
            pass

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            ctx_obj = _build_request_context(
                ToolContext(),
                Account(id="acct-1"),
                None,
            )
            return await _invoke_platform_method(
                _CrashingPlatform(),
                "get_products",
                _Req(),
                ctx_obj,
                executor=executor,
                registry=InMemoryTaskRegistry(),
            )
        finally:
            executor.shutdown(wait=True)

    mcp = MCPServer("test-562-dispatch-wrap")
    _register_tool(mcp, "get_products", "test", {"type": "object"}, caller)

    request_context = {"correlation_id": "buyer-req-562"}
    result = await mcp.call_tool("get_products", {"context": request_context})

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    # (1) The wrap ran — INTERNAL_ERROR with caused_by = ValueError.
    assert result.structured_content["adcp_error"]["code"] == "INTERNAL_ERROR"
    assert result.structured_content["adcp_error"]["details"]["caused_by"]["type"] == "ValueError"
    # (2) Context echoed end-to-end.
    assert result.structured_content.get("context") == request_context


@pytest.mark.asyncio
async def test_success_path_unchanged():
    """Regression: success-path responses still validate against the
    output schema. The structuredContent error bypass MUST NOT leak
    into the success path.
    """
    from mcp.server import MCPServer

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        return {"status": "ok", "value": 42}

    mcp = MCPServer("test-success")
    _register_tool(
        mcp,
        "ok_tool",
        "test",
        {"type": "object"},
        caller,
    )

    result = await mcp.call_tool("ok_tool", {})
    # FastMCP returns (unstructured, structured) tuple for dict returns
    # without a CallToolResult wrap; lowlevel handler then builds
    # CallToolResult(isError=False, ...).
    # mcp.call_tool here returns the convert_result output, which is a
    # tuple of (content_list, structured_dict).
    if isinstance(result, tuple):
        _content, structured = result
        assert structured == {"status": "ok", "value": 42}
    else:
        # Single-channel return: still must contain the data.
        assert result == {"status": "ok", "value": 42} or (
            hasattr(result, "structured_content")
            and result.structured_content == {"status": "ok", "value": 42}
        )
