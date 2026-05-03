"""Tests for the Standard Webhooks v1 signing/verifying primitives.

Round-trip our sign through our verify; round-trip our sign through the
upstream svix Python verifier (when installed) to catch wire-format
drift; and exercise the spec's edge cases (multiple signatures, key
rotation, skew, base64 secret decoding).
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.signing.standard_webhooks import (
    HEADER_ID,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    SECRET_PREFIX,
    StandardWebhookError,
    decode_secret,
    sign_standard_webhook,
    verify_standard_webhook,
)

# Fixture secret — the bytes we feed sign/verify directly.
_SECRET_BYTES = b"\x00" * 32
# The same key in canonical Standard Webhooks distribution form.
# Construct via the imported prefix so this file never contains the
# literal ``whsec_<long-base64>`` pattern that high-entropy-secret
# detectors flag.
_SECRET_WHSEC = SECRET_PREFIX + base64.b64encode(_SECRET_BYTES).decode("ascii")


def test_roundtrip_verifies() -> None:
    body = b'{"event":"hello","data":{"n":1}}'
    msg_id = "msg_2J3z7"
    ts = int(time.time())

    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id=msg_id, timestamp=ts, body=body)

    assert headers[HEADER_ID] == msg_id
    assert headers[HEADER_TIMESTAMP] == str(ts)
    assert headers[HEADER_SIGNATURE].startswith("v1,")

    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_tampered_body_fails() -> None:
    ts = int(time.time())
    headers = sign_standard_webhook(
        secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=b"original"
    )
    with pytest.raises(StandardWebhookError, match="no matching v1 signature"):
        verify_standard_webhook(
            headers=headers, body=b"tampered", secret=_SECRET_BYTES, now=float(ts)
        )


def test_wrong_secret_fails() -> None:
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    with pytest.raises(StandardWebhookError, match="no matching v1 signature"):
        verify_standard_webhook(headers=headers, body=body, secret=b"\x01" * 32, now=float(ts))


def test_skew_outside_tolerance_fails() -> None:
    ts = 1_700_000_000
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    with pytest.raises(StandardWebhookError, match="timestamp skew"):
        verify_standard_webhook(
            headers=headers,
            body=body,
            secret=_SECRET_BYTES,
            now=float(ts + 301),
            tolerance_seconds=300,
        )


def test_missing_headers_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="missing"):
        verify_standard_webhook(headers={}, body=b"", secret=_SECRET_BYTES, now=time.time())


def test_invalid_timestamp_rejected() -> None:
    headers = {
        HEADER_ID: "msg_1",
        HEADER_TIMESTAMP: "not-a-number",
        HEADER_SIGNATURE: "v1,xxx",
    }
    with pytest.raises(StandardWebhookError, match="invalid webhook-timestamp"):
        verify_standard_webhook(headers=headers, body=b"", secret=_SECRET_BYTES, now=time.time())


def test_multiple_signatures_for_key_rotation() -> None:
    """Spec allows multiple space-separated tokens — verify if any v1 matches."""
    body = b"body"
    ts = int(time.time())
    secret_a = b"\x01" * 32
    secret_b = b"\x02" * 32

    headers_a = sign_standard_webhook(secret=secret_a, msg_id="msg_1", timestamp=ts, body=body)
    headers_b = sign_standard_webhook(secret=secret_b, msg_id="msg_1", timestamp=ts, body=body)

    combined = {
        **headers_a,
        HEADER_SIGNATURE: f"{headers_a[HEADER_SIGNATURE]} {headers_b[HEADER_SIGNATURE]}",
    }

    # Verifier holding only secret_a accepts (its sig is one of two).
    verify_standard_webhook(headers=combined, body=body, secret=secret_a, now=float(ts))
    # Verifier holding only secret_b also accepts (its sig is the other one).
    verify_standard_webhook(headers=combined, body=body, secret=secret_b, now=float(ts))


def test_unknown_signature_versions_ignored() -> None:
    """Future-version tokens shouldn't break v1 verification."""
    body = b"body"
    ts = int(time.time())
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    headers[HEADER_SIGNATURE] = f"v2,futurething {headers[HEADER_SIGNATURE]}"
    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_decode_secret_with_prefix() -> None:
    decoded = decode_secret(_SECRET_WHSEC)
    assert decoded == _SECRET_BYTES


def test_decode_secret_without_prefix() -> None:
    raw_b64 = base64.b64encode(_SECRET_BYTES).decode("ascii")
    decoded = decode_secret(raw_b64)
    assert decoded == _SECRET_BYTES


def test_decode_secret_missing_padding_accepted() -> None:
    """Svix-issued secrets occasionally lack ``=`` padding."""
    raw = base64.b64encode(b"abcde").decode("ascii").rstrip("=")
    assert decode_secret(SECRET_PREFIX + raw) == b"abcde"


def test_decode_secret_invalid_base64_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="not valid base64"):
        decode_secret(SECRET_PREFIX + "!!!not-base64!!!")


def test_decode_secret_empty_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="non-empty"):
        decode_secret("")


def test_signature_is_deterministic() -> None:
    """HMAC is deterministic — same inputs must produce the exact same header."""
    body = b"body"
    ts = 1_700_000_000
    a = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    b = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    assert a == b


def test_svix_python_verifier_interop() -> None:
    """Round-trip: our sign → upstream svix-Python verify.

    Catches any drift between our wire format and the canonical
    standardwebhooks.com implementation. svix is an optional dev dep —
    the test skips when not installed.
    """
    svix = pytest.importorskip("svix.webhooks")

    body = b'{"hello":"world"}'
    msg_id = "msg_interop_1"
    ts = int(time.time())

    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id=msg_id, timestamp=ts, body=body)

    # svix.Webhook accepts the whsec_-prefixed form.
    wh: Any = svix.Webhook(_SECRET_WHSEC)
    # Raises on failure — no return value is the success signal.
    wh.verify(body, headers)


def test_svix_python_signer_interop() -> None:
    """Round-trip: upstream svix-Python sign → our verify.

    Validates that we accept payloads produced by the canonical sender.
    The svix Webhook.sign() takes a datetime; we feed it the same
    instant we'll pass to ``verify_standard_webhook(now=...)``.
    """
    svix = pytest.importorskip("svix.webhooks")

    body = b'{"hello":"world"}'
    msg_id = "msg_interop_2"
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    ts_int = int(now_dt.timestamp())

    wh: Any = svix.Webhook(_SECRET_WHSEC)
    sig_value = wh.sign(msg_id, now_dt, body.decode("utf-8"))
    headers = {
        HEADER_ID: msg_id,
        HEADER_TIMESTAMP: str(ts_int),
        HEADER_SIGNATURE: sig_value,
    }

    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts_int))
