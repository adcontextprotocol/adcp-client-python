"""Standard Webhooks v1 signing + verifying.

Thin AdCP wrappers over the specification's own reference package,
:mod:`standardwebhooks` (https://www.standardwebhooks.com/), so adopters
can deliver webhooks to buyers running Svix, Resend, or any other
Standard-Webhooks verifier without custom code, and verify
Standard-Webhooks-signed inbound webhooks too.

Wire format (per spec, implemented by :class:`standardwebhooks.Webhook`):

* ``webhook-id`` — a unique identifier for this delivery (UUID is fine).
* ``webhook-timestamp`` — Unix epoch seconds, string.
* ``webhook-signature`` — one or more space-separated tokens of the form
  ``v1,<base64>`` where the base64 value is
  ``HMAC-SHA256(secret, f"{webhook_id}.{webhook_timestamp}.{body}")``.

The library computes the signature (:meth:`standardwebhooks.Webhook.sign`).
This module keeps only what the SDK's API promises on top of it:

* an injectable clock and tolerance. ``Webhook.verify`` reads the wall
  clock with a fixed five-minute window and offers no public way to change
  it, so verification calls ``Webhook.sign`` and matches the ``v1`` tokens
  itself, in constant time, until the library exposes one;
* strict secret decoding (:func:`decode_secret`) that fails fast on a
  corrupted ``whsec_`` value instead of deriving a different key;
* a canonical-integer ``webhook-timestamp`` (the library accepts floats
  and signs the floored value, not the bytes on the wire);
* raw-bytes bodies and :class:`StandardWebhookError` for every failure.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Mapping
from datetime import datetime, timezone

from standardwebhooks import Webhook
from standardwebhooks.webhooks import EmptyWebhookSecretError

# Header names per spec — case-insensitive on the wire; we match
# case-folded.
HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"

# Public so test fixtures can construct the canonical form without
# embedding the literal prefix as a string literal (GitGuardian's
# generic-high-entropy detector pattern-matches ``whsec_<long-base64>``
# even in obvious test files; importing the constant keeps the literal
# in this single source-of-truth module).
SECRET_PREFIX = "whsec_"  # nosec B105 - the public secret prefix, not a secret
_SECRET_PREFIX = SECRET_PREFIX  # backward-compat alias for in-module use
_DEFAULT_TOLERANCE_SECONDS = 300


class StandardWebhookError(Exception):
    """Raised when Standard Webhooks signing or verification fails."""


def _to_datetime(timestamp: int, *, header: str | None = None) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        if header is not None:
            raise StandardWebhookError(f"invalid webhook-timestamp {header!r}") from exc
        raise StandardWebhookError("timestamp is out of range") from exc


def _webhook(secret: bytes) -> Webhook:
    # The library base64-decodes ``str`` secrets leniently; the SDK's
    # contract is raw key bytes (decode ``whsec_`` values with
    # :func:`decode_secret`), so refuse strings rather than reinterpret.
    if not isinstance(secret, (bytes, bytearray)):
        raise TypeError("secret must be raw key bytes; decode whsec_ secrets with decode_secret()")
    try:
        return Webhook(bytes(secret))
    except EmptyWebhookSecretError as exc:
        raise StandardWebhookError("secret must be non-empty") from exc


def _body_text(body: bytes) -> str:
    # The library signs ``str`` and re-encodes it as UTF-8. For valid
    # UTF-8 that round trip is byte-exact; anything else cannot be
    # represented, so reject it rather than sign different bytes.
    try:
        return bytes(body).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StandardWebhookError("body must be UTF-8") from exc


def decode_secret(secret: str) -> bytes:
    """Decode a ``whsec_<base64>`` secret to raw HMAC key bytes.

    Standard Webhooks distributes secrets as ``whsec_`` plus a base64
    payload. Verifiers MUST HMAC against the *decoded* bytes; signing
    against the literal ``whsec_...`` string produces signatures that
    no conformant verifier will accept.

    Accepts both with-prefix (``whsec_AAAA...``) and without — pass-through
    for already-decoded callers. Padding is permissive (Svix-issued secrets
    sometimes lack ``=`` padding).

    Kept rather than delegated: ``standardwebhooks.Webhook`` decodes with
    non-validating base64, which silently drops stray characters and
    yields a different key, so a corrupted secret would only surface as
    every delivery failing verification at the buyer. This decoder
    rejects it at configuration time.
    """
    if not isinstance(secret, str) or not secret:
        raise StandardWebhookError("secret must be a non-empty string")
    payload = secret[len(_SECRET_PREFIX) :] if secret.startswith(_SECRET_PREFIX) else secret
    # Pad to a multiple of 4 — base64.b64decode rejects unpadded input
    # but Svix-issued secrets are sometimes distributed unpadded.
    padding = "=" * (-len(payload) % 4)
    try:
        # binascii.Error subclasses ValueError; one catch covers both.
        key = base64.b64decode(payload + padding, validate=True)
    except ValueError as exc:
        # Do NOT include the underlying exception message in the
        # StandardWebhookError text — the binascii error string echoes
        # the offending character of the secret, which can land in
        # operator logs and leak material from a malformed but
        # otherwise-real ``whsec_`` value. The exception type is enough
        # for the operator to diagnose (chained via ``from exc`` for
        # debug-mode introspection).
        raise StandardWebhookError("secret is not valid base64") from exc
    if not key:
        # A bare ``whsec_`` decodes to zero bytes, which the library
        # refuses as a key; fail here, at configuration time.
        raise StandardWebhookError("secret must be non-empty")
    return key


def sign_standard_webhook(
    *,
    secret: bytes,
    msg_id: str,
    timestamp: int,
    body: bytes,
) -> dict[str, str]:
    """Produce the three Standard Webhooks v1 headers for an outgoing POST.

    Returns a dict with ``webhook-id``, ``webhook-timestamp``, and
    ``webhook-signature`` ready to merge into the request headers.

    The signature header value is ``v1,<base64>`` — multiple versions can
    coexist space-separated, but senders only emit ``v1`` (the only
    symmetric version the spec defines).
    """
    if not msg_id:
        raise StandardWebhookError("msg_id must be non-empty")
    # ``bool`` is an ``int`` subclass; ``str(True)`` would put "True" on
    # the wire while the library signs "1".
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise StandardWebhookError("timestamp must be an int (epoch seconds)")
    signature = _webhook(secret).sign(msg_id, _to_datetime(timestamp), _body_text(body))
    return {
        HEADER_ID: msg_id,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_SIGNATURE: signature,
    }


def verify_standard_webhook(
    *,
    headers: Mapping[str, str],
    body: bytes,
    secret: bytes,
    now: float,
    tolerance_seconds: int = _DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Verify a Standard Webhooks v1 signed POST.

    Raises :class:`StandardWebhookError` on any failure. Returns ``None``
    on success — the caller already knows the body and msg_id from the
    request itself; this function answers the binary "is this trusted?"
    question and nothing else.

    ``headers`` may be any case-insensitive mapping. Multiple v1
    signatures in ``webhook-signature`` are accepted (per spec, for key
    rotation) — verification succeeds if any one of them matches. Tokens
    with other versions (``v1a``, future schemes) are ignored.
    """
    header_map = {str(k).lower(): str(v) for k, v in headers.items()}
    msg_id = header_map.get(HEADER_ID)
    ts_value = header_map.get(HEADER_TIMESTAMP)
    sig_header = header_map.get(HEADER_SIGNATURE)
    if not msg_id or not ts_value or not sig_header:
        raise StandardWebhookError(
            "missing webhook-id, webhook-timestamp, or webhook-signature header"
        )

    # Pre-checks the library cannot express: the caller's clock and
    # tolerance, and a canonical integer timestamp. The library parses
    # the header as a float and signs ``floor(value)``, so "+1700000000"
    # or "1700000000.9" would be verified against bytes that differ from
    # the header on the wire.
    try:
        ts_int = int(ts_value)
    except ValueError as exc:
        raise StandardWebhookError(f"invalid webhook-timestamp {ts_value!r}") from exc
    if str(ts_int) != ts_value:
        raise StandardWebhookError(f"invalid webhook-timestamp {ts_value!r}")
    _to_datetime(ts_int, header=ts_value)

    skew = abs(now - ts_int)
    if skew > tolerance_seconds:
        raise StandardWebhookError(
            f"timestamp skew {skew:.0f}s exceeds tolerance {tolerance_seconds}s"
        )

    # The signature itself comes from the library (Webhook.sign). Matching is
    # done here with public API only: Webhook.verify reads the wall clock with
    # a fixed five-minute window that cannot be configured, so it cannot honor
    # the caller's ``now`` and ``tolerance_seconds`` (tracked upstream; see the
    # migration notes). The comparison is the spec's: any ``v1`` token,
    # exact base64, constant time; other versions and malformed tokens are
    # skipped.
    expected = _webhook(secret).sign(msg_id, _to_datetime(ts_int), _body_text(body))
    expected_value = expected.partition(",")[2]
    for token in sig_header.split(" "):
        version, _, value = token.partition(",")
        if version != "v1" or not value:
            continue
        if hmac.compare_digest(expected_value.encode("ascii"), value.encode("utf-8")):
            return
    raise StandardWebhookError("no matching v1 signature")


__all__ = [
    "HEADER_ID",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "SECRET_PREFIX",
    "StandardWebhookError",
    "decode_secret",
    "sign_standard_webhook",
    "verify_standard_webhook",
]
