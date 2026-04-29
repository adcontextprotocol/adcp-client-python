"""Verifier for the AdCP webhook-signing profile (adcp#2423).

The webhook profile reuses the 14-step RFC 9421 pipeline from
:mod:`adcp.signing.verifier` but binds three things differently:

* ``tag`` — ``adcp/webhook-signing/v1`` (distinct from request signing so a
  signature from one profile can never be replayed as the other).
* JWK ``adcp_use`` — ``webhook-signing`` (cross-purpose key reuse is locally
  enforceable here).
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from adcp.signing.canonical import _lookup, parse_signature_input_header
from adcp.signing.constants import (
    ADCP_USE_WEBHOOK,
    DEFAULT_SKEW_SECONDS,
    MAX_WINDOW_SECONDS,
    SIG_LABEL_DEFAULT,
    WEBHOOK_TAG,
)
from adcp.signing.crypto import ALLOWED_ALGS
from adcp.signing.errors import (
    REQUEST_TO_WEBHOOK_CODE,
    WEBHOOK_SIGNATURE_COMPONENTS_INCOMPLETE,
    WEBHOOK_SIGNATURE_INVALID,
    SignatureVerificationError,
)

logger = logging.getLogger(__name__)
from adcp.signing.jwks import JwksResolver
from adcp.signing.replay import ReplayStore
from adcp.signing.revocation import RevocationChecker, RevocationList
from adcp.signing.verifier import (
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
    """

    jwks_resolver: JwksResolver
    replay_store: ReplayStore | None = None
    revocation_checker: RevocationChecker | None = None
    revocation_list: RevocationList | None = None
    max_skew_seconds: int = DEFAULT_SKEW_SECONDS
    max_window_seconds: int = MAX_WINDOW_SECONDS
    label: str = SIG_LABEL_DEFAULT
    allowed_algs: frozenset[str] = ALLOWED_ALGS
    sender_url: str | None = None
    clock: Callable[[], float] = time.time


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
        """Identity string used to scope dedup state.

        Webhook dedup MUST be scoped to the authenticated sender — trusting a
        payload field for identity is the attack-surface hole the spec's
        "Sender requirements" paragraph calls out. The key_id is the
        cryptographically verified identity; prefer ``sender_url:key_id`` when
        a sender URL is present to tolerate JWKS reuse across co-deployed
        senders.
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
    _precheck_webhook_has_required_components(headers)

    request_options = VerifyOptions(
        now=options.clock(),
        capability=VerifierCapability(
            supported=True,
            covers_content_digest="required",
            required_for=frozenset({"webhook"}),
        ),
        operation="webhook",
        jwks_resolver=options.jwks_resolver,
        replay_store=options.replay_store,
        revocation_checker=options.revocation_checker,
        revocation_list=options.revocation_list,
        max_skew_seconds=options.max_skew_seconds,
        max_window_seconds=options.max_window_seconds,
        label=options.label,
        expected_tag=WEBHOOK_TAG,
        expected_adcp_use=ADCP_USE_WEBHOOK,
        allowed_algs=options.allowed_algs,
        agent_url=options.sender_url,
    )

    try:
        signer: VerifiedSigner = verify_request_signature(
            method=method, url=url, headers=headers, body=body, options=request_options
        )
    except SignatureVerificationError as exc:
        raise _retag_to_webhook(exc) from exc

    return VerifiedWebhookSender(
        key_id=signer.key_id,
        alg=signer.alg,
        label=signer.label,
        verified_at=signer.verified_at,
        sender_url=signer.agent_url,
    )


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
        # REQUEST_SIGNATURE_REQUIRED which retags to WEBHOOK_SIGNATURE_REQUIRED.
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
    """Translate a request_signature_* code to its webhook_signature_* twin."""
    webhook_code = REQUEST_TO_WEBHOOK_CODE.get(exc.code)
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
    )


# Re-export for callers who want to swap webhook-specific retry logic in.
__all__ = [
    "VerifiedWebhookSender",
    "WebhookVerifyOptions",
    "verify_webhook_signature",
]
