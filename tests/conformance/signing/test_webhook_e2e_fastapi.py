"""End-to-end: sign a webhook on the sender side, POST it over ASGI to a
FastAPI receiver, verify the full stack verifies + dedupes + parses.

Catches bugs the in-process receiver tests miss — URL reconstruction through
the ASGI layer, Content-Type preservation, body-byte identity, real
retry-then-duplicate, and the legacy-HMAC fallback across an actual HTTP
boundary.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import httpx
import pytest

fastapi = pytest.importorskip("fastapi")
FastAPI = fastapi.FastAPI
Request = fastapi.Request
from fastapi.responses import JSONResponse  # noqa: E402

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import StaticJwksResolver, private_key_from_jwk
from adcp.webhooks import (
    LegacyHmacFallback,
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    create_mcp_webhook_payload,
    sign_legacy_webhook,
    sign_webhook,
    to_wire_dict,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
REQUEST_ED25519 = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")
WEBHOOK_ED25519 = {
    **copy.deepcopy(REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}


def _build_app(*, legacy_hmac: LegacyHmacFallback | None = None) -> tuple[FastAPI, WebhookReceiver]:
    """Build a FastAPI app that mounts a WebhookReceiver at /webhooks/adcp.

    Returns (app, receiver) so tests can inspect dedup state across requests.
    """
    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
            legacy_hmac=legacy_hmac,
        ),
    )

    app = FastAPI()

    @app.post("/webhooks/adcp")
    async def webhook_endpoint(request: Request) -> JSONResponse:
        outcome = await receiver.receive(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            body=await request.body(),
        )
        if outcome.http_status is None:
            outcome = await receiver.acknowledge(outcome)
        if outcome.rejected:
            return JSONResponse(
                {"error": outcome.rejection_reason},
                status_code=outcome.http_status or 400,
                headers=dict(outcome.response_headers),
            )
        return JSONResponse(
            {
                "duplicate": outcome.duplicate,
                "task_id": outcome.payload.task_id if outcome.payload else None,
                "sender": outcome.sender_identity,
            },
            status_code=outcome.http_status or 200,
        )

    return app, receiver


def _sign_and_send_body(body: bytes) -> dict[str, str]:
    """Sign the exact bytes we'll post. The signature binds to these bytes —
    if httpx reserializes on the wire, the test catches it."""
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    signed = sign_webhook(
        method="POST",
        url="http://test/webhooks/adcp",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
    )
    return {"Content-Type": "application/json", **signed.as_dict()}


@pytest.mark.asyncio
async def test_signed_webhook_verifies_end_to_end() -> None:
    app, _ = _build_app()

    payload = create_mcp_webhook_payload(
        task_id="task_e2e",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_e2e_firstaaaaaaaaaaaaaa",
        result={"media_buy_id": "mb_1"},
    )
    body = json.dumps(to_wire_dict(payload)).encode("utf-8")
    headers = _sign_and_send_body(body)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/webhooks/adcp", content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "duplicate": False,
        "task_id": "task_e2e",
        "sender": "test-webhook-ed25519-2026",
    }


@pytest.mark.asyncio
async def test_duplicate_retry_dedupes_over_real_http() -> None:
    """The sender's retry MUST get a 200 with duplicate=True, and the
    receiver MUST NOT reprocess. Real HTTP roundtrip twice."""
    app, _ = _build_app()

    payload = create_mcp_webhook_payload(
        task_id="task_dup",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_e2e_dupeaaaaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload)).encode("utf-8")
    headers = _sign_and_send_body(body)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/webhooks/adcp", content=body, headers=headers)
        second = await client.post("/webhooks/adcp", content=body, headers=headers)

    assert first.status_code == 200
    assert first.json()["duplicate"] is False

    # Spec requires 2xx on duplicate, not 409. The sender interprets any
    # non-2xx as a delivery failure and retries, which is exactly the
    # feedback loop dedup exists to prevent.
    assert second.status_code == 200
    assert second.json()["duplicate"] is True


@pytest.mark.asyncio
async def test_unsigned_webhook_rejected_with_www_authenticate_over_http() -> None:
    app, _ = _build_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/adcp",
            json={
                "idempotency_key": "whk_e2e_unsignedaaaaaaa",
                "task_id": "t",
                "task_type": "create_media_buy",
                "status": "completed",
                "timestamp": "2026-04-19T00:00:00Z",
            },
        )

    assert resp.status_code == 401
    assert resp.json() == {"error": "signature_missing"}
    www_auth = resp.headers.get("www-authenticate", "")
    assert 'Signature error="webhook_signature_required"' in www_auth


@pytest.mark.asyncio
async def test_tampered_body_rejected_over_http() -> None:
    """Body tampered AFTER signing — signature binds to body bytes via
    content-digest. The receiver's signature verification MUST catch this."""
    app, _ = _build_app()

    payload = create_mcp_webhook_payload(
        task_id="task_tamper",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_e2e_tamperaaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload)).encode("utf-8")
    headers = _sign_and_send_body(body)
    tampered = body.replace(b"completed", b"failedAAA")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/webhooks/adcp", content=tampered, headers=headers)

    assert resp.status_code == 401
    assert resp.json()["error"] == "signature_invalid"


@pytest.mark.asyncio
async def test_httpx_json_kwarg_breaks_signature_by_reserialization() -> None:
    """Documents the foot-gun: posting with `json=payload` reserializes the
    dict (potentially reordering keys and losing byte identity), breaking the
    signature. Users must post with `content=body_bytes`, not `json=payload`.

    This test exists so if httpx ever changes its JSON serialization to match
    ours byte-for-byte, we notice — today it does NOT, and this is the
    canonical reason `sign_webhook` documents `body=` as raw bytes.
    """
    app, _ = _build_app()

    payload = {
        # Keys in an order that triggers reserialization differences from the
        # sender's own json.dumps. If this happens to line up, the test still
        # passes the assertion — we're documenting the hazard, not forcing it.
        "zzz_extra_field": "triggers-key-reorder",
        "idempotency_key": "whk_e2e_jsonkwaaaaaaaaaaa",
        "task_id": "t",
        "task_type": "create_media_buy",
        "status": "completed",
        "timestamp": "2026-04-19T00:00:00Z",
    }
    # Sign against our serialization
    body = json.dumps(to_wire_dict(payload)).encode("utf-8")
    headers = _sign_and_send_body(body)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Intentionally post via json= — httpx reserializes. The receiver's
        # content-digest check binds to our originally-signed bytes, which
        # httpx's re-serialized body won't match.
        resp = await client.post(
            "/webhooks/adcp",
            json=payload,
            headers={
                "Signature": headers["Signature"],
                "Signature-Input": headers["Signature-Input"],
                "Content-Digest": headers["Content-Digest"],
            },
        )

    # Either outcome is acceptable in theory, but in practice httpx's
    # json.dumps differs from ours (separators, ensure_ascii), so the digest
    # won't match. If a future httpx aligns byte-for-byte, this assertion can
    # flip — the goal is to surface the divergence, not mandate it.
    assert resp.status_code == 401
    assert "digest_mismatch" in resp.json()["error"] or "signature_invalid" in resp.json()["error"]


@pytest.mark.asyncio
async def test_legacy_hmac_e2e_over_http() -> None:
    """HMAC fallback path exercised over real HTTP — critical for 3.x
    migrators whose publishers still use the HMAC scheme."""
    secret = b"shared-webhook-secret-at-least-32b"
    fallback = LegacyHmacFallback.from_shared_secret(
        secret=secret,
        sender_identity="publisher-legacy",
    )
    app, _ = _build_app(legacy_hmac=fallback)

    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="task_hmac",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_e2e_hmaclegacyaaaaaaa",
    )
    # Use sign_legacy_webhook so body and signed bytes are guaranteed to match
    # (avoids the spaced-vs-compact json.dumps separator drift that was the
    # whole reason we added the paired-return helper in R8).
    signed_headers, body = sign_legacy_webhook(secret.decode(), payload, timestamp=ts)
    headers = {"Content-Type": "application/json", **signed_headers}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/webhooks/adcp", content=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "duplicate": False,
        "task_id": "task_hmac",
        "sender": "publisher-legacy",
    }
