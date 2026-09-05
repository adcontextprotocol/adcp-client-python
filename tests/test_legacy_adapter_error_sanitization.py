"""Transport-boundary coverage for legacy adapter error disclosure."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
import pytest
from asgi_lifespan import LifespanManager

from adcp.compat.legacy import (
    AdapterPair,
    LegacyAdapterValidationError,
    _reset_registry_for_tests,
    register_adapter,
)
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.a2a_server import create_a2a_server

_SENTINEL = "https://sentinel.internal/search?token=top-secret-1126"
_CONTEXT = {"correlation_id": "issue-1126"}


class _Issue1126Handler(ADCPHandler[Any]):
    advertised_tools = {"get_adcp_capabilities", "sync_creatives"}

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def sync_creatives(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"creatives": []}


def _register_failing_adapter(phase: Literal["request", "response"]) -> None:
    def adapt_request(payload: dict[str, Any]) -> dict[str, Any]:
        if phase == "request":
            raise RuntimeError(_SENTINEL)
        return dict(payload)

    def normalize_response(response: dict[str, Any]) -> dict[str, Any]:
        if phase == "response":
            raise RuntimeError(_SENTINEL)
        return dict(response)

    _reset_registry_for_tests()
    register_adapter(
        "2.5",
        AdapterPair(
            tool_name="sync_creatives",
            adapt_request=adapt_request,
            normalize_response=normalize_response,
        ),
    )


async def _dispatch_mcp(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    server = create_mcp_server(
        _Issue1126Handler(),
        name="legacy-error-test",
        stateless_http=True,
        validation=None,
    )
    app = server.streamable_http_app()
    request = {
        "jsonrpc": "2.0",
        "id": "req-1126",
        "method": "tools/call",
        "params": {"name": "sync_creatives", "arguments": params},
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as client:
            response = await client.post(
                "/mcp",
                json=request,
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == 200, response.text
    rpc = _parse_json_or_event_stream(response.text)
    structured = rpc["result"]["structuredContent"]
    return structured, response.text


async def _dispatch_a2a(params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    app = create_a2a_server(
        _Issue1126Handler(),
        name="legacy-error-test",
        port=8000,
        validation=None,
    )
    request = {
        "jsonrpc": "2.0",
        "id": "req-1126",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "msg-1126",
                "role": "user",
                "parts": [
                    {
                        "kind": "data",
                        "data": {"skill": "sync_creatives", "parameters": params},
                    }
                ],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as client:
            response = await client.post("/", json=request)

    assert response.status_code == 200, response.text
    rpc = response.json()
    for artifact in rpc["result"]["artifacts"]:
        for part in artifact["parts"]:
            data = part.get("data")
            if isinstance(data, dict) and "adcp_error" in data:
                return data, response.text
    raise AssertionError("no adcp_error data part")


def _parse_json_or_event_stream(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    return json.loads(body)


@pytest.fixture(autouse=True)
def _sandbox_and_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADCP_SANDBOX", "1")
    yield
    _reset_registry_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "a2a"])
@pytest.mark.parametrize(
    ("phase", "expected_code", "expected_recovery", "public_message"),
    [
        (
            "request",
            "INVALID_REQUEST",
            "correctable",
            "The legacy AdCP request could not be translated.",
        ),
        (
            "response",
            "INTERNAL_ERROR",
            "terminal",
            "The response could not be translated to the legacy AdCP format.",
        ),
    ],
)
async def test_unexpected_adapter_exception_is_private_on_every_transport(
    transport: Literal["mcp", "a2a"],
    phase: Literal["request", "response"],
    expected_code: str,
    expected_recovery: str,
    public_message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _register_failing_adapter(phase)
    params = {"adcp_version": "2.5", "creatives": [], "context": _CONTEXT}

    with caplog.at_level(logging.ERROR, logger="adcp.compat.legacy.errors"):
        payload, serialized_wire = (
            await _dispatch_mcp(params) if transport == "mcp" else await _dispatch_a2a(params)
        )

    error = payload["adcp_error"]
    assert error["code"] == expected_code
    assert error["recovery"] == expected_recovery
    assert public_message in error["message"]
    assert payload["context"] == _CONTEXT
    assert _SENTINEL not in serialized_wire

    logged_exception = next(record for record in caplog.records if record.exc_info is not None)
    assert isinstance(logged_exception.exc_info[1], RuntimeError)
    assert str(logged_exception.exc_info[1]) == _SENTINEL
    assert logged_exception.exc_info[2] is not None


@pytest.mark.asyncio
async def test_explicit_buyer_safe_request_error_retains_actionable_message() -> None:
    actionable = "format_id must name a format returned by list_creative_formats"

    def reject(_payload: dict[str, Any]) -> dict[str, Any]:
        raise LegacyAdapterValidationError(actionable)

    _reset_registry_for_tests()
    register_adapter(
        "2.5",
        AdapterPair(tool_name="sync_creatives", adapt_request=reject),
    )

    payload, _serialized_wire = await _dispatch_mcp(
        {"adcp_version": "2.5", "creatives": [], "context": _CONTEXT}
    )

    assert payload["adcp_error"]["code"] == "INVALID_REQUEST"
    assert payload["adcp_error"]["recovery"] == "correctable"
    assert actionable in payload["adcp_error"]["message"]
    assert payload["context"] == _CONTEXT
