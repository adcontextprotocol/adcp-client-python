"""Legacy HMAC-SHA256 webhook verifier.

The 3.0 baseline is RFC 9421 webhook signing (see :mod:`webhook_verifier`).
HMAC-SHA256 remains available through AdCP 3.x as an opt-in fallback — buyers
populate ``push_notification_config.authentication.credentials`` and sellers
MAY honor it. The ``authentication`` field is removed in 4.0.

Wire format, matching :func:`adcp.webhooks.get_adcp_signed_headers_for_webhook`:

* ``X-AdCP-Signature`` — ``sha256=<hex_digest>`` of ``HMAC_SHA256(secret,
  f"{timestamp}.{raw_body_bytes.decode()}")``
* ``X-AdCP-Timestamp`` — Unix epoch seconds, string

The sender serializes the payload with compact separators (``","``/``":"``)
to match what httpx / most HTTP clients put on the wire for ``json=payload``
(pinned in adcontextprotocol/adcp#2478). Receivers MUST verify against the
raw request body bytes, not a re-serialized copy — JSON round-trips through
a dict reorder keys and break the signature. Always call with
``request.body()`` / ``request.get_data()``.

**Security note — no nonce-based replay cache.** The legacy HMAC scheme binds
only the timestamp into the signed message; unlike the 9421 profile, it has
no nonce, so a replayed signed body within the skew window re-verifies
without objection. Downstream dedup (``WebhookDedupStore``) catches repeated
events by ``idempotency_key`` — a replayed payload reuses the original's
``idempotency_key`` and correctly dedupes. The residual risk is limited to
scenarios where an attacker has captured a signed body AND can influence
receiver behavior based on the rebroadcast pre-dedup; all modern receivers
should dedup before side effects. This is an accepted limitation of the
legacy scheme — migrate to 9421 for nonce-based replay resistance.
"""

from __future__ import annotations

import hashlib
import hmac
import warnings
from collections.abc import Mapping
from dataclasses import dataclass

# Wire headers (case-insensitive for HTTP, but we name them canonically).
_SIGNATURE_HEADER = "x-adcp-signature"
_TIMESTAMP_HEADER = "x-adcp-timestamp"
_HEX_PREFIX = "sha256="

# Window — matches the 9421 default skew so receivers running both schemes
# during migration behave identically on timing rejections.
_DEFAULT_WINDOW_SECONDS = 300


class LegacyWebhookHmacError(Exception):
    """Raised when HMAC-SHA256 legacy verification fails.

    Distinct from :class:`adcp.signing.errors.SignatureVerificationError` so
    callers can distinguish legacy-path failures from 9421-path failures —
    operators want to know which scheme fired when diagnosing a 401.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LegacyWebhookHmacOptions:
    """Options for the HMAC verifier.

    :param secret_resolver: callable ``(header_map) -> bytes | None`` that
        returns the shared secret for this incoming request. The receiver is
        responsible for determining sender identity from headers (Bearer
        token, IP allowlist, hostname) and looking up the secret bound to
        that sender. The resolver returns ``None`` when no sender can be
        authenticated — the verifier then rejects without attempting compare.
    :param sender_identity: trusted authentication/audit label for this legacy
        secret. In HMAC-legacy, there is no cryptographic sender identity (the
        secret IS the identity), so the caller provides one. Receivers must
        still resolve it to a stable publisher scope that survives secret
        rotation; never derive that scope from the webhook body.
    :param now: current time, epoch seconds. Defaults fetched at call time.
    :param window_seconds: accepted skew. Sender timestamp outside ``[now -
        window, now + window]`` rejects.
    """

    secret: bytes
    sender_identity: str
    now: float
    window_seconds: int = _DEFAULT_WINDOW_SECONDS


@dataclass(frozen=True)
class VerifiedLegacyWebhookSender:
    """Identity returned by the HMAC verifier on success.

    Shape-compatible with the 9421 ``VerifiedWebhookSender.as_sender_identity``
    so downstream dedup code treats both the same.
    """

    sender_identity: str

    def as_sender_identity(self) -> str:
        return self.sender_identity


def verify_webhook_hmac(
    *,
    headers: Mapping[str, str],
    body: bytes,
    options: LegacyWebhookHmacOptions,
) -> VerifiedLegacyWebhookSender:
    """Verify an HMAC-SHA256-signed webhook body per the legacy scheme.

    Raises :class:`LegacyWebhookHmacError` on any failure. Fires a one-time
    :class:`DeprecationWarning` — operators SHOULD migrate to 9421 before AdCP
    4.0 removes the ``authentication`` field.

    ``headers`` can be any ``Mapping[str, str]`` — ``dict``,
    ``werkzeug.datastructures.EnvironHeaders``, Starlette's ``Headers``, etc.
    Keys are case-folded internally.
    """
    _warn_once()

    header_map = {str(k).lower(): str(v) for k, v in headers.items()}
    sig_value = header_map.get(_SIGNATURE_HEADER)
    ts_value = header_map.get(_TIMESTAMP_HEADER)
    if sig_value is None or ts_value is None:
        raise LegacyWebhookHmacError("missing X-AdCP-Signature or X-AdCP-Timestamp header")
    if not sig_value.startswith(_HEX_PREFIX):
        raise LegacyWebhookHmacError(
            f"signature must start with {_HEX_PREFIX!r}, got {sig_value[:16]!r}"
        )
    hex_sig = sig_value[len(_HEX_PREFIX) :]

    try:
        ts_int = int(ts_value)
    except ValueError as exc:
        raise LegacyWebhookHmacError(f"invalid timestamp {ts_value!r}") from exc

    # Bound on the skew window. Matches the 9421 max window (300s) exactly —
    # the 9421 pipeline applies DEFAULT_SKEW_SECONDS inside its window check,
    # so both schemes have the same "skew budget" on the wire. Do NOT add
    # DEFAULT_SKEW_SECONDS on top; that would double-count and yield a 360s
    # budget for HMAC vs 300s for 9421.
    skew = abs(options.now - ts_int)
    if skew > options.window_seconds:
        raise LegacyWebhookHmacError(
            f"timestamp skew {skew:.0f}s exceeds window {options.window_seconds}s"
        )

    # The sender constructs the message as f"{timestamp}.{json_payload}"
    # where json_payload is the body bytes as UTF-8. Re-decoding a dict would
    # re-serialize with potentially different key order and break the
    # signature — verify against the raw bytes as received.
    message = f"{ts_value}.".encode() + body
    expected = hmac.new(options.secret, message, hashlib.sha256).hexdigest()
    # Constant-time compare — hmac.compare_digest handles str/str.
    if not hmac.compare_digest(expected, hex_sig):
        raise LegacyWebhookHmacError("signature did not match")

    return VerifiedLegacyWebhookSender(sender_identity=options.sender_identity)


_deprecation_warned = False


def _warn_once() -> None:
    global _deprecation_warned
    if _deprecation_warned:
        return
    _deprecation_warned = True
    warnings.warn(
        "HMAC-SHA256 webhook verification is the AdCP 3.x legacy fallback and "
        "will be removed in AdCP 4.0. Migrate senders to the RFC 9421 webhook "
        "signing profile — see adcp.signing.webhook_verifier. This warning "
        "fires once per process.",
        DeprecationWarning,
        stacklevel=3,
    )


__all__ = [
    "LegacyWebhookHmacError",
    "LegacyWebhookHmacOptions",
    "VerifiedLegacyWebhookSender",
    "verify_webhook_hmac",
]
