"""One-call outbound webhook delivery for AdCP senders.

A seller that wants to emit a signed webhook today has to do six steps by hand
— construct payload, JSON-serialize to bytes, sign, merge headers, POST with
``content=`` (not ``json=``, which reserializes and breaks the signature),
and remember to reuse ``idempotency_key`` on retry. Each step is a footgun.

:class:`WebhookSender` packages all of them::

    from adcp.webhooks import WebhookSender

    sender = WebhookSender.from_jwk(webhook_signing_jwk_with_private_d)

    async with sender:
        result = await sender.send_mcp(
            url="https://buyer.example.com/webhooks/adcp/create_media_buy/op_abc",
            task_id="task_456",
            task_type="create_media_buy",
            status="completed",
            result={"media_buy_id": "mb_1"},
        )
        if not result.ok:
            # Retry replays the exact same bytes under a fresh signature,
            # preserving idempotency_key so the receiver dedupes.
            retry = await sender.resend(result)
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    PrivateKey,
    load_private_key_pem,
    private_key_from_jwk,
)
from adcp.signing.ip_pinned_transport import (
    AsyncIpPinnedTransport,
    build_async_ip_pinned_transport,
)
from adcp.signing.standard_webhooks import decode_secret as _decode_sw_secret
from adcp.types import GeneratedTaskStatus, TaskType
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.webhook_auth import (
    AdcpLegacyHmacStrategy,
    BearerTokenStrategy,
    JwkSignerStrategy,
    StandardWebhooksHmacStrategy,
    WebhookAuthStrategy,
    merge_extra_headers,
)
from adcp.webhook_transport_hooks import (
    DockerLocalhostRewrite,
    TransportHook,
    apply_hooks,
)
from adcp.webhooks import (
    create_mcp_webhook_payload,
    generate_webhook_idempotency_key,
    to_wire_dict,
)

# The signer emits a signature valid for 300 seconds; anything beyond that
# requires a fresh signing call. Senders that retry past this window just
# re-enter send_*() with the same idempotency_key — the body is re-signed
# but dedup still fires at the receiver.
_DEFAULT_TIMEOUT_SECONDS = 10.0
# 10MB serialized-body cap — matches adcp.webhooks.deliver and typical
# buyer-side reverse-proxy limits. Guards against OOM when a caller passes
# an adversarial payload: json.dumps holds dict + str concurrently, and
# .encode() transiently triples memory, so a 1GB body is multiple GB RSS.
_MAX_BODY_BYTES = 10 * 1024 * 1024


_legacy_hmac_warned = False


def _warn_legacy_hmac_once() -> None:
    """Emit a one-shot DeprecationWarning when an operator builds a
    legacy-HMAC sender. Mirrors the receiver-side warn-once in
    :mod:`adcp.signing.webhook_hmac` so sender-only deployments see the
    AdCP 4.0 cutover signal at runtime, not only in the docstring."""
    global _legacy_hmac_warned
    if _legacy_hmac_warned:
        return
    _legacy_hmac_warned = True
    warnings.warn(
        "AdCP-legacy HMAC-SHA256 webhook signing is the AdCP 3.x fallback "
        "and will be removed in AdCP 4.0. Migrate to RFC 9421 JWK signing "
        "via WebhookSender.from_jwk / from_pem. See "
        "docs/webhooks/migration-from-fragmented-senders.md. This warning "
        "fires once per process.",
        DeprecationWarning,
        stacklevel=3,
    )


def _validate_hooks(hooks: tuple[TransportHook, ...], allow_private_destinations: bool) -> None:
    """Run each hook's optional ``validate_for_sender`` self-check.

    Hooks that depend on sender configuration (e.g.,
    :class:`DockerLocalhostRewrite` requiring private destinations)
    expose a ``validate_for_sender`` method that raises on
    misconfiguration. Hooks without it are unconstrained — the
    Protocol only mandates ``rewrite_url``.
    """
    for hook in hooks:
        validate = getattr(hook, "validate_for_sender", None)
        if callable(validate):
            validate(allow_private_destinations=allow_private_destinations)


@dataclass(frozen=True)
class WebhookDeliveryResult:
    """Outcome of one ``send_*`` call.

    Senders care about: did it land (``ok``), what key was used (for logs
    and retry), what did the receiver say (``status_code``, ``response_body``).

    The ``sent_body`` and ``sent_extra_headers`` fields capture exactly what
    was signed and POSTed — the sender's :meth:`WebhookSender.resend` replays
    them under a fresh signature (preserving ``idempotency_key`` for dedup)
    rather than re-serializing from a user-supplied dict, which would drift
    if any field (``timestamp``, nested ``result``) differs between calls.
    """

    status_code: int
    idempotency_key: str
    url: str
    response_headers: Mapping[str, str]
    response_body: bytes
    sent_body: bytes = b""
    sent_extra_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True on 2xx. Note: receivers MUST return 2xx on duplicates too, so
        a 200 with ``duplicate=true`` in the body is still ``ok``."""
        return 200 <= self.status_code < 300


class WebhookSender:
    """Outbound signed-webhook delivery client.

    Owns one webhook-signing private key. Reuses a single :class:`httpx.AsyncClient`
    across requests for connection pooling — pass your own via ``client=`` if
    you want to share it with other SDK surfaces.

    Thread/task safety: safe to call concurrent ``send_*`` from many asyncio
    tasks. The underlying ``httpx.AsyncClient`` manages its own pool.
    """

    def __init__(
        self,
        *,
        private_key: PrivateKey,
        key_id: str,
        alg: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> None:
        """Construct a sender wired to RFC 9421 JWK signing.

        The HMAC and bearer modes are reached via :meth:`from_bearer_token`,
        :meth:`from_adcp_legacy_hmac`, and :meth:`from_standard_webhooks_secret`
        — those classmethods bypass this initializer through
        :meth:`_from_strategy` because their key material has different
        types (``bytes`` / ``str`` rather than ``PrivateKey``).

        ``transport_hooks`` runs URL rewrites before SSRF validation —
        see :class:`adcp.webhook_transport_hooks.DockerLocalhostRewrite`
        for the canonical use case. SSRF remains authoritative on the
        rewritten URL; hooks cannot punch through the range check.
        """
        self._auth: WebhookAuthStrategy = JwkSignerStrategy(
            private_key=private_key, key_id=key_id, alg=alg
        )
        self._key_id = key_id
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._allow_private_destinations = allow_private_destinations
        self._allowed_destination_ports = allowed_destination_ports
        self._transport_hooks = tuple(transport_hooks)
        _validate_hooks(self._transport_hooks, allow_private_destinations)

    @classmethod
    def _from_strategy(
        cls,
        auth: WebhookAuthStrategy,
        *,
        key_id: str,
        client: httpx.AsyncClient | None,
        timeout_seconds: float,
        allow_private_destinations: bool,
        allowed_destination_ports: frozenset[int] | None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Build a sender around a pre-constructed auth strategy.

        Internal constructor for the HMAC/bearer paths. The public
        ``__init__`` is locked to the JWK signature for back-compat;
        new modes don't fit that signature, so they bypass it here.
        """
        sender = cls.__new__(cls)
        sender._auth = auth
        sender._key_id = key_id
        sender._timeout = timeout_seconds
        sender._client = client
        sender._owns_client = client is None
        sender._allow_private_destinations = allow_private_destinations
        sender._allowed_destination_ports = allowed_destination_ports
        sender._transport_hooks = tuple(transport_hooks)
        _validate_hooks(sender._transport_hooks, allow_private_destinations)
        return sender

    @classmethod
    def from_jwk(
        cls,
        jwk: Mapping[str, Any],
        *,
        d_field: str = "d",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Construct from a JWK that includes the private scalar.

        The JWK MUST have ``adcp_use == "webhook-signing"`` — the sender
        doesn't validate this (you're signing with your own key; validation
        happens at the receiver), but a key whose adcp_use is wrong will be
        rejected by every conformant verifier.

        ``allow_private_destinations`` and ``allowed_destination_ports``
        forward to :meth:`__init__` — see that signature for semantics.
        """
        # Snapshot the mapping once — a live Mapping could otherwise return
        # different values across the adcp_use / kid / d / alg reads.
        jwk_snapshot = dict(jwk)
        if jwk_snapshot.get("adcp_use") != "webhook-signing":
            raise ValueError(
                f"WebhookSender requires a JWK with adcp_use='webhook-signing' "
                f"(got {jwk_snapshot.get('adcp_use')!r}). Webhook-signing and "
                f"request-signing keys MUST be distinct so a signature from one "
                f"surface cannot be replayed as the other. Generate a separate "
                f"key with adcp_use='webhook-signing' and publish it in your "
                f"adagents.json alongside your request-signing key. See "
                f"https://adcontextprotocol.org/docs/building/implementation/security"
            )
        alg = jwk_snapshot.get("alg")
        if alg == "EdDSA":
            alg = "ed25519"
        elif alg == "ES256":
            alg = "ecdsa-p256-sha256"
        if alg not in ("ed25519", "ecdsa-p256-sha256"):
            raise ValueError(f"unsupported JWK alg {jwk_snapshot.get('alg')!r}")
        private_key = private_key_from_jwk(jwk_snapshot, d_field=d_field)
        return cls(
            private_key=private_key,
            key_id=str(jwk_snapshot["kid"]),
            alg=alg,
            client=client,
            timeout_seconds=timeout_seconds,
            allow_private_destinations=allow_private_destinations,
            allowed_destination_ports=allowed_destination_ports,
            transport_hooks=transport_hooks,
        )

    @classmethod
    def from_pem(
        cls,
        pem_path: str | Path | bytes,
        *,
        key_id: str,
        alg: str = "ed25519",
        passphrase: bytes | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Load a private key from a PEM file and bind it as a webhook sender.

        Companion to ``adcp-keygen --purpose webhook-signing``, which writes
        the PEM and prints the public JWK. The JWK is published at your
        ``jwks_uri``; the PEM holds the private key material. ``from_pem``
        reads the PEM, constructs the right ``PrivateKey`` type for ``alg``,
        and returns a sender ready to send.

        Args:
            pem_path: Path to the PKCS#8 PEM, or the PEM bytes directly.
            key_id: JWK ``kid`` claim — must match the published JWK.
            alg: Signature algorithm. ``ed25519`` (default) or ``es256``.
                Also accepts the RFC 9421 form ``ecdsa-p256-sha256``.
            passphrase: Required if the PEM is encrypted
                (``adcp-keygen --encrypt``).
            client: Optional pre-built :class:`httpx.AsyncClient` to share
                across the SDK; the sender owns its own client when omitted.
            timeout_seconds: Per-request timeout for the owned client.
            allow_private_destinations: Forwarded to :meth:`__init__`.
            allowed_destination_ports: Forwarded to :meth:`__init__`.

        Raises:
            ValueError: ``alg`` is not ed25519 / es256, or the PEM contains
                a key whose type doesn't match ``alg``.
        """
        if alg in ("es256", "ES256"):
            alg = ALG_ES256
        elif alg == "EdDSA":
            alg = ALG_ED25519
        if alg not in (ALG_ED25519, ALG_ES256):
            raise ValueError(
                f"unsupported alg {alg!r} — use 'ed25519' or 'es256' "
                f"(the two AdCP webhook-signing algorithms)"
            )

        if isinstance(pem_path, bytes):
            pem_bytes = pem_path
        else:
            pem_bytes = Path(pem_path).read_bytes()

        private_key = load_private_key_pem(pem_bytes, password=passphrase)

        # The PEM's key type must match the requested alg — mixing them
        # would produce signatures no verifier can validate, and the
        # resulting error at delivery time would point at the receiver.
        # Fail here so the misconfiguration surfaces at construction.
        if alg == ALG_ED25519 and not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise ValueError(
                f"PEM holds a {type(private_key).__name__} but alg='ed25519' "
                f"was requested. Re-run adcp-keygen with --alg ed25519, or "
                f"pass alg='es256' to match the existing PEM."
            )
        if alg == ALG_ES256 and not isinstance(private_key, ec.EllipticCurvePrivateKey):
            raise ValueError(
                f"PEM holds a {type(private_key).__name__} but alg='es256' "
                f"was requested. Re-run adcp-keygen with --alg es256, or "
                f"pass alg='ed25519' to match the existing PEM."
            )

        return cls(
            private_key=private_key,
            key_id=key_id,
            alg=alg,
            client=client,
            timeout_seconds=timeout_seconds,
            allow_private_destinations=allow_private_destinations,
            allowed_destination_ports=allowed_destination_ports,
            transport_hooks=transport_hooks,
        )

    @classmethod
    def from_bearer_token(
        cls,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Build a sender that POSTs with ``Authorization: Bearer <token>``.

        For buyers who authenticate the sender at the gateway and don't
        verify body signatures. The sender's marshaling guarantees still
        apply (byte-exact JSON, idempotency_key in body); body signing
        is skipped.

        A buyer treating bearer tokens as the sole authenticity signal
        SHOULD also enforce TLS/mTLS at the transport layer — a stolen
        token is a complete forgery. Prefer JWK signing (:meth:`from_jwk`)
        for AdCP-conformant deliveries.
        """
        if not isinstance(token, str) or not token:
            raise ValueError("bearer token must be a non-empty string")
        return cls._from_strategy(
            BearerTokenStrategy(token=token),
            key_id="bearer",
            client=client,
            timeout_seconds=timeout_seconds,
            allow_private_destinations=allow_private_destinations,
            allowed_destination_ports=allowed_destination_ports,
            transport_hooks=transport_hooks,
        )

    @classmethod
    def from_adcp_legacy_hmac(
        cls,
        secret: bytes,
        *,
        key_id: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Build a sender wired to AdCP-legacy HMAC-SHA256.

        Wire format matches :func:`adcp.signing.webhook_hmac.verify_webhook_hmac`:
        ``X-AdCP-Signature: sha256=<hex>`` over ``f"{timestamp}.{body}"``,
        with ``X-AdCP-Timestamp`` set fresh per delivery (resends produce
        a new signature over the same body).

        ``secret`` is the raw HMAC key — the AdCP-legacy scheme has no
        canonical encoding, so callers pass bytes directly. ``key_id``
        is echoed in ``X-AdCP-Key-Id`` for receiver-side multi-key
        rotation; it is not used in the signature itself.

        AdCP-legacy HMAC will be removed in AdCP 4.0 — operators SHOULD
        migrate to JWK signing (:meth:`from_jwk`) ahead of that boundary.
        """
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("hmac secret must be non-empty bytes")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("key_id must be a non-empty string")
        # Mirror the receiver-side _warn_once() in webhook_hmac so a
        # sender-only operator (no receiver in this process) still sees
        # the AdCP 4.0 deprecation signal at runtime, not just in the
        # docstring.
        _warn_legacy_hmac_once()
        return cls._from_strategy(
            AdcpLegacyHmacStrategy(secret=secret, key_id=key_id),
            key_id=key_id,
            client=client,
            timeout_seconds=timeout_seconds,
            allow_private_destinations=allow_private_destinations,
            allowed_destination_ports=allowed_destination_ports,
            transport_hooks=transport_hooks,
        )

    @classmethod
    def from_standard_webhooks_secret(
        cls,
        secret: str,
        *,
        key_id: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        allow_private_destinations: bool = False,
        allowed_destination_ports: frozenset[int] | None = None,
        transport_hooks: tuple[TransportHook, ...] = (),
    ) -> WebhookSender:
        """Build a sender wired to standardwebhooks.com v1 (Svix/Resend interop).

        ``secret`` is the canonical ``whsec_<base64>`` form distributed
        by buyers running Svix, Resend, or any other Standard Webhooks
        verifier. The constructor base64-decodes the prefix-stripped
        payload internally — passing the literal ``whsec_...`` to
        :meth:`from_adcp_legacy_hmac` would silently produce signatures
        Svix rejects, which is exactly the footgun this typed split
        prevents.

        Wire format per spec: ``webhook-id`` / ``webhook-timestamp`` /
        ``webhook-signature: v1,<base64>`` over
        ``f"{webhook_id}.{webhook_timestamp}.{body}"``. Each delivery
        gets a fresh ``webhook-id`` so a receiver using webhook-id for
        its own replay cache doesn't false-positive on a legitimate
        retry — :meth:`resend` re-signs and gets a new id.
        """
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be a non-empty string (whsec_<base64>)")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("key_id must be a non-empty string")
        decoded = _decode_sw_secret(secret)
        return cls._from_strategy(
            StandardWebhooksHmacStrategy(secret=decoded, key_id=key_id),
            key_id=key_id,
            client=client,
            timeout_seconds=timeout_seconds,
            allow_private_destinations=allow_private_destinations,
            allowed_destination_ports=allowed_destination_ports,
            transport_hooks=transport_hooks,
        )

    def __repr__(self) -> str:
        # Explicit repr so no future debug helper or error traceback auto-
        # renders self.__dict__ and pulls the private key (or HMAC secret /
        # bearer token) into logs.
        return f"WebhookSender(auth={type(self._auth).__name__}, " f"key_id={self._key_id!r})"

    async def aclose(self) -> None:
        """Close the internal httpx client if we own it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> WebhookSender:
        if not self._owns_client:
            await self._get_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def send_mcp(
        self,
        *,
        url: str,
        task_id: str,
        status: GeneratedTaskStatus | str,
        task_type: TaskType | str,
        result: AdcpAsyncResponseData | dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        operation_id: str | None = None,
        message: str | None = None,
        context_id: str | None = None,
        domain: str | None = None,
        idempotency_key: str | None = None,
        token: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed MCP-style task-status webhook.

        On retry, prefer :meth:`resend` over calling this again — ``resend``
        replays the exact same bytes, whereas re-invoking ``send_mcp`` with
        the "same" args would produce a fresh ``timestamp`` and potentially
        a different serialized body, which the receiver would dedupe but
        with different observed payload data.

        :param token: Buyer-supplied token from
            ``push_notification_config.token`` echoed back on the
            payload's ``token`` field per spec
            (``schemas/cache/core/push_notification_config.json``: "Echoed
            back in webhook payload to validate request authenticity").
            Cross-language wire-parity with the JS implementation.
        """
        payload = create_mcp_webhook_payload(
            task_id=task_id,
            status=status,
            task_type=task_type,
            result=result,
            timestamp=timestamp,
            operation_id=operation_id,
            message=message,
            context_id=context_id,
            domain=domain,
            idempotency_key=idempotency_key,
            token=token,
        )
        return await self.send_raw(
            url=url,
            idempotency_key=payload.idempotency_key,
            payload=to_wire_dict(payload),
            extra_headers=extra_headers,
        )

    async def send_revocation_notification(
        self,
        *,
        url: str,
        rights_id: str,
        brand_id: str,
        reason: str,
        effective_at: str,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed rights-revocation notification."""
        key = idempotency_key or generate_webhook_idempotency_key()
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "rights_id": rights_id,
            "brand_id": brand_id,
            "reason": reason,
            "effective_at": effective_at,
        }
        return await self.send_raw(
            url=url, idempotency_key=key, payload=payload, extra_headers=extra_headers
        )

    async def send_artifact_webhook(
        self,
        *,
        url: str,
        media_buy_id: str,
        batch_id: str,
        timestamp: str,
        artifacts: list[dict[str, Any]],
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed content-standards artifact webhook."""
        key = idempotency_key or generate_webhook_idempotency_key()
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "media_buy_id": media_buy_id,
            "batch_id": batch_id,
            "timestamp": timestamp,
            "artifacts": artifacts,
        }
        return await self.send_raw(
            url=url, idempotency_key=key, payload=payload, extra_headers=extra_headers
        )

    async def send_collection_list_changed(
        self,
        *,
        url: str,
        list_id: str,
        resolved_at: str,
        signature: str,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed governance collection-list-changed webhook.

        ``signature`` is the payload-level signature field that predates 9421
        webhook transport signing — it remains required by the schema. The
        9421 signature this method adds protects the transport envelope.
        """
        key = idempotency_key or generate_webhook_idempotency_key()
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "event": "collection_list_changed",
            "list_id": list_id,
            "resolved_at": resolved_at,
            "signature": signature,
        }
        return await self.send_raw(
            url=url, idempotency_key=key, payload=payload, extra_headers=extra_headers
        )

    async def send_property_list_changed(
        self,
        *,
        url: str,
        list_id: str,
        resolved_at: str,
        signature: str,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed governance property-list-changed webhook."""
        key = idempotency_key or generate_webhook_idempotency_key()
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "event": "property_list_changed",
            "list_id": list_id,
            "resolved_at": resolved_at,
            "signature": signature,
        }
        return await self.send_raw(
            url=url, idempotency_key=key, payload=payload, extra_headers=extra_headers
        )

    async def send_raw(
        self,
        *,
        url: str,
        idempotency_key: str,
        payload: dict[str, Any],
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """Low-level escape hatch: sign + POST an arbitrary payload.

        The ``idempotency_key`` kwarg is required and is injected into the
        payload before signing — the visible signature makes the contract
        impossible to forget, unlike a runtime dict check. If ``payload``
        already carries an ``idempotency_key``, the kwarg wins so the two
        cannot disagree.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        body_dict = {**payload, "idempotency_key": idempotency_key}
        # Byte-exact serialization — this is the ONLY representation that
        # gets signed AND posted. Do not allow an httpx `json=` path anywhere
        # in the stack because it would reserialize and break the digest.
        body = json.dumps(body_dict).encode("utf-8")
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError(
                f"serialized webhook body is {len(body):,} bytes, over the "
                f"{_MAX_BODY_BYTES:,}-byte cap. Split into smaller webhooks "
                "or use batch-reporting endpoints."
            )
        return await self._send_bytes(
            url=url,
            body=body,
            idempotency_key=idempotency_key,
            extra_headers=extra_headers,
        )

    async def resend(self, result: WebhookDeliveryResult) -> WebhookDeliveryResult:
        """Replay an earlier delivery under a fresh signature.

        The bytes are identical (same ``idempotency_key``, same payload
        fields, same serialization) — only the Signature / Signature-Input /
        Content-Digest headers are regenerated. The receiver dedupes via
        ``idempotency_key``, so the replayed event is a spec-correct retry
        that won't cause double-processing.
        """
        if not result.sent_body:
            raise ValueError(
                "cannot resend: result has no captured sent_body (likely constructed "
                "externally). Call a send_* method on this sender first."
            )
        return await self._send_bytes(
            url=result.url,
            body=result.sent_body,
            idempotency_key=result.idempotency_key,
            extra_headers=result.sent_extra_headers or None,
        )

    async def _send_bytes(
        self,
        *,
        url: str,
        body: bytes,
        idempotency_key: str,
        extra_headers: Mapping[str, str] | None,
    ) -> WebhookDeliveryResult:
        """Sign + POST a pre-serialized body through an SSRF-validated transport.

        When the sender owns its httpx client (the default — ``client=None``
        was passed to ``__init__``), every delivery builds a per-request
        :class:`adcp.signing.ip_pinned_transport.AsyncIpPinnedTransport`
        that resolves the destination, runs the full SSRF range check
        (loopback / RFC 1918 / link-local / CGNAT / IPv6 ULA / multicast /
        cloud metadata), enforces the port allowlist, and pins the
        connection to the validated IP. This closes the DNS-rebinding
        TOCTOU between validate and connect.

        When the operator supplied their own client
        (``WebhookSender(client=...)`` — typically a vetted egress proxy
        with mTLS to a known buyer set, or an ASGI transport for testing),
        the sender trusts the operator's transport completely. Pin-and-bind
        is skipped; the operator's transport owns SSRF.

        On the owned-client path, SSRF validation runs **before** signing
        so a hostile URL is rejected without first generating an
        Ed25519/ES256 signature over the body. That signature would
        otherwise sit in process memory until the SSRF rejection —
        anything that snapshots locals on exception (faulthandler,
        custom logging) could capture it. Validate first, sign second.

        Transport hooks run before SSRF; the rewritten URL is what gets
        validated, signed, and POSTed. The signature covers the URL the
        request actually lands at, not the URL the caller typed —
        otherwise a receiver computing ``@target-uri`` from its observed
        Host header would see a different value and verification would
        fail. The hook output is bounded (hostname-only rewrite, scheme
        and port preserved), so this can't widen the destination space.
        """
        effective_url = apply_hooks(url, self._transport_hooks)

        # Build the pinned transport up-front for the owned-client path.
        # SSRF + port validation runs against the *post-hook* URL — the
        # one we'll actually connect to. A hostile URL raises
        # SSRFValidationError here and the body never gets signed (no
        # signature material to leak via faulthandler / custom logging
        # on exception).
        transport: AsyncIpPinnedTransport | None = None
        if self._owns_client:
            transport = build_async_ip_pinned_transport(
                effective_url,
                allow_private=self._allow_private_destinations,
                allowed_ports=self._allowed_destination_ports,
            )

        base_headers = {"Content-Type": "application/json"}
        auth_headers = self._auth.build_auth_headers(method="POST", url=effective_url, body=body)
        headers = merge_extra_headers(
            base={**base_headers, **auth_headers},
            extra=extra_headers,
            reserved=self._auth.reserved_headers(),
        )

        if transport is not None:
            # Owned-client path. ``trust_env=False`` prevents httpx from
            # routing the request through ``HTTPS_PROXY`` / ``HTTP_PROXY``
            # env vars — every other pinned-transport callsite in the
            # codebase sets this for the same reason (default_jwks_fetcher,
            # async_default_jwks_fetcher, revocation_fetcher). Without it,
            # an attacker who controls process env can route the signed
            # webhook through their endpoint, defeating the IP pin entirely.
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(effective_url, content=body, headers=headers)
        else:
            # Operator-supplied client — they own the SSRF guarantees on
            # their transport (proxy allowlist, mTLS, etc.). Reachable as
            # None after aclose(); explicit raise survives ``python -O``
            # which would strip an assert.
            if self._client is None:
                raise RuntimeError(
                    "WebhookSender's operator-supplied client was already "
                    "closed. Construct a new sender or pass a fresh client."
                )
            response = await self._client.post(effective_url, content=body, headers=headers)

        return WebhookDeliveryResult(
            status_code=response.status_code,
            idempotency_key=idempotency_key,
            url=effective_url,
            response_headers=dict(response.headers),
            response_body=response.content,
            sent_body=body,
            sent_extra_headers=dict(extra_headers) if extra_headers else {},
        )


__all__ = [
    "DockerLocalhostRewrite",
    "TransportHook",
    "WebhookDeliveryResult",
    "WebhookSender",
]
