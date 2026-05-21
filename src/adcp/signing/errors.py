"""Error taxonomy for the AdCP request-signing profile.

Codes match the transport error taxonomy defined in `security.mdx`. The code
string is the normative surface — middleware adapters emit a `401` response
with `WWW-Authenticate: Signature error="<code>"` (no realm).
"""

from __future__ import annotations

from collections.abc import Mapping


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

# brand.json discovery chain mirrors for the webhook profile. The chain
# walks identically (capabilities → brand.json → agents[] → jwks_uri),
# just consulting the ``webhook_signing`` purpose under
# ``identity.key_origins`` instead of ``request_signing``.
WEBHOOK_SIGNATURE_BRAND_JSON_URL_MISSING = "webhook_signature_brand_json_url_missing"
WEBHOOK_SIGNATURE_CAPABILITIES_UNREACHABLE = "webhook_signature_capabilities_unreachable"
WEBHOOK_SIGNATURE_BRAND_JSON_UNREACHABLE = "webhook_signature_brand_json_unreachable"
WEBHOOK_SIGNATURE_BRAND_JSON_MALFORMED = "webhook_signature_brand_json_malformed"
WEBHOOK_SIGNATURE_BRAND_ORIGIN_MISMATCH = "webhook_signature_brand_origin_mismatch"
WEBHOOK_SIGNATURE_AGENT_NOT_IN_BRAND_JSON = "webhook_signature_agent_not_in_brand_json"
WEBHOOK_SIGNATURE_BRAND_JSON_AMBIGUOUS = "webhook_signature_brand_json_ambiguous"
WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH = "webhook_signature_key_origin_mismatch"
WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING = "webhook_signature_key_origin_missing"

# Code-family translation used by the webhook verifier wrapper. The verifier
# pipeline raises request_signature_* codes; the wrapper retags them into
# webhook_signature_* before exposing to callers. Keeps the 300-line verifier
# unchanged and guarantees webhook routes never leak request-family codes.
REQUEST_TO_WEBHOOK_CODE = {
    REQUEST_SIGNATURE_REQUIRED: WEBHOOK_SIGNATURE_REQUIRED,
    REQUEST_SIGNATURE_HEADER_MALFORMED: WEBHOOK_SIGNATURE_HEADER_MALFORMED,
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
    REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING: WEBHOOK_SIGNATURE_BRAND_JSON_URL_MISSING,
    REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE: WEBHOOK_SIGNATURE_CAPABILITIES_UNREACHABLE,
    REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE: WEBHOOK_SIGNATURE_BRAND_JSON_UNREACHABLE,
    REQUEST_SIGNATURE_BRAND_JSON_MALFORMED: WEBHOOK_SIGNATURE_BRAND_JSON_MALFORMED,
    REQUEST_SIGNATURE_BRAND_ORIGIN_MISMATCH: WEBHOOK_SIGNATURE_BRAND_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON: WEBHOOK_SIGNATURE_AGENT_NOT_IN_BRAND_JSON,
    REQUEST_SIGNATURE_BRAND_JSON_AMBIGUOUS: WEBHOOK_SIGNATURE_BRAND_JSON_AMBIGUOUS,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH: WEBHOOK_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING: WEBHOOK_SIGNATURE_KEY_ORIGIN_MISSING,
}
