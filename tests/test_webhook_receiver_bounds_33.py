"""AdCP 3.3 inbound body bounds on webhook / push-notification receivers.

``security.mdx`` §Inbound body bounds: pre-reject an over-cap
``Content-Length``, count bytes while streaming, then hold the JSON to depth
64 and 100,000 members -- all *before* RFC 9421 / HMAC verification -- and
answer 413 without truncating. ``webhooks.mdx`` §Security requirements keeps
the 1 MB payload limit for webhook deliveries, which governs over the 4 MiB
generic default.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

import adcp.webhook_receiver as webhook_receiver_module
from adcp._json_bounds import DEFAULT_MAX_INBOUND_BODY_BYTES
from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import StaticJwksResolver, private_key_from_jwk
from adcp.webhooks import (
    DEFAULT_MAX_WEBHOOK_BODY_BYTES,
    InboundBodyRejectedError,
    LegacyHmacFallback,
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
    check_inbound_body,
    create_mcp_webhook_payload,
    read_inbound_body,
    sign_webhook,
    to_wire_dict,
)

VECTORS_DIR = Path(__file__).parent / "conformance" / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
WEBHOOK_KEY = {
    **copy.deepcopy(next(k for k in KEYS if k["kid"] == "test-ed25519-2026")),
    "kid": "test-webhook-ed25519-2026",
    "adcp_use": "webhook-signing",
}
URL = "https://buyer.example.com/webhooks/adcp"
JSON_HEADERS = {"Content-Type": "application/json"}


def _receiver(**config) -> WebhookReceiver:
    return WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=StaticJwksResolver({"keys": [WEBHOOK_KEY]}),
            ),
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="test-receiver",
            publisher_scope_for=lambda _signer: "test-publisher",
            **config,
        ),
    )


def _signed(body: bytes) -> dict[str, str]:
    signed = sign_webhook(
        method="POST",
        url=URL,
        headers=JSON_HEADERS,
        body=body,
        private_key=private_key_from_jwk(WEBHOOK_KEY, d_field="_private_d_for_test_only"),
        key_id=WEBHOOK_KEY["kid"],
        alg="ed25519",
    )
    return {**JSON_HEADERS, **signed.as_dict()}


def _webhook_body(key: str, **extra) -> bytes:
    payload = create_mcp_webhook_payload(
        task_id="task-bounds",
        task_type="create_media_buy",
        operation_id="op-bounds",
        status="working",  # type: ignore[arg-type]
        idempotency_key=key,
    )
    wire = to_wire_dict(payload)
    wire.update(extra)
    return json.dumps(wire, separators=(",", ":")).encode()


def _lists(depth: int) -> object:
    """`depth` nested arrays around a leaf."""
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value


@pytest.fixture
def verification_must_not_run(monkeypatch):
    """Fail the test if any signature check runs."""

    def boom(*args, **kwargs):
        raise AssertionError("signature verification ran on an over-limit body")

    monkeypatch.setattr(webhook_receiver_module, "verify_webhook_signature", boom)
    monkeypatch.setattr(webhook_receiver_module, "verify_webhook_hmac", boom)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_webhook_cap_is_the_1mb_payload_limit_not_10mib():
    assert DEFAULT_MAX_WEBHOOK_BODY_BYTES == 1024 * 1024
    assert DEFAULT_MAX_WEBHOOK_BODY_BYTES < DEFAULT_MAX_INBOUND_BODY_BYTES == 4 * 1024 * 1024
    assert _receiver().max_body_bytes == DEFAULT_MAX_WEBHOOK_BODY_BYTES


@pytest.mark.parametrize("value", [0, -1, None, True, 1.5])
def test_there_is_no_unbounded_mode(value):
    with pytest.raises(ValueError, match="max_body_bytes"):
        _receiver(max_body_bytes=value)
    with pytest.raises(ValueError):
        check_inbound_body(b"{}", headers=JSON_HEADERS, max_bytes=value)


# ---------------------------------------------------------------------------
# WebhookReceiver: bounds run before verification
# ---------------------------------------------------------------------------


async def test_body_over_the_cap_is_413_before_verification(verification_must_not_run):
    body = b" " * (DEFAULT_MAX_WEBHOOK_BODY_BYTES + 1)
    outcome = await _receiver().receive(method="POST", url=URL, headers=_signed(b"{}"), body=body)
    assert outcome.rejection_reason == "body_too_large"
    assert outcome.http_status == 413
    assert outcome.sender_identity is None


async def test_declared_content_length_over_the_cap_is_413(verification_must_not_run):
    headers = {**JSON_HEADERS, "Content-Length": str(DEFAULT_MAX_WEBHOOK_BODY_BYTES + 1)}
    outcome = await _receiver().receive(method="POST", url=URL, headers=headers, body=b"{}")
    assert outcome.rejection_reason == "body_too_large"
    assert outcome.http_status == 413


async def test_depth_65_is_413_before_verification(verification_must_not_run):
    # Signed, so the only reason to refuse it is its shape.
    body = _webhook_body("whk_bounds_depth_aaaaaaaaaa", ext=_lists(64))  # 65 with the root
    outcome = await _receiver().receive(method="POST", url=URL, headers=_signed(body), body=body)
    assert outcome.rejection_reason == "body_too_complex"
    assert outcome.http_status == 413
    assert outcome.response_headers == {}


async def test_member_100001_is_413_before_verification(verification_must_not_run):
    body = _webhook_body("whk_bounds_members_aaaaaaaa", ext=[0] * 100_001)
    assert len(body) < DEFAULT_MAX_WEBHOOK_BODY_BYTES  # the byte cap is not what trips
    outcome = await _receiver().receive(method="POST", url=URL, headers=_signed(body), body=body)
    assert outcome.rejection_reason == "body_too_complex"
    assert outcome.http_status == 413


async def test_unsigned_over_deep_body_is_413_not_401(verification_must_not_run):
    """Bounds come first, so even an unauthenticated peer only gets a 413."""
    body = json.dumps({"x": _lists(80)}).encode()
    outcome = await _receiver().receive(method="POST", url=URL, headers=JSON_HEADERS, body=body)
    assert outcome.http_status == 413


async def test_depth_64_is_accepted_and_verified():
    body = _webhook_body("whk_bounds_ok_depth_aaaaaaaa", ext=_lists(63))  # 64 with the root
    outcome = await _receiver().receive(method="POST", url=URL, headers=_signed(body), body=body)
    assert outcome.rejected is False, outcome.rejection_reason
    assert outcome.sender_identity == "test-webhook-ed25519-2026"


async def test_raising_the_cap_per_endpoint_admits_a_larger_body():
    body = _webhook_body("whk_bounds_big_aaaaaaaaaaaa", pad="x" * (DEFAULT_MAX_WEBHOOK_BODY_BYTES))
    small = await _receiver().receive(method="POST", url=URL, headers=_signed(body), body=body)
    assert small.rejection_reason == "body_too_large"

    larger = _receiver(max_body_bytes=2 * DEFAULT_MAX_WEBHOOK_BODY_BYTES)
    outcome = await larger.receive(method="POST", url=URL, headers=_signed(body), body=body)
    assert outcome.rejected is False, outcome.rejection_reason


@pytest.mark.parametrize(
    "extra",
    [
        {"Content-Length": "2", "Transfer-Encoding": "chunked"},
        {"Content-Length": "2, 3"},
        {"Content-Length": "2", "content-length": "3"},
        {"Content-Length": "-2"},
    ],
)
async def test_ambiguous_framing_is_refused(extra, verification_must_not_run):
    outcome = await _receiver().receive(
        method="POST", url=URL, headers={**JSON_HEADERS, **extra}, body=b"{}"
    )
    assert outcome.rejection_reason == "body_framing_invalid"
    assert outcome.http_status == 400


async def test_content_encoded_body_is_refused(verification_must_not_run):
    headers = {**JSON_HEADERS, "Content-Encoding": "gzip"}
    outcome = await _receiver().receive(method="POST", url=URL, headers=headers, body=b"\x1f\x8b")
    assert outcome.rejection_reason == "content_encoding_unsupported"
    assert outcome.http_status == 415


async def test_legacy_hmac_path_is_bounded_first(verification_must_not_run):
    receiver = _receiver(
        legacy_hmac=LegacyHmacFallback.from_shared_secret(
            secret=b"s" * 32, sender_identity="legacy-sender"
        )
    )
    body = json.dumps({"x": _lists(80)}).encode()
    outcome = await receiver.receive(method="POST", url=URL, headers=JSON_HEADERS, body=body)
    assert outcome.rejection_reason == "body_too_complex"


async def test_malformed_json_within_bounds_still_reaches_verification():
    """Bounds are not a JSON validator: a malformed body is verification's call."""
    outcome = await _receiver().receive(
        method="POST", url=URL, headers=JSON_HEADERS, body=b"{not json"
    )
    assert outcome.rejection_reason == "signature_missing"


# ---------------------------------------------------------------------------
# read_inbound_body / check_inbound_body (any push receiver, A2A included)
# ---------------------------------------------------------------------------


class _Stream:
    """An async body stream that records how much of it was consumed."""

    def __init__(self, chunks, *, delay: float = 0.0):
        self._chunks = list(chunks)
        self._delay = delay
        self.consumed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        if self._delay:
            await asyncio.sleep(self._delay)
        self.consumed += 1
        return self._chunks.pop(0)


async def test_read_returns_a_body_within_bounds():
    task = {"kind": "task", "id": "t1", "status": {"state": "completed"}, "artifacts": []}
    raw = json.dumps(task).encode()
    stream = _Stream([raw[:10], raw[10:]])
    body = await read_inbound_body(stream, headers=JSON_HEADERS)
    assert body == raw


async def test_read_stops_at_the_cap_without_content_length():
    stream = _Stream([b" " * 1024] * 100)
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(stream, headers=JSON_HEADERS, max_bytes=4096)
    assert caught.value.reason == "body_too_large"
    assert caught.value.status_code == 413
    # Abandoned at the chunk that crossed the cap, not read to the end.
    assert stream.consumed == 5


async def test_understated_content_length_does_not_exempt_the_stream():
    stream = _Stream([b" " * 1024] * 10)
    headers = {**JSON_HEADERS, "Content-Length": "10"}
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(stream, headers=headers, max_bytes=4096)
    assert caught.value.reason == "body_too_large"


async def test_declared_length_is_refused_before_the_first_chunk():
    stream = _Stream([b"{}"])
    headers = {**JSON_HEADERS, "content-length": "5000"}
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(stream, headers=headers, max_bytes=4096)
    assert caught.value.status_code == 413
    assert stream.consumed == 0


async def test_read_applies_the_parse_bounds():
    raw = json.dumps({"x": _lists(80)}).encode()
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(_Stream([raw]), headers=JSON_HEADERS)
    assert caught.value.reason == "body_too_complex"
    assert caught.value.status_code == 413


async def test_idle_deadline_is_enforced():
    stream = _Stream([b"{", b"}"], delay=0.5)
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(stream, headers=JSON_HEADERS, idle_timeout=0.05)
    assert caught.value.reason == "body_timeout"
    assert caught.value.status_code == 408


async def test_total_deadline_bounds_a_trickling_peer():
    stream = _Stream([b" "] * 50, delay=0.02)
    with pytest.raises(InboundBodyRejectedError) as caught:
        await read_inbound_body(stream, headers=JSON_HEADERS, idle_timeout=1.0, total_timeout=0.2)
    assert caught.value.reason == "body_timeout"
    assert stream.consumed < 50


def test_check_inbound_body_is_the_buffered_form():
    check_inbound_body(b'{"ok": true}', headers=JSON_HEADERS)
    with pytest.raises(InboundBodyRejectedError) as caught:
        check_inbound_body(json.dumps([0] * 100_001).encode(), headers=JSON_HEADERS)
    assert caught.value.reason == "body_too_complex"


def test_rejection_never_quotes_the_body():
    body = b'{"token": "sk_live_SECRET", "x": ' + b"[" * 70 + b"]" * 70 + b"}"
    with pytest.raises(InboundBodyRejectedError) as caught:
        check_inbound_body(body, headers=JSON_HEADERS)
    assert "SECRET" not in str(caught.value)
