"""Signer for the AdCP webhook-signing profile (adcp#2423).

Same 9421 substrate as :func:`adcp.signing.signer.sign_request`, with values
pinned by the webhook profile:

* ``tag`` — ``adcp/webhook-signing/v1``
* ``cover_content_digest`` — always ``True`` (body IS the event)
* ``Signature`` — unpadded Base64URL throughout 3.x, independently of the
  AdCP 3.2 request-signing migration to padded standard Base64.
* the signing JWK MUST have ``adcp_use: "webhook-signing"`` in the sender's
  published ``adagents.json``; verifying this at publish time is out of scope
  for the signer, but callers should enforce it when registering their keyring.
"""

from __future__ import annotations

from collections.abc import Mapping

from adcp.signing.constants import (
    DEFAULT_EXPIRES_IN_SECONDS,
    SIG_LABEL_DEFAULT,
    WEBHOOK_TAG,
)
from adcp.signing.crypto import PrivateKey
from adcp.signing.signer import SignedHeaders, sign_request


def sign_webhook(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    private_key: PrivateKey,
    key_id: str,
    alg: str,
    created: int | None = None,
    expires_in_seconds: int = DEFAULT_EXPIRES_IN_SECONDS,
    nonce: str | None = None,
    label: str = SIG_LABEL_DEFAULT,
) -> SignedHeaders:
    """Sign an outgoing webhook POST per adcp/webhook-signing/v1.

    ``cover_content_digest=True``, ``tag=WEBHOOK_TAG`` and unpadded Base64URL
    Signature encoding are pinned. The caller attaches
    ``SignedHeaders.as_dict()`` to the outgoing HTTP request.

    The ``method`` is normally ``"POST"`` for webhook delivery; passed through
    unchanged so callers signing a retried ``PUT`` or variant delivery verb
    are not forced into an extra translation.

    See also:
        :class:`adcp.webhooks.WebhookSender` — higher-level one-call helper
        that builds the payload, signs, and POSTs in a single call. Prefer it
        unless you need to own the HTTP transport yourself.
    """
    return sign_request(
        method=method,
        url=url,
        headers=headers,
        body=body,
        private_key=private_key,
        key_id=key_id,
        alg=alg,
        cover_content_digest=True,
        created=created,
        expires_in_seconds=expires_in_seconds,
        nonce=nonce,
        tag=WEBHOOK_TAG,
        label=label,
        # Webhook-v1 keeps legacy Signature encoding even on AdCP 3.2 routes.
        # Explicit digest coverage preserves the existing Content-Digest bytes.
        signing_profile_version="3.1",
    )


__all__ = ["sign_webhook"]
