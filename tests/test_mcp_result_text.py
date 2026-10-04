"""MCP summaries preserve canonical payloads and prebuilt result envelopes."""

import importlib
import json
from copy import deepcopy

import pytest
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from adcp.decisioning.types import AdcpError
from adcp.server import ADCPHandler, ServeConfig, ToolContext, create_mcp_server
from adcp.server.serve import _register_tool
from adcp.validation.client_hooks import ValidationHookConfig
from adcp.validation.schema_validator import ValidationIssue, ValidationOutcome


class Seller(ADCPHandler):
    advertised_tools = {"get_products"}

    async def get_products(self, params, context=None):
        return {"products": [{"product_id": "p1", "name": "Video"}], "message": "Found one product"}


ARGS = {"buying_mode": "brief", "brief": "video", "context": {"trace": "request-1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("formatter", [None, "missing", "products", lambda name, result, ctx: None])
async def test_default_and_none_fallback_are_json(formatter):
    mcp = create_mcp_server(Seller(), validation=None, mcp_result_text=formatter)
    result = await mcp.call_tool("get_products", ARGS)
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_summary_runs_after_context_echo_validation_and_enhancement(
    monkeypatch, asynchronous
):
    events = []
    context = ToolContext(caller_identity="buyer")

    def enhance(method, result, ctx):
        assert result["context"] == ARGS["context"]
        result["extra"] = {"validated": True}
        events.append("enhance")

    def validate(method, result, **kwargs):
        assert result["extra"] == {"validated": True}
        events.append("validate")
        return ValidationOutcome(valid=True)

    monkeypatch.setattr("adcp.validation.schema_validator.validate_response", validate)
    seen = []

    def summary(name, result, ctx):
        assert events == ["enhance", "validate"]
        assert name == "get_products"
        assert ctx is context
        seen.append(deepcopy(result))
        result["products"][0]["name"] = "formatter mutation"
        result["context"]["trace"] = "formatter mutation"
        result["extra"]["validated"] = False
        return "One product ready"

    async def async_summary(name, result, ctx):
        return summary(name, result, ctx)

    mcp = create_mcp_server(
        Seller(),
        validation=ValidationHookConfig(requests="off", responses="strict"),
        response_enhancer=enhance,
        context_factory=lambda meta: context,
        mcp_result_text=async_summary if asynchronous else summary,
    )
    result = await mcp.call_tool("get_products", ARGS)
    assert result.content == [TextContent(type="text", text="One product ready")]
    assert result.structured_content == seen[0]
    assert result.structured_content["products"][0]["name"] == "Video"
    assert ARGS["context"]["trace"] == "request-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "formatter", ["message", lambda name, result, ctx: "", lambda name, result, ctx: "Summary"]
)
async def test_summaries_preserve_baseline_data(formatter):
    baseline = await create_mcp_server(Seller(), validation=None).call_tool("get_products", ARGS)
    result = await create_mcp_server(
        Seller(), validation=None, mcp_result_text=formatter
    ).call_tool("get_products", ARGS)
    assert result.structured_content == baseline.structured_content
    expected = (
        "Found one product" if formatter == "message" else formatter("get_products", {}, None)
    )
    assert result.content == [TextContent(type="text", text=expected)]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("empty_content", [False, True])
async def test_prebuilt_results_are_not_double_wrapped(is_error, empty_content):
    result = CallToolResult(
        is_error=is_error,
        content=[] if empty_content else [TextContent(type="text", text="prebuilt")],
        structured_content={"canonical": [1, 2, 3]},
    )

    async def caller(kwargs, *, context=None):
        return result

    def forbidden(*args):
        pytest.fail("prebuilt result must not invoke the formatter")

    mcp = MCPServer("prebuilt")
    _register_tool(mcp, "example", "example", {"type": "object"}, caller, mcp_result_text=forbidden)
    actual = await mcp.call_tool("example", {})
    assert actual is result
    assert actual.structured_content == {"canonical": [1, 2, 3]}


@pytest.mark.asyncio
async def test_strict_validation_error_uses_error_conversion_without_summary(monkeypatch):
    def invalid(*args, **kwargs):
        return ValidationOutcome(
            valid=False, issues=[ValidationIssue("/products", "invalid shape", "type", "")]
        )

    monkeypatch.setattr("adcp.validation.schema_validator.validate_response", invalid)
    mcp = create_mcp_server(
        Seller(),
        validation=ValidationHookConfig(requests="off", responses="strict"),
        mcp_result_text=lambda *args: pytest.fail("invalid response must not invoke the formatter"),
    )
    result = await mcp.call_tool("get_products", ARGS)
    assert result.is_error
    assert result.structured_content["adcp_error"]["code"] == "VALIDATION_ERROR"
    assert result.structured_content["context"] == ARGS["context"]


@pytest.mark.asyncio
async def test_handler_errors_keep_error_text_and_structure():
    class Failing(Seller):
        async def get_products(self, params, context=None):
            raise AdcpError("INVALID_REQUEST", message="Fix the brief")

    result = await create_mcp_server(
        Failing(),
        validation=None,
        mcp_result_text=lambda *args: pytest.fail("error must not invoke the formatter"),
    ).call_tool("get_products", ARGS)
    assert result.is_error
    assert result.structured_content["adcp_error"]["code"] == "INVALID_REQUEST"
    assert "Fix the brief" in result.content[0].text


@pytest.mark.asyncio
async def test_wrong_callback_return_type_fails_clearly():
    mcp = create_mcp_server(Seller(), validation=None, mcp_result_text=lambda *args: 123)
    with pytest.raises(Exception, match="mcp_result_text must return str or None"):
        await mcp._tool_manager._tools["get_products"].fn(**ARGS)


@pytest.mark.parametrize("transport", ["streamable-http", "both"])
def test_serve_config_forwards_hook(monkeypatch, transport):
    module = importlib.import_module("adcp.server.serve")
    captured = []
    target = "_serve_mcp" if transport == "streamable-http" else "_serve_mcp_and_a2a"
    monkeypatch.setattr(module, target, lambda *args, **kwargs: captured.append(kwargs))
    module.serve(Seller(), config=ServeConfig(transport=transport, mcp_result_text="message"))
    assert captured[0]["mcp_result_text"] == "message"


@pytest.mark.asyncio
async def test_hook_changes_only_mcp_payload_in_combined_app():
    import httpx
    from asgi_lifespan import LifespanManager

    from adcp.server.serve import _build_mcp_and_a2a_app

    calls = []
    a2a_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": "get_products", "parameters": ARGS}}],
            },
        },
    }
    a2a_data = []
    for hook in [None, lambda name, result, ctx: calls.append(name) or "Summary"]:
        app = _build_mcp_and_a2a_app(
            Seller(),
            name="result",
            port=3001,
            host="127.0.0.1",
            instructions=None,
            test_controller=None,
            validation=None,
            stateless_http=True,
            allowed_hosts=["localhost"],
            mcp_result_text=hook,
        )
        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                response = await client.post("/", json=a2a_body)
                assert response.status_code == 200, response.text
                a2a_data.append(response.json()["result"]["artifacts"][0]["parts"][0]["data"])
                assert calls == []
                mcp_response = await client.post(
                    "/mcp",
                    headers={"accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "get_products",
                            "arguments": ARGS,
                        },
                    },
                )
                assert mcp_response.status_code == 200
                payload = mcp_response.json()["result"]
                if hook:
                    assert payload["content"][0]["text"] == "Summary"
                else:
                    assert json.loads(payload["content"][0]["text"]) == payload["structuredContent"]
    assert a2a_data[0] == a2a_data[1]
    assert calls == ["get_products"]


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", ["Readable", None])
async def test_formatter_receives_baseline_json_values(summary):
    from datetime import datetime, timezone
    from decimal import Decimal

    from pydantic import BaseModel, Field

    class Nested(BaseModel):
        value: str = Field(serialization_alias="wire_value")

    class RichSeller(Seller):
        async def get_products(self, params, context=None):
            return {
                "products": [],
                "timestamp": datetime(2026, 10, 2, tzinfo=timezone.utc),
                "price": Decimal("1.25"),
                "pair": (1, 2),
                "nested": Nested(value="ok"),
            }

    baseline = await create_mcp_server(RichSeller(), validation=None).call_tool(
        "get_products", ARGS
    )
    seen = []

    def formatter(name, result, ctx):
        seen.append(result)
        assert result == baseline.structured_content
        assert result["price"] == "1.25"
        assert result["pair"] == [1, 2]
        assert isinstance(result["nested"], dict)
        return summary

    result = await create_mcp_server(
        RichSeller(), validation=None, mcp_result_text=formatter
    ).call_tool("get_products", ARGS)
    assert seen == [baseline.structured_content]
    assert result.structured_content == baseline.structured_content
    assert result.content == (
        baseline.content if summary is None else [TextContent(type="text", text=summary)]
    )
