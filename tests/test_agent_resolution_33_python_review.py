"""Malformed discovery documents retain the public resolver error contract."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

import httpx
import pytest

from adcp.protocols.a2a import A2AAdapter
from adcp.signing.agent_resolver import AgentResolverError, async_resolve_agent

_real_send_and_aggregate = A2AAdapter._send_and_aggregate

AGENT = "https://seller.example.com/mcp"
RECORD = "https://seller.example.com/brand.json"


def discovery_factory(
    result: dict[str, Any],
    record: dict[str, Any] | None = None,
    *,
    duplicate_envelope: bool = False,
    sse: bool = False,
) -> Callable[[str], httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if str(request.url).endswith("/.well-known/agent-card.json"):
                return httpx.Response(
                    200,
                    json={
                        "name": "resolver-test",
                        "description": "Test agent",
                        "version": "1",
                        "url": AGENT,
                        "capabilities": {},
                        "skills": [],
                        "defaultInputModes": ["application/json"],
                        "defaultOutputModes": ["application/json"],
                    },
                )
            if str(request.url) == AGENT:
                return httpx.Response(405)
            assert str(request.url) == RECORD
            assert record is not None
            return httpx.Response(200, json=record)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "protocolVersion": payload["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "resolver-test", "version": "1"},
                    },
                },
            )
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        if duplicate_envelope:
            body = (
                b'{"jsonrpc":"2.0","id":'
                + json.dumps(payload["id"]).encode()
                + b',"result":{"content":[],"structuredContent":{"identity":'
                b'{"brand_json_url":"https://first.example/brand.json",'
                b'"brand_json_url":"https://second.example/brand.json"}}}}'
            )
        else:
            body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
        return httpx.Response(
            200,
            content=b"event: message\ndata: " + body + b"\n\n" if sse else body,
            headers={"content-type": "text/event-stream" if sse else "application/json"},
        )

    return lambda _url: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("malformed", [None, 1, "invalid", {}])
@pytest.mark.parametrize("container", ["mcp-content", "a2a-parts", "a2a-artifact-parts"])
async def test_malformed_capabilities_container_raises_resolver_error(
    container: str, malformed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(A2AAdapter, "_send_and_aggregate", _real_send_and_aggregate)
    protocol: Literal["mcp", "a2a"] = "mcp" if container == "mcp-content" else "a2a"
    if container == "mcp-content":
        result = {"content": malformed}
    elif container == "a2a-parts":
        result = {"parts": malformed}
    else:
        result = {"artifacts": [{"parts": malformed}]}
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            AGENT, protocol=protocol, _capabilities_client_factory=discovery_factory(result)
        )
    assert exc.value.code == "capabilities_invalid"


@pytest.mark.parametrize("malformed", [None, 1, "invalid", {}])
async def test_malformed_portfolio_brands_raises_resolver_error(malformed: Any) -> None:
    factory = discovery_factory(
        {"content": [], "structuredContent": {"identity": {"brand_json_url": RECORD}}},
        {"house": {"agents": [{"type": "sales", "url": AGENT}]}, "brands": malformed},
    )
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(
            AGENT,
            _capabilities_client_factory=factory,
            _brand_jwks_client_factory=factory,
        )
    assert exc.value.code == "brand_json_resolution_failed"
    assert exc.value.signature_code == "request_signature_brand_json_malformed"


@pytest.mark.parametrize("sse", [False, True])
async def test_duplicate_keys_in_protocol_envelope_are_rejected(sse: bool) -> None:
    factory = discovery_factory({}, duplicate_envelope=True, sse=sse)
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(AGENT, _capabilities_client_factory=factory)
    assert exc.value.code == "capabilities_invalid"


async def test_duplicate_keys_in_mcp_text_trust_root_are_rejected() -> None:
    factory = discovery_factory(
        {
            "content": [
                {
                    "type": "text",
                    "text": '{"identity":{"brand_json_url":"https://first.example/brand.json",'
                    '"brand_json_url":"https://second.example/brand.json"}}',
                }
            ]
        }
    )
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(AGENT, _capabilities_client_factory=factory)
    assert exc.value.code == "capabilities_invalid"


@pytest.mark.parametrize("part", [{"type": "text", "text": None}, {"type": "unknown"}])
async def test_sdk_content_validation_raises_capabilities_invalid(part: dict[str, Any]) -> None:
    factory = discovery_factory({"content": [part]})
    with pytest.raises(AgentResolverError) as exc:
        await async_resolve_agent(AGENT, _capabilities_client_factory=factory)
    assert exc.value.code == "capabilities_invalid"
