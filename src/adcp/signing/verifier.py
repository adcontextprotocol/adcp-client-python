"""Verifier checklist for the AdCP request-signing profile.

Implements the 14-point pipeline (pre-check 0 + checklist steps 1-13) defined
in `security.mdx`. Each step either passes or raises
`SignatureVerificationError` with the exact code from the transport error
taxonomy — conformance requires byte-for-byte match on the code string.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from adcp.signing.canonical import (
    _lookup,
    build_signature_base,
    parse_signature_input_header,
)
from adcp.signing.constants import (
    ADCP_USE_REQUEST,
    DEFAULT_SKEW_SECONDS,
    DEFAULT_TAG,
    MAX_WINDOW_SECONDS,
    SIG_LABEL_DEFAULT,
)
from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    ALLOWED_ALGS,
    alg_for_jwk,
    extract_signature_bytes,
    public_key_from_jwk,
    verify_signature,
)
from adcp.signing.digest import content_digest_matches
from adcp.signing.errors import (
    REQUEST_SIGNATURE_ALG_NOT_ALLOWED,
    REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE,
    REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED,
    REQUEST_SIGNATURE_DIGEST_MISMATCH,
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_INVALID,
    REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
    REQUEST_SIGNATURE_KEY_REVOKED,
    REQUEST_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_PARAMS_INCOMPLETE,
    REQUEST_SIGNATURE_RATE_ABUSE,
    REQUEST_SIGNATURE_REPLAYED,
    REQUEST_SIGNATURE_REQUIRED,
    REQUEST_SIGNATURE_REVOCATION_STALE,
    REQUEST_SIGNATURE_TAG_INVALID,
    REQUEST_SIGNATURE_WINDOW_INVALID,
    SignatureVerificationError,
)
from adcp.signing.jwks import JwksResolver
from adcp.signing.replay import InMemoryReplayStore, ReplayStore
from adcp.signing.revocation import RevocationChecker, RevocationList

CoversDigestPolicy = Literal["required", "forbidden", "either"]

REQUIRED_COMPONENTS = ("@method", "@target-uri", "@authority")
REQUIRED_PARAMS = ("created", "expires", "nonce", "keyid", "alg", "tag")
_INT_PARAMS = frozenset({"created", "expires"})
_STR_PARAMS = frozenset({"nonce", "keyid", "alg", "tag"})

# Defensive upper bound against log/dict-key poisoning. RFC 7517 has no hard kid
# limit; 256 bytes is plenty for any real-world kid/nonce.
_MAX_PARAM_LEN = 256


@dataclass(frozen=True)
class VerifiedSigner:
    """Returned on successful verification. The key_id is the signer's identity."""

    key_id: str
    alg: str
    label: str
    verified_at: float
    agent_url: str | None = None


@dataclass
class VerifierCapability:
    """The `request_signing` block a verifier advertises on get_adcp_capabilities.

    Defaults to `covers_content_digest="required"`: body integrity must be
    authenticated end-to-end. With `"either"`, a MITM inside TLS termination
    (reverse proxy, service mesh) can swap bodies freely on unsigned-digest
    requests — only pick `"either"` if you've weighed that tradeoff.
    """

    supported: bool = True
    covers_content_digest: CoversDigestPolicy = "required"
    required_for: frozenset[str] = field(default_factory=frozenset)
    supported_for: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, kw_only=True)
class VerifyOptions:
    """Options bag passed to ``verify_request_signature``.

    ``replay_store`` defaults to a fresh :class:`InMemoryReplayStore` so the
    verifier always enforces nonce uniqueness on every request — defaulting
    to ``None`` would silently disable replay protection for callers who
    forget to wire a store, the exact security regression the AdCP profile's
    step 12 exists to prevent. Wire an explicit shared store (Redis, Postgres,
    etc.) for multi-replica deployments where replay state must be
    coordinated across processes; pass ``replay_store=None`` if you genuinely
    need to bypass the check (uncommon — typically only short-lived
    integration tests).

    ``revocation_checker`` and ``revocation_list`` remain optional —
    most agents don't track key revocations at runtime, and the verifier
    correctly skips the check when both are absent. Wire one when you
    publish a revocation list or expose an admin tool for emergency rotation.
    """

    now: float
    capability: VerifierCapability
    operation: str
    jwks_resolver: JwksResolver
    replay_store: ReplayStore | None = field(default_factory=InMemoryReplayStore)
    revocation_checker: RevocationChecker | None = None
    revocation_list: RevocationList | None = None
    max_skew_seconds: int = DEFAULT_SKEW_SECONDS
    max_window_seconds: int = MAX_WINDOW_SECONDS
    label: str = SIG_LABEL_DEFAULT
    expected_tag: str = DEFAULT_TAG
    expected_adcp_use: str = ADCP_USE_REQUEST
    allowed_algs: frozenset[str] = ALLOWED_ALGS
    agent_url: str | None = None
    # Multi-tenant deployments pass tenant_id so a JWKS resolver shared
    # across tenants refuses keys outside the active tenant's published
    # JWKS. Single-tenant deployments leave it None; resolvers that
    # don't enforce tenant scoping ignore it. See JwksResolver Protocol.
    tenant_id: str | None = None


def verify_request_signature(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    options: VerifyOptions,
) -> VerifiedSigner:
    """Run the AdCP request-signing verifier checklist against a request.

    Raises SignatureVerificationError with the spec error code on failure.
    """
    sig_input_raw = _lookup(headers, "signature-input")
    sig_raw = _lookup(headers, "signature")

    _precheck_presence(
        sig_input_raw=sig_input_raw,
        sig_raw=sig_raw,
        operation=options.operation,
        required_for=options.capability.required_for,
    )
    assert sig_input_raw is not None and sig_raw is not None

    try:
        labels = parse_signature_input_header(sig_input_raw)
    except (ValueError, KeyError) as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=1,
            message=f"Signature-Input parse error: {exc}",
        ) from exc
    if options.label not in labels:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=1,
            message=f"label {options.label!r} not present in Signature-Input",
        )
    parsed = labels[options.label]

    try:
        sig_bytes = extract_signature_bytes(sig_raw, options.label)
    except (ValueError, KeyError) as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=1,
            message=f"Signature parse error: {exc}",
        ) from exc

    _check_params_present(parsed.params)
    _check_tag(parsed.params, options.expected_tag)
    _check_alg(parsed.params, options.allowed_algs)
    try:
        _check_window(
            parsed.params,
            now=options.now,
            max_skew=options.max_skew_seconds,
            max_window=options.max_window_seconds,
        )
    except (ValueError, KeyError) as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=5,
            message=f"window param parse error: {exc}",
        ) from exc
    _check_components(
        parsed.components,
        headers=headers,
        body=body,
        policy=options.capability.covers_content_digest,
    )

    keyid = str(parsed.params["keyid"])
    nonce = str(parsed.params["nonce"])
    if len(keyid) > _MAX_PARAM_LEN:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=2,
            message=f"keyid exceeds {_MAX_PARAM_LEN} bytes",
        )
    if len(nonce) > _MAX_PARAM_LEN:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=2,
            message=f"nonce exceeds {_MAX_PARAM_LEN} bytes",
        )

    jwk = options.jwks_resolver(keyid, tenant_id=options.tenant_id)
    if jwk is None:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_UNKNOWN,
            step=7,
            message=f"no JWK for keyid {keyid!r}",
        )

    alg = str(parsed.params["alg"])
    _check_key_purpose(jwk, alg, expected_adcp_use=options.expected_adcp_use)

    if options.revocation_list is not None:
        as_of = datetime.fromtimestamp(options.now, tz=timezone.utc)
        if options.revocation_list.is_stale(as_of):
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_REVOCATION_STALE,
                step=9,
                message=(
                    f"revocation list next_update {options.revocation_list.next_update} "
                    f"is in the past"
                ),
            )
    if options.revocation_checker is not None and options.revocation_checker(keyid):
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_REVOKED,
            step=9,
            message=f"key {keyid!r} is revoked",
        )

    # Step 9a (per spec, after adcp#2342): per-keyid cap runs between JWKS
    # resolution and crypto verify. A compromised or misconfigured signer
    # hitting the cap must be rejected cheaply, not after Ed25519/ECDSA verify.
    if options.replay_store is not None and options.replay_store.at_capacity(keyid):
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_RATE_ABUSE,
            step="9a",
            message=f"replay cache at capacity for keyid {keyid!r}",
        )

    try:
        base = build_signature_base(method=method, url=url, headers=headers, parsed=parsed).encode(
            "utf-8"
        )
    except (ValueError, KeyError) as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=6,
            message=f"signature base construction failed: {exc}",
        ) from exc
    try:
        public_key = public_key_from_jwk(jwk)
    except (ValueError, KeyError) as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=8,
            message=f"JWK decode failed: {exc}",
        ) from exc
    if not verify_signature(
        alg=alg, public_key=public_key, signature_base=base, signature=sig_bytes
    ):
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_INVALID,
            step=10,
            message="signature did not verify over the computed base",
        )

    if "content-digest" in parsed.components:
        digest_header = _lookup(headers, "content-digest")
        if digest_header is None or not content_digest_matches(digest_header, body):
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_DIGEST_MISMATCH,
                step=11,
                message="Content-Digest does not match body",
            )

    if options.replay_store is not None:
        if options.replay_store.seen(keyid, nonce):
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_REPLAYED,
                step=12,
                message=f"nonce {nonce!r} already seen for keyid {keyid!r}",
            )
        ttl = max(
            float(parsed.params["expires"]) - options.now + options.max_skew_seconds,
            0.0,
        )
        options.replay_store.remember(keyid, nonce, ttl)

    return VerifiedSigner(
        key_id=keyid,
        alg=alg,
        label=options.label,
        verified_at=options.now,
        agent_url=options.agent_url,
    )


def _precheck_presence(
    *,
    sig_input_raw: str | None,
    sig_raw: str | None,
    operation: str,
    required_for: frozenset[str],
) -> None:
    if sig_input_raw is None and sig_raw is None:
        if operation in required_for:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_REQUIRED,
                step=0,
                message=f"operation {operation!r} requires a signature",
            )
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_REQUIRED,
            step=0,
            message="no signature headers on request",
        )
    if (sig_input_raw is None) != (sig_raw is None):
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_HEADER_MALFORMED,
            step=1,
            message="Signature and Signature-Input must both be present",
        )


def _check_params_present(params: Mapping[str, Any]) -> None:
    missing = [p for p in REQUIRED_PARAMS if p not in params]
    if missing:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_PARAMS_INCOMPLETE,
            step=2,
            message=f"missing required signature params: {missing}",
        )
    for name in _INT_PARAMS:
        if not isinstance(params[name], int) or isinstance(params[name], bool):
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_HEADER_MALFORMED,
                step=2,
                message=f"param {name!r} must be an integer, got {type(params[name]).__name__}",
            )
    for name in _STR_PARAMS:
        if not isinstance(params[name], str):
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_HEADER_MALFORMED,
                step=2,
                message=f"param {name!r} must be a string, got {type(params[name]).__name__}",
            )


def _check_tag(params: Mapping[str, Any], expected_tag: str) -> None:
    if str(params.get("tag")) != expected_tag:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_TAG_INVALID,
            step=3,
            message=f"tag {params.get('tag')!r} != expected {expected_tag!r}",
        )


def _check_alg(params: Mapping[str, Any], allowed_algs: frozenset[str]) -> None:
    if str(params.get("alg")) not in allowed_algs:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_ALG_NOT_ALLOWED,
            step=4,
            message=f"alg {params.get('alg')!r} not in allowlist {sorted(allowed_algs)}",
        )


def _check_window(params: Mapping[str, Any], *, now: float, max_skew: int, max_window: int) -> None:
    created = int(params["created"])
    expires = int(params["expires"])
    if expires <= created:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_WINDOW_INVALID,
            step=5,
            message=f"expires {expires} must be greater than created {created}",
        )
    if expires - created > max_window:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_WINDOW_INVALID,
            step=5,
            message=f"window {expires - created}s exceeds max {max_window}s",
        )
    if now < created - max_skew:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_WINDOW_INVALID,
            step=5,
            message=f"signature not yet valid (now={now}, created={created})",
        )
    if now > expires + max_skew:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_WINDOW_INVALID,
            step=5,
            message=f"signature expired (now={now}, expires={expires})",
        )


def _check_components(
    components: tuple[str, ...],
    *,
    headers: Mapping[str, str],
    body: bytes,
    policy: CoversDigestPolicy,
) -> None:
    if len(components) != len(set(components)):
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED,
            step=6,
            message=f"duplicate covered components: {components}",
        )
    component_set = set(components)
    for required in REQUIRED_COMPONENTS:
        if required not in component_set:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE,
                step=6,
                message=f"required covered component missing: {required}",
            )
    if _lookup(headers, "content-type") is not None and "content-type" not in component_set:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE,
            step=6,
            message="content-type header present but not covered",
        )
    digest_covered = "content-digest" in component_set
    if policy == "required" and not digest_covered:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE,
            step=6,
            message="verifier requires content-digest coverage",
        )
    if policy == "forbidden" and digest_covered:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED,
            step=6,
            message="verifier forbids content-digest coverage",
        )


def _check_key_purpose(jwk: Mapping[str, Any], alg: str, *, expected_adcp_use: str) -> None:
    if jwk.get("use") != "sig":
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
            step=8,
            message=f"JWK.use {jwk.get('use')!r} != 'sig'",
        )
    key_ops = jwk.get("key_ops")
    if not isinstance(key_ops, (list, tuple)) or "verify" not in key_ops:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
            step=8,
            message=f"JWK.key_ops {key_ops!r} missing 'verify'",
        )
    if jwk.get("adcp_use") != expected_adcp_use:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
            step=8,
            message=f"JWK.adcp_use {jwk.get('adcp_use')!r} != {expected_adcp_use!r}",
        )
    try:
        jwk_alg = alg_for_jwk(dict(jwk))
    except ValueError as exc:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
            step=8,
            message=f"JWK alg cannot be derived: {exc}",
        ) from exc
    if jwk_alg != alg:
        raise SignatureVerificationError(
            REQUEST_SIGNATURE_KEY_PURPOSE_INVALID,
            step=8,
            message=f"JWK alg {jwk_alg!r} does not match signature alg {alg!r}",
        )


__all__ = [
    "ALG_ED25519",
    "ALG_ES256",
    "ALLOWED_ALGS",
    "CoversDigestPolicy",
    "JwksResolver",
    "VerifiedSigner",
    "VerifierCapability",
    "VerifyOptions",
    "verify_request_signature",
]
