"""Tests for the ADCPClient auto-sign httpx request event hook.

These exercise ``ADCPClient._sign_outgoing_request`` directly: build a real
``httpx.Request``, call the hook with a controlled capability response and
``current_operation`` ContextVar, then verify the resulting signature with
the RFC 9421 verifier. The goal is to prove the client-side orchestration
produces bytes the server-side verifier accepts — without standing up an
A2A SDK or a full test server.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from adcp.client import ADCPClient
from adcp.signing import (
    SigningConfig,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    private_key_from_jwk,
    verify_request_signature,
)
from adcp.signing.autosign import current_operation
from adcp.types.core import AgentConfig, Protocol
from adcp.types.generated_poc.protocol.get_adcp_capabilities_response import (
    Adcp,
    CoversContentDigest,
    GetAdcpCapabilitiesResponse,
    Idempotency,
    MajorVersion,
    RequestSigning,
    SupportedProtocol,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


# -- fixtures ------------------------------------------------------------


def _make_client(signing: SigningConfig | None = None, **kwargs: Any) -> ADCPClient:
    agent = AgentConfig(
        id="test-seller",
        agent_uri="https://seller.example.com",
        protocol=Protocol.A2A,
    )
    return ADCPClient(agent, signing=signing, **kwargs)


def _make_caps(
    *,
    required: list[str] | None = None,
    warn: list[str] | None = None,
    supported_for: list[str] | None = None,
    covers: CoversContentDigest = CoversContentDigest.either,
    signing_supported: bool = True,
) -> GetAdcpCapabilitiesResponse:
    return GetAdcpCapabilitiesResponse(
        adcp=Adcp(
            major_versions=[MajorVersion(root=3)],
            idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
        ),
        supported_protocols=[SupportedProtocol.media_buy],
        request_signing=RequestSigning(
            supported=signing_supported,
            covers_content_digest=covers,
            required_for=required or [],
            warn_for=warn or [],
            supported_for=supported_for,
        ),
    )


def _build_request(
    url: str = "https://seller.example.com/adcp/create_media_buy",
    body: bytes = b'{"plan_id":"p1"}',
) -> httpx.Request:
    # httpx.Request constructor populates headers and content eagerly,
    # matching the shape the event hook receives during real dispatch.
    return httpx.Request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        content=body,
    )


def _verify(
    request: httpx.Request,
    body: bytes,
    *,
    operation: str,
    covers_policy: str = "either",
    required_for: frozenset[str] = frozenset(),
) -> None:
    jwks_resolver = StaticJwksResolver({"keys": [ED25519_KEY]})
    options = VerifyOptions(
        now=float(int(time.time())),
        capability=VerifierCapability(
            covers_content_digest=covers_policy,  # type: ignore[arg-type]
            required_for=required_for,
        ),
        operation=operation,
        jwks_resolver=jwks_resolver,
    )
    verify_request_signature(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        body=body,
        options=options,
    )


@pytest.fixture()
def signing_config() -> SigningConfig:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    return SigningConfig(private_key=private_key, key_id=ED25519_KEY["kid"])


# -- construction --------------------------------------------------------


def test_no_signing_kwarg_leaves_adapter_hook_unset() -> None:
    client = _make_client()
    assert client.signing is None
    assert client.adapter.signing_request_hook is None


def test_signing_kwarg_installs_adapter_hook(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    assert client.signing is signing_config
    assert client.adapter.signing_request_hook is not None


def test_signing_profile_derives_from_adcp_pin(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config, adcp_version="3.1")
    assert client._signing_profile_version == "3.1"


def test_server_pin_controls_effective_signing_profile(
    signing_config: SigningConfig,
) -> None:
    client = _make_client(
        signing=signing_config,
        adcp_version="3.1",
        server_version="3.0",
    )
    assert client._signing_profile_version == "3.0"


def test_legacy_server_pin_without_signing_needs_no_profile() -> None:
    with pytest.warns(DeprecationWarning):
        client = _make_client(server_version="2.5")
    assert client._signing_profile_version is None


def test_explicit_signing_profile_overrides_client_pin(
    signing_config: SigningConfig,
) -> None:
    explicit = SigningConfig(
        private_key=signing_config.private_key,
        key_id=signing_config.key_id,
        signing_profile_version="3.2",
    )
    client = _make_client(signing=explicit, adcp_version="3.1")
    assert client._signing_profile_version == "3.2"


@pytest.mark.parametrize(
    ("adcp_version", "uses_padded_base64"),
    [("3.0", False), ("3.1", False), ("3.2-beta.0", True)],
)
async def test_client_pin_controls_signature_wire_encoding(
    signing_config: SigningConfig,
    adcp_version: str,
    uses_padded_base64: bool,
) -> None:
    client = _make_client(signing=signing_config, adcp_version=adcp_version)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["create_media_buy"])
    )
    request = _build_request()
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert request.headers["Signature"].endswith("==:") is uses_padded_base64


# -- hook: skip paths ----------------------------------------------------


async def test_hook_skips_when_signing_is_none() -> None:
    # No signing config → hook itself isn't installed by __init__,
    # but the method is still callable as a bound method for clarity:
    # invoking it manually should return without mutating headers.
    client = _make_client()
    request = _build_request()
    before = dict(request.headers)
    # current_operation set; signing None → still skip.
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert dict(request.headers) == before


async def test_hook_skips_when_context_var_unset(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    request = _build_request()
    before = dict(request.headers)
    # No current_operation set → simulated out-of-band request.
    await client._sign_outgoing_request(request)
    assert dict(request.headers) == before


async def test_hook_reads_mcp_operation_from_jsonrpc_body(
    signing_config: SigningConfig,
) -> None:
    client = _make_client(signing=signing_config)
    client._capabilities = _make_caps(required=["create_media_buy"])
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_media_buy", "arguments": {"plan_id": "p1"}},
        },
        separators=(",", ":"),
    ).encode()
    request = _build_request(url="https://seller.example.com/mcp", body=body)

    # No ContextVar is set: this matches the MCP writer task that actually
    # invokes the httpx hook.
    await client._sign_outgoing_request(request)

    assert "Signature" in request.headers
    assert "Signature-Input" in request.headers
    _verify(
        request,
        body,
        operation="create_media_buy",
        required_for=frozenset({"create_media_buy"}),
    )


async def test_mcp_hook_fails_closed_without_prefetched_policy(
    signing_config: SigningConfig,
) -> None:
    client = _make_client(signing=signing_config)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_media_buy", "arguments": {}},
        }
    ).encode()
    request = _build_request(url="https://seller.example.com/mcp", body=body)

    with pytest.raises(RuntimeError, match="was not prefetched"):
        await client._sign_outgoing_request(request)


async def test_hook_skips_for_get_adcp_capabilities(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    request = _build_request()
    before = dict(request.headers)
    token = current_operation.set("get_adcp_capabilities")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert dict(request.headers) == before


async def test_hook_skips_when_op_not_in_any_list(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["create_media_buy"])
    )
    request = _build_request(url="https://seller.example.com/adcp/get_products")
    token = current_operation.set("get_products")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert "Signature" not in request.headers
    assert "Signature-Input" not in request.headers


async def test_hook_skips_when_seller_unsupported(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(signing_supported=False, required=["create_media_buy"])
    )
    request = _build_request()
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert "Signature" not in request.headers


# -- hook: sign paths ----------------------------------------------------


async def test_hook_signs_required_for_op_and_server_verifies(
    signing_config: SigningConfig,
) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["create_media_buy"])
    )
    body = b'{"plan_id":"p1"}'
    request = _build_request(body=body)
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)

    assert "Signature" in request.headers
    assert "Signature-Input" in request.headers
    # covers_content_digest defaults to "either" → signer's choice →
    # default stricter (cover), so Content-Digest must be present.
    assert "Content-Digest" in request.headers
    _verify(
        request,
        body,
        operation="create_media_buy",
        required_for=frozenset({"create_media_buy"}),
    )


async def test_hook_signs_supported_for_op(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(supported_for=["sync_creatives"])
    )
    body = b'{"creatives":[]}'
    request = _build_request(
        url="https://seller.example.com/adcp/sync_creatives",
        body=body,
    )
    token = current_operation.set("sync_creatives")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)

    assert "Signature" in request.headers
    _verify(request, body, operation="sync_creatives")


async def test_hook_signs_warn_for_op(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(warn=["update_media_buy"])
    )
    body = b'{"media_buy_id":"m1"}'
    request = _build_request(
        url="https://seller.example.com/adcp/update_media_buy",
        body=body,
    )
    token = current_operation.set("update_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)

    assert "Signature" in request.headers
    _verify(request, body, operation="update_media_buy")


# -- hook: covers_content_digest tri-state ------------------------------


async def test_hook_honors_covers_required(signing_config: SigningConfig) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(
            required=["create_media_buy"],
            covers=CoversContentDigest.required,
        )
    )
    body = b'{"plan_id":"p1"}'
    request = _build_request(body=body)
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)

    assert "Content-Digest" in request.headers
    _verify(
        request,
        body,
        operation="create_media_buy",
        covers_policy="required",
        required_for=frozenset({"create_media_buy"}),
    )


async def test_hook_rejects_covers_forbidden_under_32(
    signing_config: SigningConfig,
) -> None:
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(
            required=["create_media_buy"],
            covers=CoversContentDigest.forbidden,
        )
    )
    body = b'{"plan_id":"p1"}'
    request = _build_request(body=body)
    token = current_operation.set("create_media_buy")
    try:
        with pytest.raises(ValueError, match="must cover content-digest"):
            await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)


# -- invariants ---------------------------------------------------------


async def test_clearing_signing_post_init_silently_skips(
    signing_config: SigningConfig,
) -> None:
    # ``signing`` is a public attribute; if a caller clears it after
    # construction the hook must short-circuit cleanly rather than raise
    # or produce a partial signature. The earliest guard in the hook
    # (``self.signing is None``) enforces this.
    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["create_media_buy"])
    )
    client.signing = None
    request = _build_request()
    token = current_operation.set("create_media_buy")
    try:
        await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)
    assert "Signature" not in request.headers


async def test_hook_warns_on_contradictory_seller_policy(
    signing_config: SigningConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # supported=False + non-empty required_for is a seller-config error.
    # The classifier correctly skips, but a silent skip hides a misconfig
    # that will bite pilots — surface it as a warning.
    import logging as _logging

    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(signing_supported=False, required=["create_media_buy"])
    )
    request = _build_request()
    token = current_operation.set("create_media_buy")
    try:
        with caplog.at_level(_logging.WARNING, logger="adcp.client"):
            await client._sign_outgoing_request(request)
    finally:
        current_operation.reset(token)

    assert "Signature" not in request.headers
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("supported=false" in r.getMessage().lower() for r in warnings), [
        r.getMessage() for r in warnings
    ]


# -- concurrency --------------------------------------------------------


async def test_context_var_isolates_concurrent_calls(
    signing_config: SigningConfig,
) -> None:
    """Two tasks dispatching different operations must see their own names.

    The ``current_operation`` ContextVar's whole purpose (vs a thread-local
    or instance attribute) is to isolate per-task values across
    ``asyncio.gather``. This test pins that invariant: concurrent hook
    invocations with different operations each sign with the correct
    operation context.
    """
    import asyncio

    client = _make_client(signing=signing_config)
    client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(
            required=["create_media_buy"],
            supported_for=["sync_creatives"],
        )
    )

    async def _dispatch(op: str, body: bytes) -> httpx.Request:
        request = _build_request(url=f"https://seller.example.com/adcp/{op}", body=body)
        token = current_operation.set(op)
        try:
            # Yield to let the other task interleave between the set and
            # the hook body, stressing the isolation invariant.
            await asyncio.sleep(0)
            await client._sign_outgoing_request(request)
        finally:
            current_operation.reset(token)
        return request

    req_create, req_sync = await asyncio.gather(
        _dispatch("create_media_buy", b'{"plan_id":"p1"}'),
        _dispatch("sync_creatives", b'{"creatives":[]}'),
    )

    # Each request's signature must validate for its own operation name.
    _verify(
        req_create,
        b'{"plan_id":"p1"}',
        operation="create_media_buy",
        required_for=frozenset({"create_media_buy"}),
    )
    _verify(req_sync, b'{"creatives":[]}', operation="sync_creatives")


# -- multi-agent forwarding ---------------------------------------------


def test_multi_agent_client_forwards_signing(signing_config: SigningConfig) -> None:
    from adcp import ADCPMultiAgentClient

    agents = [
        AgentConfig(id="a", agent_uri="https://a.example", protocol=Protocol.A2A),
        AgentConfig(id="b", agent_uri="https://b.example", protocol=Protocol.A2A),
    ]
    multi = ADCPMultiAgentClient(agents=agents, signing=signing_config)
    assert multi.agent("a").signing is signing_config
    assert multi.agent("b").signing is signing_config
    assert multi.agent("a").adapter.signing_request_hook is not None
    assert multi.agent("b").adapter.signing_request_hook is not None


# -- suppress fixture warnings for unused params ------------------------

_ = Any  # silence flake on module-level Any import
