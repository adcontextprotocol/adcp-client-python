"""Verifier for the AdCP webhook-signing profile (adcp#2423).

The webhook profile reuses the 14-step RFC 9421 pipeline from
:mod:`adcp.signing.verifier` but binds three things differently:

* ``tag`` — ``adcp/webhook-signing/v1`` (distinct from request signing so a
  signature from one profile can never be replayed as the other).
* JWK ``adcp_use`` — ``request-signing`` for current senders, while the
  deprecated ``webhook-signing`` value remains accepted for compatibility.
* ``content-digest`` — REQUIRED. No ``covers_content_digest: "forbidden"``
  escape hatch; webhooks are delivery of an *event*, and a signature that
  doesn't cover the body is not protecting the attack surface.

Error codes follow the ``webhook_signature_*`` taxonomy. The wrapper catches
the request-family codes the core verifier raises and translates them via
``REQUEST_TO_WEBHOOK_CODE`` — keeps the core verifier unchanged and guarantees
webhook routes never leak request-signing error strings.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from adcp.signing.canonical import _lookup, parse_signature_input_header, split_structured_field
from adcp.signing.constants import (
    ADCP_USE_REQUEST,
    ADCP_USE_WEBHOOK,
    DEFAULT_SKEW_SECONDS,
    MAX_WINDOW_SECONDS,
    SIG_LABEL_DEFAULT,
    WEBHOOK_TAG,
)
from adcp.signing.crypto import ALLOWED_ALGS
from adcp.signing.errors import (
    REQUEST_SIGNATURE_KEY_UNKNOWN,
    REQUEST_TO_WEBHOOK_CODE,
    WEBHOOK_SIGNATURE_COMPONENTS_INCOMPLETE,
    WEBHOOK_SIGNATURE_HEADER_MALFORMED,
    WEBHOOK_SIGNATURE_INVALID,
    WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    SignatureVerificationError,
)

logger = logging.getLogger(__name__)
from adcp.signing.jwks import JwksResolver, SSRFValidationError
from adcp.signing.publisher_pins import PublisherPins, matches_publisher_pins
from adcp.signing.replay import InMemoryReplayStore, ReplayStore
from adcp.signing.revocation import RevocationChecker, RevocationList
from adcp.signing.verifier import (
    RequestBodyMalformedError,
    VerifiedSigner,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)

_REQUIRED_WEBHOOK_COMPONENTS = (
    "@method",
    "@target-uri",
    "@authority",
    "content-type",
    "content-digest",
)


@dataclass(frozen=True, kw_only=True)
class WebhookVerifyOptions:
    """Options for the webhook verifier.

    Subset of :class:`VerifyOptions` — several fields are pinned (tag, adcp_use,
    content-digest policy) because the webhook profile doesn't leave them as
    caller choices.

    Unlike the request verifier, there is no ``now`` field — the webhook
    verifier stamps time-of-check itself, so the same :class:`WebhookVerifyOptions`
    instance can live for the lifetime of your receiver without a factory
    closure around it. Override via ``clock=`` for deterministic tests.

    ``replay_store`` defaults to a per-options in-memory store so captured
    signatures are rejected without extra configuration. Multi-process
    receivers should supply a shared store. Passing ``None`` explicitly is
    the opt-out for specialized tests or externally enforced replay policy.
    """

    jwks_resolver: JwksResolver
    replay_store: ReplayStore | None = field(default_factory=InMemoryReplayStore)
    revocation_checker: RevocationChecker | None = None
    revocation_list: RevocationList | None = None
    max_skew_seconds: int = DEFAULT_SKEW_SECONDS
    max_window_seconds: int = MAX_WINDOW_SECONDS
    label: str = SIG_LABEL_DEFAULT
    allowed_algs: frozenset[str] = ALLOWED_ALGS
    sender_url: str | None = None
    expected_key_origins: Mapping[str, str] | None = None
    posture: str | None = None
    clock: Callable[[], float] = time.time
    publisher_pins: PublisherPins | None = None
    refresh_publisher_pins: Callable[[], PublisherPins] | None = None

    def __post_init__(self) -> None:
        if self.publisher_pins and self.refresh_publisher_pins is None:
            raise ValueError(
                "publisher_pins requires refresh_publisher_pins to refresh before rejection"
            )


@dataclass(frozen=True)
class VerifiedWebhookSender:
    """Returned on successful webhook verification.

    Distinct type from :class:`VerifiedSigner` so a caller that mistakenly
    passes a request-verified signer into a webhook-scoped dedup store (or the
    reverse) will fail to type-check. Both carry the same bytes; the type
    separation is a guardrail, not a data difference.
    """

    key_id: str
    alg: str
    label: str
    verified_at: float
    sender_url: str | None = None

    def as_sender_identity(self) -> str:
        """Verified signing-key identity for authentication and audit.

        This value changes when a publisher rotates keys, so it MUST NOT be
        used as the stable publisher scope for webhook deduplication. Configure
        :attr:`WebhookReceiverConfig.publisher_scope_for` to resolve verified
        key evidence to a trusted seller identity that survives rotation.
        """
        if self.sender_url is not None:
            return f"{self.sender_url}|{self.key_id}"
        return self.key_id


def verify_webhook_signature(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    options: WebhookVerifyOptions,
) -> VerifiedWebhookSender:
    """Verify an incoming signed webhook per the adcp/webhook-signing/v1 profile.

    Raises :class:`SignatureVerificationError` with a ``webhook_signature_*``
    code on failure. Success returns a :class:`VerifiedWebhookSender` carrying
    the identity to scope dedup state by.
    """
    _precheck_webhook_signature_alphabet(headers, options.label)
    _precheck_webhook_has_required_components(headers)

    request_options = VerifyOptions(
        now=options.clock(),
        capability=VerifierCapability(
            supported=True,
            covers_content_digest="required",
            required_for=frozenset({"webhook"}),
        ),
        operation="webhook",
        jwks_resolver=(
            _PinnedWebhookResolver(options, headers)
            if options.publisher_pins
            else options.jwks_resolver
        ),
        replay_store=options.replay_store,
        revocation_checker=options.revocation_checker,
        revocation_list=options.revocation_list,
        max_skew_seconds=options.max_skew_seconds,
        max_window_seconds=options.max_window_seconds,
        label=options.label,
        expected_tag=WEBHOOK_TAG,
        accepted_adcp_uses=frozenset({ADCP_USE_REQUEST, ADCP_USE_WEBHOOK}),
        allowed_algs=options.allowed_algs,
        agent_url=options.sender_url,
        expected_key_origins=(
            (options.expected_key_origins or {})
            if options.publisher_pins
            else options.expected_key_origins
        ),
        signing_purpose="webhook_signing",
        posture=options.posture,
        # The rc.4 webhook-v1 corpus retains legacy Base64URL signatures.
        # Do not inherit the stricter 3.2 *request* profile for this route.
        signing_profile_version="3.1",
    )

    try:
        signer: VerifiedSigner = verify_request_signature(
            method=method, url=url, headers=headers, body=body, options=request_options
        )
    except RequestBodyMalformedError as exc:
        # Step 14: the signature verified, so keep the sender attached for
        # audit/dedup attribution rather than collapsing to an anonymous error.
        raise RequestBodyMalformedError(
            _retag_to_webhook(exc).code,
            signer=_as_webhook_sender(exc.signer),
            step=exc.step,
            message=str(exc),
            detail=exc.detail,
        ) from exc
    except SignatureVerificationError as exc:
        raise _retag_to_webhook(exc) from exc

    return _as_webhook_sender(signer)


def _as_webhook_sender(signer: VerifiedSigner) -> VerifiedWebhookSender:
    return VerifiedWebhookSender(
        key_id=signer.key_id,
        alg=signer.alg,
        label=signer.label,
        verified_at=signer.verified_at,
        sender_url=signer.agent_url,
    )


def _precheck_webhook_signature_alphabet(headers: Mapping[str, str], label: str) -> None:
    """Keep legacy decoder tolerance without accepting mixed alphabets.

    Webhook-v1 emits unpadded Base64URL throughout 3.x. The shared legacy
    decoder also tolerates standard Base64, but its URL fallback would
    accept a mixed token. That tolerance is not conformant emission.
    Confine this profile check to the selected webhook Signature value.
    Content-Digest and request/JWK/JWT decoders keep their own contracts.
    """
    raw = _lookup(headers, "signature")
    if raw is None:
        return
    for entry in split_structured_field(raw, ","):
        name, separator, value = entry.strip().partition("=")
        if not separator or name.strip() != label:
            continue
        value = value.strip()
        if value.startswith(":") and value.endswith(":"):
            token = value[1:-1]
            if any(char in token for char in "+/") and any(char in token for char in "-_"):
                raise SignatureVerificationError(
                    WEBHOOK_SIGNATURE_HEADER_MALFORMED,
                    step=1,
                    message="webhook Signature must not mix Base64 alphabets",
                )
        return


def _precheck_webhook_has_required_components(headers: Mapping[str, str]) -> None:
    """Reject before crypto if Signature-Input omits webhook-required components.

    The core verifier's component check only requires method/target-uri/authority
    unconditionally (content-type "if present"). The webhook profile escalates
    content-type + content-digest to unconditionally required — this is step 6
    of the webhook verifier checklist per security.mdx. Doing the stricter
    check here keeps the core verifier unchanged.

    Content-digest absence is caught separately by the core verifier's
    ``covers_content_digest="required"`` policy and surfaces as
    REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE → webhook code via retag. This
    precheck exists specifically for content-type coverage when content-type
    is present but not listed in Signature-Input.
    """
    sig_input_raw = _lookup(headers, "signature-input")
    if sig_input_raw is None:
        # Let the core verifier handle the presence/absence error — it raises
        # REQUEST_SIGNATURE_REQUIRED, which retags to
        # WEBHOOK_SIGNATURE_HEADER_MALFORMED (the profile has no ``required``).
        return
    try:
        labels = parse_signature_input_header(sig_input_raw)
    except (ValueError, KeyError):
        # Core verifier will raise the malformed error with retag.
        return
    parsed = next(iter(labels.values()), None)
    if parsed is None:
        return
    covered = set(parsed.components)
    if _lookup(headers, "content-type") is not None and "content-type" not in covered:
        raise SignatureVerificationError(
            WEBHOOK_SIGNATURE_COMPONENTS_INCOMPLETE,
            step=6,
            message="webhook signature must cover content-type when present",
        )


def _retag_to_webhook(exc: SignatureVerificationError) -> SignatureVerificationError:
    """Translate a request_signature_* code to its webhook-profile code.

    ``REQUEST_TO_WEBHOOK_CODE`` decides the code. Most rows mirror the request
    code into the webhook family and keep its suffix; the key-discovery rows
    map to the coarser ``webhook_signature_key_unknown`` because the webhook
    profile declares nothing finer. A changed suffix is exactly that collapse,
    so the precise request-family cause goes to the log rather than the wire,
    and stays on the exception as ``__cause__``. ``transient`` carries over from
    the request-family error, so a JWKS fetch that may succeed later stays
    distinguishable in-process from a refused or malformed one.
    """
    webhook_code: str | None = REQUEST_TO_WEBHOOK_CODE.get(exc.code)
    if webhook_code is not None and webhook_code.removeprefix("webhook_") != exc.code.removeprefix(
        "request_"
    ):
        logger.warning("webhook profile has no code for %r; emitting %r", exc.code, webhook_code)
    if webhook_code is None:
        # Unknown code means the core verifier grew a new error code and the
        # translation map wasn't updated. Surface as generic auth-failure
        # (not "signature missing" — that would mis-describe what happened)
        # and log loudly so the map gets patched on the next release.
        logger.warning(
            "webhook verifier saw unknown request-family code %r; "
            "emitting %r — add to REQUEST_TO_WEBHOOK_CODE map",
            exc.code,
            WEBHOOK_SIGNATURE_INVALID,
        )
        webhook_code = WEBHOOK_SIGNATURE_INVALID
    return SignatureVerificationError(
        webhook_code,
        step=exc.step,
        message=str(exc),
        detail=exc.detail,
        transient=exc.transient,
    )


class _PinnedWebhookResolver:
    """Never source a key from the pin; intersect the resolved operator key."""

    jwks_source = "brand_json"

    def __init__(self, options: WebhookVerifyOptions, headers: Mapping[str, str]) -> None:
        self._options = options
        self._headers = headers

    @property
    def jwks_uri(self) -> str | None:
        return getattr(self._options.jwks_resolver, "jwks_uri", None)

    def __call__(self, keyid: str) -> dict[str, Any] | None:
        # The core verifier has already validated the selected parameters and
        # signature window before invoking its resolver.
        parsed = parse_signature_input_header(_lookup(self._headers, "signature-input") or "")
        created = int(parsed[self._options.label].params["created"])
        key = self._options.jwks_resolver(keyid)
        pins = self._options.publisher_pins or {}
        if key is not None and matches_publisher_pins(key, pins, now=created):
            return key
        refresh = self._options.refresh_publisher_pins
        if refresh is not None:
            try:
                refreshed = refresh()
            except Exception as exc:
                raise SignatureVerificationError(
                    REQUEST_SIGNATURE_KEY_UNKNOWN,
                    step=7,
                    message="publisher pin refresh failed",
                ) from exc
            if set(refreshed) != set(pins):
                raise SignatureVerificationError(
                    REQUEST_SIGNATURE_KEY_UNKNOWN,
                    step=7,
                    message="publisher refresh changed the applicable inventory scope",
                )
            if key is not None and matches_publisher_pins(key, refreshed, now=created):
                return key
        return None


async def verify_webhook_from_agent_url(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    agent_url: str,
    publisher_domains: Sequence[str] = (),
    replay_store: ReplayStore | None = None,
    revocation_checker: RevocationChecker | None = None,
    revocation_list: RevocationList | None = None,
    allow_private_destinations: bool = False,
    clock: Callable[[], float] = time.time,
    protocol: Literal["mcp", "a2a"] = "mcp",
) -> VerifiedWebhookSender:
    """Discover operator keys and verify a webhook with publisher intersections.

    ``publisher_domains`` MUST come from the receiver's own media-buy record,
    never the webhook body. Every call reconfirms capabilities, so no cached
    onboarding mapping can outlive the operator's advertised record. On pin
    rejection each applicable publisher's adagents.json is fetched again.
    """
    from adcp.adagents import fetch_publisher_signing_pins
    from adcp.exceptions import AdagentsValidationError
    from adcp.signing.agent_resolver import (
        AgentResolverError,
        _BrandJsonStaticJwksResolver,
        _canonical_agent_origin,
        _default_replay_store_for_origin,
        _refresh_jwks_after_miss,
        async_resolve_agent,
        request_signature_code,
    )
    from adcp.signing.canonical import parse_signature_input_header

    _precheck_webhook_signature_alphabet(headers, SIG_LABEL_DEFAULT)
    _precheck_webhook_has_required_components(headers)
    signature_input = _lookup(headers, "signature-input")
    if signature_input is None:
        raise SignatureVerificationError(WEBHOOK_SIGNATURE_HEADER_MALFORMED, step=1)
    try:
        labels = parse_signature_input_header(signature_input)
        parsed = labels.get(SIG_LABEL_DEFAULT)
        if parsed is None:
            raise ValueError("selected webhook signature label is absent")
        keyid = str(parsed.params.get("keyid", ""))
        created = parsed.params.get("created")
    except ValueError as exc:
        raise SignatureVerificationError(WEBHOOK_SIGNATURE_HEADER_MALFORMED, step=1) from exc

    try:
        resolution = await async_resolve_agent(
            agent_url,
            allow_private_destinations=allow_private_destinations,
            allow_legacy_fallback=True,
            signing_purpose="webhook_signing",
            protocol=protocol,
        )
    except AgentResolverError as exc:
        cause = request_signature_code(exc)
        logger.warning("webhook agent resolution failed: %s", cause)
        raise SignatureVerificationError(
            WEBHOOK_SIGNATURE_KEY_UNKNOWN,
            step=7,
            message="webhook key discovery failed",
            transient=SignatureVerificationError(cause).transient,
        ) from exc

    resolver = _BrandJsonStaticJwksResolver(resolution.jwks, jwks_uri=resolution.jwks_uri)
    key = resolver(keyid)
    if key is None:
        try:
            refreshed = await _refresh_jwks_after_miss(
                resolution.jwks_uri, allow_private=allow_private_destinations
            )
        except (ValueError, OSError, httpx.HTTPError, SSRFValidationError) as exc:
            raise SignatureVerificationError(
                WEBHOOK_SIGNATURE_KEY_UNKNOWN,
                step=7,
                message="operator JWKS refresh failed",
                transient=_fetch_failure_is_transient(exc),
            ) from exc
        if refreshed is not None:
            resolver = _BrandJsonStaticJwksResolver(refreshed, jwks_uri=resolution.jwks_uri)
            key = resolver(keyid)
    if publisher_domains:
        try:
            pins = await fetch_publisher_signing_pins(
                tuple(publisher_domains), resolution.agent_url
            )
            if (
                key is None
                or not isinstance(created, int)
                or not matches_publisher_pins(key, pins, now=created)
            ):
                pins = await fetch_publisher_signing_pins(
                    tuple(publisher_domains), resolution.agent_url
                )
                if (
                    key is None
                    or not isinstance(created, int)
                    or not matches_publisher_pins(key, pins, now=created)
                ):
                    raise ValueError("operator key does not match every publisher pin")
        except (ValueError, OSError, httpx.HTTPError, AdagentsValidationError) as exc:
            raise SignatureVerificationError(
                WEBHOOK_SIGNATURE_KEY_UNKNOWN,
                step=7,
                message="publisher pin resolution failed",
                transient=_fetch_failure_is_transient(exc),
            ) from exc

    options = WebhookVerifyOptions(
        jwks_resolver=resolver,
        sender_url=resolution.agent_url,
        expected_key_origins=None if resolution.legacy_discovery else resolution.key_origins or {},
        replay_store=(
            replay_store
            if replay_store is not None
            else _default_replay_store_for_origin(_canonical_agent_origin(resolution.agent_url))
        ),
        clock=clock,
        revocation_checker=revocation_checker,
        revocation_list=revocation_list,
    )
    return verify_webhook_signature(
        method=method, url=url, headers=headers, body=body, options=options
    )


def _fetch_failure_is_transient(exc: Exception) -> bool:
    """Whether a key-discovery fetch failure could succeed on a later attempt.

    Network and HTTP failures are transient, as is an SSRF gate failure the gate
    itself marks transient (the host did not resolve), an adagents.json timeout,
    and an adagents.json 429 or 5xx. A refused destination, a document that did
    not parse, and a pin that does not match are not.
    """
    from adcp.exceptions import AdagentsHTTPError, AdagentsTimeoutError

    if isinstance(exc, SSRFValidationError):
        return exc.transient
    if isinstance(exc, AdagentsHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500
    return isinstance(exc, (OSError, httpx.HTTPError, AdagentsTimeoutError))


# Re-export for callers who want to swap webhook-specific retry logic in.
__all__ = [
    "VerifiedWebhookSender",
    "WebhookVerifyOptions",
    "verify_webhook_signature",
    "verify_webhook_from_agent_url",
]
