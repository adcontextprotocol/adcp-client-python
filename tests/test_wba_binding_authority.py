"""Binding authorization stays fresh independently of transport key discovery."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from starlette.responses import JSONResponse

from adcp.server.signed_requests import (
    RequestSignatureVerification,
    SignedRequestVerificationMiddleware,
)
from adcp.signing import (
    OriginBindings,
    StaticSignerKeys,
    TrustRecordEvaluator,
    TrustResponse,
    VerifiedSigner,
    public_directory_key,
    sign_request,
    sign_wba_request,
)
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY

ORIGIN = "https://buyer.platform.com"
TARGET = "https://seller.com/mcp"


async def not_revoked(origin, keyid):
    return False


@pytest.mark.parametrize("preverified", [False, True])
@pytest.mark.parametrize("failure", ["delisted", "offline"])
async def test_bound_identity_loses_authority_on_refresh_without_legacy_downgrade(
    preverified, failure
):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = {
        **public_directory_key(private),
        "alg": "EdDSA",
        "adcp_use": "request-signing",
        "key_ops": ["verify"],
    }
    now, mode, fetches, dispatched = [1000], ["valid"], [], []

    async def fetch(url, *, allow_private=False):
        fetches.append(url)
        if mode[0] == "offline":
            raise OSError("offline")
        agents = [{"url": ORIGIN, "roles": ["adcp:buying"]}] if mode[0] == "valid" else []
        return TrustResponse(json.dumps({"agents": agents}).encode())

    records = TrustRecordEvaluator(fetcher=fetch, clock=lambda: now[0])
    bindings = OriginBindings()
    await records.bind(
        VerifiedSigner(
            jwk["kid"], "ed25519", "sig1", time.time(), agent_url=ORIGIN, profile="web-bot-auth"
        ),
        principal_id="principal-1",
        bindings=bindings,
    )
    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        origin_bindings=bindings,
        trust_records=records,
        wba_revocation_checker=not_revoked,
        signing_profile_version="3.2",
    )

    async def dispatch(scope, receive, send):
        dispatched.append(True)
        await JSONResponse({"ok": True})(scope, receive, send)

    middleware = SignedRequestVerificationMiddleware(dispatch, config, transport="mcp")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "sync_accounts", "arguments": {}},
    }
    body = json.dumps(payload).encode()

    async def app(scope, receive, send):
        if preverified:
            scope[VERIFIED_SIGNER_SCOPE_KEY] = VerifiedSigner(
                jwk["kid"],
                "ed25519",
                "sig1",
                time.time(),
                agent_url=ORIGIN,
                profile="web-bot-auth",
                parsed_body=payload,
            )
        await middleware(scope, receive, send)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:

        async def post():
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url=TARGET,
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=ORIGIN,
                )
            )
            return await client.post(TARGET, content=body, headers=headers)

        assert (await post()).status_code == 200
        mode[0] = failure
        now[0] += 61
        response = await post()
        assert response.status_code == 403
        assert response.json()["errors"][0]["code"] == "PERMISSION_DENIED"
        assert len(dispatched) == 1
        assert len(fetches) == 2
        assert await bindings(ORIGIN) == "principal-1"

        # Historical approval must still prevent transport downgrade.
        headers = {"Content-Type": "application/json"}
        signed = sign_request(
            method="POST",
            url=TARGET,
            headers=headers,
            body=body,
            private_key=private,
            key_id=jwk["kid"],
            alg="ed25519",
            signing_profile_version="3.2",
        )
        legacy = SignedRequestVerificationMiddleware(dispatch, config, transport="mcp")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(legacy)) as legacy_client:
            assert (
                await legacy_client.post(
                    TARGET, content=body, headers={**headers, **signed.as_dict()}
                )
            ).status_code == 403
        assert len(dispatched) == 1


@pytest.mark.parametrize("operation", ["get_products", "list_creative_formats", "sync_principal"])
async def test_unbound_policy_can_expose_anonymous_reads_and_explicit_open_onboarding(operation):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)
    dispatched = []

    async def dispatch(scope, receive, send):
        dispatched.append(scope["adcp.server.signature_verified"])
        await JSONResponse({"ok": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        wba_revocation_checker=not_revoked,
        unbound_for=frozenset({operation}),
    )
    app = SignedRequestVerificationMiddleware(dispatch, config, transport="mcp")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        for op, status in ((operation, 200), ("create_media_buy", 403)):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": op, "arguments": {}},
                }
            ).encode()
            headers = {"Content-Type": "application/json"}
            headers.update(
                sign_wba_request(
                    method="POST",
                    url=TARGET,
                    headers=headers,
                    body=body,
                    private_key=private,
                    agent_origin=ORIGIN,
                )
            )
            assert (await client.post(TARGET, content=body, headers=headers)).status_code == status
    assert dispatched == [True]


@pytest.mark.parametrize("method", ["initialize", "tools/list"])
async def test_unbound_policy_can_include_public_protocol_methods(method):
    private = ed25519.Ed25519PrivateKey.generate()
    jwk = public_directory_key(private)

    async def dispatch(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)

    config = RequestSignatureVerification(
        signer_keys=StaticSignerKeys({ORIGIN: {"keys": [jwk]}}),
        enable_web_bot_auth=True,
        wba_revocation_checker=not_revoked,
        unbound_for=frozenset({method}),
    )
    app = SignedRequestVerificationMiddleware(dispatch, config, transport="mcp")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
    headers = {"Content-Type": "application/json"}
    headers.update(
        sign_wba_request(
            method="POST",
            url=TARGET,
            headers=headers,
            body=body,
            private_key=private,
            agent_origin=ORIGIN,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client:
        assert (await client.post(TARGET, content=body, headers=headers)).status_code == 200
