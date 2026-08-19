"""End-to-end: sign with the client-side API, verify with the FastAPI helper.

Covers both `covers_content_digest: "either"` (digest off) and `"required"`
(digest on) to exercise the full middleware path a real seller would run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
FastAPI = fastapi.FastAPI
Request = fastapi.Request
from fastapi.responses import JSONResponse  # noqa: E402

from adcp.signing import (
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    private_key_from_jwk,
    sign_request,
    unauthorized_response_headers,
    verify_starlette_request,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


def _build_app(covers_content_digest: str) -> FastAPI:
    app = FastAPI()

    jwks_resolver = StaticJwksResolver({"keys": [ED25519_KEY]})

    @app.post("/adcp/create_media_buy")
    async def create_media_buy(request: Request) -> Any:
        options = VerifyOptions(
            now=float(int(time.time())),
            capability=VerifierCapability(
                covers_content_digest=covers_content_digest,  # type: ignore[arg-type]
                required_for=frozenset({"create_media_buy"}),
            ),
            operation="create_media_buy",
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
        return {"verified_key_id": signer.key_id, "alg": signer.alg}

    return app


@pytest.mark.parametrize("policy,cover_digest", [("either", False), ("required", True)])
async def test_signed_request_verifies_end_to_end(policy: str, cover_digest: bool) -> None:
    app = _build_app(policy)
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")

    body = json.dumps({"plan_id": "plan_001"}).encode("utf-8")
    url = "http://test/adcp/create_media_buy"
    headers = {"Content-Type": "application/json"}

    signed = sign_request(
        method="POST",
        url=url,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        cover_content_digest=cover_digest,
        signing_profile_version="3.2",
    )
    request_headers = {**headers, **signed.as_dict()}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/adcp/create_media_buy", content=body, headers=request_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["verified_key_id"] == "test-ed25519-2026"


async def test_unsigned_request_rejected_with_401_and_www_authenticate() -> None:
    app = _build_app("either")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/adcp/create_media_buy",
            json={"plan_id": "x"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "request_signature_required"
    www_auth = resp.headers.get("www-authenticate", "")
    assert 'Signature error="request_signature_required"' in www_auth
    assert "realm" not in www_auth.lower()


async def test_tampered_body_fails_digest_when_required() -> None:
    app = _build_app("required")
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")

    body = b'{"plan_id":"plan_001"}'
    url = "http://test/adcp/create_media_buy"

    signed = sign_request(
        method="POST",
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id="test-ed25519-2026",
        alg="ed25519",
        cover_content_digest=True,
        signing_profile_version="3.2",
    )
    tampered_body = b'{"plan_id":"plan_TAMPERED"}'
    request_headers = {"Content-Type": "application/json", **signed.as_dict()}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/adcp/create_media_buy", content=tampered_body, headers=request_headers
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "request_signature_digest_mismatch"
