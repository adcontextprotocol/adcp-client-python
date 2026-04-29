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
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.signing.webhook_signer import sign_webhook
from adcp.types import GeneratedTaskStatus
from adcp.types.generated_poc.core.async_response_data import AdcpAsyncResponseData
from adcp.webhooks import (
    create_mcp_webhook_payload,
    generate_webhook_idempotency_key,
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
    ) -> None:
        self._private_key = private_key
        self._key_id = key_id
        self._alg = alg
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._allow_private_destinations = allow_private_destinations
        self._allowed_destination_ports = allowed_destination_ports

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
        )

    def __repr__(self) -> str:
        # Explicit repr so no future debug helper or error traceback auto-
        # renders self.__dict__ and pulls the private key into logs.
        return f"WebhookSender(key_id={self._key_id!r}, alg={self._alg!r})"

    async def aclose(self) -> None:
        """Close the internal httpx client if we own it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> WebhookSender:
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
        task_type: str | None = None,
        result: AdcpAsyncResponseData | dict[str, Any] | None = None,
        timestamp: datetime | None = None,
        operation_id: str | None = None,
        message: str | None = None,
        context_id: str | None = None,
        domain: str | None = None,
        idempotency_key: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WebhookDeliveryResult:
        """POST a signed MCP-style task-status webhook.

        On retry, prefer :meth:`resend` over calling this again — ``resend``
        replays the exact same bytes, whereas re-invoking ``send_mcp`` with
        the "same" args would produce a fresh ``timestamp`` and potentially
        a different serialized body, which the receiver would dedupe but
        with different observed payload data.
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
        )
        return await self.send_raw(
            url=url,
            idempotency_key=str(payload["idempotency_key"]),
            payload=payload,
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
        """
        base_headers = {"Content-Type": "application/json"}
        signed = sign_webhook(
            method="POST",
            url=url,
            headers=base_headers,
            body=body,
            private_key=self._private_key,
            key_id=self._key_id,
            alg=self._alg,
        )
        headers: dict[str, str] = {**base_headers, **signed.as_dict()}
        if extra_headers:
            # Pre-scan so a bad extra_header doesn't leave half-merged state.
            reserved = {"signature", "signature-input", "content-digest", "content-type"}
            for k in extra_headers:
                if str(k).lower() in reserved:
                    raise ValueError(
                        f"extra_headers may not override signature-binding or "
                        f"content-type header {k!r}"
                    )
            for k, v in extra_headers.items():
                headers[k] = v

        if self._owns_client:
            # Per-request pinned transport. Building one client per delivery
            # is the security-correct choice: keeping a pinned transport
            # alive across deliveries to the same hostname would defeat the
            # rebinding defense (the IP would be frozen at first delivery).
            transport = build_async_ip_pinned_transport(
                url,
                allow_private=self._allow_private_destinations,
                allowed_ports=self._allowed_destination_ports,
            )
            async with httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, content=body, headers=headers)
        else:
            # Operator-supplied client — they own the SSRF guarantees on
            # their transport (proxy allowlist, mTLS, etc.). Reachable as
            # None after aclose(); a runtime check beats an assert that
            # python -O strips to silently NoneType.post().
            if self._client is None:
                raise RuntimeError(
                    "WebhookSender's operator-supplied client was already "
                    "closed. Construct a new sender or pass a fresh client."
                )
            response = await self._client.post(url, content=body, headers=headers)

        return WebhookDeliveryResult(
            status_code=response.status_code,
            idempotency_key=idempotency_key,
            url=url,
            response_headers=dict(response.headers),
            response_body=response.content,
            sent_body=body,
            sent_extra_headers=dict(extra_headers) if extra_headers else {},
        )


__all__ = ["WebhookDeliveryResult", "WebhookSender"]
