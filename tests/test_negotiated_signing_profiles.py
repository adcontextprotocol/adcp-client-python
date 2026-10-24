"""Negotiated request profiles reach HTTP writers without trusting body pins."""

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from a2a import types as pb
from cryptography.hazmat.primitives.asymmetric import ed25519
from google.protobuf.json_format import MessageToDict

from adcp import ADCPClient
from adcp.protocols.a2a import A2AAdapter, _make_data_part
from adcp.signing import SigningConfig
from adcp.signing.negotiated_profiles import NegotiatedSigningProfiles
from adcp.types import GetAdcpCapabilitiesResponse, GetProductsRequest, GetProductsResponse
from adcp.types.core import AgentConfig, TaskResult, TaskStatus


def mcp_body(params, tool="get_products"):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": params,
            },
        }
    ).encode()


def make_client(*, override=None, covers="forbidden"):
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp", protocol="mcp"),
        signing=SigningConfig(
            private_key=ed25519.Ed25519PrivateKey.generate(),
            key_id="buyer",
            signing_profile_version=override,
        ),
    )
    client._capabilities = GetAdcpCapabilitiesResponse.model_validate(
        {
            "adcp": {
                "major_versions": [3],
                "supported_versions": ["3.0", "3.1"],
                "idempotency": {"supported": False},
            },
            "supported_protocols": ["media_buy"],
            "request_signing": {
                "supported": True,
                "supported_for": ["get_products"],
                "covers_content_digest": covers,
            },
        }
    )
    client.adapter.signing_capability_check = AsyncMock()
    client.fetch_capabilities = AsyncMock(return_value=client._capabilities)
    client.adapter.request_validation_mode = "off"
    client.adapter.response_validation_mode = "off"
    return client


async def test_mcp_writer_uses_negotiated_31_profile():
    client = make_client()
    observed = []
    queue = asyncio.Queue()

    # Start the writer before entering the dispatch scope, as MCP does. Its
    # ContextVars never inherit the caller's operation/profile changes.
    async def writer():
        request, completed = await queue.get()
        try:
            await client._sign_outgoing_request(request)
            completed.set_result(request)
        except Exception as exc:
            completed.set_exception(exc)

    writer_task = asyncio.create_task(writer())

    async def call_tool(tool, params):
        request = httpx.Request("POST", "https://seller.example/mcp", content=mcp_body(params))
        done = asyncio.get_running_loop().create_future()
        await queue.put((request, done))
        observed.append(await done)
        return MagicMock(
            isError=False,
            structuredContent={"products": [], "status": "completed", "cache_scope": "public"},
            content=[],
        )

    session = MagicMock(call_tool=AsyncMock(side_effect=call_tool))
    client.adapter._get_session = AsyncMock(return_value=session)
    try:
        result = await client.get_products(GetProductsRequest(buying_mode="wholesale"))
        assert result.success, result.error
        assert session.call_tool.call_args.args[1]["adcp_version"] == "3.1"
        signed = observed[0]
        assert "signature" in signed.headers
        assert not signed.headers["signature"].split(":")[1].endswith("=")
        assert "content-digest" not in signed.headers
        assert "content-digest" not in signed.headers["signature-input"]
        assert client._negotiated_signing_profiles.resolve(signed.content) is None
    finally:
        if not writer_task.done():
            writer_task.cancel()
        await asyncio.gather(writer_task, return_exceptions=True)


async def test_explicit_profile_override_wins_over_31_negotiation():
    client = make_client(override="3.2", covers="required")
    registry = client._negotiated_signing_profiles
    params = {"adcp_version": "3.1"}
    request = httpx.Request("POST", "https://seller.example/mcp", content=mcp_body(params))
    with registry.bind("get_products", params, "3.1"):
        await client._sign_outgoing_request(request)
    assert request.headers["signature"].split(":")[1].endswith("==")
    assert "content-digest" in request.headers


async def test_concurrent_bindings_survive_sibling_completion_and_cancellation():
    registry = NegotiatedSigningProfiles()
    params = {"adcp_version": "3.1", "idempotency_key": "same-outbound-key"}
    body = mcp_body(params)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def sibling():
        with registry.bind("get_products", params, "3.1"):
            entered.set()
            await released.wait()

    task = asyncio.create_task(sibling())
    await entered.wait()
    with registry.bind("get_products", params, "3.1"):
        assert registry.resolve(body) == "3.1"
    assert registry.resolve(body) == "3.1"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)
    assert registry.resolve(body) is None


def test_unregistered_and_modified_parameters_cannot_select_a_profile():
    registry = NegotiatedSigningProfiles()
    params = {"adcp_version": "3.1", "idempotency_key": "registered-key"}
    assert registry.resolve(mcp_body(params)) is None
    with registry.bind("get_products", params, "3.1"):
        assert registry.resolve(mcp_body(params)) == "3.1"
        changed = {**params, "adcp_version": "3.0"}
        assert registry.resolve(mcp_body(changed)) is None
        assert registry.resolve(mcp_body(params, tool="sync_accounts")) is None
        assert NegotiatedSigningProfiles().resolve(mcp_body(params)) is None
    assert registry.resolve(mcp_body(params)) is None


def test_duplicate_version_keys_cannot_match_a_registered_invocation():
    registry = NegotiatedSigningProfiles()
    params = {"adcp_version": "3.1"}
    ambiguous = mcp_body(params).replace(
        b'"adcp_version": "3.1"', b'"adcp_version": "3.2", "adcp_version": "3.1"'
    )
    with registry.bind("get_products", params, "3.1"):
        assert registry.resolve(ambiguous) is None


@pytest.mark.parametrize("jsonrpc", [False, True])
def test_a2a_fingerprint_matches_actual_protobuf_parameters(jsonrpc):
    registry = NegotiatedSigningProfiles()
    params = {"adcp_version": "3.1", "budget": 100, "ext": {"nested": [5, 3.25]}}
    message = MessageToDict(
        pb.Message(
            parts=[
                _make_data_part(
                    {
                        "skill": "get_products",
                        "parameters": params,
                    }
                )
            ]
        )
    )
    normalized = message["parts"][0]["data"]["parameters"]
    payload = {"message": message}
    if jsonrpc:
        payload = {"method": "message/send", "params": payload}
    body = json.dumps(payload).encode()
    with registry.bind("get_products", normalized, "3.1"):
        assert registry.resolve(body) == "3.1"
    assert registry.resolve(body) is None


def test_natural_language_a2a_invocation_uses_registered_parameters():
    registry = NegotiatedSigningProfiles()
    params = {"adcp_version": "3.1", "brief": "video"}
    text = f"Execute tool: get_products\nParameters: {json.dumps(params, indent=2)}"
    body = json.dumps({"message": {"parts": [{"text": text}]}}).encode()
    with registry.bind("get_products", params, "3.1"):
        assert registry.resolve(body) == "3.1"


async def test_a2a_adapter_binds_after_key_injection_and_cleans_up_failed_send():
    adapter = A2AAdapter(
        AgentConfig(id="seller", agent_uri="https://seller.example/a2a", protocol="a2a")
    )
    adapter.request_validation_mode = "off"
    adapter.response_validation_mode = "off"
    registry = NegotiatedSigningProfiles()
    adapter.signing_request_scope = registry.bind
    adapter._get_a2a_client = AsyncMock(return_value=object())
    sent = []

    async def send(client, request):
        body = json.dumps(MessageToDict(request)).encode()
        actual = MessageToDict(request)["message"]["parts"][0]["data"]["parameters"]
        assert actual["idempotency_key"]
        assert registry.resolve(body) == "3.1"
        sent.append(body)
        raise RuntimeError("simulated send failure")

    adapter._send_and_aggregate = AsyncMock(side_effect=send)
    params = {"adcp_version": "3.1", "accounts": [], "ext": {"count": 123}}
    result = await adapter._call_a2a_tool_wire("sync_accounts", params)
    assert not result.success
    assert "idempotency_key" not in params
    assert len(sent) == 1
    assert registry.resolve(sent[0]) is None


def test_failed_typed_parse_preserves_conversion_metadata():
    client = make_client()
    report = {"adcp_version_conversion": {"wire_version": "3.0", "losses": [{"pointer": "/x"}]}}
    error = {"code": "UNSUPPORTED_FEATURE", "message": "No old representation"}
    raw = TaskResult(
        status=TaskStatus.COMPLETED,
        success=True,
        data={"products": 42},
        metadata=report,
        adcp_error=error,
    )
    parsed = client.adapter._parse_response(raw, GetProductsResponse)
    assert not parsed.success
    assert parsed.metadata == deepcopy(report)
    assert parsed.adcp_error == error


def test_unsuccessful_typed_result_preserves_structured_conversion_error():
    client = make_client()
    error = {"code": "UNSUPPORTED_FEATURE", "message": "No old representation"}
    raw = TaskResult(status=TaskStatus.FAILED, success=False, adcp_error=error)
    parsed = client.adapter._parse_response(raw, GetProductsResponse)
    assert not parsed.success
    assert parsed.adcp_error == error
