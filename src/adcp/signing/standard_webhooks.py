"""Standard Webhooks v1 signing + verifying.

Pure-Python implementation of the standardwebhooks.com v1 spec
(https://www.standardwebhooks.com/) so adopters can deliver webhooks to
buyers running Svix, Resend, or any other Standard-Webhooks verifier
without custom code, and verify Standard-Webhooks-signed inbound
webhooks too.

Wire format (per spec):

* ``webhook-id`` — a unique identifier for this delivery (UUID is fine).
* ``webhook-timestamp`` — Unix epoch seconds, string.
* ``webhook-signature`` — one or more space-separated tokens of the form
  ``v1,<base64>`` where the base64 value is
  ``HMAC-SHA256(secret, f"{webhook_id}.{webhook_timestamp}.{body}")``.

Secrets are typically distributed in the canonical ``whsec_<base64>`` form;
:func:`decode_secret` strips the prefix and base64-decodes to raw bytes.
The signing/verifying functions take raw bytes — pass the decoded form.

Why standalone (not depending on the ``standardwebhooks`` PyPI package):
~80 LOC. The package's dependency footprint is not worth carrying for an
algorithm this size, and a pure-Python implementation removes one
moving part from our supply chain.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping

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
SECRET_PREFIX = "whsec_"
_SECRET_PREFIX = SECRET_PREFIX  # backward-compat alias for in-module use
_SIGNATURE_VERSION = "v1"
_DEFAULT_TOLERANCE_SECONDS = 300


class StandardWebhookError(Exception):
    """Raised when Standard Webhooks signing or verification fails."""


def decode_secret(secret: str) -> bytes:
    """Decode a ``whsec_<base64>`` secret to raw HMAC key bytes.

    Standard Webhooks distributes secrets as ``whsec_`` plus a base64
    payload. Verifiers MUST HMAC against the *decoded* bytes; signing
    against the literal ``whsec_...`` string produces signatures that
    no conformant verifier will accept.

    Accepts both with-prefix (``whsec_AAAA...``) and without — pass-through
    for already-decoded callers. Padding is permissive (Svix-issued secrets
    sometimes lack ``=`` padding).
    """
    if not isinstance(secret, str) or not secret:
        raise StandardWebhookError("secret must be a non-empty string")
    payload = secret[len(_SECRET_PREFIX) :] if secret.startswith(_SECRET_PREFIX) else secret
    # Pad to a multiple of 4 — base64.b64decode rejects unpadded input
    # but Svix-issued secrets are sometimes distributed unpadded.
    padding = "=" * (-len(payload) % 4)
    try:
        # binascii.Error subclasses ValueError; one catch covers both.
        return base64.b64decode(payload + padding, validate=True)
    except ValueError as exc:
        # Do NOT include the underlying exception message in the
        # StandardWebhookError text — the binascii error string echoes
        # the offending character of the secret, which can land in
        # operator logs and leak material from a malformed but
        # otherwise-real ``whsec_`` value. The exception type is enough
        # for the operator to diagnose (chained via ``from exc`` for
        # debug-mode introspection).
        raise StandardWebhookError("secret is not valid base64") from exc


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
    version the spec defines).
    """
    if not msg_id:
        raise StandardWebhookError("msg_id must be non-empty")
    if not isinstance(timestamp, int):
        raise StandardWebhookError("timestamp must be an int (epoch seconds)")
    message = f"{msg_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return {
        HEADER_ID: msg_id,
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_SIGNATURE: f"{_SIGNATURE_VERSION},{encoded}",
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
    rotation) — verification succeeds if any one of them matches.
    """
    header_map = {str(k).lower(): str(v) for k, v in headers.items()}
    msg_id = header_map.get(HEADER_ID)
    ts_value = header_map.get(HEADER_TIMESTAMP)
    sig_header = header_map.get(HEADER_SIGNATURE)
    if not msg_id or not ts_value or not sig_header:
        raise StandardWebhookError(
            "missing webhook-id, webhook-timestamp, or webhook-signature header"
        )

    try:
        ts_int = int(ts_value)
    except ValueError as exc:
        raise StandardWebhookError(f"invalid webhook-timestamp {ts_value!r}") from exc

    skew = abs(now - ts_int)
    if skew > tolerance_seconds:
        raise StandardWebhookError(
            f"timestamp skew {skew:.0f}s exceeds tolerance {tolerance_seconds}s"
        )

    message = f"{msg_id}.{ts_value}.".encode() + body
    expected = base64.b64encode(hmac.new(secret, message, hashlib.sha256).digest()).decode("ascii")

    # Spec allows multiple space-separated tokens for key rotation:
    # "v1,sig1 v1,sig2". Accept a match against any v1 token; ignore
    # unknown versions (forward-compat with future signature schemes).
    for token in sig_header.split(" "):
        version, _, value = token.partition(",")
        if version != _SIGNATURE_VERSION or not value:
            continue
        if hmac.compare_digest(expected, value):
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
