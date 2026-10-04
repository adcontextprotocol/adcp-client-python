"""Error taxonomy for the AdCP request-signing profile.

Codes match the transport error taxonomy defined in `security.mdx`. The code
string is the normative surface — middleware adapters emit a `401` response
with `WWW-Authenticate: Signature error="<code>"` (no realm).
"""

from __future__ import annotations

from collections.abc import Mapping

from adcp.signing.canonical import REQUEST_TARGET_URI_MALFORMED


class SignatureVerificationError(Exception):
    """Raised when a request signature fails any step of the verifier checklist.

    ``detail`` carries the spec-mandated structured fields for codes that
    require them — e.g. ``request_signature_key_origin_mismatch`` carries
    ``{purpose, expected_origin, actual_origin}`` per ADCP #3690
    security.mdx step 7, and ``request_signature_brand_json_url_missing``
    carries ``{agent_url}`` per the same section's rejection-code table.
    Middleware adapters surface these as structured fields on the 401
    response or in a DLQ payload; ``str(exc)`` continues to render the
    free-form message for unstructured logs.
    """

    def __init__(
        self,
        code: str,
        *,
        step: int | str | None = None,
        message: str | None = None,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.step = step
        self.detail = dict(detail) if detail is not None else None


def signature_challenge(code: str) -> str:
    """``WWW-Authenticate`` value for a 401 the signing profile rejects.

    security.mdx § Transport error taxonomy: "AdCP does NOT define a realm
    value for request-signing challenges. Verifiers MUST emit
    ``WWW-Authenticate: Signature error="<code>"`` with no ``realm``
    parameter and no other parameters."

    Every 401 the SDK emits for a signature failure formats the value here —
    the request leg (:func:`adcp.signing.middleware.unauthorized_response_headers`),
    the webhook leg (:mod:`adcp.webhook_receiver`), and the bearer middleware's
    signature-fallback challenge (:mod:`adcp.server.auth`) — so the three
    cannot drift from the byte string peers and conformance harnesses match.
    """
    return f'Signature error="{code}"'


REQUEST_SIGNATURE_REQUIRED = "request_signature_required"
REQUEST_SIGNATURE_HEADER_MALFORMED = "request_signature_header_malformed"
REQUEST_SIGNATURE_PARAMS_INCOMPLETE = "request_signature_params_incomplete"
REQUEST_SIGNATURE_TAG_INVALID = "request_signature_tag_invalid"
REQUEST_SIGNATURE_ALG_NOT_ALLOWED = "request_signature_alg_not_allowed"
REQUEST_SIGNATURE_WINDOW_INVALID = "request_signature_window_invalid"
REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE = "request_signature_components_incomplete"
REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED = "request_signature_components_unexpected"
REQUEST_SIGNATURE_KEY_UNKNOWN = "request_signature_key_unknown"
REQUEST_SIGNATURE_KEY_PURPOSE_INVALID = "request_signature_key_purpose_invalid"
REQUEST_SIGNATURE_INVALID = "request_signature_invalid"
REQUEST_SIGNATURE_DIGEST_MISMATCH = "request_signature_digest_mismatch"
REQUEST_SIGNATURE_REPLAYED = "request_signature_replayed"
REQUEST_SIGNATURE_KEY_REVOKED = "request_signature_key_revoked"
REQUEST_SIGNATURE_REVOCATION_STALE = "request_signature_revocation_stale"
REQUEST_SIGNATURE_JWKS_UNAVAILABLE = "request_signature_jwks_unavailable"
REQUEST_SIGNATURE_JWKS_UNTRUSTED = "request_signature_jwks_untrusted"
REQUEST_SIGNATURE_RATE_ABUSE = "request_signature_rate_abuse"
# Checklist step 14. Named without the ``request_signature_`` infix because the
# spec names it that way: the signature IS valid; the signed body is not strict,
# unambiguous JSON (duplicate object keys, invalid UTF-8, or not JSON at all).
REQUEST_BODY_MALFORMED = "request_body_malformed"

# brand.json discovery chain (ADCP #3690). Verifiers bootstrap an agent's
# signing keys via ``identity.brand_json_url`` on the agent's
# ``get_adcp_capabilities`` response → brand.json → ``agents[]`` →
# ``jwks_uri``. Each step has a dedicated rejection code so callers can
# disambiguate retryable transport failures (``*_unreachable``) from
# misconfiguration (``*_missing`` / ``*_malformed`` / ``*_mismatch``).
REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING = "request_signature_brand_json_url_missing"
REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE = "request_signature_capabilities_unreachable"
REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE = "request_signature_brand_json_unreachable"
REQUEST_SIGNATURE_BRAND_JSON_MALFORMED = "request_signature_brand_json_malformed"
REQUEST_SIGNATURE_BRAND_ORIGIN_MISMATCH = "request_signature_brand_origin_mismatch"
REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON = "request_signature_agent_not_in_brand_json"
REQUEST_SIGNATURE_BRAND_JSON_AMBIGUOUS = "request_signature_brand_json_ambiguous"

# identity.key_origins consistency check (ADCP #3690). For every purpose
# declared under capabilities ``identity.key_origins``, the resolved
# ``jwks_uri`` host MUST equal the declared origin (after IDNA-A-label
# canonicalization). Mismatch → ``_key_origin_mismatch``. Missing
# declaration when signing posture is asserted → ``_key_origin_missing``.
REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH = "request_signature_key_origin_mismatch"
REQUEST_SIGNATURE_KEY_ORIGIN_MISSING = "request_signature_key_origin_missing"

# Webhook-signing error taxonomy — adcp#2423 / webhooks.mdx + security.mdx.
# Distinct strings from the request-signing family so receivers can route the
# 401 response through webhook-specific observability.
WEBHOOK_SIGNATURE_REQUIRED = "webhook_signature_required"
WEBHOOK_SIGNATURE_HEADER_MALFORMED = "webhook_signature_header_malformed"
WEBHOOK_SIGNATURE_PARAMS_INCOMPLETE = "webhook_signature_params_incomplete"
WEBHOOK_SIGNATURE_TAG_INVALID = "webhook_signature_tag_invalid"
WEBHOOK_SIGNATURE_ALG_NOT_ALLOWED = "webhook_signature_alg_not_allowed"
WEBHOOK_SIGNATURE_WINDOW_INVALID = "webhook_signature_window_invalid"
WEBHOOK_SIGNATURE_COMPONENTS_INCOMPLETE = "webhook_signature_components_incomplete"
WEBHOOK_SIGNATURE_COMPONENTS_UNEXPECTED = "webhook_signature_components_unexpected"
WEBHOOK_SIGNATURE_KEY_UNKNOWN = "webhook_signature_key_unknown"
WEBHOOK_SIGNATURE_KEY_PURPOSE_INVALID = "webhook_signature_key_purpose_invalid"
WEBHOOK_SIGNATURE_INVALID = "webhook_signature_invalid"
WEBHOOK_SIGNATURE_DIGEST_MISMATCH = "webhook_signature_digest_mismatch"
WEBHOOK_SIGNATURE_REPLAYED = "webhook_signature_replayed"
WEBHOOK_SIGNATURE_KEY_REVOKED = "webhook_signature_key_revoked"
WEBHOOK_SIGNATURE_REVOCATION_STALE = "webhook_signature_revocation_stale"
WEBHOOK_SIGNATURE_JWKS_UNAVAILABLE = "webhook_signature_jwks_unavailable"
WEBHOOK_SIGNATURE_JWKS_UNTRUSTED = "webhook_signature_jwks_untrusted"
WEBHOOK_SIGNATURE_RATE_ABUSE = "webhook_signature_rate_abuse"

# The webhook profile declares no per-hop code for the key-discovery chain.
# security.mdx § "Webhook callbacks" → JWKS discovery walks four steps
# (brand.json fetch → ``agents[]`` match → ``jwks_uri`` fetch → ``keyid``
# resolve) and names exactly one rejection code for the whole chain,
# ``webhook_signature_key_unknown``; webhook checklist step 7 repeats it and
# adds "Reject if ``keyid`` cannot be resolved to a specific ``agents[]``
# entry in the signer's brand.json". The webhook error taxonomy table carries
# no brand.json, capabilities or key-origin row. That is the deliberate
# asymmetry with the request profile, whose own discovery-chain
# rejection-code table assigns a distinct code and a structured ``detail``
# shape to every hop: a webhook sender learns that its key did not resolve
# and nothing about the receiver's walk to reach that conclusion.
#
# Every ``request_signature_*`` discovery code therefore translates to
# ``WEBHOOK_SIGNATURE_KEY_UNKNOWN`` in ``REQUEST_TO_WEBHOOK_CODE`` below.

# Structural code for a malformed authority on the webhook profile. Named
# without the ``webhook_signature_`` prefix the rest of this family carries
# because the spec names it that way: security.mdx's webhook checklist lists
# ``webhook_target_uri_malformed`` in the error taxonomy and requires it at
# step 10 for a malformed or mismatched authority.
WEBHOOK_TARGET_URI_MALFORMED = "webhook_target_uri_malformed"
# Webhook twin of ``request_body_malformed`` (webhook checklist step 14).
WEBHOOK_BODY_MALFORMED = "webhook_body_malformed"

# Code-family translation used by the webhook verifier wrapper. The verifier
# pipeline raises request_signature_* codes; the wrapper retags them into
# webhook_signature_* before exposing to callers. Keeps the 300-line verifier
# unchanged and guarantees webhook routes never leak request-family codes.
#
# This table is the only place the webhook profile's code for a request-family
# failure is decided. A row that mirrors the request code into the webhook
# family keeps its suffix; a row that maps to a coarser webhook code because
# the profile declares nothing finer changes it, and the wrapper logs the
# precise request-family cause whenever that happens. Readers grade the webhook
# profile's emitted taxonomy off the values in this table — so a row that no
# input can reach is a row that lies.
REQUEST_TO_WEBHOOK_CODE = {
    REQUEST_SIGNATURE_REQUIRED: WEBHOOK_SIGNATURE_REQUIRED,
    REQUEST_SIGNATURE_HEADER_MALFORMED: WEBHOOK_SIGNATURE_HEADER_MALFORMED,
    REQUEST_TARGET_URI_MALFORMED: WEBHOOK_TARGET_URI_MALFORMED,
    REQUEST_SIGNATURE_PARAMS_INCOMPLETE: WEBHOOK_SIGNATURE_PARAMS_INCOMPLETE,
    REQUEST_SIGNATURE_TAG_INVALID: WEBHOOK_SIGNATURE_TAG_INVALID,
    REQUEST_SIGNATURE_ALG_NOT_ALLOWED: WEBHOOK_SIGNATURE_ALG_NOT_ALLOWED,
    REQUEST_SIGNATURE_WINDOW_INVALID: WEBHOOK_SIGNATURE_WINDOW_INVALID,
    REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE: WEBHOOK_SIGNATURE_COMPONENTS_INCOMPLETE,
    REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED: WEBHOOK_SIGNATURE_COMPONENTS_UNEXPECTED,
    REQUEST_SIGNATURE_KEY_UNKNOWN: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_KEY_PURPOSE_INVALID: WEBHOOK_SIGNATURE_KEY_PURPOSE_INVALID,
    REQUEST_SIGNATURE_INVALID: WEBHOOK_SIGNATURE_INVALID,
    REQUEST_SIGNATURE_DIGEST_MISMATCH: WEBHOOK_SIGNATURE_DIGEST_MISMATCH,
    REQUEST_SIGNATURE_REPLAYED: WEBHOOK_SIGNATURE_REPLAYED,
    REQUEST_SIGNATURE_KEY_REVOKED: WEBHOOK_SIGNATURE_KEY_REVOKED,
    REQUEST_SIGNATURE_REVOCATION_STALE: WEBHOOK_SIGNATURE_REVOCATION_STALE,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE: WEBHOOK_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED: WEBHOOK_SIGNATURE_JWKS_UNTRUSTED,
    REQUEST_SIGNATURE_RATE_ABUSE: WEBHOOK_SIGNATURE_RATE_ABUSE,
    REQUEST_BODY_MALFORMED: WEBHOOK_BODY_MALFORMED,
    # Key-discovery chain. The webhook profile stops at
    # ``webhook_signature_key_unknown`` for every hop — see the comment above
    # the WEBHOOK_* constants.
    REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_BRAND_JSON_MALFORMED: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_BRAND_ORIGIN_MISMATCH: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_BRAND_JSON_AMBIGUOUS: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING: WEBHOOK_SIGNATURE_KEY_UNKNOWN,
}
