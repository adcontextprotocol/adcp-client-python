"""Auth-mode strategies for :class:`adcp.webhook_sender.WebhookSender`.

Each strategy owns one authentication mode (RFC 9421 JWK signing,
Standard-Webhooks HMAC, AdCP-legacy HMAC, bearer token) and produces
the auth-bearing headers for one outgoing webhook delivery. The sender
calls :meth:`WebhookAuthStrategy.build_auth_headers` per request and
merges the returned headers into the wire request.

Why a strategy interface and not a polymorphic ``__init__`` parameter:
the JWK path needs ``PrivateKey``, the HMAC paths need ``bytes``, the
bearer path needs ``str``. Carrying all three as ``Optional[...]`` on
the sender propagates the optional through every typecheck site and
splits ``_send_bytes`` into a chain of mode-conditionals. A strategy
collapses that into one method call.

Strategies are constructed by the public :class:`WebhookSender`
classmethods (:meth:`from_jwk`, :meth:`from_pem`,
:meth:`from_bearer_token`, :meth:`from_adcp_legacy_hmac`,
:meth:`from_standard_webhooks_secret`). They are not part of the
public API directly — call the matching ``WebhookSender.from_*``.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from adcp.signing.crypto import PrivateKey
from adcp.signing.standard_webhooks import (
    HEADER_ID as _SW_HEADER_ID,
)
from adcp.signing.standard_webhooks import (
    HEADER_SIGNATURE as _SW_HEADER_SIGNATURE,
)
from adcp.signing.standard_webhooks import (
    HEADER_TIMESTAMP as _SW_HEADER_TIMESTAMP,
)
from adcp.signing.standard_webhooks import (
    sign_standard_webhook,
)
from adcp.signing.webhook_signer import sign_webhook

# Reserved headers shared by every mode — signature/digest/auth headers
# the strategy emits cannot be overridden by an extra_headers payload,
# nor can Content-Type (changing it would break body framing on the
# receiver).
_BASE_RESERVED: frozenset[str] = frozenset({"content-type"})


class WebhookAuthStrategy(Protocol):
    """Produce the auth-bearing headers for one webhook POST.

    Implementations are responsible for any per-call timestamping or
    nonce so retries (``WebhookSender.resend``) re-sign with fresh
    metadata over the same body — the sender simply re-enters
    ``build_auth_headers``.
    """

    def build_auth_headers(self, *, method: str, url: str, body: bytes) -> dict[str, str]: ...

    def reserved_headers(self) -> frozenset[str]:
        """Lower-case header names callers MUST NOT override via ``extra_headers``."""
        ...


@dataclass(frozen=True)
class JwkSignerStrategy:
    """RFC 9421 webhook-signing profile (the AdCP-conformant default)."""

    # ``repr=False`` on every secret-bearing field across all strategies:
    # the auto-generated dataclass repr would otherwise render the
    # private key / HMAC secret / bearer token in plaintext, defeating
    # the WebhookSender.__repr__ redaction the moment any code touches
    # ``self._auth`` (traceback locals, ``vars()``, faulthandler).
    private_key: PrivateKey = field(repr=False)
    key_id: str
    alg: str

    def build_auth_headers(self, *, method: str, url: str, body: bytes) -> dict[str, str]:
        signed = sign_webhook(
            method=method,
            url=url,
            headers={"Content-Type": "application/json"},
            body=body,
            private_key=self.private_key,
            key_id=self.key_id,
            alg=self.alg,
        )
        return signed.as_dict()

    def reserved_headers(self) -> frozenset[str]:
        return _BASE_RESERVED | {"signature", "signature-input", "content-digest"}


@dataclass(frozen=True)
class AdcpLegacyHmacStrategy:
    """AdCP 3.x legacy HMAC-SHA256 (``X-AdCP-Signature`` / ``X-AdCP-Timestamp``).

    The wire format matches :func:`adcp.signing.webhook_hmac.verify_webhook_hmac`
    so a buyer running the AdCP-legacy verifier accepts deliveries from
    this strategy unchanged. The signed payload is
    ``f"{timestamp}.{body}"``; the timestamp is read fresh from
    :func:`time.time` on every call so resends produce a new signature
    over the same body bytes (the receiver dedupes on
    ``idempotency_key`` — same body, fresh sig, fresh skew window).
    """

    secret: bytes = field(repr=False)
    key_id: str

    def build_auth_headers(self, *, method: str, url: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = f"{timestamp}.".encode() + body
        digest = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return {
            "X-AdCP-Signature": f"sha256={digest}",
            "X-AdCP-Timestamp": timestamp,
            "X-AdCP-Key-Id": self.key_id,
        }

    def reserved_headers(self) -> frozenset[str]:
        return _BASE_RESERVED | {
            "x-adcp-signature",
            "x-adcp-timestamp",
            "x-adcp-key-id",
        }


@dataclass(frozen=True)
class StandardWebhooksHmacStrategy:
    """standardwebhooks.com v1 (Svix/Resend interop).

    ``secret`` MUST be the raw HMAC key bytes — pass output of
    :func:`adcp.signing.standard_webhooks.decode_secret` if the operator
    holds the canonical ``whsec_<base64>`` form. Building this strategy
    with the literal ``whsec_`` string would silently produce signatures
    no conformant verifier accepts, so the constructor doesn't accept a
    string — types enforce the encoding contract.
    """

    secret: bytes = field(repr=False)
    key_id: str

    def build_auth_headers(self, *, method: str, url: str, body: bytes) -> dict[str, str]:
        # webhook-id is per-delivery (not per-payload). The receiver
        # uses idempotency_key in the body for dedup; webhook-id is the
        # spec-required transport identifier and changes on every
        # send/resend so receivers using webhook-id for replay
        # detection don't false-positive on a legitimate retry.
        return sign_standard_webhook(
            secret=self.secret,
            msg_id=f"msg_{uuid.uuid4().hex}",
            timestamp=int(time.time()),
            body=body,
        )

    def reserved_headers(self) -> frozenset[str]:
        return _BASE_RESERVED | {
            _SW_HEADER_ID,
            _SW_HEADER_TIMESTAMP,
            _SW_HEADER_SIGNATURE,
        }


@dataclass(frozen=True)
class BearerTokenStrategy:
    """``Authorization: Bearer <token>`` — for buyers who authenticate the
    sender at the gateway and don't (yet) verify body signatures.

    No body signing. The sender's marshaling guarantees still apply
    (byte-exact serialization, idempotency_key in body), but a buyer
    treating bearer tokens as the sole authenticity signal MUST also
    enforce TLS pinning or mTLS at the transport layer — a stolen
    token is a complete forgery.
    """

    token: str = field(repr=False)

    def build_auth_headers(self, *, method: str, url: str, body: bytes) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def reserved_headers(self) -> frozenset[str]:
        return _BASE_RESERVED | {"authorization"}


def merge_extra_headers(
    *,
    base: Mapping[str, str],
    extra: Mapping[str, str] | None,
    reserved: frozenset[str],
) -> dict[str, str]:
    """Merge ``extra`` into ``base``; raise if ``extra`` collides with reserved.

    Pre-scan so a bad extra-header doesn't leave half-merged state on
    the request — the sender promises atomicity here.
    """
    merged = dict(base)
    if not extra:
        return merged
    for key in extra:
        if str(key).lower() in reserved:
            raise ValueError(
                f"extra_headers may not override auth-binding or content-type " f"header {key!r}"
            )
    for key, value in extra.items():
        merged[key] = value
    return merged


__all__ = [
    "AdcpLegacyHmacStrategy",
    "BearerTokenStrategy",
    "JwkSignerStrategy",
    "StandardWebhooksHmacStrategy",
    "WebhookAuthStrategy",
    "merge_extra_headers",
]
