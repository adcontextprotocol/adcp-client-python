"""Framework request-signature verification before dispatch (issue #1260).

``serve(request_signature_verification=...)`` verifies RFC 9421 signatures
at the ASGI edge on both MCP and A2A legs, rejects unsigned requests to
``required_for`` operations with ``request_signature_required``, and turns a
verified signer into ``ToolContext.caller_identity`` plus an ``http_sig``
:class:`AuthInfo` that ``BuyerAgentRegistry`` dispatch resolves.
"""

from __future__ import annotations

import contextlib
import json
import warnings
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.middleware.base import BaseHTTPMiddleware

from adcp.decisioning.context import AuthInfo
from adcp.decisioning.handler import _resolve_buyer_agent
from adcp.decisioning.registry import BuyerAgent, HttpSigCredential
from adcp.server import ADCPHandler, RequestSignatureVerification, ToolContext
from adcp.server.auth import BearerTokenAuth, Principal, validator_from_token_map
from adcp.server.signed_requests import check_request_signature_verification
from adcp.signing import (
    InMemoryReplayStore,
    JwksUriSignerKeys,
    StaticSignerKeys,
    VerifierCapability,
    VerifyOptions,
    generate_signing_keypair,
    load_private_key_pem,
    sign_request,
    verify_starlette_request,
)
from adcp.types.capabilities import RequestSigning

AGENT_URL = "https://buyer.example/"
KID = "buyer-key-1"
_PEM, _PUBLIC_JWK = generate_signing_keypair(kid=KID)
_PRIVATE_KEY = load_private_key_pem(_PEM)
_OTHER_PEM, _OTHER_JWK = generate_signing_keypair(kid="stranger-key")

MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}

VECTORS = Path(__file__).parent / "conformance" / "vectors" / "request-signing"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Recording(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.contexts: list[ToolContext | None] = []

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        self.contexts.append(context)
        return {"products": []}

    async def get_media_buys(self, params: Any, context: Any = None) -> dict[str, Any]:
        self.contexts.append(context)
        return {"media_buys": []}


def _config(**overrides: Any) -> RequestSignatureVerification:
    kwargs: dict[str, Any] = {
        "signer_keys": StaticSignerKeys({AGENT_URL: {"keys": [_PUBLIC_JWK]}}),
        "required_for": frozenset({"get_products"}),
        "supported_for": frozenset({"get_products", "get_media_buys"}),
    }
    kwargs.update(overrides)
    return RequestSignatureVerification(**kwargs)


def _both_app(
    handler: ADCPHandler[Any],
    config: RequestSignatureVerification | None,
    *,
    auth: BearerTokenAuth | None = None,
    stateless_http: bool = True,
) -> Any:
    from adcp.server.serve import _build_mcp_and_a2a_app

    return _build_mcp_and_a2a_app(
        handler,
        name="test-agent",
        port=0,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        advertise_all=True,
        stateless_http=stateless_http,
        allowed_hosts=["localhost"],
        auth=auth,
        request_signature_verification=config,
    )


def _signed(
    url: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    private_key: Any = _PRIVATE_KEY,
    key_id: str = KID,
) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body).encode()
    base = {"content-type": "application/json", **(headers or {})}
    signed = sign_request(
        method="POST",
        url=url,
        headers=base,
        body=raw,
        private_key=private_key,
        key_id=key_id,
        alg="ed25519",
        cover_content_digest=True,
        signing_profile_version="3.1",
    )
    return raw, {**base, **signed.as_dict()}


def _tools_call(name: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }


def _a2a_send(skill: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "user",
                "parts": [{"kind": "data", "data": {"skill": skill, "parameters": {}}}],
            }
        },
    }


@contextlib.asynccontextmanager
async def _client(app: Any, lifespan_app: Any = None) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(lifespan_app or app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            yield client


async def _post(app: Any, path: str, content: bytes, headers: dict[str, str]) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(path, content=content, headers=headers)


def _assert_rejected(response: httpx.Response, code: str) -> None:
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == f'Signature error="{code}"'
    assert response.json() == {"error": code}


def _assert_signed_identity(context: ToolContext | None) -> None:
    assert context is not None
    assert context.caller_identity == AGENT_URL
    auth_info = context.metadata["adcp.auth_info"]
    assert isinstance(auth_info, AuthInfo)
    assert auth_info.kind == "http_sig"
    assert isinstance(auth_info.credential, HttpSigCredential)
    assert auth_info.credential.agent_url == AGENT_URL
    assert auth_info.credential.keyid == KID


# ---------------------------------------------------------------------------
# Policy classification
# ---------------------------------------------------------------------------


def test_posture_precedence_across_namespaces() -> None:
    config = _config(
        required_for=frozenset({"create_media_buy"}),
        warn_for=frozenset({"update_media_buy"}),
        supported_for=frozenset({"create_media_buy", "update_media_buy", "get_products"}),
        protocol_methods_required_for=frozenset({"tasks/cancel"}),
        protocol_methods_warn_for=frozenset({"tasks/get"}),
    )
    assert config.posture_for("tools/call", "create_media_buy") == "required"
    assert config.posture_for("tools/call", "update_media_buy") == "warn"
    assert config.posture_for("tools/call", "get_products") == "supported"
    assert config.posture_for("tools/list", None) is None
    assert config.posture_for("tasks/cancel", None) == "required"
    assert config.posture_for("tasks/get", None) == "warn"
    # A2A message sends carry both a method and a skill; the stricter wins.
    assert config.posture_for("message/send", "create_media_buy") == "required"
    # An opaque target (batch / unparseable / unknown A2A skill) fails closed.
    assert config.posture_for(None, None, opaque=True) == "required"
    assert _config(required_for=frozenset()).posture_for(None, None, opaque=True) is None
    # A JSON-RPC response names no method and invokes nothing.
    assert config.posture_for(None, None) is None


def test_protocol_method_exact_match_vectors() -> None:
    vectors = json.loads((VECTORS / "protocol-method-names.json").read_text())
    for case in vectors["exact_match_cases"]:
        config = _config(
            required_for=frozenset(),
            protocol_methods_required_for=frozenset(case["declared_methods"]),
        )
        matched = config.posture_for(case["wire_method"], None) == "required"
        assert matched is case["matches"], case["id"]


def test_from_capability_mirrors_declared_block() -> None:
    capability = RequestSigning(
        supported=True,
        covers_content_digest="required",
        required_for=["create_media_buy"],
        warn_for=["update_media_buy"],
        supported_for=["create_media_buy", "update_media_buy"],
        protocol_methods_supported_for=["tasks/cancel"],
        protocol_methods_required_for=["tasks/cancel"],
    )
    store = InMemoryReplayStore()
    config = RequestSignatureVerification.from_capability(
        capability, signer_keys=StaticSignerKeys({}), replay_store=store
    )
    assert config.required_for == {"create_media_buy"}
    assert config.warn_for == {"update_media_buy"}
    assert config.protocol_methods_required_for == {"tasks/cancel"}
    assert config.covers_content_digest == "required"
    assert config.replay_store is store


def test_config_rejects_inconsistent_policy() -> None:
    with pytest.raises(ValueError, match="both required_for and warn_for"):
        _config(required_for=frozenset({"x"}), warn_for=frozenset({"x"}))
    with pytest.raises(ValueError, match="tools/call"):
        _config(protocol_methods_required_for=frozenset({"tools/call"}))


def test_default_replay_store_is_created_per_config() -> None:
    # Each config owns one store that every request it verifies shares
    # (test_mcp_replayed_nonce_rejected); separate configs never share one.
    config = _config()
    assert isinstance(config.replay_store, InMemoryReplayStore)
    assert _config().replay_store is not config.replay_store


# ---------------------------------------------------------------------------
# Signer key resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_signer_keys_resolve_agent() -> None:
    keys = StaticSignerKeys({AGENT_URL: {"keys": [_PUBLIC_JWK]}})
    resolved = await keys(KID)
    assert resolved is not None
    assert resolved.agent_url == AGENT_URL
    assert resolved.jwk["kid"] == KID
    assert await keys("unknown") is None


def test_static_signer_keys_reject_shared_kid() -> None:
    with pytest.raises(ValueError, match="published by both"):
        StaticSignerKeys(
            {
                AGENT_URL: {"keys": [_PUBLIC_JWK]},
                "https://other.example/": {"keys": [_PUBLIC_JWK]},
            }
        )


@pytest.mark.asyncio
async def test_jwks_uri_signer_keys_resolve_and_treat_ambiguity_as_unknown() -> None:
    documents = {
        "https://a.example/jwks.json": {"keys": [_PUBLIC_JWK]},
        "https://b.example/jwks.json": {"keys": [_PUBLIC_JWK, _OTHER_JWK]},
    }

    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        return documents[uri]

    keys = JwksUriSignerKeys(
        {
            "https://a.example/": "https://a.example/jwks.json",
            "https://b.example/": "https://b.example/jwks.json",
        },
        fetcher=fetcher,
    )
    only_b = await keys("stranger-key")
    assert only_b is not None and only_b.agent_url == "https://b.example/"
    assert await keys(KID) is None
    assert await keys("nobody") is None


# ---------------------------------------------------------------------------
# MCP leg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_unsigned_required_operation_rejected_before_dispatch() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    response = await _post(
        app, "/mcp", json.dumps(_tools_call("get_products")).encode(), MCP_HEADERS
    )
    _assert_rejected(response, "request_signature_required")
    assert handler.contexts == []


@pytest.mark.asyncio
async def test_mcp_unsigned_discovery_and_optional_operations_pass() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    tools_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with _client(app) as client:
        listed = await client.post("/mcp", json=tools_list, headers=MCP_HEADERS)
        assert listed.status_code == 200, listed.text
        optional = await client.post(
            "/mcp", json=_tools_call("get_media_buys"), headers=MCP_HEADERS
        )
    assert optional.status_code == 200, optional.text
    assert len(handler.contexts) == 1
    assert handler.contexts[0] is None or handler.contexts[0].caller_identity is None


@pytest.mark.asyncio
async def test_mcp_signed_request_populates_caller_identity() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    response = await _post(app, "/mcp", raw, headers)
    assert response.status_code == 200, response.text
    _assert_signed_identity(handler.contexts[-1])


@pytest.mark.asyncio
async def test_mcp_signature_covers_path_before_trailing_slash_normalization() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    raw, headers = _signed(
        "http://localhost/mcp/", _tools_call("get_products"), headers=MCP_HEADERS
    )
    response = await _post(app, "/mcp/", raw, headers)
    assert response.status_code == 200, response.text
    _assert_signed_identity(handler.contexts[-1])


@pytest.mark.asyncio
async def test_mcp_replayed_nonce_rejected() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            first = await client.post("/mcp", content=raw, headers=headers)
            second = await client.post("/mcp", content=raw, headers=headers)
    assert first.status_code == 200, first.text
    _assert_rejected(second, "request_signature_replayed")


@pytest.mark.asyncio
async def test_mcp_partial_signature_pair_rejected_even_on_optional_operation() -> None:
    app = _both_app(_Recording(), _config(warn_for=frozenset({"get_media_buys"})))
    raw, headers = _signed(
        "http://localhost/mcp", _tools_call("get_media_buys"), headers=MCP_HEADERS
    )
    headers = {k: v for k, v in headers.items() if k.lower() != "signature"}
    response = await _post(app, "/mcp", raw, headers)
    _assert_rejected(response, "request_signature_header_malformed")


@pytest.mark.asyncio
async def test_mcp_unknown_signer_rejected() -> None:
    app = _both_app(_Recording(), _config())
    raw, headers = _signed(
        "http://localhost/mcp",
        _tools_call("get_products"),
        headers=MCP_HEADERS,
        private_key=load_private_key_pem(_OTHER_PEM),
        key_id="stranger-key",
    )
    response = await _post(app, "/mcp", raw, headers)
    _assert_rejected(response, "request_signature_key_unknown")


@pytest.mark.asyncio
async def test_mcp_tampered_body_rejected() -> None:
    app = _both_app(_Recording(), _config())
    _raw, headers = _signed(
        "http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS
    )
    tampered = json.dumps(_tools_call("get_products") | {"id": 2}).encode()
    response = await _post(app, "/mcp", tampered, headers)
    _assert_rejected(response, "request_signature_digest_mismatch")


@pytest.mark.asyncio
async def test_warn_for_failure_continues_without_identity() -> None:
    handler = _Recording()
    app = _both_app(
        handler,
        _config(required_for=frozenset(), warn_for=frozenset({"get_products"})),
    )
    raw, headers = _signed(
        "http://localhost/mcp",
        _tools_call("get_products"),
        headers=MCP_HEADERS,
        private_key=load_private_key_pem(_OTHER_PEM),
        key_id="stranger-key",
    )
    response = await _post(app, "/mcp", raw, headers)
    assert response.status_code == 200, response.text
    context = handler.contexts[-1]
    assert context is None or context.caller_identity is None


@pytest.mark.asyncio
async def test_resolver_exception_is_a_401_not_a_500() -> None:
    async def broken(keyid: str) -> Any:
        raise RuntimeError("database down")

    app = _both_app(_Recording(), _config(signer_keys=broken))
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    response = await _post(app, "/mcp", raw, headers)
    _assert_rejected(response, "request_signature_jwks_unavailable")


@pytest.mark.asyncio
async def test_seller_verified_signer_without_agent_url_grants_no_identity() -> None:
    """The framework never vouches for a key the seller verified: without a
    seller-supplied agent_url there is no identity and no bearer bypass."""
    store = InMemoryReplayStore()
    auth = BearerTokenAuth(
        validate_token=validator_from_token_map({"good": Principal(caller_identity="p")})
    )
    handler = _Recording()
    inner = _both_app(handler, _config(replay_store=store), auth=auth)

    async def seller_verify(request: Any, call_next: Any) -> Any:
        import time

        await verify_starlette_request(
            request,
            options=VerifyOptions(
                now=time.time(),
                capability=VerifierCapability(),
                operation="get_products",
                jwks_resolver=lambda keyid: _PUBLIC_JWK if keyid == KID else None,
                replay_store=store,
            ),
        )
        return await call_next(request)

    app = BaseHTTPMiddleware(inner, dispatch=seller_verify)
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    async with _client(app, lifespan_app=inner) as client:
        response = await client.post("/mcp", content=raw, headers=headers)
    assert response.status_code == 401
    assert "Bearer" in response.headers["www-authenticate"]
    assert handler.contexts == []


@pytest.mark.asyncio
async def test_oversized_keyid_rejected_before_resolver_runs() -> None:
    calls: list[str] = []

    async def counting(keyid: str) -> Any:
        calls.append(keyid)
        return None

    app = _both_app(_Recording(), _config(signer_keys=counting))
    raw, headers = _signed(
        "http://localhost/mcp",
        _tools_call("get_products"),
        headers=MCP_HEADERS,
        key_id="k" * 300,
    )
    response = await _post(app, "/mcp", raw, headers)
    _assert_rejected(response, "request_signature_header_malformed")
    assert calls == []


@pytest.mark.asyncio
async def test_jsonrpc_response_body_is_not_treated_as_opaque() -> None:
    app = _both_app(_Recording(), _config())
    reply = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {}}).encode()
    response = await _post(app, "/mcp", reply, MCP_HEADERS)
    assert "signature" not in response.headers.get("www-authenticate", "").lower()


@pytest.mark.asyncio
async def test_jwks_uri_signer_keys_cool_down_failing_endpoint() -> None:
    calls: dict[str, int] = {}

    async def fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
        calls[uri] = calls.get(uri, 0) + 1
        if "down" in uri:
            raise httpx.ConnectError("down")
        return {"keys": [_PUBLIC_JWK]}

    keys = JwksUriSignerKeys(
        {
            AGENT_URL: "https://up.example/jwks.json",
            "https://other.example/": "https://down.example/jwks.json",
        },
        fetcher=fetcher,
    )
    for _ in range(5):
        resolved = await keys(KID)
        assert resolved is not None and resolved.agent_url == AGENT_URL
    assert calls["https://down.example/jwks.json"] == 1
    with pytest.raises(Exception, match="JWKS fetch failed"):
        await keys("nobody")


@pytest.mark.asyncio
async def test_batch_body_fails_closed_when_anything_is_required() -> None:
    app = _both_app(_Recording(), _config())
    batch = json.dumps([_tools_call("get_products")]).encode()
    response = await _post(app, "/mcp", batch, MCP_HEADERS)
    _assert_rejected(response, "request_signature_required")


@pytest.mark.asyncio
async def test_signed_request_needs_no_bearer_when_auth_configured() -> None:
    auth = BearerTokenAuth(
        validate_token=validator_from_token_map({"good": Principal(caller_identity="p")})
    )
    handler = _Recording()
    app = _both_app(handler, _config(), auth=auth)
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    async with _client(app) as client:
        signed = await client.post("/mcp", content=raw, headers=headers)
        # An unsigned request to an operation that doesn't require signing
        # still needs its bearer.
        unsigned = await client.post(
            "/mcp", json=_tools_call("get_media_buys"), headers=MCP_HEADERS
        )
    assert signed.status_code == 200, signed.text
    _assert_signed_identity(handler.contexts[-1])
    assert unsigned.status_code == 401
    assert "Bearer" in unsigned.headers["www-authenticate"]


@pytest.mark.asyncio
async def test_seller_verify_starlette_request_is_reused_not_reverified() -> None:
    """A seller's own ``verify_starlette_request`` middleware claims the nonce;
    framework verification must reuse its result instead of reporting a replay."""
    store = InMemoryReplayStore()
    handler = _Recording()
    config = _config(replay_store=store)
    inner = _both_app(handler, config)

    async def seller_verify(request: Any, call_next: Any) -> Any:
        if request.method == "POST" and request.headers.get("signature"):
            import time

            await verify_starlette_request(
                request,
                options=VerifyOptions(
                    now=time.time(),
                    capability=VerifierCapability(),
                    operation="get_products",
                    jwks_resolver=lambda keyid: _PUBLIC_JWK if keyid == KID else None,
                    replay_store=store,
                    agent_url=AGENT_URL,
                ),
            )
        return await call_next(request)

    app = BaseHTTPMiddleware(inner, dispatch=seller_verify)
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    async with LifespanManager(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post("/mcp", content=raw, headers=headers)
    assert response.status_code == 200, response.text
    _assert_signed_identity(handler.contexts[-1])


@pytest.mark.asyncio
async def test_seller_verification_without_opt_in_keeps_bearer_and_identity() -> None:
    """A seller running ``verify_starlette_request`` without opting in to
    framework verification sees no change: bearer is still required and the
    signer does not become the caller identity."""
    auth = BearerTokenAuth(
        validate_token=validator_from_token_map({"good": Principal(caller_identity="p-bearer")})
    )
    handler = _Recording()
    inner = _both_app(handler, None, auth=auth)

    async def seller_verify(request: Any, call_next: Any) -> Any:
        import time

        await verify_starlette_request(
            request,
            options=VerifyOptions(
                now=time.time(),
                capability=VerifierCapability(),
                operation="get_products",
                jwks_resolver=lambda keyid: _PUBLIC_JWK if keyid == KID else None,
                replay_store=InMemoryReplayStore(),
                agent_url=AGENT_URL,
            ),
        )
        return await call_next(request)

    app = BaseHTTPMiddleware(inner, dispatch=seller_verify)
    async with _client(app, lifespan_app=inner) as client:
        raw, headers = _signed(
            "http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS
        )
        no_bearer = await client.post("/mcp", content=raw, headers=headers)
        raw, headers = _signed(
            "http://localhost/mcp",
            _tools_call("get_products"),
            headers={**MCP_HEADERS, "authorization": "Bearer good"},
        )
        with_bearer = await client.post("/mcp", content=raw, headers=headers)
    assert no_bearer.status_code == 401
    assert with_bearer.status_code == 200, with_bearer.text
    context = handler.contexts[-1]
    assert context is None or (
        context.caller_identity != AGENT_URL and "adcp.auth_info" not in context.metadata
    )


@pytest.mark.asyncio
async def test_stateful_session_does_not_leak_signer_to_later_unsigned_request() -> None:
    """A signed ``initialize`` must not make later unsigned calls on the same
    stateful session look signed; identity is read per request from its
    scope, never from state carried by the long-lived session task."""
    handler = _Recording()
    app = _both_app(handler, _config(), stateless_http=False)
    initialize = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    raw_init, init_headers = _signed("http://localhost/mcp", initialize, headers=MCP_HEADERS)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            init = await client.post("/mcp", content=raw_init, headers=init_headers)
            assert init.status_code == 200, init.text
            session = {"mcp-session-id": init.headers["mcp-session-id"]}

            unsigned = await client.post(
                "/mcp",
                content=json.dumps(_tools_call("get_media_buys")).encode(),
                headers={**MCP_HEADERS, **session},
            )
            assert unsigned.status_code == 200, unsigned.text
            leaked = handler.contexts[-1]
            assert leaked is None or leaked.caller_identity is None

            raw, headers = _signed(
                "http://localhost/mcp",
                _tools_call("get_products"),
                headers={**MCP_HEADERS, **session},
            )
            signed = await client.post("/mcp", content=raw, headers=headers)
            assert signed.status_code == 200, signed.text
            _assert_signed_identity(handler.contexts[-1])


# ---------------------------------------------------------------------------
# A2A leg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_unsigned_required_skill_rejected() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    response = await _post(
        app,
        "/",
        json.dumps(_a2a_send("get_products")).encode(),
        {"content-type": "application/json"},
    )
    _assert_rejected(response, "request_signature_required")
    assert handler.contexts == []


@pytest.mark.asyncio
async def test_a2a_signed_skill_populates_caller_identity() -> None:
    handler = _Recording()
    app = _both_app(handler, _config())
    raw, headers = _signed("http://localhost/", _a2a_send("get_products"))
    response = await _post(app, "/", raw, headers)
    assert response.status_code == 200, response.text
    _assert_signed_identity(handler.contexts[-1])


@pytest.mark.asyncio
async def test_a2a_unparseable_message_fails_closed() -> None:
    """A body the A2A parser rejects must not skip enforcement: dispatch has
    recovery paths that can still find an operation in it."""
    body = _a2a_send("get_products")
    body["params"]["message"]["parts"].append({"kind": "data", "data": {"x": float("nan")}})
    app = _both_app(_Recording(), _config())
    response = await _post(
        app, "/", json.dumps(body).encode(), {"content-type": "application/json"}
    )
    _assert_rejected(response, "request_signature_required")


@pytest.mark.asyncio
async def test_a2a_message_without_skill_fails_closed() -> None:
    body = _a2a_send("get_products")
    body["params"]["message"]["parts"] = [{"kind": "text", "text": "not an invocation"}]
    app = _both_app(_Recording(), _config())
    response = await _post(
        app, "/", json.dumps(body).encode(), {"content-type": "application/json"}
    )
    _assert_rejected(response, "request_signature_required")


@pytest.mark.asyncio
async def test_a2a_signed_buyers_do_not_share_task_ownership() -> None:
    other_agent = "https://other-buyer.example/"
    config = _config(
        signer_keys=StaticSignerKeys(
            {AGENT_URL: {"keys": [_PUBLIC_JWK]}, other_agent: {"keys": [_OTHER_JWK]}}
        )
    )
    app = _both_app(_Recording(), config)
    other_key = load_private_key_pem(_OTHER_PEM)
    async with _client(app) as client:
        raw, headers = _signed("http://localhost/", _a2a_send("get_products"))
        sent = await client.post("/", content=raw, headers=headers)
        assert sent.status_code == 200, sent.text
        task_id = sent.json()["result"]["id"]

        get_task = {"jsonrpc": "2.0", "id": "2", "method": "tasks/get", "params": {"id": task_id}}
        raw, headers = _signed(
            "http://localhost/", get_task, private_key=other_key, key_id="stranger-key"
        )
        stolen = await client.post("/", content=raw, headers=headers)
        raw, headers = _signed("http://localhost/", get_task)
        owned = await client.post("/", content=raw, headers=headers)

    assert "result" not in stolen.json(), stolen.text
    assert owned.json()["result"]["id"] == task_id, owned.text


@pytest.mark.asyncio
async def test_a2a_unsigned_protocol_method_required() -> None:
    """Vector 028 end to end: ``tasks/cancel`` in protocol_methods_required_for."""
    vector = json.loads(
        (VECTORS / "negative" / "028-unsigned-protocol-method-required.json").read_text()
    )
    capability = vector["verifier_capability"]
    config = RequestSignatureVerification.from_capability(
        capability, signer_keys=StaticSignerKeys({})
    )
    app = _both_app(_Recording(), config)
    response = await _post(
        app,
        "/",
        vector["request"]["body"].encode(),
        {"content-type": "application/json"},
    )
    _assert_rejected(response, vector["expected_outcome"]["error_code"])


# ---------------------------------------------------------------------------
# Buyer-agent registry dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verified_signer_resolves_through_buyer_agent_registry() -> None:
    seeded = BuyerAgent(agent_url=AGENT_URL, display_name="Buyer", status="active")

    class _Registry:
        async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
            return seeded if agent_url == AGENT_URL else None

        async def resolve_by_credential(self, credential: Any) -> BuyerAgent | None:
            return None

    handler = _Recording()
    app = _both_app(handler, _config())
    raw, headers = _signed("http://localhost/mcp", _tools_call("get_products"), headers=MCP_HEADERS)
    response = await _post(app, "/mcp", raw, headers)
    assert response.status_code == 200, response.text
    context = handler.contexts[-1]
    assert context is not None
    agent = await _resolve_buyer_agent(_Registry(), context.metadata["adcp.auth_info"])
    assert agent.agent_url == AGENT_URL


# ---------------------------------------------------------------------------
# Boot configuration
# ---------------------------------------------------------------------------


def test_inmemory_replay_store_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADCP_ENV", "production")
    monkeypatch.delenv("ADCP_ALLOW_INMEMORY_REPLAY_STORE", raising=False)
    with pytest.raises(ValueError, match="InMemoryReplayStore"):
        check_request_signature_verification(_config())
    monkeypatch.setenv("ADCP_ALLOW_INMEMORY_REPLAY_STORE", "1")
    check_request_signature_verification(_config())


def test_check_rejects_wrong_config_type() -> None:
    with pytest.raises(TypeError, match="RequestSignatureVerification"):
        check_request_signature_verification({"required_for": []})  # type: ignore[arg-type]


def _platform(request_signing: RequestSigning | None) -> Any:
    return SimpleNamespace(capabilities=SimpleNamespace(request_signing=request_signing))


def test_decisioning_serve_builds_config_from_capabilities() -> None:
    from adcp.decisioning.serve import _configure_request_signature_verification

    capability = RequestSigning(
        supported=True,
        required_for=["create_media_buy"],
        supported_for=["create_media_buy"],
    )
    store = InMemoryReplayStore()
    serve_kwargs: dict[str, Any] = {}
    keys = StaticSignerKeys({})
    _configure_request_signature_verification(_platform(capability), keys, store, serve_kwargs)
    config = serve_kwargs["request_signature_verification"]
    assert isinstance(config, RequestSignatureVerification)
    assert config.required_for == {"create_media_buy"}
    assert config.signer_keys is keys
    assert config.replay_store is store


def test_decisioning_serve_requires_declared_signing_for_signer_keys() -> None:
    from adcp.decisioning.serve import _configure_request_signature_verification

    with pytest.raises(ValueError, match="request_signing.supported"):
        _configure_request_signature_verification(_platform(None), StaticSignerKeys({}), None, {})


def test_decisioning_serve_warns_when_required_signatures_are_unenforced() -> None:
    from adcp.decisioning.serve import _configure_request_signature_verification

    capability = RequestSigning(
        supported=True,
        required_for=["create_media_buy"],
        supported_for=["create_media_buy"],
    )
    with pytest.warns(UserWarning, match="not verifying request signatures"):
        _configure_request_signature_verification(_platform(capability), None, None, {})

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _configure_request_signature_verification(
            _platform(capability),
            None,
            None,
            {"request_signature_verification": _config()},
        )
