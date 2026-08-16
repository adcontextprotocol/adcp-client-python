"""AdCP RFC 9421 request-signing profile.

Implements the transport-layer signed-request profile from the AdCP
specification. See:
https://adcontextprotocol.org/docs/building/implementation/security#signed-requests-transport-layer

Quickstart
==========

The core names you'll reach for (everything else is for advanced use):

**Buyers** (signing outgoing requests):

* :func:`sign_request` — produce ``Signature`` / ``Signature-Input``
  headers for one request
* :func:`load_private_key_pem` — rehydrate the PEM ``adcp-keygen`` wrote
* :class:`SigningConfig` — bundle key material for auto-signing via
  ``ADCPClient(signing=...)``

**Provisioning** (new keypairs):

* :func:`generate_signing_keypair` — programmatic counterpart to the
  ``adcp-keygen`` CLI. Returns ``(pem_bytes, public_jwk)`` so tests,
  provisioning scripts, and any non-shell context can mint keys
  without spawning a subprocess. Both paths share the same spine — a
  PEM generated here is indistinguishable from one the CLI wrote.

  .. code-block:: python

      import os

      from adcp.signing import generate_signing_keypair

      # CLI equivalence:
      #   adcp-keygen --alg ed25519 --purpose webhook-signing
      pem, public_jwk = generate_signing_keypair(
          alg="ed25519", purpose="webhook-signing"
      )

      # Mode 0600, O_EXCL so an existing file is never overwritten.
      # Path.write_bytes inherits the process umask (often 0644 =
      # world-readable) — don't use it for private-key material.
      fd = os.open("webhook-key.pem", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      try:
          os.write(fd, pem)
      finally:
          os.close(fd)
      publish_to_jwks_uri(public_jwk)

**Sellers** (verifying incoming requests):

* :func:`verify_starlette_request` / :func:`verify_flask_request` —
  framework-shaped wrappers around :func:`verify_request_signature`
* :class:`VerifyOptions` — the knobs (capability, jwks_resolver,
  replay_store, revocation_checker)
* :class:`VerifierCapability` — what the seller advertises (e.g.
  ``required_for={"create_media_buy"}``)
* :class:`StaticJwksResolver` — for testing; use
  :class:`CachingJwksResolver` against a live ``jwks_uri``
* :class:`SignatureVerificationError` — raised on rejection; ``.code``
  is the spec error string
* :func:`unauthorized_response_headers` — builds the 401
  ``WWW-Authenticate: Signature error="..."`` header
* :class:`InMemoryReplayStore` for single-process deployments;
  :class:`PgReplayStore` (behind ``[pg]`` extra) for multi-worker

**Governance agents**:

* :class:`CachingRevocationChecker` — fetches + caches a signed
  revocation list from ``{issuer}/.well-known/governance-revocations.json``
* Async variants: :class:`AsyncCachingJwksResolver`,
  :class:`AsyncCachingRevocationChecker`

**Custom fetchers** (rolling your own JWKS / revocation transport):

* :func:`build_ip_pinned_transport` /
  :func:`build_async_ip_pinned_transport` — returns an
  :class:`httpx.HTTPTransport` wired to resolve the URI's host once
  (with SSRF validation) and pin subsequent connects to that IP.
  Closes the DNS-rebinding TOCTOU for anything built on
  :class:`httpx.Client`.
* :func:`resolve_and_validate_host` — returns ``(host, ip, port)``;
  same SSRF rules as :func:`validate_jwks_uri`. Use this if you're
  wiring your own transport and only need the resolved + validated
  IP.
"""

from __future__ import annotations

from adcp.signing.agent_resolver import (
    AgentResolution,
    AgentResolverError,
    AgentResolverErrorCode,
    TraceEntry,
    async_resolve_agent,
    resolve_agent,
    verify_from_agent_url,
)
from adcp.signing.autosign import (
    SigningConfig,
    SigningDecision,
    operation_needs_signing,
)
from adcp.signing.brand_authz import (
    BrandAuthorizationReason,
    BrandAuthorizationResolver,
    BrandAuthorizationResult,
    BrandJsonAuthorizationResolver,
    build_brand_json_resolvers,
)
from adcp.signing.brand_jwks import (
    BrandAgentType,
    BrandJsonJwksResolver,
    BrandJsonResolverError,
    BrandJsonResolverErrorCode,
)
from adcp.signing.canonical import (
    SignatureInputLabel,
    build_signature_base,
    canonicalize_authority,
    canonicalize_target_uri,
    parse_signature_input_header,
)
from adcp.signing.capability_cache import (
    CachedCapability,
    CapabilityCache,
    build_capability_cache_key,
    default_capability_cache,
)
from adcp.signing.capability_priming import (
    CAPABILITY_OP,
    NEGATIVE_CACHE_TTL_SECONDS,
    ensure_capability_loaded,
)
from adcp.signing.client import (
    CapabilityProvider,
    install_signing_event_hook,
    signing_operation,
)
from adcp.signing.constants import (
    DEFAULT_EXPIRES_IN_SECONDS,
    DEFAULT_SKEW_SECONDS,
    DEFAULT_TAG,
    MAX_WINDOW_SECONDS,
    NONCE_BYTES,
    SIG_LABEL_DEFAULT,
)
from adcp.signing.crypto import (
    ALG_ED25519,
    ALG_ES256,
    ALLOWED_ALGS,
    alg_for_jwk,
    b64url_decode,
    b64url_encode,
    extract_signature_bytes,
    format_signature_header,
    load_private_key_pem,
    private_key_from_jwk,
    public_key_from_jwk,
    sign_signature_base,
    verify_signature,
)
from adcp.signing.digest import compute_content_digest_sha256, content_digest_matches
from adcp.signing.errors import (
    REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON,
    REQUEST_SIGNATURE_ALG_NOT_ALLOWED,
    REQUEST_SIGNATURE_BRAND_JSON_AMBIGUOUS,
    REQUEST_SIGNATURE_BRAND_JSON_MALFORMED,
    REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE,
    REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING,
    REQUEST_SIGNATURE_BRAND_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE,
    REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE,
    REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED,
    REQUEST_SIGNATURE_DIGEST_MISMATCH,
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_INVALID,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH,
    REQUEST_SIGNATURE_KEY_ORIGIN_MISSING,
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
from adcp.signing.etld import (
    host_from,
    registrable_domain,
    same_registrable_domain,
)
from adcp.signing.ip_pinned_transport import (
    AsyncIpPinnedTransport,
    IpPinnedTransport,
    abuild_ip_pinned_transport,
    build_async_ip_pinned_transport,
    build_ip_pinned_transport,
)
from adcp.signing.jwks import (
    DEFAULT_ALLOWED_PORTS,
    AsyncCachingJwksResolver,
    AsyncJwksFetcher,
    AsyncJwksResolver,
    BrandSourcedJwksResolver,
    CachingJwksResolver,
    JwksResolver,
    SSRFValidationError,
    StaticJwksResolver,
    as_async_resolver,
    async_default_jwks_fetcher,
    default_jwks_fetcher,
    resolve_and_validate_host,
    validate_jwks_uri,
)
from adcp.signing.jws import (
    JwsError,
    JwsMalformedError,
    JwsSignatureInvalidError,
    JwsUnknownKeyError,
    averify_detached_jws,
    averify_jws_document,
    verify_detached_jws,
    verify_jws_document,
)
from adcp.signing.key_origins import check_key_origin_consistency
from adcp.signing.keygen import generate_signing_keypair, pem_to_adcp_jwk
from adcp.signing.middleware import (
    unauthorized_response_headers,
    verify_flask_request,
    verify_starlette_request,
)
from adcp.signing.provider import (
    InMemorySigningProvider,
    SigningAlgorithm,
    SigningProvider,
)
from adcp.signing.replay import (
    AtomicReplayStore,
    InMemoryReplayStore,
    ReplayClaimResult,
    ReplayStore,
    supports_atomic_claim,
)
from adcp.signing.revocation import RevocationChecker, RevocationList
from adcp.signing.revocation_fetcher import (
    DEFAULT_GRACE_MULTIPLIER,
    REVOCATION_LIST_TYP,
    AsyncCachingRevocationChecker,
    AsyncRevocationListFetcher,
    CachingRevocationChecker,
    FetchResult,
    RevocationListFetcher,
    RevocationListFetchError,
    RevocationListFreshnessError,
    RevocationListParseError,
    async_default_revocation_list_fetcher,
    default_revocation_list_fetcher,
)
from adcp.signing.signer import (
    SignedHeaders,
    async_sign_request,
    sign_request,
)
from adcp.signing.standard_webhooks import (
    StandardWebhookError,
    sign_standard_webhook,
    verify_standard_webhook,
)
from adcp.signing.standard_webhooks import (
    decode_secret as decode_standard_webhook_secret,
)
from adcp.signing.verifier import (
    VerifiedSigner,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)

# Conditional import: PgReplayStore needs the [pg] extra. Always expose
# the name — if psycopg isn't installed we fall through to a stub class
# whose constructor raises ImportError with the install hint. Exposing
# None would give callers a confusing ``TypeError: 'NoneType' object is
# not callable`` on instantiation; the stub turns that into a
# self-explanatory error at the right moment.
try:
    from adcp.signing.pg import PgReplayStore  # noqa: F401
except ImportError:  # pragma: no cover — exercised by the [pg] extra tests

    class PgReplayStore:  # type: ignore[no-redef]
        """Stub raised when ``adcp[pg]`` isn't installed.

        Attempting to instantiate raises :class:`ImportError` with the
        install-hint text from :mod:`adcp.signing.pg.replay_store`.
        """

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise ImportError(
                "PgReplayStore requires psycopg3 and psycopg-pool. Install the "
                "'pg' extra: `pip install 'adcp[pg]'` (Poetry: "
                "`poetry add 'adcp[pg]'`)."
            )


__all__ = [
    "ALG_ED25519",
    "ALG_ES256",
    "ALLOWED_ALGS",
    "AgentResolution",
    "AgentResolverError",
    "AgentResolverErrorCode",
    "AsyncCachingJwksResolver",
    "AsyncCachingRevocationChecker",
    "AsyncIpPinnedTransport",
    "AsyncJwksFetcher",
    "AsyncJwksResolver",
    "AsyncRevocationListFetcher",
    "AtomicReplayStore",
    "BrandAgentType",
    "BrandAuthorizationReason",
    "BrandAuthorizationResolver",
    "BrandAuthorizationResult",
    "BrandJsonAuthorizationResolver",
    "BrandJsonJwksResolver",
    "BrandSourcedJwksResolver",
    "BrandJsonResolverError",
    "BrandJsonResolverErrorCode",
    "CAPABILITY_OP",
    "CachedCapability",
    "CachingJwksResolver",
    "CachingRevocationChecker",
    "CapabilityCache",
    "CapabilityProvider",
    "DEFAULT_ALLOWED_PORTS",
    "DEFAULT_EXPIRES_IN_SECONDS",
    "DEFAULT_GRACE_MULTIPLIER",
    "DEFAULT_SKEW_SECONDS",
    "DEFAULT_TAG",
    "FetchResult",
    "InMemoryReplayStore",
    "InMemorySigningProvider",
    "IpPinnedTransport",
    "JwksResolver",
    "JwsError",
    "JwsMalformedError",
    "JwsSignatureInvalidError",
    "JwsUnknownKeyError",
    "MAX_WINDOW_SECONDS",
    "NEGATIVE_CACHE_TTL_SECONDS",
    "NONCE_BYTES",
    "PgReplayStore",
    "REQUEST_SIGNATURE_AGENT_NOT_IN_BRAND_JSON",
    "REQUEST_SIGNATURE_ALG_NOT_ALLOWED",
    "REQUEST_SIGNATURE_BRAND_JSON_AMBIGUOUS",
    "REQUEST_SIGNATURE_BRAND_JSON_MALFORMED",
    "REQUEST_SIGNATURE_BRAND_JSON_UNREACHABLE",
    "REQUEST_SIGNATURE_BRAND_JSON_URL_MISSING",
    "REQUEST_SIGNATURE_BRAND_ORIGIN_MISMATCH",
    "REQUEST_SIGNATURE_CAPABILITIES_UNREACHABLE",
    "REQUEST_SIGNATURE_COMPONENTS_INCOMPLETE",
    "REQUEST_SIGNATURE_COMPONENTS_UNEXPECTED",
    "REQUEST_SIGNATURE_DIGEST_MISMATCH",
    "REQUEST_SIGNATURE_HEADER_MALFORMED",
    "REQUEST_SIGNATURE_INVALID",
    "REQUEST_SIGNATURE_JWKS_UNAVAILABLE",
    "REQUEST_SIGNATURE_JWKS_UNTRUSTED",
    "REQUEST_SIGNATURE_KEY_ORIGIN_MISMATCH",
    "REQUEST_SIGNATURE_KEY_ORIGIN_MISSING",
    "REQUEST_SIGNATURE_KEY_PURPOSE_INVALID",
    "REQUEST_SIGNATURE_KEY_REVOKED",
    "REQUEST_SIGNATURE_KEY_UNKNOWN",
    "REQUEST_SIGNATURE_PARAMS_INCOMPLETE",
    "REQUEST_SIGNATURE_RATE_ABUSE",
    "REQUEST_SIGNATURE_REPLAYED",
    "REQUEST_SIGNATURE_REQUIRED",
    "REQUEST_SIGNATURE_REVOCATION_STALE",
    "REQUEST_SIGNATURE_TAG_INVALID",
    "REQUEST_SIGNATURE_WINDOW_INVALID",
    "REVOCATION_LIST_TYP",
    "ReplayStore",
    "ReplayClaimResult",
    "supports_atomic_claim",
    "RevocationChecker",
    "RevocationList",
    "RevocationListFetchError",
    "RevocationListFetcher",
    "RevocationListFreshnessError",
    "RevocationListParseError",
    "SIG_LABEL_DEFAULT",
    "SSRFValidationError",
    "SignatureInputLabel",
    "SignatureVerificationError",
    "SignedHeaders",
    "StandardWebhookError",
    "SigningAlgorithm",
    "SigningConfig",
    "SigningDecision",
    "SigningProvider",
    "StaticJwksResolver",
    "TraceEntry",
    "VerifiedSigner",
    "VerifierCapability",
    "VerifyOptions",
    "alg_for_jwk",
    "abuild_ip_pinned_transport",
    "as_async_resolver",
    "async_default_jwks_fetcher",
    "async_default_revocation_list_fetcher",
    "async_resolve_agent",
    "async_sign_request",
    "averify_detached_jws",
    "averify_jws_document",
    "b64url_decode",
    "b64url_encode",
    "build_async_ip_pinned_transport",
    "build_brand_json_resolvers",
    "build_capability_cache_key",
    "build_ip_pinned_transport",
    "build_signature_base",
    "canonicalize_authority",
    "canonicalize_target_uri",
    "check_key_origin_consistency",
    "compute_content_digest_sha256",
    "content_digest_matches",
    "decode_standard_webhook_secret",
    "default_capability_cache",
    "default_jwks_fetcher",
    "default_revocation_list_fetcher",
    "ensure_capability_loaded",
    "extract_signature_bytes",
    "format_signature_header",
    "generate_signing_keypair",
    "host_from",
    "install_signing_event_hook",
    "load_private_key_pem",
    "operation_needs_signing",
    "parse_signature_input_header",
    "pem_to_adcp_jwk",
    "private_key_from_jwk",
    "public_key_from_jwk",
    "registrable_domain",
    "resolve_agent",
    "resolve_and_validate_host",
    "same_registrable_domain",
    "sign_request",
    "sign_signature_base",
    "sign_standard_webhook",
    "signing_operation",
    "unauthorized_response_headers",
    "validate_jwks_uri",
    "verify_detached_jws",
    "verify_flask_request",
    "verify_from_agent_url",
    "verify_jws_document",
    "verify_request_signature",
    "verify_signature",
    "verify_standard_webhook",
    "verify_starlette_request",
]
