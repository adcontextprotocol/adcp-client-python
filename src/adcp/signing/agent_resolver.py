"""Bootstrap from an agent URL to that agent's signing keys.

AdCP 3.x adds ``identity.brand_json_url`` to the
``get_adcp_capabilities`` response (per adcontextprotocol/adcp#3690,
schema-relaxed in 3.0.5 via ``identity.additionalProperties: true``).
With that field present, a verifier can hand an agent URL alone and
the resolver walks:

    agent_url → get_adcp_capabilities → identity.brand_json_url →
    brand.json → jwks_uri → JWK set

without out-of-band knowledge of the operator domain.

Three hops, three SSRF guards:

* **Capabilities (this module):** built atop
  :func:`adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport`.
  Same posture as the brand.json + JWKS hops — IP-pinned at connect,
  redirect-capped, body-capped, HTTPS-validated. **Not routed through
  :class:`adcp.client.ADCPClient`** because that client is for
  trusted-counterparty traffic; here ``agent_url`` is attacker-shaped.
* **brand.json:** delegated to :class:`BrandJsonJwksResolver` (already
  IP-pinned per-hop with redirect cap).
* **JWKS:** delegated to :func:`async_default_jwks_fetcher` (IP-pinned,
  trust_env=False).

The resolver returns an :class:`AgentResolution` snapshot —
``agent_url``, ``brand_json_url``, ``jwks_uri``, the full JWK set, and
a per-hop ``trace``. Adopters who want ongoing rotation handling
instantiate :class:`BrandJsonJwksResolver` directly with the resolved
``brand_json_url``; this resolver is one-shot.

The 8 ``request_signature_*`` verifier-side error codes are NOT mapped
here — those belong to the :func:`verify_from_agent_url` factory (still
to ship). Resolver-side failures surface as :class:`AgentResolverError`
with a stable ``code`` attribute.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from adcp.signing._bounded_http import ResponseTooLargeError, async_read_limited_bytes
from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.brand_jwks import (
    BrandAgentType,
    BrandJsonJwksResolver,
    BrandJsonResolverError,
)
from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport
from adcp.signing.jwks import (
    SSRFValidationError,
    StaticJwksResolver,
    async_default_jwks_fetcher,
)
from adcp.signing.replay import (
    InMemoryReplayStore,
    ReplayClaimResult,
    ReplayStore,
    supports_atomic_claim,
)

#: Maximum capabilities response body in bytes. Capabilities documents
#: are larger than brand.json (operator/agent declarations, supported
#: protocol matrix, idempotency policy) so the cap is higher — 64 KiB
#: matches the issue's spec quickstart guidance.
DEFAULT_MAX_CAPABILITIES_BYTES = 64 * 1024

#: Capabilities fetch must NOT follow redirects across origins — a
#: malicious agent_url could redirect to a trusted origin's
#: capabilities endpoint and steal that origin's identity claim.
#: Following 0 redirects forces the resolver to surface the redirect
#: as ``capabilities_unreachable`` rather than silently follow it.
DEFAULT_CAPABILITIES_MAX_REDIRECTS = 0

DEFAULT_CAPABILITIES_TIMEOUT_SECONDS = 10.0

#: Stable error codes raised by :class:`AgentResolverError`. Surface
#: matches the resolver-side concerns (the verifier-side
#: ``request_signature_*`` codes ship with :func:`verify_from_agent_url`,
#: not here).
AgentResolverErrorCode = Literal[
    "invalid_agent_url",
    "capabilities_unreachable",
    "capabilities_invalid",
    "brand_json_url_missing",
    "brand_json_resolution_failed",
    "jwks_fetch_failed",
]


class AgentResolverError(Exception):
    """Raised when ``async_resolve_agent`` cannot produce an
    :class:`AgentResolution`. The ``code`` attribute is stable across
    versions and intended for ``except`` clarity / structured logging.
    """

    def __init__(self, code: AgentResolverErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: AgentResolverErrorCode = code
        self.message = message


# ---- Trace + AgentResolution ----


class TraceEntry(BaseModel):
    """One hop in the resolver chain. Captured for observability and
    CLI ``--json`` output. Adopters reading ``trace`` for telemetry
    should treat hop names as a stable enum (``capabilities``,
    ``brand_json``, ``jwks``)."""

    model_config = ConfigDict(extra="forbid")

    hop: Literal["capabilities", "brand_json", "jwks"]
    url: str
    status: Literal["ok", "error"]
    latency_ms: float
    error_code: str | None = None
    error_message: str | None = None


class AgentResolution(BaseModel):
    """Snapshot of an agent's signing-key chain at a point in time.

    Carries everything a verifier needs to validate a request from
    ``agent_url``: the brand.json URL the operator advertised, the
    matched agent entry, the JWKS URI and the JWK set itself, plus
    a per-hop trace for observability.

    Note ``identity_posture`` and ``consistency`` (proposed in the
    original #344 issue body) are **not** present — neither term
    has normative AdCP provenance in 3.0.5 schemas, so emitting them
    in cross-SDK ``--json`` output would leak SDK-invented terms.
    """

    model_config = ConfigDict(extra="forbid")

    agent_url: str = Field(description="Agent URL passed to the resolver")
    brand_json_url: str = Field(
        description="Operator-declared brand.json URL discovered via "
        "``identity.brand_json_url`` on the capabilities response"
    )
    agent_entry: dict[str, Any] = Field(
        description="The matching entry from brand.json's agents[] array"
    )
    jwks_uri: str = Field(description="The JWKS URI from the matched agent entry")
    jwks: dict[str, Any] = Field(
        description="Full JWK set fetched from ``jwks_uri`` (RFC 7517 ``{keys: [...]}``)"
    )
    fetched_at: float = Field(description="Resolution wall-clock time (Unix epoch seconds)")
    key_origins: dict[str, str] | None = Field(
        default=None,
        description=(
            "Verbatim ``identity.key_origins`` map from the capabilities "
            "response — purpose → declared origin (e.g. "
            "``{'request_signing': 'https://keys.brand.com'}``). The "
            "verifier consults this to enforce the spec's key-origin "
            "consistency check (resolved jwks_uri host MUST equal the "
            "declared origin for the purpose under verification). "
            "``None`` when the operator advertises no key_origins map; "
            "the verifier raises ``request_signature_key_origin_missing`` "
            "for any signed-traffic purpose without a corresponding entry."
        ),
    )
    trace: list[TraceEntry] = Field(default_factory=list)


# ---- Capabilities fetch (the SSRF gap this module closes) ----


@dataclass
class _CapabilitiesPayload:
    body: dict[str, Any]
    final_url: str


async def _fetch_capabilities(
    agent_url: str,
    *,
    allow_private: bool,
    max_body_bytes: int,
    max_redirects: int,
    timeout_seconds: float,
    client_factory: Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]] | None,
) -> _CapabilitiesPayload:
    """SSRF-pinned ``GET <agent_url>`` returning the parsed
    capabilities body and the final URL after redirects (if any are
    allowed).

    Mirrors the brand.json fetcher's posture: per-hop IP pin via
    :func:`build_async_ip_pinned_transport`, body cap before parse,
    no auto-redirect, ``trust_env=False`` so proxy env vars can't
    rewrite the destination.

    Capabilities-specific tightening: default ``max_redirects=0``
    blocks cross-origin redirect-as-identity-pivot.
    """
    if client_factory is not None:
        client_cm = client_factory(agent_url)
    else:
        try:
            transport = build_async_ip_pinned_transport(agent_url, allow_private=allow_private)
        except SSRFValidationError as exc:
            raise AgentResolverError(
                "capabilities_unreachable", f"agent_url failed SSRF check: {exc}"
            ) from exc
        except ValueError as exc:
            raise AgentResolverError(
                "invalid_agent_url", f"agent_url is not a valid URL: {exc}"
            ) from exc
        client_cm = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    seen: set[str] = set()
    url = agent_url
    for hop in range(max_redirects + 1):
        if url in seen:
            raise AgentResolverError(
                "capabilities_unreachable",
                "capabilities fetch hit redirect loop",
            )
        seen.add(url)

        try:
            async with client_cm as client:
                try:
                    request_cm = client.stream(
                        "GET",
                        url,
                        headers={"accept": "application/json", "accept-encoding": "identity"},
                    )
                    async with request_cm as response:
                        if 300 <= response.status_code < 400 and "location" in response.headers:
                            if hop == max_redirects:
                                raise AgentResolverError(
                                    "capabilities_unreachable",
                                    f"capabilities fetch hit redirect limit ({max_redirects})",
                                )
                            url = str(httpx.URL(url).join(response.headers["location"]))
                            try:
                                transport = build_async_ip_pinned_transport(
                                    url, allow_private=allow_private
                                )
                            except SSRFValidationError as exc:
                                raise AgentResolverError(
                                    "capabilities_unreachable",
                                    f"redirect target failed SSRF check: {exc}",
                                ) from exc
                            client_cm = httpx.AsyncClient(
                                transport=transport,
                                timeout=timeout_seconds,
                                follow_redirects=False,
                                trust_env=False,
                            )
                            continue

                        if response.status_code != 200:
                            raise AgentResolverError(
                                "capabilities_unreachable",
                                f"capabilities fetch returned HTTP {response.status_code}",
                            )

                        try:
                            body_bytes = await async_read_limited_bytes(
                                response, limit=max_body_bytes
                            )
                        except ResponseTooLargeError as exc:
                            raise AgentResolverError(
                                "capabilities_invalid", f"capabilities {exc}"
                            ) from exc

                        try:
                            parsed = json.loads(body_bytes)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise AgentResolverError(
                                "capabilities_invalid",
                                "capabilities response is not valid JSON",
                            ) from exc
                except SSRFValidationError as exc:
                    raise AgentResolverError(
                        "capabilities_unreachable",
                        f"agent_url failed SSRF check: {exc}",
                    ) from exc
                except (httpx.HTTPError, OSError) as exc:
                    raise AgentResolverError(
                        "capabilities_unreachable",
                        f"capabilities fetch failed: {exc}",
                    ) from exc

                if not isinstance(parsed, dict):
                    raise AgentResolverError(
                        "capabilities_invalid",
                        "capabilities response is not a JSON object",
                    )

                return _CapabilitiesPayload(body=parsed, final_url=url)
        except AgentResolverError:
            raise

    # Unreachable: loop body either returns or raises on every iteration.
    raise AgentResolverError(
        "capabilities_unreachable", "capabilities fetch exhausted redirect chain"
    )


def _extract_brand_json_url(capabilities: dict[str, Any]) -> str:
    """Pluck ``identity.brand_json_url`` from the capabilities body.

    The field is forward-compat — typed in 3.1, accepted via
    ``additionalProperties: true`` on 3.0.5+. Reading the raw dict
    avoids depending on the typed Pydantic surface (which won't carry
    the field until 3.1 schemas land).
    """
    identity = capabilities.get("identity")
    if not isinstance(identity, dict):
        raise AgentResolverError(
            "brand_json_url_missing",
            "capabilities response has no `identity` object",
        )
    brand_json_url = identity.get("brand_json_url")
    if not isinstance(brand_json_url, str) or not brand_json_url:
        raise AgentResolverError(
            "brand_json_url_missing",
            "capabilities `identity.brand_json_url` is missing or not a string "
            "(operator must publish 3690 to be discoverable from agent URL)",
        )
    return brand_json_url


#: Per-entry size clamp on ``identity.key_origins`` values. DNS hostname
#: limit is 253 octets (RFC 1035); origin strings carry scheme+host so
#: the practical cap is a bit higher, but 512 is well above any
#: legitimate value while still bounding the surface against a
#: pathologically-large entry from a 64 KiB capabilities body.
_MAX_KEY_ORIGIN_VALUE_BYTES = 512


def _extract_key_origins(capabilities: dict[str, Any]) -> dict[str, str] | None:
    """Pluck ``identity.key_origins`` from the capabilities body.

    Returns ``None`` when the operator advertises no key_origins map (a
    common posture for unsigned-traffic-only deployments — the verifier
    layer treats absence as "no per-purpose origin pin to check" and
    only raises ``request_signature_key_origin_missing`` when a signed
    purpose is actually exercised). Filters values to strings — a
    malformed entry is skipped rather than poisoning the whole map.

    **Per-entry length cap (``_MAX_KEY_ORIGIN_VALUE_BYTES``).** Each
    origin value is bounded to 512 bytes — well above any legitimate
    ``scheme + host + port`` shape but tight enough that a pathological
    multi-kilobyte value from the 64 KiB capabilities body doesn't
    propagate through downstream comparisons. Entries exceeding the cap
    are skipped (the verifier then surfaces the purpose as missing on
    the consistency check).

    Forward-compat with operators on 3.0 schemas: the map travels under
    ``additionalProperties: true`` and the SDK reads it as a plain dict
    rather than via the typed Pydantic surface (which won't carry the
    field until 3.1 lands).
    """
    identity = capabilities.get("identity")
    if not isinstance(identity, dict):
        return None
    raw = identity.get("key_origins")
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[str, str] = {}
    for purpose, origin in raw.items():
        if not (isinstance(purpose, str) and isinstance(origin, str) and origin):
            continue
        if len(origin.encode("utf-8")) > _MAX_KEY_ORIGIN_VALUE_BYTES:
            # Length-capped entry — skip rather than truncate (a
            # truncated host would silently match the wrong domain).
            continue
        out[purpose] = origin
    return out or None


# ---- Public API ----


async def async_resolve_agent(
    agent_url: str,
    *,
    agent_type: BrandAgentType,
    agent_id: str | None = None,
    brand_id: str | None = None,
    allow_private_destinations: bool = False,
    max_capabilities_bytes: int = DEFAULT_MAX_CAPABILITIES_BYTES,
    max_capabilities_redirects: int = DEFAULT_CAPABILITIES_MAX_REDIRECTS,
    capabilities_timeout_seconds: float = DEFAULT_CAPABILITIES_TIMEOUT_SECONDS,
    _capabilities_client_factory: (
        Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]] | None
    ) = None,
    _brand_jwks_client_factory: (
        Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]] | None
    ) = None,
) -> AgentResolution:
    """Bootstrap from ``agent_url`` to its JWK set via brand.json.

    Walks three hops with SSRF guards on each:

    1. ``GET <agent_url>`` — capabilities fetch (this module).
    2. ``GET <identity.brand_json_url>`` — brand.json walk via
       :class:`BrandJsonJwksResolver`.
    3. ``GET <jwks_uri>`` — JWKS fetch via
       :func:`async_default_jwks_fetcher`.

    The selector tuple ``(agent_type, agent_id, brand_id)`` matches the
    brand.json ``agents[]`` entry. ``agent_type`` is required because
    brand.json may list multiple agents (sales, governance, creative,
    etc.) under the same operator and the resolver can't infer which
    one ``agent_url`` corresponds to from the agent URL alone — that's
    operator topology, not in the wire response.
    """
    trace: list[TraceEntry] = []
    fetched_at = time.time()

    # --- Hop 1: capabilities ---
    cap_start = time.monotonic()
    try:
        capabilities = await _fetch_capabilities(
            agent_url,
            allow_private=allow_private_destinations,
            max_body_bytes=max_capabilities_bytes,
            max_redirects=max_capabilities_redirects,
            timeout_seconds=capabilities_timeout_seconds,
            client_factory=_capabilities_client_factory,
        )
        trace.append(
            TraceEntry(
                hop="capabilities",
                url=capabilities.final_url,
                status="ok",
                latency_ms=(time.monotonic() - cap_start) * 1000.0,
            )
        )
    except AgentResolverError as exc:
        trace.append(
            TraceEntry(
                hop="capabilities",
                url=agent_url,
                status="error",
                latency_ms=(time.monotonic() - cap_start) * 1000.0,
                error_code=exc.code,
                error_message=exc.message,
            )
        )
        raise

    brand_json_url = _extract_brand_json_url(capabilities.body)
    key_origins = _extract_key_origins(capabilities.body)

    # --- Hop 2: brand.json ---
    bj_start = time.monotonic()
    brand_kwargs: dict[str, Any] = {
        "agent_type": agent_type,
        "agent_id": agent_id,
        "brand_id": brand_id,
        "allow_private_destinations": allow_private_destinations,
    }
    # Only forward _client_factory when caller passed one — keeps the
    # test seam from squashing the patched-init default with None.
    if _brand_jwks_client_factory is not None:
        brand_kwargs["_client_factory"] = _brand_jwks_client_factory
    resolver = BrandJsonJwksResolver(brand_json_url, **brand_kwargs)
    try:
        await resolver.force_refresh()
    except BrandJsonResolverError as exc:
        trace.append(
            TraceEntry(
                hop="brand_json",
                url=brand_json_url,
                status="error",
                latency_ms=(time.monotonic() - bj_start) * 1000.0,
                error_code=exc.code,
                error_message=str(exc),
            )
        )
        raise AgentResolverError(
            "brand_json_resolution_failed",
            f"brand.json resolution failed: {exc.code}: {exc}",
        ) from exc

    jwks_uri = resolver.jwks_uri
    resolved_agent_url = resolver.agent_url
    if jwks_uri is None or resolved_agent_url is None:
        # Defensive — force_refresh must populate the snapshot or raise.
        raise AgentResolverError(
            "brand_json_resolution_failed",
            "brand.json refresh completed without populating jwks_uri / agent_url",
        )
    trace.append(
        TraceEntry(
            hop="brand_json",
            url=brand_json_url,
            status="ok",
            latency_ms=(time.monotonic() - bj_start) * 1000.0,
        )
    )

    # --- Hop 3: JWKS ---
    jwks_start = time.monotonic()
    try:
        jwks = await async_default_jwks_fetcher(jwks_uri, allow_private=allow_private_destinations)
    except SSRFValidationError as exc:
        trace.append(
            TraceEntry(
                hop="jwks",
                url=jwks_uri,
                status="error",
                latency_ms=(time.monotonic() - jwks_start) * 1000.0,
                error_code="ssrf",
                error_message=str(exc),
            )
        )
        raise AgentResolverError("jwks_fetch_failed", f"JWKS URL failed SSRF check: {exc}") from exc
    except (httpx.HTTPError, ValueError, OSError) as exc:
        trace.append(
            TraceEntry(
                hop="jwks",
                url=jwks_uri,
                status="error",
                latency_ms=(time.monotonic() - jwks_start) * 1000.0,
                error_code="fetch_failed",
                error_message=str(exc),
            )
        )
        raise AgentResolverError("jwks_fetch_failed", f"JWKS fetch failed: {exc}") from exc
    trace.append(
        TraceEntry(
            hop="jwks",
            url=jwks_uri,
            status="ok",
            latency_ms=(time.monotonic() - jwks_start) * 1000.0,
        )
    )

    return AgentResolution(
        agent_url=agent_url,
        brand_json_url=brand_json_url,
        agent_entry=_make_agent_entry(resolved_agent_url, jwks_uri, agent_type, agent_id),
        jwks_uri=jwks_uri,
        jwks=jwks,
        fetched_at=fetched_at,
        key_origins=key_origins,
        trace=trace,
    )


def resolve_agent(
    agent_url: str,
    *,
    agent_type: BrandAgentType,
    agent_id: str | None = None,
    brand_id: str | None = None,
    allow_private_destinations: bool = False,
) -> AgentResolution:
    """Sync wrapper over :func:`async_resolve_agent` for CLI / scripts.

    Library code on an event loop should call
    :func:`async_resolve_agent` directly — wrapping it in
    :func:`asyncio.run` would deadlock the loop.
    """
    return asyncio.run(
        async_resolve_agent(
            agent_url,
            agent_type=agent_type,
            agent_id=agent_id,
            brand_id=brand_id,
            allow_private_destinations=allow_private_destinations,
        )
    )


# ---- verify factory ----


_REPLAY_STORE_UNSET = object()


class _NamespacedReplayStore:
    """Partition one bounded replay store by canonical counterparty origin."""

    def __init__(self, backend: ReplayStore, namespace: str) -> None:
        self._backend = backend
        self._namespace = namespace

    def _keyid(self, keyid: str) -> str:
        return f"{len(self._namespace)}:{self._namespace}{keyid}"

    def seen(self, keyid: str, nonce: str) -> bool:
        return self._backend.seen(self._keyid(keyid), nonce)

    def remember(self, keyid: str, nonce: str, ttl_seconds: float) -> bool | None:
        return self._backend.remember(self._keyid(keyid), nonce, ttl_seconds)

    def at_capacity(self, keyid: str) -> bool:
        return self._backend.at_capacity(self._keyid(keyid))

    def supports_atomic_claim(self) -> bool:
        return supports_atomic_claim(self._backend)

    def claim(self, keyid: str, nonce: str, ttl_seconds: float) -> ReplayClaimResult:
        if not supports_atomic_claim(self._backend):
            raise AttributeError("underlying replay store does not implement claim")
        return self._backend.claim(self._keyid(keyid), nonce, ttl_seconds)


_DEFAULT_REPLAY_STORE = InMemoryReplayStore()


def _default_replay_store_for_origin(origin: str) -> ReplayStore:
    """Return an origin partition backed by one process-wide bounded store."""
    return _NamespacedReplayStore(_DEFAULT_REPLAY_STORE, origin)


def _canonical_agent_origin(url: str) -> str:
    parsed = httpx.URL(url)
    host = canonicalize_host(parsed.host)
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.scheme}://{host}:{port}"


class _BrandJsonStaticJwksResolver(StaticJwksResolver):
    """A :class:`StaticJwksResolver` carrying the ``"brand_json"``
    source discriminant AND the resolved ``jwks_uri``.

    Conforms to :class:`adcp.signing.BrandSourcedJwksResolver` — the
    verifier's ``_maybe_check_key_origin`` engages the spec's
    consistency check on every signed request routed through
    :func:`verify_from_agent_url`. Adopters wiring custom resolvers
    declare the same conformance by setting ``jwks_source = "brand_json"``
    (class attribute) and exposing ``jwks_uri`` (instance attribute);
    they MAY also import :class:`BrandSourcedJwksResolver` to type-check
    the contract at static analysis time.

    The brand.json walk in :func:`async_resolve_agent` resolved this
    JWKS — that's exactly the source the spec's key-origin consistency
    check (ADCP #3690 step 7) defends. The verifier reads
    ``getattr(resolver, "jwks_uri", None)`` to look up the resolved
    host for the comparison. :class:`StaticJwksResolver` does not
    carry a ``jwks_uri`` (it's a static keyset), so this subclass
    stores the brand.json-resolved URI on the instance. Without it
    the check would mismatch every legitimate signer with
    ``actual_origin=""``.

    Defined inside the module rather than as a public type because the
    helper composition is internal to the buyer-side verify factory.
    """

    jwks_source: ClassVar[Literal["brand_json"]] = "brand_json"

    def __init__(self, jwks: dict[str, Any], *, jwks_uri: str) -> None:
        super().__init__(jwks)
        self.jwks_uri = jwks_uri


async def verify_from_agent_url(
    request: Any,
    agent_url: str,
    *,
    agent_type: BrandAgentType,
    operation: str,
    agent_id: str | None = None,
    brand_id: str | None = None,
    capability: Any = None,
    now: float | None = None,
    replay_store: Any = _REPLAY_STORE_UNSET,
    revocation_checker: Any = None,
    revocation_list: Any = None,
    allow_private_destinations: bool = False,
    signing_purpose: str = "request_signing",
    posture: str | None = None,
) -> Any:
    """Single-call factory: resolve ``agent_url`` and verify the
    request signature against the resolved JWKS.

    Composes :func:`async_resolve_agent` (3-hop walk to the JWK set)
    with the existing :func:`verify_starlette_request` verifier. Use
    this when the verifier is handed an agent URL and needs to
    bootstrap to that agent's signing keys without out-of-band
    knowledge of the operator domain — the common path for buyer-side
    request verification on AdCP 3.x sellers.

    ``request`` is a Starlette / FastAPI ``Request``-shaped object
    (matches :func:`verify_starlette_request`'s duck type — needs
    ``method``, ``url``, ``headers``, and ``await body()``). Body is
    consumed once; downstream handlers calling ``await request.body()``
    again get the same cached bytes (Starlette behavior).

    Resolver-side failures (capabilities unreachable, brand.json
    walk failed, JWKS fetch failed) are mapped to
    :class:`SignatureVerificationError` with
    ``REQUEST_SIGNATURE_JWKS_UNAVAILABLE`` so callers handle
    resolution and verification failures through one ``except`` clause.
    The exception to that rule is ``invalid_agent_url`` — that's a
    trust-boundary rejection, so it maps to
    ``REQUEST_SIGNATURE_JWKS_UNTRUSTED``.

    Adopters needing finer-grain dispatch on the resolver-side cause
    can read ``exc.__cause__`` and check the
    :class:`AgentResolverError.code` directly — both exception
    hierarchies are preserved.

    When ``replay_store`` is omitted, replay protection uses a bounded,
    process-wide in-memory store namespaced by resolved agent URL so separate
    counterparties may safely reuse a ``kid``. Pass ``None`` explicitly only
    when replay protection is intentionally disabled, or provide a shared
    store for multi-process deployments.

    Returns
    -------
    VerifiedSigner
        On success — carries the verified ``key_id`` and metadata.

    Raises
    ------
    SignatureVerificationError
        Either from the resolver (mapped per above) or from the
        verifier (passes through with the spec ``code`` already set).
    """
    import time as _time

    from adcp.signing.errors import (
        REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
        REQUEST_SIGNATURE_JWKS_UNTRUSTED,
        SignatureVerificationError,
    )
    from adcp.signing.middleware import verify_starlette_request
    from adcp.signing.verifier import VerifierCapability, VerifyOptions

    try:
        resolution = await async_resolve_agent(
            agent_url,
            agent_type=agent_type,
            agent_id=agent_id,
            brand_id=brand_id,
            allow_private_destinations=allow_private_destinations,
        )
    except AgentResolverError as exc:
        # invalid_agent_url is a trust-boundary rejection (URL wouldn't
        # canonicalize / scheme / SSRF-banned host). Everything else
        # (capabilities_unreachable, brand_json_resolution_failed,
        # jwks_fetch_failed) is a discovery-time failure even when
        # underlying cause was SSRF — verifiers map those to
        # JWKS_UNAVAILABLE per the spec's "couldn't get keys" reading.
        mapped = (
            REQUEST_SIGNATURE_JWKS_UNTRUSTED
            if exc.code == "invalid_agent_url"
            else REQUEST_SIGNATURE_JWKS_UNAVAILABLE
        )
        raise SignatureVerificationError(
            mapped,
            step="resolve",
            message=f"agent-url resolution failed ({exc.code}): {exc.message}",
        ) from exc

    # Mark the resolver with ``jwks_source = "brand_json"`` so the
    # verifier's ``_maybe_check_key_origin`` step engages the spec's
    # key-origin consistency check (the JWKS WAS sourced via the brand.json
    # walk in ``async_resolve_agent``; the check applies). Without this
    # marker the verifier would treat a bare ``StaticJwksResolver`` as a
    # publisher-pin-equivalent and skip the check — defeating the
    # production helper's defense against the shared-tenancy spoof.
    if replay_store is _REPLAY_STORE_UNSET:
        resolved_agent_url = str(resolution.agent_entry.get("url") or resolution.agent_url)
        replay_store = _default_replay_store_for_origin(_canonical_agent_origin(resolved_agent_url))
    options = VerifyOptions(
        now=now if now is not None else _time.time(),
        capability=capability if capability is not None else VerifierCapability(supported=True),
        operation=operation,
        jwks_resolver=_BrandJsonStaticJwksResolver(resolution.jwks, jwks_uri=resolution.jwks_uri),
        revocation_checker=revocation_checker,
        revocation_list=revocation_list,
        agent_url=resolution.agent_entry.get("url"),
        expected_key_origins=resolution.key_origins or {},
        signing_purpose=signing_purpose,
        posture=posture,
        replay_store=replay_store,
    )
    return await verify_starlette_request(request, options=options)


# ---- helpers ----


def _make_agent_entry(
    agent_url: str,
    jwks_uri: str,
    agent_type: BrandAgentType,
    agent_id: str | None,
) -> dict[str, Any]:
    """Synthesize the matched ``agents[]`` entry from the resolved
    snapshot. The brand.json walk already discarded the surrounding
    document; this is the projection consumers want.
    """
    entry: dict[str, Any] = {
        "type": agent_type,
        "url": agent_url,
        "jwks_uri": jwks_uri,
    }
    if agent_id is not None:
        entry["id"] = agent_id
    return entry


__all__ = [
    "AgentResolution",
    "AgentResolverError",
    "AgentResolverErrorCode",
    "TraceEntry",
    "async_resolve_agent",
    "resolve_agent",
    "verify_from_agent_url",
]
