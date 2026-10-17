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


# ---------- Delegation to the ``standardwebhooks`` reference package ----------


def _library_webhook() -> Any:
    from standardwebhooks import Webhook

    return Webhook(_SECRET_WHSEC)


def test_library_signature_verifies_with_sdk_wrapper() -> None:
    """Reference ``standardwebhooks.Webhook.sign`` → SDK verify."""
    body = '{"hello":"wörld","emoji":"\U0001f680"}'.encode()
    now_dt = datetime.now(timezone.utc).replace(microsecond=0)
    ts_int = int(now_dt.timestamp())
    headers = {
        HEADER_ID: "msg_lib_1",
        HEADER_TIMESTAMP: str(ts_int),
        HEADER_SIGNATURE: _library_webhook().sign("msg_lib_1", now_dt, body.decode("utf-8")),
    }
    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts_int))


def test_sdk_signature_verifies_with_library() -> None:
    """SDK sign → reference ``standardwebhooks.Webhook.verify`` (wall clock)."""
    body = '﻿{"hello":"wörld"}'.encode()
    headers = sign_standard_webhook(
        secret=_SECRET_BYTES, msg_id="msg_lib_2", timestamp=int(time.time()), body=body
    )
    assert _library_webhook().verify(body, headers, json_parse=False) is None


def test_signature_matches_spec_formula() -> None:
    """Pin the wire value to the spec's HMAC formula, independent of the library."""
    import hashlib
    import hmac

    body = b'{"n":1}'
    headers = sign_standard_webhook(
        secret=_SECRET_BYTES, msg_id="msg_1", timestamp=1_700_000_000, body=body
    )
    digest = hmac.new(_SECRET_BYTES, b"msg_1.1700000000." + body, hashlib.sha256).digest()
    assert headers[HEADER_SIGNATURE] == "v1," + base64.b64encode(digest).decode("ascii")


def test_injected_clock_is_authoritative() -> None:
    """``now`` and ``tolerance_seconds`` decide; the library's fixed wall-clock window does not."""
    ts = 1_000_000_000  # 2001, far outside any wall-clock window
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts + 10))
    verify_standard_webhook(
        headers=headers,
        body=body,
        secret=_SECRET_BYTES,
        now=float(ts + 3600),
        tolerance_seconds=3600,
    )


def test_skew_at_tolerance_boundary_accepted() -> None:
    ts = 1_700_000_000
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    for now in (ts - 300, ts + 300):
        verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(now))


@pytest.mark.parametrize(
    "transform", [lambda t: f"+{t}", lambda t: f"{t}.0", lambda t: f" {t}", lambda t: f"0{t}"]
)
def test_non_canonical_timestamp_rejected(transform: Any) -> None:
    """The library would sign the floored integer, not the header bytes."""
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    headers[HEADER_TIMESTAMP] = transform(ts)
    with pytest.raises(StandardWebhookError, match="invalid webhook-timestamp"):
        verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_header_names_case_insensitive() -> None:
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    mixed = {k.title(): v for k, v in headers.items()}
    assert HEADER_SIGNATURE not in mixed
    verify_standard_webhook(headers=mixed, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_asymmetric_v1a_tokens_ignored() -> None:
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    headers[HEADER_SIGNATURE] = f"v1a,AAAA {headers[HEADER_SIGNATURE]}"
    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


@pytest.mark.parametrize("bad_token", ["garbage", "v1,abc", "v2,a,b"])
def test_malformed_signature_tokens_are_skipped_like_unknown_versions(bad_token: str) -> None:
    """A malformed token never matches, and never hides a valid one beside it."""
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    valid = headers[HEADER_SIGNATURE]
    headers[HEADER_SIGNATURE] = f"{bad_token} {valid}"
    verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))
    headers[HEADER_SIGNATURE] = bad_token
    with pytest.raises(StandardWebhookError, match="no matching v1 signature"):
        verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_signature_value_must_match_exactly() -> None:
    """Junk inside a correct signature is not lenient-decoded into a match."""
    ts = int(time.time())
    body = b"body"
    headers = sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=ts, body=body)
    version, _, value = headers[HEADER_SIGNATURE].partition(",")
    headers[HEADER_SIGNATURE] = f"{version},{value[:4]}*{value[4:]}"
    with pytest.raises(StandardWebhookError, match="no matching v1 signature"):
        verify_standard_webhook(headers=headers, body=body, secret=_SECRET_BYTES, now=float(ts))


def test_non_utf8_body_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="UTF-8"):
        sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=1, body=b"\xff")
    headers = {HEADER_ID: "msg_1", HEADER_TIMESTAMP: "1", HEADER_SIGNATURE: "v1,AAAA"}
    with pytest.raises(StandardWebhookError, match="UTF-8"):
        verify_standard_webhook(headers=headers, body=b"\xff", secret=_SECRET_BYTES, now=1.0)


def test_empty_key_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="non-empty"):
        decode_secret(SECRET_PREFIX)
    with pytest.raises(StandardWebhookError, match="non-empty"):
        sign_standard_webhook(secret=b"", msg_id="msg_1", timestamp=1, body=b"")


def test_string_secret_rejected() -> None:
    """The library would base64-decode a ``str``; the SDK requires raw bytes."""
    with pytest.raises(TypeError, match="raw key bytes"):
        sign_standard_webhook(
            secret=_SECRET_WHSEC,  # type: ignore[arg-type]
            msg_id="msg_1",
            timestamp=1,
            body=b"",
        )


def test_bool_timestamp_rejected() -> None:
    with pytest.raises(StandardWebhookError, match="timestamp must be an int"):
        sign_standard_webhook(secret=_SECRET_BYTES, msg_id="msg_1", timestamp=True, body=b"")
