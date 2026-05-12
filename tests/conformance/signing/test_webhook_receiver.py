"""End-to-end tests for WebhookReceiver.

Covers the full flow: verify 9421 (or HMAC fallback) → dedupe → parse typed
payload. Uses a real signer on the sender side so the test catches any
sender/receiver wire-format divergence.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import StaticJwksResolver, private_key_from_jwk
from adcp.types.generated_poc.core.mcp_webhook_payload import McpWebhookPayload
from adcp.webhooks import (
    LegacyHmacFallback,
    LegacyWebhookHmacOptions,
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    create_mcp_webhook_payload,
    get_adcp_signed_headers_for_webhook,
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

URL = "https://buyer.example.com/webhooks/adcp"


def _build_receiver(
    *,
    legacy_hmac: LegacyHmacFallback | None = None,
    kind: str = "mcp",
) -> WebhookReceiver:
    return WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            legacy_hmac=legacy_hmac,
            kind=kind,  # type: ignore[arg-type]
        ),
    )


def _sign_webhook(body: bytes) -> dict[str, str]:
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
    )
    return {"Content-Type": "application/json", **signed.as_dict()}


@pytest.mark.asyncio
async def test_happy_path_9421() -> None:
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_aaaaaaaaaaaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)

    receiver = _build_receiver()
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)

    assert outcome.rejected is False
    assert outcome.duplicate is False
    assert outcome.sender_identity == "test-webhook-ed25519-2026"
    assert isinstance(outcome.payload, McpWebhookPayload)
    assert outcome.payload.idempotency_key == "whk_aaaaaaaaaaaaaaaaaaaaaa"
    assert outcome.payload.task_id == "t1"


@pytest.mark.asyncio
async def test_duplicate_detected() -> None:
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_dupeaaaaaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)
    receiver = _build_receiver()

    first = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    second = await receiver.receive(method="POST", url=URL, headers=headers, body=body)

    assert first.duplicate is False
    assert second.duplicate is True
    # Payload still parsed on duplicate — caller gets the full event for logging.
    assert second.payload is not None
    assert second.rejected is False


@pytest.mark.asyncio
async def test_missing_signature_rejects_with_www_authenticate() -> None:
    body = json.dumps(
        {
            "idempotency_key": "whk_xaaaaaaaaaaaaaa",
            "task_id": "t1",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2026-04-19T00:00:00Z",
        }
    ).encode("utf-8")
    receiver = _build_receiver()

    outcome = await receiver.receive(
        method="POST", url=URL, headers={"Content-Type": "application/json"}, body=body
    )

    assert outcome.rejected is True
    assert outcome.rejection_reason == "signature_missing"
    assert outcome.response_headers.get("WWW-Authenticate", "").startswith('Signature error="')


@pytest.mark.asyncio
async def test_tampered_body_rejected() -> None:
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_tamperaaaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)
    receiver = _build_receiver()

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body + b" ")
    assert outcome.rejected is True
    assert outcome.rejection_reason == "signature_invalid"


@pytest.mark.asyncio
async def test_missing_idempotency_key_rejected() -> None:
    """Spec 3.0-rc: idempotency_key is REQUIRED on every webhook payload."""
    body_dict = {
        "task_id": "t1",
        "task_type": "create_media_buy",
        "status": "completed",
        "timestamp": "2026-04-19T00:00:00Z",
    }
    body = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)
    receiver = _build_receiver()

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is True
    assert outcome.rejection_reason == "idempotency_key_missing"


@pytest.mark.asyncio
async def test_legacy_hmac_fallback_when_9421_absent() -> None:
    """HMAC accepted when no 9421 headers are present (opt-in fallback)."""
    secret = "s" * 32
    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_hmaclegacyaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    get_adcp_signed_headers_for_webhook(
        headers=headers, secret=secret, timestamp=ts, payload=payload
    )

    fallback = LegacyHmacFallback(
        options_for=lambda _hdrs: LegacyWebhookHmacOptions(
            secret=secret.encode(),
            sender_identity="buyer-legacy",
            now=float(int(ts)),
        ),
        only_when_9421_absent=True,
    )
    receiver = _build_receiver(legacy_hmac=fallback)

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.sender_identity == "buyer-legacy"


@pytest.mark.asyncio
async def test_from_shared_secret_shortcut() -> None:
    """The one-line shortcut constructor works for the common case."""
    secret = "s" * 32
    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_sharedshortcutaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    get_adcp_signed_headers_for_webhook(
        headers=headers, secret=secret, timestamp=ts, payload=payload
    )

    fallback = LegacyHmacFallback.from_shared_secret(
        secret=secret.encode(),
        sender_identity="buyer-shortcut",
    )
    receiver = _build_receiver(legacy_hmac=fallback)

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.sender_identity == "buyer-shortcut"


@pytest.mark.asyncio
async def test_downgrade_guard_rejects_forged_hmac_when_9421_present_but_invalid() -> None:
    """MITM strips valid 9421, substitutes forged HMAC — default settings reject."""
    body = json.dumps(
        {
            "idempotency_key": "whk_downgradeaaaaaaaa",
            "task_id": "t1",
            "task_type": "create_media_buy",
            "status": "completed",
            "timestamp": "2026-04-19T00:00:00Z",
        }
    ).encode("utf-8")
    # Present BOTH an invalid 9421 Signature-Input and a valid HMAC — downgrade
    # guard (only_when_9421_absent=True) means HMAC is never consulted.
    ts = str(int(time.time()))
    sig_input = (
        'sig1=("@method" "@target-uri" "@authority")'
        ';created=100;expires=400;nonce="n";keyid="nope"'
        ';alg="ed25519";tag="adcp/webhook-signing/v1"'
    )
    headers = {
        "Content-Type": "application/json",
        "Signature-Input": sig_input,
        "Signature": "sig1=:AAAA:",
    }
    # Compute a VALID HMAC so only the 9421 signature is bad.
    get_adcp_signed_headers_for_webhook(
        headers=headers, secret="s" * 32, timestamp=ts, payload=json.loads(body)
    )
    fallback = LegacyHmacFallback(
        options_for=lambda _hdrs: LegacyWebhookHmacOptions(
            secret=b"s" * 32,
            sender_identity="legacy",
            now=float(int(ts)),
        ),
        only_when_9421_absent=True,  # Default — the guard
    )
    receiver = _build_receiver(legacy_hmac=fallback)

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is True
    assert outcome.rejection_reason == "signature_invalid"


@pytest.mark.asyncio
async def test_invalid_json_body_rejected() -> None:
    body = b"not-json"
    headers = _sign_webhook(body)
    receiver = _build_receiver()

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is True
    assert outcome.rejection_reason == "body_invalid_json"


@pytest.mark.asyncio
async def test_rejects_non_json_content_type() -> None:
    body = b'{"idempotency_key":"whk_fakeaaaaaaaaaaaaa","task_id":"t1"}'
    receiver = _build_receiver()
    # Missing content-type short-circuits before verify.
    outcome = await receiver.receive(method="POST", url=URL, headers={}, body=body)
    assert outcome.rejected is True
    assert outcome.rejection_reason == "content_type_invalid"


@pytest.mark.asyncio
async def test_accepts_content_type_with_charset() -> None:
    """Receiver must accept `application/json; charset=utf-8`, not just bare."""
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_charsetaaaaaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    # Sign with plain content-type (what sign_webhook emits) then mutate the
    # header on the wire to include a charset parameter. The 9421 signature
    # covers content-type; this test asserts the receiver's content-type
    # check (pre-verify) tolerates the common charset suffix.
    headers = _sign_webhook(body)
    # Keep the signed content-type value intact; just confirm tolerating
    # `application/json; charset=utf-8` would pass _content_type_is_json.
    # (Can't mutate without breaking the signature — so test the parser
    # directly via _content_type_is_json at the unit level instead.)
    from adcp.webhook_receiver import _content_type_is_json

    assert _content_type_is_json({"Content-Type": "application/json; charset=utf-8"})
    assert _content_type_is_json({"content-type": "APPLICATION/JSON"})
    assert not _content_type_is_json({"content-type": "text/plain"})

    # Round-trip with bare content-type to ensure the happy path still works.
    receiver = _build_receiver()
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False


@pytest.mark.asyncio
async def test_rejects_malformed_idempotency_key() -> None:
    """Spec regex ^[A-Za-z0-9_.:-]{16,255}$ — short keys and exotic chars reject."""
    # Too short
    payload = {
        "idempotency_key": "short",
        "task_id": "t1",
        "task_type": "create_media_buy",
        "status": "completed",
        "timestamp": "2026-04-19T00:00:00Z",
    }
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)
    receiver = _build_receiver()

    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is True
    assert outcome.rejection_reason == "idempotency_key_invalid"


@pytest.mark.asyncio
async def test_only_signature_input_header_falls_through_to_hmac_when_configured() -> None:
    """If a sender provides only Signature-Input (no Signature), the receiver
    with HMAC fallback should NOT commit to the 9421 path — it should fall
    through to HMAC. Otherwise malformed single-header attempts DoS the HMAC
    migration path."""
    secret = "s" * 32
    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_onlyfallbackaaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    get_adcp_signed_headers_for_webhook(
        headers=headers, secret=secret, timestamp=ts, payload=payload
    )
    # Add a lone Signature-Input (no Signature). The receiver MUST still fall
    # through to HMAC because both-present is required to commit to 9421.
    headers["Signature-Input"] = 'sig1=("@method")'

    fallback = LegacyHmacFallback(
        options_for=lambda _h: LegacyWebhookHmacOptions(
            secret=secret.encode(),
            sender_identity="buyer-legacy",
            now=float(int(ts)),
        ),
        only_when_9421_absent=True,
    )
    receiver = _build_receiver(legacy_hmac=fallback)
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.sender_identity == "buyer-legacy"


@pytest.mark.asyncio
async def test_hmac_fallback_declined_when_options_for_returns_none() -> None:
    """options_for returning None is a receiver declining this particular
    sender — the request must reject, not silently succeed or crash."""
    body = b'{"idempotency_key":"whk_xxxxxxxxxxxxxxxx","task_id":"t1"}'
    fallback = LegacyHmacFallback(
        options_for=lambda _h: None,
        only_when_9421_absent=True,
    )
    receiver = _build_receiver(legacy_hmac=fallback)

    outcome = await receiver.receive(
        method="POST",
        url=URL,
        headers={"Content-Type": "application/json"},
        body=body,
    )
    assert outcome.rejected is True
    assert outcome.rejection_reason == "signature_missing"


@pytest.mark.asyncio
async def test_hmac_fallback_accepts_invalid_9421_when_opted_in() -> None:
    """only_when_9421_absent=False lets HMAC verify even when a 9421 attempt
    failed — opt-in for known-homogenous sender cohorts."""
    secret = "s" * 32
    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        status="completed",  # type: ignore[arg-type]
        idempotency_key="whk_optinfallbackaaaaaaa",
    )
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    get_adcp_signed_headers_for_webhook(
        headers=headers, secret=secret, timestamp=ts, payload=payload
    )
    # Add an invalid 9421 signature; HMAC is valid.
    headers["Signature-Input"] = (
        'sig1=("@method" "@target-uri" "@authority")'
        ';created=100;expires=400;nonce="n";keyid="nope"'
        ';alg="ed25519";tag="adcp/webhook-signing/v1"'
    )
    headers["Signature"] = "sig1=:AAAA:"

    fallback = LegacyHmacFallback(
        options_for=lambda _h: LegacyWebhookHmacOptions(
            secret=secret.encode(),
            sender_identity="buyer-legacy",
            now=float(int(ts)),
        ),
        only_when_9421_absent=False,  # Opt-in: try HMAC on 9421 failure
    )
    receiver = _build_receiver(legacy_hmac=fallback)
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.sender_identity == "buyer-legacy"


# ---- All-kinds parse coverage ---------------------------------------------


@pytest.mark.asyncio
async def test_receives_revocation_notification() -> None:
    payload = {
        "idempotency_key": "whk_revaaaaaaaaaaaaaaaa",
        "rights_id": "rights_1",
        "brand_id": "brand_1",
        "reason": "Rights revoked",
        "effective_at": "2026-04-19T00:00:00Z",
    }
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)

    receiver = _build_receiver(kind="revocation_notification")
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.payload is not None
    assert outcome.payload.idempotency_key == "whk_revaaaaaaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_receives_artifact_webhook() -> None:
    payload = {
        "idempotency_key": "whk_artaaaaaaaaaaaaaaaa",
        "media_buy_id": "mb_1",
        "batch_id": "batch_1",
        "timestamp": "2026-04-19T00:00:00Z",
        "artifacts": [],
    }
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)

    receiver = _build_receiver(kind="artifact")
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.payload is not None


@pytest.mark.asyncio
async def test_receives_collection_list_changed() -> None:
    payload = {
        "idempotency_key": "whk_colaaaaaaaaaaaaaaaa",
        "event": "collection_list_changed",
        "list_id": "cl_1",
        "resolved_at": "2026-04-19T00:00:00Z",
        "signature": "sig",
    }
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)

    receiver = _build_receiver(kind="collection_list_changed")
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.payload is not None


@pytest.mark.asyncio
async def test_receives_property_list_changed() -> None:
    payload = {
        "idempotency_key": "whk_propaaaaaaaaaaaaaaa",
        "event": "property_list_changed",
        "list_id": "pl_1",
        "resolved_at": "2026-04-19T00:00:00Z",
        "signature": "sig",
    }
    body = json.dumps(to_wire_dict(payload), separators=(",", ":")).encode("utf-8")
    headers = _sign_webhook(body)

    receiver = _build_receiver(kind="property_list_changed")
    outcome = await receiver.receive(method="POST", url=URL, headers=headers, body=body)
    assert outcome.rejected is False
    assert outcome.payload is not None
