"""Tests for :func:`sign_legacy_webhook` — the tuple-returning companion.

The whole point is byte-equality between signed input and HTTP body, so the
tests here exercise that invariant against real httpx serialization and
against the receiver (:func:`verify_webhook_hmac`) that relies on it.
"""

from __future__ import annotations

import json
import time

import httpx

from adcp.webhooks import (
    LegacyWebhookHmacOptions,
    create_mcp_webhook_payload,
    sign_legacy_webhook,
    verify_webhook_hmac,
)


def test_sign_legacy_webhook_round_trip_verifies() -> None:
    secret = "s" * 32
    ts = str(int(time.time()))
    payload = create_mcp_webhook_payload(
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op_test_123",
        status="completed",
        result={"media_buy_id": "mb_1"},
    )
    signed_headers, body = sign_legacy_webhook(secret, payload, timestamp=ts)

    # The tuple contract: headers ready to attach, bytes ready to POST.
    assert signed_headers["X-AdCP-Signature"].startswith("sha256=")
    assert signed_headers["X-AdCP-Timestamp"] == ts
    assert isinstance(body, bytes)

    verified = verify_webhook_hmac(
        headers=signed_headers,
        body=body,
        options=LegacyWebhookHmacOptions(
            secret=secret.encode(),
            sender_identity="test-sender",
            now=float(int(ts)),
        ),
    )
    assert verified.as_sender_identity() == "test-sender"


def test_sign_legacy_webhook_timestamp_pinned_across_calls() -> None:
    """Pinning timestamp produces reproducible headers/body — useful in
    deterministic tests and when regenerating a signature for a replay."""
    secret = "abc123"
    payload = {"idempotency_key": "whk_x", "task_id": "t"}
    ts = "1773185740"
    first_headers, first_body = sign_legacy_webhook(secret, payload, timestamp=ts)
    second_headers, second_body = sign_legacy_webhook(secret, payload, timestamp=ts)
    assert first_headers == second_headers
    assert first_body == second_body


def test_sign_legacy_webhook_body_matches_httpx_content_bytes() -> None:
    """The DX claim: passing ``content=body_bytes`` to httpx transmits the
    exact same bytes that were signed. If httpx ever re-serialized ``content``
    or the SDK stopped pinning compact separators, this test catches it.
    The check is structural — compare our bytes to what httpx produces from
    the same payload under ``json=`` with compact separators enforced."""
    payload = {"idempotency_key": "whk_1", "task_id": "t", "status": "completed"}
    _headers, body = sign_legacy_webhook("secret", payload, timestamp="100")

    # httpx's default json= path uses compact separators, same as us.
    httpx_request = httpx.Request("POST", "http://example.com/hook", json=payload)
    assert body == httpx_request.content


def test_sign_legacy_webhook_merges_into_existing_headers() -> None:
    """When ``headers`` is passed, the returned dict includes the caller's
    headers plus the two signature headers. Byte-equality still holds."""
    secret = "s" * 32
    payload = {"idempotency_key": "whk_2", "task_id": "t"}
    base = {"Content-Type": "application/json", "X-Request-Id": "r1"}
    merged, body = sign_legacy_webhook(secret, payload, headers=base, timestamp="123")

    assert merged["Content-Type"] == "application/json"
    assert merged["X-Request-Id"] == "r1"
    assert merged["X-AdCP-Signature"].startswith("sha256=")
    assert merged["X-AdCP-Timestamp"] == "123"
    # The caller's dict is NOT mutated — merging returns a fresh dict.
    assert "X-AdCP-Signature" not in base

    verified = verify_webhook_hmac(
        headers=merged,
        body=body,
        options=LegacyWebhookHmacOptions(
            secret=secret.encode(),
            sender_identity="sender",
            now=123.0,
        ),
    )
    assert verified.as_sender_identity() == "sender"


def test_sign_legacy_webhook_accepts_pydantic_model() -> None:
    """Payload may be a Pydantic model (AdCPBaseModel) — it's model_dumped
    with mode=json before serialization, matching the existing behavior of
    :func:`get_adcp_signed_headers_for_webhook`."""
    from adcp.types.base import AdCPBaseModel

    class _Payload(AdCPBaseModel):
        idempotency_key: str
        task_id: str
        status: str

    payload = _Payload(idempotency_key="whk_3", task_id="t", status="completed")
    headers, body = sign_legacy_webhook("secret", payload, timestamp="1")

    # Bytes are the compact-separator serialization of the model's JSON form.
    assert body == json.dumps(payload.model_dump(mode="json"), separators=(",", ":")).encode(
        "utf-8"
    )
    assert headers["X-AdCP-Signature"].startswith("sha256=")
