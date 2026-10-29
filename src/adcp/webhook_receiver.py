"""One-call webhook receiver: verify signature, dedupe, parse.

Packages the things every AdCP webhook receiver has to do so callers
don't re-read the five-point normative checklist in ``webhooks.mdx``:

0. Bound the body before anything expensive runs (AdCP 3.3 ``security.mdx``
   §Inbound body bounds): pre-reject an over-cap ``Content-Length``, cap the
   bytes, then hold the JSON to depth 64 and 100,000 members -- all before
   RFC 9421 or HMAC verification. Over-limit bodies get 413, never truncation.
1. Verify the RFC 9421 signature (or fall back to HMAC-SHA256 when the buyer
   explicitly opts in for 3.x migration).
2. Resolve a trusted stable publisher scope from the authenticated key, then
   claim ``(receiver_scope, publisher_scope, idempotency_key, JCS payload)``
   for at least the publisher's advertised retry horizon.
3. Parse the body into the right typed payload so the caller gets a
   ``McpWebhookPayload`` / ``RevocationNotification`` / etc. back.
4. Mark the claim handled only after application publication succeeds.

Usage::

    from adcp.webhooks import (
        WebhookReceiver,
        WebhookReceiverConfig,
        WebhookVerifyOptions,
    )
    from adcp.server.idempotency import MemoryBackend, WebhookDedupStore

    receiver = WebhookReceiver(
        config=WebhookReceiverConfig(
            verify_options=WebhookVerifyOptions(
                jwks_resolver=my_jwks_resolver,
                replay_store=my_replay_store,
            ),
            # Match or exceed the publisher's advertised
            # webhook_signing.delivery_retry_horizon_seconds.
            dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
            receiver_scope="buyer-account-123",
            publisher_scope_for=lambda _signer: "seller-agent-456",
        ),
    )

    @app.post("/webhooks/adcp")
    async def hook(request):
        headers = dict(request.headers)
        try:
            # Counts bytes as they arrive and stops at the cap, rather than
            # letting `await request.body()` buffer whatever was sent.
            body = await read_inbound_body(
                request.stream(), headers=headers, max_bytes=receiver.max_body_bytes
            )
        except InboundBodyRejectedError as exc:
            return Response(status_code=exc.status_code, headers={"Connection": "close"})
        outcome = await receiver.receive(
            method=request.method,
            url=str(request.url),
            headers=headers,
            body=body,
        )
        if outcome.http_status is not None:
            return Response(
                status_code=outcome.http_status, headers=outcome.response_headers
            )
        try:
            await process(outcome.payload)
        except Exception:
            await receiver.release(outcome)
            raise
        await receiver.acknowledge(outcome)
        return Response(status_code=200)
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, cast, runtime_checkable

import rfc8785
from pydantic import ValidationError

from adcp._json_bounds import (
    DEFAULT_MAX_INBOUND_BODY_BYTES,
    MAX_INBOUND_JSON_MEMBERS,
    MAX_JSON_DEPTH,
    JsonBoundsError,
    check_json_bounds,
)
from adcp.server.idempotency.webhook_dedup import WebhookDedupStore
from adcp.signing.errors import (
    WEBHOOK_BODY_MALFORMED,
    WEBHOOK_ERROR_CODES,
    WEBHOOK_SIGNATURE_HEADER_MALFORMED,
    SignatureVerificationError,
    signature_challenge,
)
from adcp.signing.webhook_hmac import (
    LegacyWebhookHmacError,
    LegacyWebhookHmacOptions,
    verify_webhook_hmac,
)
from adcp.signing.webhook_verifier import (
    VerifiedWebhookSender,
    WebhookVerifyOptions,
    verify_webhook_signature,
)
from adcp.types.domains.brand.revocation_notification import RevocationNotification
from adcp.types.domains.collection.collection_list_changed_webhook import (
    CollectionListChangedWebhook,
)
from adcp.types.domains.content_standards.artifact_webhook_payload import (
    ArtifactWebhookPayload,
)
from adcp.types.domains.core.mcp_webhook_payload import McpWebhookPayload
from adcp.types.domains.property.property_list_changed_webhook import (
    PropertyListChangedWebhook,
)

logger = logging.getLogger(__name__)


class _DuplicateJsonKeyError(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON object member: {key}")
        result[key] = value
    return result


WebhookKind = Literal[
    "mcp",
    "revocation_notification",
    "collection_list_changed",
    "property_list_changed",
    "artifact",
]

WebhookPayload = (
    McpWebhookPayload
    | RevocationNotification
    | CollectionListChangedWebhook
    | PropertyListChangedWebhook
    | ArtifactWebhookPayload
)

RejectionReason = Literal[
    "signature_missing",
    "signature_invalid",
    "signature_legacy_failed",
    "content_type_invalid",
    "body_invalid_json",
    "payload_invalid",
    "idempotency_key_missing",
    "idempotency_key_invalid",
    "idempotency_conflict",
    "payload_not_i_json",
    "body_too_large",
    "body_too_complex",
    "body_framing_invalid",
    "content_encoding_unsupported",
]

# Spec: ^[A-Za-z0-9_.:-]{16,255}$ per security.mdx §Idempotency. Enforce at the
# receiver so a non-conformant sender can't churn dedup storage with
# unreasonable key lengths or exotic charsets.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{16,255}$")

_ACCEPTED_CONTENT_TYPES = ("application/json",)

DEFAULT_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
"""Default byte cap on an inbound webhook or push-notification body (1 MiB).

``webhooks.mdx`` §Security requirements sets a 1 MB payload limit for webhook
deliveries -- MCP-shaped and A2A push notifications alike -- and under the
precedence rule of ``security.mdx`` §Inbound body bounds that tighter
per-feature cap governs over the 4 MiB generic default
(:data:`DEFAULT_MAX_INBOUND_BODY_BYTES`). Raise it per endpoint via
:attr:`WebhookReceiverConfig.max_body_bytes` or the ``max_bytes`` argument of
:func:`read_inbound_body` / :func:`check_inbound_body` only for a known sender
with a reason to send more.
"""

DEFAULT_INBOUND_IDLE_TIMEOUT_SECONDS = 10.0
"""Longest wait for the next body chunk in :func:`read_inbound_body`."""

DEFAULT_INBOUND_TOTAL_TIMEOUT_SECONDS = 10.0
"""Deadline for reading the whole body in :func:`read_inbound_body`."""

InboundRejectionReason = Literal[
    "body_too_large",
    "body_too_complex",
    "body_framing_invalid",
    "content_encoding_unsupported",
    "body_timeout",
]

_INBOUND_STATUS: dict[str, int] = {
    "body_too_large": 413,
    "body_too_complex": 413,
    "body_framing_invalid": 400,
    "content_encoding_unsupported": 415,
    "body_timeout": 408,
}


class InboundBodyRejectedError(ValueError):
    """An inbound push or webhook body was refused before verification.

    ``reason`` says why and ``status_code`` is the HTTP status to answer with:
    413 for a byte, depth, or member violation (AdCP 3.3 ``security.mdx``
    §Inbound body bounds item 5), 400 for ambiguous framing, 415 for a
    content coding other than identity, 408 for a missed read deadline. The
    rejection happens before signature verification, so it is an HTTP status,
    not a ``webhook_*`` verification code. The body is never truncated, and
    nothing from it appears in the exception -- it has passed no verification
    or string hygiene. A sender gets the same answer on every retry, and the
    caller should close the connection when the body was not fully read.
    """

    def __init__(self, reason: InboundRejectionReason, message: str) -> None:
        self.reason: InboundRejectionReason = reason
        self.status_code = _INBOUND_STATUS[reason]
        super().__init__(message)


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    """Every comma-separated value of ``name``, across case-variant keys."""
    values: list[str] = []
    for key, value in headers.items():
        if str(key).lower() == name:
            values.extend(part.strip() for part in str(value).split(","))
    return values


def _check_inbound_headers(headers: Mapping[str, str], *, max_bytes: int) -> None:
    """Framing, coding and declared-length checks, before any body byte is read."""
    lengths = _header_values(headers, "content-length")
    transfer_codings = [v.lower() for v in _header_values(headers, "transfer-encoding") if v]
    if lengths and "chunked" in transfer_codings:
        raise InboundBodyRejectedError(
            "body_framing_invalid",
            "request carries both Content-Length and Transfer-Encoding: chunked",
        )
    declared: int | None = None
    if lengths:
        if any(not value.isascii() or not value.isdigit() for value in lengths):
            raise InboundBodyRejectedError("body_framing_invalid", "Content-Length is not a number")
        if len({int(value) for value in lengths}) != 1:
            raise InboundBodyRejectedError(
                "body_framing_invalid", "request carries conflicting Content-Length values"
            )
        declared = int(lengths[0])
    codings = [v.lower() for v in _header_values(headers, "content-encoding") if v]
    if any(coding != "identity" for coding in codings):
        # Nothing in the webhook profile needs a compressed body, and
        # inflating one before verification is the expansion this bound
        # exists to stop (security.mdx §Inbound body bounds item 2).
        raise InboundBodyRejectedError(
            "content_encoding_unsupported", "webhook bodies must not be content-encoded"
        )
    if declared is not None and declared > max_bytes:
        raise InboundBodyRejectedError(
            "body_too_large",
            f"declared Content-Length {declared} exceeds the {max_bytes}-byte cap",
        )


def _check_body_structure(body: bytes) -> None:
    try:
        check_json_bounds(body, max_depth=MAX_JSON_DEPTH, max_members=MAX_INBOUND_JSON_MEMBERS)
    except JsonBoundsError as exc:
        raise InboundBodyRejectedError("body_too_complex", str(exc)) from None


def _validate_max_bytes(max_bytes: int) -> int:
    # No unbounded mode: security.mdx §Inbound body bounds forbids shipping one
    # as the default, and a None/0 sentinel is too easy to set by accident.
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"max_bytes must be a positive int, got {max_bytes!r}")
    return max_bytes


def check_inbound_body(
    body: bytes,
    *,
    headers: Mapping[str, str],
    max_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES,
) -> None:
    """Apply the inbound body bounds to an already-buffered push/webhook body.

    Use before RFC 9421, Web Bot Auth, HMAC or Bearer verification of an A2A
    push notification or AdCP webhook. In order: ambiguous framing (both
    ``Content-Length`` and ``Transfer-Encoding: chunked``, or conflicting
    lengths) and any ``Content-Encoding`` other than identity are refused; a
    ``Content-Length`` or body over ``max_bytes`` is refused; then the JSON is
    held to depth 64 and 100,000 members by a counting scanner, without
    parsing it. A body that is not JSON at all passes, and is left to the
    caller's parser to reject.

    This does not bound memory already spent buffering the body; prefer
    :func:`read_inbound_body` where the framework exposes a body stream.

    :raises InboundBodyRejectedError: the body must be refused with
        ``exc.status_code``.
    """
    _validate_max_bytes(max_bytes)
    _check_inbound_headers(headers, max_bytes=max_bytes)
    if len(body) > max_bytes:
        raise InboundBodyRejectedError(
            "body_too_large", f"body of {len(body)} bytes exceeds the {max_bytes}-byte cap"
        )
    _check_body_structure(body)


async def read_inbound_body(
    chunks: AsyncIterable[bytes],
    *,
    headers: Mapping[str, str],
    max_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES,
    idle_timeout: float | None = DEFAULT_INBOUND_IDLE_TIMEOUT_SECONDS,
    total_timeout: float | None = DEFAULT_INBOUND_TOTAL_TIMEOUT_SECONDS,
) -> bytes:
    """Read a push/webhook body under the inbound body bounds, then return it.

    The streaming form of :func:`check_inbound_body`, for frameworks that
    expose the request body as an async byte stream (Starlette/FastAPI
    ``request.stream()``, aiohttp ``request.content.iter_any()``). The
    ``Content-Length`` and framing checks run before the first chunk is
    read; bytes are counted as they arrive and the read stops the moment the
    count passes ``max_bytes`` -- a missing or understated ``Content-Length``
    does not exempt a body. ``idle_timeout`` bounds the wait for each chunk
    and ``total_timeout`` the whole read, so a peer that trickles bytes
    cannot hold the worker. The depth and member bounds then run on the
    admitted bytes. Server connect and header-read deadlines belong to the
    ASGI/WSGI server configuration.

    The stream must yield the bytes the peer sent. Content-encoded bodies are
    refused rather than decoded, so raw and decoded counts are the same.

    :raises InboundBodyRejectedError: the body must be refused with
        ``exc.status_code``; close the connection, since the body may not
        have been read in full.
    """
    _validate_max_bytes(max_bytes)
    _check_inbound_headers(headers, max_bytes=max_bytes)
    loop = asyncio.get_running_loop()
    deadline = None if total_timeout is None else loop.time() + total_timeout
    iterator = chunks.__aiter__()
    parts: list[bytes] = []
    received = 0
    while True:
        wait = idle_timeout
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise InboundBodyRejectedError(
                    "body_timeout", "request body read deadline exceeded"
                )
            wait = remaining if wait is None else min(wait, remaining)
        try:
            if wait is None:
                chunk = await iterator.__anext__()
            else:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=wait)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise InboundBodyRejectedError(
                "body_timeout", "request body read deadline exceeded"
            ) from None
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise InboundBodyRejectedError(
                "body_too_large",
                f"body exceeds the {max_bytes}-byte cap (read at least {received} bytes)",
            )
        parts.append(bytes(chunk))
    body = b"".join(parts)
    _check_body_structure(body)
    return body


@dataclass(frozen=True)
class LegacyHmacFallback:
    """Opt-in policy for accepting HMAC-SHA256 senders during 3.x migration.

    The default behavior of the receiver is to reject any request that fails
    9421 verification. Pass an instance of this class to ``WebhookReceiverConfig``
    to accept HMAC-signed webhooks as a fallback.

    :param options_for: callback that returns a populated
        :class:`LegacyWebhookHmacOptions` given the incoming request headers.
        Your implementation resolves the sender (from Bearer, hostname, or
        legacy shared-secret tag) and returns the secret + sender_identity
        tuple the verifier needs. Return ``None`` to decline the fallback
        for this request (rejection follows the 9421-only failure path).
    :param only_when_9421_absent: when ``True`` (default), HMAC fallback only
        fires when no 9421 headers are present at all. When a request carries
        9421 headers that FAIL verification, it still rejects — preventing a
        downgrade attack where a MITM strips the 9421 signature and replaces
        it with a forged HMAC one it knows the secret for. When ``False``,
        HMAC is tried on any 9421 failure; only set this for testing or known
        homogenous sender cohorts.
    """

    options_for: Callable[[Mapping[str, str]], LegacyWebhookHmacOptions | None]
    only_when_9421_absent: bool = True

    @classmethod
    def from_shared_secret(
        cls,
        *,
        secret: bytes,
        sender_identity: str,
        only_when_9421_absent: bool = True,
        window_seconds: int = 300,
    ) -> LegacyHmacFallback:
        """Convenience constructor for the "one secret, one sender" case.

        Covers the common 3.x migration setup where the receiver has exactly
        one publisher on the legacy scheme and binds them to a known
        ``sender_identity`` (typically a buyer-defined string). For multi-
        sender or header-derived-identity setups, construct with an
        ``options_for`` callback directly.
        """
        import time as _time

        def _options_for(_headers: Mapping[str, str]) -> LegacyWebhookHmacOptions:
            return LegacyWebhookHmacOptions(
                secret=secret,
                sender_identity=sender_identity,
                now=_time.time(),
                window_seconds=window_seconds,
            )

        return cls(
            options_for=_options_for,
            only_when_9421_absent=only_when_9421_absent,
        )


@dataclass(frozen=True)
class WebhookReceiverConfig:
    """Configuration bundle.

    :param verify_options: verifier configuration (JWKS, replay store, etc.).
        A single instance is reused for every request — the verifier stamps
        ``now`` itself via ``verify_options.clock()``, so there's no need to
        refresh a time field per request.
    :param dedup: webhook-dedup store.
    :param receiver_scope: trusted tenant/subscription/endpoint scope for this
        receiver instance. Never derive it from the webhook payload.
    :param publisher_scope_for: maps the verified signing identity to a stable
        seller/publisher identity. The returned value must survive signing-key
        rotation; key IDs are authentication evidence, not publication scope.
    :param legacy_hmac: optional HMAC-SHA256 fallback for 3.x migration.
    :param kind: which webhook payload type to parse into. Default ``"mcp"``
        (the task-status webhook that dominates most integrations); pass
        explicitly for list-change / artifact / revocation receivers.
    :param max_body_bytes: byte cap enforced before verification. Defaults to
        :data:`DEFAULT_MAX_WEBHOOK_BODY_BYTES` (1 MiB, the ``webhooks.mdx``
        payload limit). Must be a positive int; there is no unbounded mode.
    """

    verify_options: WebhookVerifyOptions
    dedup: WebhookDedupStore
    receiver_scope: str
    publisher_scope_for: Callable[[VerifiedSignerLike], str]
    legacy_hmac: LegacyHmacFallback | None = None
    kind: WebhookKind = "mcp"
    max_body_bytes: int = DEFAULT_MAX_WEBHOOK_BODY_BYTES


@dataclass(frozen=True)
class WebhookOutcome:
    """Result of a single ``receive`` call.

    ``duplicate`` means the exact delivery was durably handled and should be
    acknowledged with 2xx. ``in_progress`` means another owner is publishing
    the identical delivery and should receive 503. A fresh claim has neither
    flag and must be passed to :meth:`WebhookReceiver.acknowledge` only after
    application publication succeeds (or to ``release`` on failure).
    """

    rejected: bool = False
    rejection_reason: RejectionReason | None = None
    response_headers: Mapping[str, str] = field(default_factory=dict)
    # Populated on successful verify (even when rejected downstream of crypto)
    sender_identity: str | None = None
    # Populated on successful verify + parse
    payload: WebhookPayload | None = None
    duplicate: bool = False
    in_progress: bool = False
    handled: bool = False
    idempotency_key: str | None = None
    # Set on a ``signature_invalid`` rejection whose cause could clear on a
    # later attempt (the receiver's own JWKS or brand.json fetch failed on
    # DNS, network or a 5xx). The wire code is ``webhook_signature_key_unknown``
    # either way; this keeps the distinction for local retry and alerting.
    transient: bool = False
    _payload_hash: str | None = field(default=None, repr=False, compare=False)
    _claim_token: str | None = field(default=None, repr=False, compare=False)
    _dedup_scope: str | None = field(default=None, repr=False, compare=False)
    _observation_scope: str | None = field(default=None, repr=False, compare=False)
    _observation_key: str | None = field(default=None, repr=False, compare=False)
    _observation_hash: str | None = field(default=None, repr=False, compare=False)
    _observation_token: str | None = field(default=None, repr=False, compare=False)

    @property
    def http_status(self) -> int | None:
        """Recommended immediate HTTP status, or ``None`` for a fresh claim."""
        if self.rejected:
            if self.rejection_reason in {"body_too_large", "body_too_complex"}:
                return 413
            if self.rejection_reason == "content_encoding_unsupported":
                return 415
            if self.rejection_reason == "idempotency_conflict":
                return 409
            if self.rejection_reason in {
                "signature_missing",
                "signature_invalid",
                "signature_legacy_failed",
            }:
                return 401
            return 400
        if self.in_progress:
            return 503
        if self.duplicate or self.handled:
            return 200
        return None


@runtime_checkable
class VerifiedSignerLike(Protocol):
    """Anything with ``as_sender_identity() -> str``.

    Both :class:`VerifiedWebhookSender` (9421) and :class:`VerifiedLegacyWebhookSender`
    (HMAC) implement this shape, so the receiver treats both verification
    paths identically downstream.
    """

    def as_sender_identity(self) -> str: ...


class WebhookReceiver:
    """Stateless webhook entry point, one instance per receiver configuration.

    Instance state (``config``) is read-only after construction. Per-request
    state lives in the :class:`WebhookOutcome` returned from :meth:`receive`.
    """

    def __init__(self, config: WebhookReceiverConfig) -> None:
        if not config.receiver_scope:
            raise ValueError("WebhookReceiverConfig.receiver_scope must be non-empty")
        try:
            _validate_max_bytes(config.max_body_bytes)
        except ValueError:
            raise ValueError(
                "WebhookReceiverConfig.max_body_bytes must be a positive int, "
                f"got {config.max_body_bytes!r}"
            ) from None
        self._config = config

    @property
    def max_body_bytes(self) -> int:
        """The byte cap in force, for :func:`read_inbound_body`."""
        return self._config.max_body_bytes

    async def receive(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookOutcome:
        """Verify, dedupe, parse. Returns a :class:`WebhookOutcome`.

        Never raises for sender-caused cryptographic or protocol failures —
        returns an outcome with ``rejected=True`` and populated
        ``response_headers`` so the caller can convert to an HTTP response
        without try/except around every call. Operational failures inside
        the dedup backend or verify-options factory MAY still raise; wrap
        the call if you need to 5xx cleanly on internal errors.

        The inbound body bounds run first, before any signature work: a
        ``Content-Length`` or body over ``max_body_bytes``, or JSON deeper
        than 64 or wider than 100,000 members, is rejected as
        ``body_too_large`` / ``body_too_complex`` (HTTP 413). ``body`` has
        already been buffered by the time this runs; to stop reading at the
        cap, obtain it with :func:`read_inbound_body`.
        """
        if not _content_type_is_json(headers):
            return _reject("content_type_invalid", sender_identity=None)
        try:
            check_inbound_body(body, headers=headers, max_bytes=self._config.max_body_bytes)
        except InboundBodyRejectedError as exc:
            # check_inbound_body never reads, so it never reports a timeout;
            # every other inbound reason is also a RejectionReason.
            return _reject(cast(RejectionReason, exc.reason), sender_identity=None)

        signer, rejection = await self._verify(method=method, url=url, headers=headers, body=body)
        if rejection is not None:
            return rejection
        assert signer is not None  # verification succeeded

        sender_id = signer.as_sender_identity()
        publisher_scope = self._config.publisher_scope_for(signer)
        if not isinstance(publisher_scope, str) or not publisher_scope:
            raise ValueError("publisher_scope_for must return a non-empty string")
        dedup_scope = json.dumps(
            [self._config.receiver_scope, publisher_scope, "delivery"],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        observation_scope = json.dumps(
            [self._config.receiver_scope, publisher_scope, "terminal-observation"],
            ensure_ascii=True,
            separators=(",", ":"),
        )

        # The depth and member bounds held before verification, so this
        # parse is over a document of known-bounded shape.
        try:
            payload_dict = json.loads(body, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, _DuplicateJsonKeyError, RecursionError):
            return _reject("body_invalid_json", sender_identity=sender_id)
        if not isinstance(payload_dict, dict):
            return _reject("body_invalid_json", sender_identity=sender_id)

        idempotency_key = payload_dict.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            # Spec 3.0-rc: idempotency_key is REQUIRED on every webhook payload.
            return _reject("idempotency_key_missing", sender_identity=sender_id)
        if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
            # Non-conformant format — charset or length out of bounds.
            return _reject("idempotency_key_invalid", sender_identity=sender_id)

        try:
            canonical_payload = rfc8785.dumps(payload_dict)
        except (TypeError, ValueError, RecursionError):
            return _reject("payload_not_i_json", sender_identity=sender_id)
        payload_hash = hashlib.sha256(canonical_payload).hexdigest()
        claim = await self._config.dedup.claim(
            sender_id=dedup_scope,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
        )

        if claim.status == "conflict":
            return WebhookOutcome(
                rejected=True,
                rejection_reason="idempotency_conflict",
                sender_identity=sender_id,
                idempotency_key=idempotency_key,
                _payload_hash=payload_hash,
                _dedup_scope=dedup_scope,
            )

        parsed = self._parse(payload_dict)
        if parsed is None:
            if claim.status == "claimed":
                assert claim.claim_token is not None
                await self._config.dedup.complete(
                    dedup_scope,
                    idempotency_key,
                    payload_hash,
                    claim.claim_token,
                )
            return _reject("payload_invalid", sender_identity=sender_id)

        observation = _terminal_observation(payload_dict, self._config.kind)
        observation_claim = None
        if claim.status == "claimed" and observation is not None:
            observation_key, observation_hash = observation
            observation_claim = await self._config.dedup.claim(
                sender_id=observation_scope,
                idempotency_key=observation_key,
                payload_hash=observation_hash,
            )
            assert claim.claim_token is not None
            if observation_claim.status == "conflict":
                await self._config.dedup.release(
                    dedup_scope,
                    idempotency_key,
                    payload_hash,
                    claim.claim_token,
                )
                return WebhookOutcome(
                    rejected=True,
                    rejection_reason="idempotency_conflict",
                    sender_identity=sender_id,
                    payload=parsed,
                    idempotency_key=idempotency_key,
                    _payload_hash=payload_hash,
                    _dedup_scope=dedup_scope,
                )
            if observation_claim.status == "handled":
                await self._config.dedup.complete(
                    dedup_scope,
                    idempotency_key,
                    payload_hash,
                    claim.claim_token,
                )
                return WebhookOutcome(
                    sender_identity=sender_id,
                    payload=parsed,
                    duplicate=True,
                    idempotency_key=idempotency_key,
                    _payload_hash=payload_hash,
                    _dedup_scope=dedup_scope,
                )
            if observation_claim.status == "in_progress":
                await self._config.dedup.release(
                    dedup_scope,
                    idempotency_key,
                    payload_hash,
                    claim.claim_token,
                )
                return WebhookOutcome(
                    sender_identity=sender_id,
                    payload=parsed,
                    in_progress=True,
                    idempotency_key=idempotency_key,
                    _payload_hash=payload_hash,
                    _dedup_scope=dedup_scope,
                )

        return WebhookOutcome(
            sender_identity=sender_id,
            payload=parsed,
            duplicate=claim.status == "handled",
            in_progress=claim.status == "in_progress",
            idempotency_key=idempotency_key,
            _payload_hash=payload_hash,
            _claim_token=claim.claim_token,
            _dedup_scope=dedup_scope,
            _observation_scope=observation_scope if observation is not None else None,
            _observation_key=observation[0] if observation is not None else None,
            _observation_hash=observation[1] if observation is not None else None,
            _observation_token=(
                observation_claim.claim_token if observation_claim is not None else None
            ),
        )

    async def acknowledge(self, outcome: WebhookOutcome) -> WebhookOutcome:
        """Durably acknowledge a fresh claim after publication succeeds."""
        sender_id, key, payload_hash, token = self._owned_claim(outcome)
        if outcome._observation_token is not None:
            assert outcome._observation_scope is not None
            assert outcome._observation_key is not None
            assert outcome._observation_hash is not None
            await self._config.dedup.complete(
                outcome._observation_scope,
                outcome._observation_key,
                outcome._observation_hash,
                outcome._observation_token,
            )
        await self._config.dedup.complete(sender_id, key, payload_hash, token)
        return replace(
            outcome,
            handled=True,
            _claim_token=None,
            _observation_token=None,
        )

    async def release(self, outcome: WebhookOutcome) -> WebhookOutcome:
        """Release a failed publication while retaining immutable payload binding."""
        sender_id, key, payload_hash, token = self._owned_claim(outcome)
        if outcome._observation_token is not None:
            assert outcome._observation_scope is not None
            assert outcome._observation_key is not None
            assert outcome._observation_hash is not None
            await self._config.dedup.release(
                outcome._observation_scope,
                outcome._observation_key,
                outcome._observation_hash,
                outcome._observation_token,
            )
        await self._config.dedup.release(sender_id, key, payload_hash, token)
        return replace(outcome, _claim_token=None, _observation_token=None)

    async def receive_and_process(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        handler: Callable[[WebhookPayload], Awaitable[None] | None],
    ) -> WebhookOutcome:
        """Verify, claim, publish, and durably acknowledge one delivery.

        Existing duplicates, conflicts, and in-progress deliveries return
        without calling ``handler``. Handler failures release the owner lease
        for an exact retry and are re-raised so the HTTP framework can return
        a retryable 5xx.
        """
        outcome = await self.receive(method=method, url=url, headers=headers, body=body)
        if outcome.http_status is not None:
            return outcome
        assert outcome.payload is not None
        try:
            result = handler(outcome.payload)
            if inspect.isawaitable(result):
                await result
        except BaseException:
            await self.release(outcome)
            raise
        return await self.acknowledge(outcome)

    @staticmethod
    def _owned_claim(outcome: WebhookOutcome) -> tuple[str, str, str, str]:
        values = (
            outcome._dedup_scope,
            outcome.idempotency_key,
            outcome._payload_hash,
            outcome._claim_token,
        )
        if not all(isinstance(value, str) and value for value in values):
            raise ValueError("WebhookOutcome does not carry an owned delivery claim")
        return cast(tuple[str, str, str, str], values)

    def receive_sync(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> WebhookOutcome:
        """Synchronous wrapper around :meth:`receive` for WSGI-style frameworks.

        This is the low-level verification/claim surface. A fresh outcome
        still requires asynchronous ``acknowledge`` or ``release``. Sync-only
        applications should normally use :meth:`receive_and_process_sync`,
        which owns the complete claim lifecycle in one event loop.

            @app.post("/webhooks/adcp")
            def hook():
                outcome = receiver.receive_sync(
                    method=request.method,
                    url=request.url,
                    headers=dict(request.headers),
                    body=request.get_data(),
                )
                ...

        Raises :class:`RuntimeError` if invoked from a thread that already has
        a running event loop — the underlying verify / dedup path is async and
        cannot be driven from inside an active loop without blocking it. From
        async code, call :meth:`receive` directly.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop in this thread — safe to spin one up.
            return asyncio.run(self.receive(method=method, url=url, headers=headers, body=body))
        raise RuntimeError(
            "WebhookReceiver.receive_sync() cannot be called from a running "
            "event loop. Use `await receiver.receive(...)` instead."
        )

    def receive_and_process_sync(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        handler: Callable[[WebhookPayload], None],
    ) -> WebhookOutcome:
        """Sync entry point that publishes and settles a delivery atomically."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.receive_and_process(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    handler=handler,
                )
            )
        raise RuntimeError(
            "WebhookReceiver.receive_and_process_sync() cannot be called from a "
            "running event loop. Use `await receiver.receive_and_process(...)` instead."
        )

    async def _verify(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[VerifiedSignerLike | None, WebhookOutcome | None]:
        """Returns (signer, None) on success or (None, rejection_outcome)."""
        has_9421 = _has_9421_headers(headers)

        if has_9421:
            try:
                signer = verify_webhook_signature(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    options=self._config.verify_options,
                )
                return signer, None
            except SignatureVerificationError as exc:
                if exc.code == WEBHOOK_BODY_MALFORMED:
                    # Step 14: the signature verified (nonce already burned)
                    # but the body is not strict JSON. A body failure, not an
                    # auth failure -- and never a reason to try HMAC.
                    sender = getattr(exc, "signer", None)
                    return None, _reject(
                        "body_invalid_json",
                        sender_identity=(
                            sender.as_sender_identity()
                            if isinstance(sender, VerifiedWebhookSender)
                            else None
                        ),
                    )
                # Downgrade defense: when 9421 IS present but fails, do NOT
                # consult HMAC fallback by default. A MITM that stripped a
                # valid 9421 signature and replaced it with a forged HMAC one
                # is exactly what the downgrade guard exists for.
                fallback = self._config.legacy_hmac
                allow_hmac = fallback is not None and not fallback.only_when_9421_absent
                if not allow_hmac:
                    return None, WebhookOutcome(
                        rejected=True,
                        rejection_reason="signature_invalid",
                        response_headers=_www_authenticate_header(exc.code),
                        transient=exc.transient,
                    )
                logger.warning(
                    "9421 webhook verify failed (%s); trying HMAC legacy because "
                    "legacy_hmac.only_when_9421_absent=False is set",
                    exc.code,
                )

        fallback = self._config.legacy_hmac
        if fallback is None:
            # No 9421 headers AND no HMAC fallback configured → spec says 9421
            # is baseline-required in 3.0, so this is non-conformant.
            return None, WebhookOutcome(
                rejected=True,
                rejection_reason="signature_missing",
                response_headers=_www_authenticate_header(WEBHOOK_SIGNATURE_HEADER_MALFORMED),
            )

        hmac_options = fallback.options_for(headers)
        if hmac_options is None:
            return None, WebhookOutcome(
                rejected=True,
                rejection_reason="signature_missing",
                response_headers=_www_authenticate_header(WEBHOOK_SIGNATURE_HEADER_MALFORMED),
            )
        try:
            legacy_signer = verify_webhook_hmac(headers=headers, body=body, options=hmac_options)
            return legacy_signer, None
        except LegacyWebhookHmacError:
            return None, WebhookOutcome(
                rejected=True,
                rejection_reason="signature_legacy_failed",
                response_headers=_www_authenticate_header("webhook_signature_invalid"),
            )

    def _parse(self, payload_dict: dict[str, Any]) -> WebhookPayload | None:
        model = _MODEL_BY_KIND[self._config.kind]
        try:
            return cast(WebhookPayload, model.model_validate(payload_dict))
        except ValidationError as exc:
            # Operators need the field-level reason to diagnose sender bugs.
            # The receiver still returns payload_invalid downstream; this is
            # just observability.
            logger.warning(
                "webhook payload failed %s validation: %s",
                self._config.kind,
                exc.errors(include_url=False),
            )
            return None


def _has_9421_headers(headers: Mapping[str, str]) -> bool:
    """True only when BOTH 9421 headers are present.

    Requiring both prevents a malformed single-header attempt from blocking
    HMAC fallback: if a sender emits only ``Signature-Input`` (without
    ``Signature``) and the receiver has legacy HMAC configured, we want the
    HMAC path to run, not the 9421 header-malformed rejection.
    """
    lowered = {str(k).lower() for k in headers.keys()}
    return "signature-input" in lowered and "signature" in lowered


def _content_type_is_json(headers: Mapping[str, str]) -> bool:
    """Require application/json (the 9421 profile signs content-type, but the
    receiver still must reject obvious mismatches before parsing)."""
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            ct = str(v).split(";", 1)[0].strip().lower()
            return ct in _ACCEPTED_CONTENT_TYPES
    return False


# The webhook profile's codes -- used to validate WWW-Authenticate values.
# Anything else (e.g. a future code, or an attacker-influenced string) gets
# replaced with WEBHOOK_SIGNATURE_INVALID so we never emit untrusted data in
# a response header, nor a code the profile does not define.
_VALID_WWW_AUTHENTICATE_CODES = WEBHOOK_ERROR_CODES


def _www_authenticate_header(code: str) -> dict[str, str]:
    """401-response header per spec — realm intentionally omitted.

    Code is whitelisted against known webhook_signature_* values so a
    future contributor passing an unchecked string can't inject CR/LF into
    a response header.
    """
    safe_code = code if code in _VALID_WWW_AUTHENTICATE_CODES else "webhook_signature_invalid"
    return {"WWW-Authenticate": signature_challenge(safe_code)}


def _reject(reason: RejectionReason, *, sender_identity: str | None) -> WebhookOutcome:
    return WebhookOutcome(
        rejected=True,
        rejection_reason=reason,
        sender_identity=sender_identity,
        response_headers={},  # 400/422 — non-auth failure, no WWW-Authenticate
    )


def _terminal_observation(
    payload: dict[str, Any],
    kind: WebhookKind,
) -> tuple[str, str] | None:
    """Return stable terminal identity and canonical artifact fingerprint."""
    if kind != "mcp" or payload.get("status") not in {
        "completed",
        "failed",
        "canceled",
        "rejected",
    }:
        return None
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        return None
    # Compare the normalized terminal artifact, not delivery metadata or
    # explanatory text that may legitimately differ between observations.
    artifact = {
        key: payload[key]
        for key in (
            "notification_id",
            "operation_id",
            "task_id",
            "task_type",
            "protocol",
            "status",
            "result",
        )
        if key in payload
    }
    artifact_hash = hashlib.sha256(rfc8785.dumps(artifact)).hexdigest()
    return f"terminal:{task_id}", artifact_hash


_MODEL_BY_KIND: dict[WebhookKind, type[Any]] = {
    "mcp": McpWebhookPayload,
    "revocation_notification": RevocationNotification,
    "collection_list_changed": CollectionListChangedWebhook,
    "property_list_changed": PropertyListChangedWebhook,
    "artifact": ArtifactWebhookPayload,
}


__all__ = [
    "DEFAULT_INBOUND_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_INBOUND_TOTAL_TIMEOUT_SECONDS",
    "DEFAULT_MAX_INBOUND_BODY_BYTES",
    "DEFAULT_MAX_WEBHOOK_BODY_BYTES",
    "InboundBodyRejectedError",
    "InboundRejectionReason",
    "LegacyHmacFallback",
    "VerifiedSignerLike",
    "WebhookKind",
    "WebhookOutcome",
    "WebhookPayload",
    "WebhookReceiver",
    "WebhookReceiverConfig",
    "check_inbound_body",
    "read_inbound_body",
]
