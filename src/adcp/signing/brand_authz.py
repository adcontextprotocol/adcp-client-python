"""Per-brand authorization check for buyer agents.

Tier 3 of the v3 identity stack. Composes with — but is separate from —
the JWKS resolution surface (:mod:`adcp.signing.brand_jwks`). Where the
JWKS resolver answers "what public key signs for this counterparty?",
this resolver answers "is this agent authorized to act *for this brand*?"

Per ADCP request-signing spec (#3690), the binding is:

1. **Listed in ``agents[]``.** The brand's ``/.well-known/brand.json``
   ``agents[]`` array enumerates the agents the brand has authorized.
   The verified ``agent_url`` MUST appear as ``url`` on one of these
   entries (canonical-URL match). When the caller supplies an
   ``agent_type`` filter, the entry's ``type`` MUST match too.

2. **Host-bound.** EITHER the agent host shares an eTLD+1 with the
   brand domain (the agent lives under the brand's own registrable
   domain — the common case for first-party operations), OR the agent
   host is listed in ``house.authorized_operators[]`` (multi-tenant
   SaaS operators — WPP / GroupM / etc. acting on behalf of multiple
   brand clients).

When the caller passes ``brand_id``, the operator-delegation check is
scoped: the operator's ``brands[]`` must contain ``brand_id`` (or
``"*"``). Without ``brand_id``, only operators authorized for ``"*"``
satisfy the delegation check — the resolver fails closed on ambiguous
scope.

**Shared fetcher.** Both this resolver and
:class:`BrandJsonJwksResolver` walk brand.json. Construct them with a
shared ``_BrandJsonFetcher`` (one fetch, two consumers) via
:func:`build_brand_json_resolvers` to avoid double-fetching.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from adcp.signing.brand_jwks import (
    DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_MAX_BRAND_JSON_BYTES,
    DEFAULT_MAX_REDIRECTS,
    DEFAULT_MIN_COOLDOWN_SECONDS,
    BrandAgentType,
    BrandJsonJwksResolver,
    BrandJsonResolverError,
    _BrandJsonFetcher,
    _BrandJsonSnapshot,
    _canonicalize_url,
    _ClientFactory,
)
from adcp.signing.etld import host_from, registrable_domain, same_registrable_domain

#: Reason a brand-authorization check resolved the way it did. Used
#: for verifier error attribution and adopter logging. The framework
#: maps these to ``request_signature_*`` error codes when refusing a
#: request (stage 4 wires that up).
BrandAuthorizationReason = Literal[
    "etld1_match",
    "operator_delegation",
    "agent_not_listed",
    "agent_type_mismatch",
    "binding_failed",
    "brand_json_unavailable",
    "brand_domain_invalid",
]


@dataclass(frozen=True)
class BrandAuthorizationResult:
    """Structured outcome of a brand-authorization check.

    ``authorized`` is the bottom-line gate; ``reason`` carries the why
    so the verifier can emit a precise error code and adopters can log
    the decision. ``matched_*`` fields are populated on success for
    audit attribution and on certain failure paths (e.g. agent matched
    but binding failed → ``matched_agent_url`` is set).
    """

    authorized: bool
    reason: BrandAuthorizationReason
    matched_agent_url: str | None = None
    matched_agent_type: BrandAgentType | None = None
    matched_operator_domain: str | None = None
    #: When ``reason == "brand_json_unavailable"``, this carries the
    #: underlying fetch error so the framework can decide between
    #: 5xx-retryable and 4xx-misconfiguration.
    fetch_error: BrandJsonResolverError | None = None


@runtime_checkable
class BrandAuthorizationResolver(Protocol):
    """Verify a buyer agent is authorized to act for a brand.

    The framework calls :meth:`is_authorized` per request after
    cryptographic identity verification (Tier 1) and registry
    resolution (Tier 2). Adopters implement this Protocol to control
    the authorization decision; the default
    :class:`BrandJsonAuthorizationResolver` reads ``brand.json``.

    Implementations MUST be safe under concurrent calls.
    """

    async def is_authorized(
        self,
        *,
        agent_url: str,
        brand_domain: str,
        agent_type: BrandAgentType | None = None,
        brand_id: str | None = None,
    ) -> bool:
        """Return True iff ``agent_url`` is authorized to act for
        ``brand_domain`` (optionally narrowed to ``brand_id`` and/or
        ``agent_type``)."""
        ...


class BrandJsonAuthorizationResolver:
    """Reference :class:`BrandAuthorizationResolver` reading brand.json.

    Walks the brand's ``/.well-known/brand.json`` (with redirect
    following, body cap, and SSRF-pinned transport handled by the
    shared :class:`_BrandJsonFetcher`), then applies the two-step
    binding check:

    1. The verified ``agent_url`` must be listed in ``agents[]`` —
       optionally narrowed by ``agent_type`` and/or ``brand_id``.
    2. The agent host must be eTLD+1-bound to the brand domain, OR
       listed in ``house.authorized_operators[]`` with a ``brands[]``
       scope that covers the request.

    On a cold cache, fetches synchronously inside the first
    :meth:`is_authorized` / :meth:`check` call. The fetcher caches the
    parsed body and honors ``Cache-Control`` / ``ETag`` exactly as the
    JWKS resolver does.
    """

    def __init__(
        self,
        brand_json_url: str,
        *,
        min_cooldown_seconds: float = DEFAULT_MIN_COOLDOWN_SECONDS,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_body_bytes: int = DEFAULT_MAX_BRAND_JSON_BYTES,
        allow_private_destinations: bool = False,
        timeout_seconds: float = DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
        _client_factory: _ClientFactory | None = None,
        _fetcher: _BrandJsonFetcher | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._allow_private = allow_private_destinations
        self._fetcher = _fetcher or _BrandJsonFetcher(
            brand_json_url,
            min_cooldown_seconds=min_cooldown_seconds,
            max_age_seconds=max_age_seconds,
            max_redirects=max_redirects,
            max_body_bytes=max_body_bytes,
            allow_private_destinations=allow_private_destinations,
            timeout_seconds=timeout_seconds,
            clock=self._clock,
            _client_factory=_client_factory,
        )

    @property
    def brand_json_url(self) -> str:
        return self._fetcher.brand_json_url

    async def is_authorized(
        self,
        *,
        agent_url: str,
        brand_domain: str,
        agent_type: BrandAgentType | None = None,
        brand_id: str | None = None,
    ) -> bool:
        result = await self.check(
            agent_url=agent_url,
            brand_domain=brand_domain,
            agent_type=agent_type,
            brand_id=brand_id,
        )
        return result.authorized

    async def check(
        self,
        *,
        agent_url: str,
        brand_domain: str,
        agent_type: BrandAgentType | None = None,
        brand_id: str | None = None,
    ) -> BrandAuthorizationResult:
        """Run the full binding check and return a structured result.

        Use :meth:`is_authorized` for the boolean gate; use this
        method when you need the reason for logging or to emit a
        precise error code.
        """
        # Validate brand_domain up front so we never silently let a
        # blank / IP-literal brand domain match anything via shared
        # binding semantics downstream.
        try:
            brand_host = host_from(brand_domain)
        except ValueError:
            return BrandAuthorizationResult(False, reason="brand_domain_invalid")
        if registrable_domain(brand_host) is None:
            return BrandAuthorizationResult(False, reason="brand_domain_invalid")

        snap = await self._snapshot()
        if isinstance(snap, BrandJsonResolverError):
            return BrandAuthorizationResult(
                False,
                reason="brand_json_unavailable",
                fetch_error=snap,
            )

        matched = _find_listed_agent(
            snap.data,
            agent_url=agent_url,
            agent_type=agent_type,
            brand_id=brand_id,
        )

        if matched is None:
            # Distinguish "not present at all" from "present but wrong type":
            # the latter is a stronger signal of misconfiguration.
            if agent_type is not None and _has_listed_agent_at(
                snap.data, agent_url=agent_url, brand_id=brand_id
            ):
                return BrandAuthorizationResult(False, reason="agent_type_mismatch")
            return BrandAuthorizationResult(False, reason="agent_not_listed")

        # Step 2a: eTLD+1 binding.
        if same_registrable_domain(agent_url, brand_host):
            return BrandAuthorizationResult(
                True,
                reason="etld1_match",
                matched_agent_url=matched.url,
                matched_agent_type=matched.type,
            )

        # Step 2b: authorized_operators[] delegation.
        operator_domain = _find_authorized_operator(
            snap.data,
            agent_url=agent_url,
            brand_id=brand_id,
        )
        if operator_domain is not None:
            return BrandAuthorizationResult(
                True,
                reason="operator_delegation",
                matched_agent_url=matched.url,
                matched_agent_type=matched.type,
                matched_operator_domain=operator_domain,
            )

        return BrandAuthorizationResult(
            False,
            reason="binding_failed",
            matched_agent_url=matched.url,
            matched_agent_type=matched.type,
        )

    async def _snapshot(self) -> _BrandJsonSnapshot | BrandJsonResolverError:
        """Return a fresh-enough snapshot or the fetch error."""
        snap = self._fetcher.snapshot
        if snap is None:
            try:
                return await self._fetcher.refresh()
            except BrandJsonResolverError as exc:
                return exc

        if self._fetcher.is_stale(snap) and self._fetcher.can_refresh(snap):
            try:
                return await self._fetcher.refresh()
            except BrandJsonResolverError:
                # Stale-on-error: serve the prior snapshot. Matches
                # the JWKS resolver's posture exactly.
                return snap
        return snap


# --- builder for sharing the fetcher with the JWKS resolver ---


def build_brand_json_resolvers(
    brand_json_url: str,
    *,
    agent_type: BrandAgentType,
    agent_id: str | None = None,
    brand_id: str | None = None,
    min_cooldown_seconds: float = DEFAULT_MIN_COOLDOWN_SECONDS,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_body_bytes: int = DEFAULT_MAX_BRAND_JSON_BYTES,
    allow_private_destinations: bool = False,
    timeout_seconds: float = DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
    clock: Callable[[], float] | None = None,
) -> tuple[BrandJsonJwksResolver, BrandJsonAuthorizationResolver]:
    """Construct a JWKS resolver and an authorization resolver that
    share one underlying brand.json snapshot.

    Both resolvers walk brand.json; constructing them via this builder
    avoids paying two fetches per request and keeps cache-control /
    ETag state single-source.

    Returns ``(jwks_resolver, authz_resolver)``. Hand the JWKS resolver
    to the request-signature verifier; hand the authz resolver to the
    framework's ``serve(brand_authz_resolver=...)``.
    """
    fetcher = _BrandJsonFetcher(
        brand_json_url,
        min_cooldown_seconds=min_cooldown_seconds,
        max_age_seconds=max_age_seconds,
        max_redirects=max_redirects,
        max_body_bytes=max_body_bytes,
        allow_private_destinations=allow_private_destinations,
        timeout_seconds=timeout_seconds,
        clock=clock,
    )
    jwks = BrandJsonJwksResolver(
        brand_json_url,
        agent_type=agent_type,
        agent_id=agent_id,
        brand_id=brand_id,
        min_cooldown_seconds=min_cooldown_seconds,
        max_age_seconds=max_age_seconds,
        max_redirects=max_redirects,
        max_body_bytes=max_body_bytes,
        allow_private_destinations=allow_private_destinations,
        timeout_seconds=timeout_seconds,
        clock=clock,
        _fetcher=fetcher,
    )
    authz = BrandJsonAuthorizationResolver(
        brand_json_url,
        min_cooldown_seconds=min_cooldown_seconds,
        max_age_seconds=max_age_seconds,
        max_redirects=max_redirects,
        max_body_bytes=max_body_bytes,
        allow_private_destinations=allow_private_destinations,
        timeout_seconds=timeout_seconds,
        clock=clock,
        _fetcher=fetcher,
    )
    return jwks, authz


# --- internal helpers ---


@dataclass(frozen=True)
class _ListedAgent:
    """The brand-listed agent we matched against the request."""

    url: str
    type: BrandAgentType | None


def _find_listed_agent(
    data: dict[str, Any],
    *,
    agent_url: str,
    agent_type: BrandAgentType | None,
    brand_id: str | None,
) -> _ListedAgent | None:
    """Search ``agents[]`` arrays for an entry matching ``agent_url``.

    Walks (in order) top-level ``agents``, ``house.agents``, and per-
    brand ``brands[].agents`` — bounded by ``brand_id`` when provided.
    Returns the first canonical-URL match (with ``type`` filter when
    set); ``None`` if no entry matches.
    """
    target = _canonicalize_agent_url(agent_url)

    for entry in _walk_agents(data, brand_id=brand_id):
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        if _canonicalize_agent_url(url) != target:
            continue
        if agent_type is not None and entry.get("type") != agent_type:
            continue
        listed_type = entry.get("type")
        return _ListedAgent(
            url=url,
            type=listed_type if isinstance(listed_type, str) else None,  # type: ignore[arg-type]
        )
    return None


def _has_listed_agent_at(
    data: dict[str, Any],
    *,
    agent_url: str,
    brand_id: str | None,
) -> bool:
    """Return True if ``agent_url`` appears in ``agents[]`` regardless
    of ``type`` — used to distinguish ``agent_type_mismatch`` from
    ``agent_not_listed`` for caller diagnostics."""
    target = _canonicalize_agent_url(agent_url)
    for entry in _walk_agents(data, brand_id=brand_id):
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if isinstance(url, str) and _canonicalize_agent_url(url) == target:
            return True
    return False


def _walk_agents(data: dict[str, Any], *, brand_id: str | None) -> list[Any]:
    """Collect all ``agents[]`` entries we should consult.

    Layers consulted:

    * Top-level ``agents`` (Brand Agent doc shape).
    * ``house.agents`` (House Portfolio doc shape — agents serving the
      whole portfolio).
    * ``brands[].agents`` — when ``brand_id`` is set, only that one
      brand's entry; otherwise every brand's entries (broadest match,
      since the caller didn't narrow).
    """
    out: list[Any] = []

    top = data.get("agents")
    if isinstance(top, list):
        out.extend(top)

    house = data.get("house")
    if isinstance(house, dict):
        h_agents = house.get("agents")
        if isinstance(h_agents, list):
            out.extend(h_agents)

    brands = data.get("brands")
    if isinstance(brands, list):
        for brand in brands:
            if not isinstance(brand, dict):
                continue
            if brand_id is not None and brand.get("id") != brand_id:
                continue
            b_agents = brand.get("agents")
            if isinstance(b_agents, list):
                out.extend(b_agents)

    return out


def _find_authorized_operator(
    data: dict[str, Any],
    *,
    agent_url: str,
    brand_id: str | None,
) -> str | None:
    """Find an ``authorized_operators[]`` entry whose ``domain`` matches
    the agent host's eTLD+1 AND whose ``brands[]`` scope covers
    ``brand_id`` (or ``"*"`` when ``brand_id`` is None — fail-closed
    on unscoped requests).

    Returns the operator's ``domain`` string on match; ``None`` on no
    match. The match is on eTLD+1 equality (agent host registrable
    domain == declared operator domain registrable domain) so an
    operator declared as ``wpp.com`` covers ``api.wpp.com``,
    ``us-east.wpp.com``, etc. — same posture as eTLD+1 step 2a.
    """
    house = data.get("house")
    if not isinstance(house, dict):
        return None
    operators = house.get("authorized_operators")
    if not isinstance(operators, list):
        return None

    agent_etld1 = registrable_domain(agent_url)
    if agent_etld1 is None:
        # Agent host is not eTLD+1-bindable — IP literal, single label,
        # etc. Fail closed (no operator delegation can rescue it).
        return None

    for op in operators:
        if not isinstance(op, dict):
            continue
        domain = op.get("domain")
        if not isinstance(domain, str):
            continue
        op_etld1 = registrable_domain(domain)
        if op_etld1 is None or op_etld1 != agent_etld1:
            continue

        brands_scope = op.get("brands")
        if not isinstance(brands_scope, list):
            # Schema requires brands[] minItems=1; absence is a
            # malformed doc, treat as unscoped → fail closed.
            continue

        if brand_id is None:
            # No brand context from the caller → only "*" wildcard
            # operators satisfy the check. Operators scoped to specific
            # brands cannot be honored without knowing which one we're
            # acting for.
            if any(b == "*" for b in brands_scope if isinstance(b, str)):
                return domain
            continue

        for b in brands_scope:
            if not isinstance(b, str):
                continue
            if b == "*" or b == brand_id:
                return domain

    return None


def _canonicalize_agent_url(url: str) -> str:
    """Canonicalize an agent URL for byte-equal comparison.

    Reuses the brand.json URL canonicalizer (scheme/host lowercased,
    default port stripped, fragment stripped, userinfo rejected).
    Falls back to a basic ``urlsplit``-based lowercase on inputs the
    canonicalizer rejects (the comparison is best-effort here — a URL
    we cannot canonicalize will never match a properly-canonicalized
    target, which is the correct failure direction).
    """
    try:
        return _canonicalize_url(url, allow_private=True)
    except BrandJsonResolverError:
        # Fall back to a permissive normalization so the comparison can
        # still proceed (the caller's verified agent_url has already
        # been validated upstream; the brand.json's listed url is the
        # one we can't structurally trust).
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return url.lower()
        netloc = parts.netloc.lower()
        path = parts.path or "/"
        return f"{parts.scheme.lower()}://{netloc}{path}"


__all__ = [
    "BrandAuthorizationReason",
    "BrandAuthorizationResolver",
    "BrandAuthorizationResult",
    "BrandJsonAuthorizationResolver",
    "build_brand_json_resolvers",
]
