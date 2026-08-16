"""End-to-end smoke test: auto-sign hook through real httpx to a verifier.

The unit tests in ``test_autosign_hook.py`` call
``ADCPClient._sign_outgoing_request`` directly against a synthetic
``httpx.Request``. This file pins the layer above: httpx actually invokes
our request event hook before writing bytes, and the bytes it writes are
accepted by a live RFC 9421 verifier behind an ASGI app. If the hook
ever stops firing on the real wire path (e.g., an upstream httpx change
that drops event hooks in a code path we care about), these tests catch
it.

Uses Starlette directly (not FastAPI) so the test runs without the
optional FastAPI dependency — Starlette is already a transitive dep of
the project's core SDKs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.client import ADCPClient
from adcp.signing import (
    SignatureVerificationError,
    SigningConfig,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    private_key_from_jwk,
    unauthorized_response_headers,
    verify_starlette_request,
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


def _build_verifier_app(
    *,
    covers: str,
    required_for: frozenset[str],
) -> Starlette:
    jwks_resolver = StaticJwksResolver({"keys": [ED25519_KEY]})

    async def verify(request: Request) -> JSONResponse:
        operation = request.path_params["operation"]
        options = VerifyOptions(
            now=float(int(time.time())),
            capability=VerifierCapability(
                covers_content_digest=covers,  # type: ignore[arg-type]
                required_for=required_for,
            ),
            operation=operation,
            jwks_resolver=jwks_resolver,
        )
        try:
            signer = await verify_starlette_request(request, options=options)
        except SignatureVerificationError as exc:
            return JSONResponse(
                {"error": exc.code, "message": str(exc)},
                status_code=401,
                headers=unauthorized_response_headers(exc),
            )
        return JSONResponse({"verified_key_id": signer.key_id, "alg": signer.alg})

    return Starlette(routes=[Route("/adcp/{operation}", verify, methods=["POST"])])


def _make_caps(
    *,
    required: list[str] | None = None,
    supported_for: list[str] | None = None,
    covers: CoversContentDigest = CoversContentDigest.either,
) -> GetAdcpCapabilitiesResponse:
    return GetAdcpCapabilitiesResponse(
        adcp=Adcp(
            major_versions=[MajorVersion(root=3)],
            idempotency=Idempotency(supported=True, replay_ttl_seconds=86400),
        ),
        supported_protocols=[SupportedProtocol.media_buy],
        request_signing=RequestSigning(
            supported=True,
            covers_content_digest=covers,
            required_for=required or [],
            warn_for=[],
            supported_for=supported_for,
        ),
    )


@pytest.fixture()
def signing_config() -> SigningConfig:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    return SigningConfig(private_key=private_key, key_id=ED25519_KEY["kid"])


@pytest.fixture()
def signing_client(signing_config: SigningConfig) -> ADCPClient:
    agent = AgentConfig(
        id="smoke-seller",
        agent_uri="https://verifier.test",
        protocol=Protocol.A2A,
    )
    return ADCPClient(agent, signing=signing_config)


@pytest.mark.parametrize(
    ("covers", "required", "operation"),
    [
        (CoversContentDigest.either, ["create_media_buy"], "create_media_buy"),
        (CoversContentDigest.required, ["create_media_buy"], "create_media_buy"),
        (CoversContentDigest.forbidden, ["create_media_buy"], "create_media_buy"),
    ],
)
async def test_hook_on_real_httpx_round_trip_accepted_by_verifier(
    signing_client: ADCPClient,
    covers: CoversContentDigest,
    required: list[str],
    operation: str,
) -> None:
    """With the hook installed on a real AsyncClient, the server verifies the request.

    This is the load-bearing contract: httpx runs the hook, the hook
    mutates the request headers, those bytes reach the server, and the
    server's RFC 9421 verifier accepts them.
    """
    signing_client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=required, covers=covers)
    )
    verifier_covers = covers.value if covers is not None else "either"
    app = _build_verifier_app(
        covers=verifier_covers,
        required_for=frozenset(required),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://verifier.test",
        event_hooks={"request": [signing_client._sign_outgoing_request]},
    ) as client:
        token = current_operation.set(operation)
        try:
            resp = await client.post(
                f"/adcp/{operation}",
                content=b'{"plan_id":"p1"}',
                headers={"Content-Type": "application/json"},
            )
        finally:
            current_operation.reset(token)

    assert resp.status_code == 200, resp.text
    assert resp.json()["verified_key_id"] == "test-ed25519-2026"


async def test_hook_skips_when_context_var_unset_server_rejects(
    signing_client: ADCPClient,
) -> None:
    """With ContextVar unset (simulating an out-of-band call like agent-card
    fetch), the hook does nothing — the server gets an unsigned request and
    rejects with request_signature_required."""
    signing_client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["create_media_buy"])
    )
    app = _build_verifier_app(covers="either", required_for=frozenset({"create_media_buy"}))
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://verifier.test",
        event_hooks={"request": [signing_client._sign_outgoing_request]},
    ) as client:
        # No current_operation.set — the hook will see None and skip.
        resp = await client.post(
            "/adcp/create_media_buy",
            content=b'{"plan_id":"p1"}',
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 401
    assert resp.json()["error"] == "request_signature_required"


async def test_hook_skips_get_adcp_capabilities_server_rejects_if_listed(
    signing_client: ADCPClient,
) -> None:
    """Bootstrap carve-out: get_adcp_capabilities is never signed even if the
    seller pathologically listed it in required_for. The server would
    reject in that case — which proves the carve-out is operating (the
    client is NOT sending a signature, even though it has capabilities
    advertising get_adcp_capabilities as required)."""
    signing_client.fetch_capabilities = AsyncMock(  # type: ignore[method-assign]
        return_value=_make_caps(required=["get_adcp_capabilities"])
    )
    app = _build_verifier_app(
        covers="either",
        required_for=frozenset({"get_adcp_capabilities"}),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://verifier.test",
        event_hooks={"request": [signing_client._sign_outgoing_request]},
    ) as client:
        token = current_operation.set("get_adcp_capabilities")
        try:
            resp = await client.post(
                "/adcp/get_adcp_capabilities",
                content=b"{}",
                headers={"Content-Type": "application/json"},
            )
        finally:
            current_operation.reset(token)

    # Server sees no signature → 401. Test confirms the bootstrap carve-out
    # short-circuited before signing (otherwise server would have accepted).
    assert resp.status_code == 401
    assert resp.json()["error"] == "request_signature_required"
