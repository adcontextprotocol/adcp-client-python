"""Round-trip tests for the webhook signer + verifier.

Sign a webhook POST, verify it goes through. Then verify the distinguishing
webhook-profile checks — cross-tag rejection, adcp_use binding, required
content-digest — each in isolation.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from adcp.signing import (
    DEFAULT_TAG,
    StaticJwksResolver,
    private_key_from_jwk,
    sign_request,
)
from adcp.signing.errors import (
    WEBHOOK_SIGNATURE_KEY_PURPOSE_INVALID,
    WEBHOOK_SIGNATURE_REPLAYED,
    WEBHOOK_SIGNATURE_REQUIRED,
    WEBHOOK_SIGNATURE_TAG_INVALID,
    SignatureVerificationError,
)
from adcp.webhooks import (
    WebhookVerifyOptions,
    sign_webhook,
    verify_webhook_signature,
)

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
REQUEST_ED25519 = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")

# Webhook-signing key: same crypto, different adcp_use + kid. Cloning is the
# cheapest way to get a webhook-profile JWK into tests without minting a new
# private key alongside the existing request-signing test vectors.
WEBHOOK_ED25519 = {
    **copy.deepcopy(REQUEST_ED25519),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}


def _webhook_verify_options(keys: list[dict]) -> WebhookVerifyOptions:
    return WebhookVerifyOptions(
        jwks_resolver=StaticJwksResolver({"keys": keys}),
    )


def _sign_and_headers(
    body: bytes,
    *,
    url: str = "https://buyer.example.com/webhooks/adcp",
    method: str = "POST",
    key: dict = WEBHOOK_ED25519,
) -> dict[str, str]:
    private_key = private_key_from_jwk(key, d_field="_private_d_for_test_only")
    signed = sign_webhook(
        method=method,
        url=url,
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=key["kid"],
        alg="ed25519",
    )
    return {"Content-Type": "application/json", **signed.as_dict()}


def test_sign_then_verify_roundtrip() -> None:
    body = b'{"idempotency_key":"whk_abc123","task_id":"t1"}'
    headers = _sign_and_headers(body)

    result = verify_webhook_signature(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers=headers,
        body=body,
        options=_webhook_verify_options([WEBHOOK_ED25519]),
    )
    assert result.key_id == "test-webhook-ed25519-2026"
    assert result.alg == "ed25519"


def test_default_replay_store_rejects_captured_signature() -> None:
    body = b'{"idempotency_key":"whk_replay","task_id":"t1"}'
    headers = _sign_and_headers(body)
    options = _webhook_verify_options([WEBHOOK_ED25519])

    verify_webhook_signature(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers=headers,
        body=body,
        options=options,
    )
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers=headers,
            body=body,
            options=options,
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_REPLAYED


def test_explicit_none_opts_out_of_signature_replay_check() -> None:
    body = b'{"idempotency_key":"whk_external_dedup","task_id":"t1"}'
    headers = _sign_and_headers(body)
    options = WebhookVerifyOptions(
        jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_ED25519]}),
        replay_store=None,
    )

    for _ in range(2):
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers=headers,
            body=body,
            options=options,
        )


def test_accepts_request_signing_key_for_webhook_profile() -> None:
    """Current request-signing JWKs MUST verify with the webhook tag."""
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    private_key = private_key_from_jwk(REQUEST_ED25519, d_field="_private_d_for_test_only")
    signed = sign_request(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=REQUEST_ED25519["kid"],
        alg="ed25519",
        cover_content_digest=True,
        tag="adcp/webhook-signing/v1",
    )
    headers = {"Content-Type": "application/json", **signed.as_dict()}

    verified = verify_webhook_signature(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers=headers,
        body=body,
        options=_webhook_verify_options([REQUEST_ED25519]),
    )
    assert verified.key_id == REQUEST_ED25519["kid"]


def test_rejects_unknown_webhook_key_purpose() -> None:
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    unknown_purpose_key = {**WEBHOOK_ED25519, "adcp_use": "governance-signing"}
    headers = _sign_and_headers(body, key=unknown_purpose_key)

    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers=headers,
            body=body,
            options=_webhook_verify_options([unknown_purpose_key]),
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_KEY_PURPOSE_INVALID


def test_rejects_request_signing_tag() -> None:
    """A request-signing signature replayed on a webhook route MUST be rejected."""
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    private_key = private_key_from_jwk(WEBHOOK_ED25519, d_field="_private_d_for_test_only")
    # Sign with the REQUEST tag, not the webhook tag.
    signed = sign_request(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers={"Content-Type": "application/json"},
        body=body,
        private_key=private_key,
        key_id=WEBHOOK_ED25519["kid"],
        alg="ed25519",
        cover_content_digest=True,
        tag=DEFAULT_TAG,
    )
    headers = {"Content-Type": "application/json", **signed.as_dict()}

    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers=headers,
            body=body,
            options=_webhook_verify_options([WEBHOOK_ED25519]),
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_TAG_INVALID


def test_rejects_missing_signature_headers() -> None:
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers={"Content-Type": "application/json"},
            body=body,
            options=_webhook_verify_options([WEBHOOK_ED25519]),
        )
    assert exc_info.value.code == WEBHOOK_SIGNATURE_REQUIRED


def test_rejects_body_tampering() -> None:
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    headers = _sign_and_headers(body)

    with pytest.raises(SignatureVerificationError):
        verify_webhook_signature(
            method="POST",
            url="https://buyer.example.com/webhooks/adcp",
            headers=headers,
            body=body + b" ",  # Body tampered post-sign
            options=_webhook_verify_options([WEBHOOK_ED25519]),
        )


def test_sender_identity_shape() -> None:
    """Default as_sender_identity uses key_id when no sender_url is given."""
    body = b'{"idempotency_key":"whk_abc","task_id":"t1"}'
    headers = _sign_and_headers(body)

    result = verify_webhook_signature(
        method="POST",
        url="https://buyer.example.com/webhooks/adcp",
        headers=headers,
        body=body,
        options=_webhook_verify_options([WEBHOOK_ED25519]),
    )
    assert result.as_sender_identity() == "test-webhook-ed25519-2026"
