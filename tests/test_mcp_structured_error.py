"""Tests for MCP structured error projection (issue #509).

Verifies that AdcpError raised from a platform method projects onto the
wire as a ``CallToolResult`` with ``isError=True`` AND
``structuredContent.adcp_error`` populated — matching transport-errors.mdx
§MCP Binding and the storyboard runner's ``/adcp_error/code`` JSON-pointer
assertion.
"""

from __future__ import annotations

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
        assert result.isError is True

    def test_structured_content_keyed_under_adcp_error(self):
        exc = DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
        result = build_mcp_error_result(exc)
        assert result.structuredContent is not None
        assert "adcp_error" in result.structuredContent

    def test_structured_content_carries_code(self):
        exc = DecisioningAdcpError("MEDIA_BUY_NOT_FOUND", message="no such buy")
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"

    def test_structured_content_carries_message(self):
        exc = DecisioningAdcpError("PACKAGE_NOT_FOUND", message="package gone")
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["message"] == "package gone"

    def test_structured_content_carries_recovery(self):
        exc = DecisioningAdcpError("BUDGET_TOO_LOW", message="under floor", recovery="correctable")
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["recovery"] == "correctable"

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
        assert result.structuredContent["adcp_error"]["field"] == "total_budget"

    def test_field_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "field" not in result.structuredContent["adcp_error"]

    def test_suggestion_populated_when_present(self):
        exc = DecisioningAdcpError(
            "BUDGET_TOO_LOW",
            message="too low",
            suggestion="Increase to at least $0.50",
        )
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["suggestion"] == "Increase to at least $0.50"

    def test_suggestion_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "suggestion" not in result.structuredContent["adcp_error"]

    def test_details_populated_when_present(self):
        details: dict[str, Any] = {"validation_errors": [{"path": "x", "msg": "bad"}]}
        exc = DecisioningAdcpError("VALIDATION_ERROR", message="bad fields", details=details)
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["details"] == details

    def test_details_omitted_when_absent(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc)
        assert "details" not in result.structuredContent["adcp_error"]

    def test_retry_after_populated_when_present(self):
        exc = DecisioningAdcpError(
            "RATE_LIMITED",
            message="slow down",
            recovery="transient",
            retry_after=30,
        )
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["retry_after"] == 30


class TestBuildMcpErrorResultExceptionTypes:
    """Handles all three input shapes: ADCPError, decisioning AdcpError, Error model."""

    def test_handles_adcp_task_error(self):
        err = Error(code="TERMS_REJECTED", message="terms unacceptable")
        exc = ADCPTaskError("create_media_buy", [err])
        result = build_mcp_error_result(exc)
        assert result.isError is True
        assert result.structuredContent["adcp_error"]["code"] == "TERMS_REJECTED"
        # ADCPTaskError prefixes the operation; the original Error.message
        # text is preserved in the projected message.
        assert "terms unacceptable" in result.structuredContent["adcp_error"]["message"]

    def test_handles_error_model(self):
        err = Error(
            code="VALIDATION_ERROR",
            message="missing field",
            field="packages[0].budget",
        )
        result = build_mcp_error_result(err)
        assert result.structuredContent["adcp_error"]["code"] == "VALIDATION_ERROR"
        assert result.structuredContent["adcp_error"]["field"] == "packages[0].budget"

    def test_handles_plain_adcp_error_falls_back_to_internal(self):
        exc = ADCPError("unexpected")
        result = build_mcp_error_result(exc)
        assert result.structuredContent["adcp_error"]["code"] == "INTERNAL_ERROR"

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
    from mcp.server.fastmcp import FastMCP

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(
            "MEDIA_BUY_NOT_FOUND",
            message="No media buy with id mb-404",
            recovery="terminal",
            field="media_buy_id",
            details={"media_buy_id": "mb-404"},
        )

    mcp = FastMCP("test-structured-error")
    _register_tool(
        mcp,
        "get_media_buy_delivery",
        "test description",
        {"type": "object", "properties": {"media_buy_id": {"type": "string"}}},
        caller,
    )

    result = await mcp.call_tool("get_media_buy_delivery", {"media_buy_id": "mb-404"})

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"
    assert result.structuredContent["adcp_error"]["message"] == "No media buy with id mb-404"
    assert result.structuredContent["adcp_error"]["recovery"] == "terminal"
    assert result.structuredContent["adcp_error"]["field"] == "media_buy_id"
    assert result.structuredContent["adcp_error"]["details"] == {"media_buy_id": "mb-404"}
    # Text fallback preserved.
    assert any(
        "MEDIA_BUY_NOT_FOUND" in c.text for c in result.content if isinstance(c, TextContent)
    )


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
    from mcp.server.fastmcp import FastMCP

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(code, message="test", recovery=recovery)

    mcp = FastMCP("test-codes")
    _register_tool(
        mcp,
        "test_tool",
        "test",
        {"type": "object"},
        caller,
    )

    result = await mcp.call_tool("test_tool", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["adcp_error"]["code"] == code
    assert result.structuredContent["adcp_error"]["recovery"] == recovery


@pytest.mark.asyncio
async def test_adcp_task_error_round_trips_through_register_tool():
    """ADCPError subclasses (ADCPTaskError, IdempotencyConflictError, etc.)
    also reach the wire as structured envelopes, not ToolError text.
    """
    from mcp.server.fastmcp import FastMCP

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        err = Error(code="IDEMPOTENCY_CONFLICT", message="payload differs")
        raise ADCPTaskError("create_media_buy", [err])

    mcp = FastMCP("test-task-error")
    _register_tool(
        mcp,
        "create_media_buy",
        "test",
        {"type": "object"},
        caller,
    )

    result = await mcp.call_tool("create_media_buy", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.structuredContent["adcp_error"]["code"] == "IDEMPOTENCY_CONFLICT"


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
        assert "context" not in result.structuredContent

    def test_params_without_context_omits_context_from_envelope(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        result = build_mcp_error_result(exc, {"media_buy_id": "mb-1"})
        assert "context" not in result.structuredContent

    def test_params_with_context_echoes_into_envelope(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        ctx = {"correlation_id": "abc-123", "buyer_trace": "trace-xyz"}
        result = build_mcp_error_result(exc, {"media_buy_id": "mb-1", "context": ctx})
        assert result.structuredContent.get("context") == ctx

    def test_echoed_context_is_sibling_of_adcp_error_not_inside_it(self):
        exc = DecisioningAdcpError("INTERNAL_ERROR", message="oops")
        ctx = {"correlation_id": "abc-123"}
        result = build_mcp_error_result(exc, {"context": ctx})
        assert "context" in result.structuredContent
        assert "context" not in result.structuredContent["adcp_error"]


@pytest.mark.asyncio
async def test_context_echo_round_trips_through_register_tool():
    """End-to-end: a request with a ``context`` field that triggers an
    AdcpError raise produces a wire response with that same ``context``
    echoed alongside ``adcp_error`` in structuredContent.
    """
    from mcp.server.fastmcp import FastMCP

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        raise DecisioningAdcpError(
            "MEDIA_BUY_NOT_FOUND",
            message="No media buy with id mb-404",
            recovery="terminal",
        )

    mcp = FastMCP("test-context-echo")
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
    assert result.isError is True
    assert result.structuredContent["adcp_error"]["code"] == "MEDIA_BUY_NOT_FOUND"
    assert result.structuredContent.get("context") == request_context


@pytest.mark.asyncio
async def test_success_path_unchanged():
    """Regression: success-path responses still validate against the
    output schema. The structuredContent error bypass MUST NOT leak
    into the success path.
    """
    from mcp.server.fastmcp import FastMCP

    from adcp.server.serve import _register_tool

    async def caller(_kwargs: dict[str, Any], *, context: Any = None) -> Any:
        return {"status": "ok", "value": 42}

    mcp = FastMCP("test-success")
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
            hasattr(result, "structuredContent")
            and result.structuredContent == {"status": "ok", "value": 42}
        )
