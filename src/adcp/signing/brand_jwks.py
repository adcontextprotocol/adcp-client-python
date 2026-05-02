"""Receiver-side: resolve a sender's JWKS via brand.json.

Port of ``src/lib/signing/brand-jwks.ts`` from the JS SDK
(``adcontextprotocol/adcp-client``). Same selector model, same
redirect-following, same security posture (origin-bound well-known
fallback). Composes with the existing
:class:`adcp.signing.AsyncCachingJwksResolver` for the inner JWKS
fetch — does NOT reinvent JWK caching.

**Why this exists.** The seller's verifier never trusts an
``agent_url/.well-known/jwks.json`` directly — that would let any agent
self-attest its own keys. Per ADCP, keys root through the brand: the
brand's ``/.well-known/brand.json`` lists each authorized agent and
its ``jwks_uri``, operator-attested. This resolver walks brand.json,
picks the right agent entry, and delegates JWK fetch to the inner
JWKS resolver pinned to that ``jwks_uri``.

**What it doesn't do (yet).** ADCP #3690's eTLD+1 binding and
``authorized_operators[]`` delegation are not enforced here — they're
verifier-side concerns added when #3690 lands. This resolver only does
the brand-json walk + JWKS fetch chain; the verifier composes the
authorization check around it.

Hand the resulting instance to ``verify_request_signature`` (or
``verify_starlette_request``) as the ``jwks`` dependency. The
receiver never has to know where the sender hosts their keys —
brand.json is the single source of truth.

Caching is stacked: brand.json honors its own ``Cache-Control`` /
``ETag`` (bounded by ``max_age_seconds``); unknown-kid refreshes
cascade — first to the inner JWKS endpoint, then (if the JWKS still
doesn't have the kid and the brand.json cooldown has elapsed) to
brand.json itself, in case the sender rotated ``jwks_uri``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from adcp.signing.jwks import (
    AsyncCachingJwksResolver,
    AsyncJwksFetcher,
    async_default_jwks_fetcher,
)

#: Functional roles an agent may declare in brand.json's ``agents[]``
#: array. Mirrors ``schemas/cache/enums/brand-agent-type.json`` and the
#: JS ``BrandAgentType`` union.
BrandAgentType = Literal[
    "brand",
    "rights",
    "measurement",
    "governance",
    "creative",
    "sales",
    "buying",
    "signals",
]

#: Error codes raised by :class:`BrandJsonResolverError`. Verifier
#: callers fold these into ``REQUEST_SIGNATURE_JWKS_*`` codes (or
#: treat ambiguous / schema errors as config bugs) without parsing
#: error message strings.
BrandJsonResolverErrorCode = Literal[
    "invalid_url",
    "invalid_house",
    "redirect_loop",
    "redirect_depth_exceeded",
    "fetch_failed",
    "invalid_body",
    "schema_invalid",
    "agent_not_found",
    "agent_ambiguous",
    "jwks_origin_mismatch",
]

DEFAULT_MIN_COOLDOWN_SECONDS = 30.0
DEFAULT_MAX_AGE_SECONDS = 3600.0
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_BRAND_JSON_TIMEOUT_SECONDS = 10.0

# Bare hostname pattern for the ``house`` redirect variant. Matches
# the regex in brand.json's schema (``schemas/cache/brand.json``)
# exactly so cross-language conformance behavior stays identical.
_BARE_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class BrandJsonResolverError(Exception):
    """Typed error surfaced by the resolver pipeline."""

    def __init__(self, code: BrandJsonResolverErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: BrandJsonResolverErrorCode = code


@dataclass
class _BrandSnapshot:
    """One cached brand.json snapshot — the agent we picked + the
    ``jwks_uri`` we resolved + cache metadata."""

    jwks_uri: str
    agent_url: str
    fetched_at: float
    expires_at: float
    etag: str | None = None


@dataclass(frozen=True)
class _SelectedAgent:
    """The agent we picked from a brand.json walk."""

    url: str
    jwks_uri: str


@dataclass
class _FetchedBrandJson:
    """One brand.json fetch outcome — an OK body or a 304 ETag bump."""

    status: Literal["ok", "not_modified"]
    final_url: str
    data: dict[str, Any] | None
    etag: str | None = None
    cache_control: str | None = None


class BrandJsonJwksResolver:
    """JWKS resolver backed by a sender's ``brand.json``.

    Implements :class:`adcp.signing.AsyncJwksResolver` (callable as
    ``await resolver(kid)``). Construct one per counterparty (or per
    ``brand.json`` URL + agent selector tuple) and hand it to the
    request / webhook verifier as the ``jwks`` dependency.

    On a cold cache, fetches brand.json first; on an expired
    snapshot, refreshes respecting the cooldown. Unknown kids
    cascade: first ask the inner :class:`AsyncCachingJwksResolver`
    (which will refetch its own URL if cooldown has elapsed); if
    still unknown, refresh brand.json in case ``jwks_uri`` rotated.
    """

    def __init__(
        self,
        brand_json_url: str,
        *,
        agent_type: BrandAgentType,
        agent_id: str | None = None,
        brand_id: str | None = None,
        min_cooldown_seconds: float = DEFAULT_MIN_COOLDOWN_SECONDS,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        allow_private_destinations: bool = False,
        jwks_fetcher: AsyncJwksFetcher | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: float = DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
    ) -> None:
        self._url = brand_json_url
        self._agent_type = agent_type
        self._agent_id = agent_id
        self._brand_id = brand_id
        self._min_cooldown = min_cooldown_seconds
        self._max_age = max_age_seconds
        self._max_redirects = max_redirects
        self._allow_private = allow_private_destinations
        self._jwks_fetcher = jwks_fetcher or async_default_jwks_fetcher
        self._clock = clock or time.time
        self._timeout = timeout_seconds

        self._snapshot: _BrandSnapshot | None = None
        self._inner: AsyncCachingJwksResolver | None = None
        self._refresh_lock: asyncio.Lock = asyncio.Lock()

    # AsyncJwksResolver Protocol — callable as ``await resolver(kid)``.
    async def __call__(self, kid: str) -> dict[str, Any] | None:
        return await self.resolve(kid)

    async def resolve(self, kid: str) -> dict[str, Any] | None:
        """Resolve a JWK by ``kid``.

        Cold cache → fetch brand.json + inner JWKS. Expired snapshot
        past cooldown → refresh, keep stale on transient failure.
        Unknown kid → cascade: inner resolver refresh first, then
        brand.json refresh.
        """
        if self._snapshot is None or self._inner is None:
            await self._refresh()
        elif (
            self._clock() > self._snapshot.expires_at
            and self._clock() - self._snapshot.fetched_at >= self._min_cooldown
        ):
            try:
                await self._refresh()
            except BrandJsonResolverError:
                # Keep stale on transient failure — same posture as JS.
                pass

        if self._inner is None:
            return None

        hit = await self._inner(kid)
        if hit is not None:
            return hit

        # Cascade: refresh brand.json in case jwks_uri rotated.
        if (
            self._snapshot is not None
            and self._clock() - self._snapshot.fetched_at >= self._min_cooldown
        ):
            try:
                await self._refresh()
            except BrandJsonResolverError:
                return None
            return await self._inner(kid) if self._inner is not None else None
        return None

    @property
    def agent_url(self) -> str | None:
        """The agent URL we resolved ``jwks_uri`` from. Populated
        after the first successful refresh; useful for verifier
        result attribution."""
        return self._snapshot.agent_url if self._snapshot is not None else None

    async def force_refresh(self) -> None:
        """Force refetch of both brand.json and inner JWKS, bypassing
        the cooldown."""
        async with self._refresh_lock:
            self._snapshot = None
            self._inner = None
            await self._do_refresh()

    async def _refresh(self) -> None:
        async with self._refresh_lock:
            await self._do_refresh()

    async def _do_refresh(self) -> None:
        fetched = await _fetch_brand_json(
            start_url=self._url,
            current_etag=self._snapshot.etag if self._snapshot is not None else None,
            max_redirects=self._max_redirects,
            allow_private=self._allow_private,
            timeout_seconds=self._timeout,
        )

        # 304 on the entry URL: extend the lifetime, keep the inner resolver.
        if fetched.status == "not_modified" and self._snapshot is not None:
            now = self._clock()
            self._snapshot = _BrandSnapshot(
                jwks_uri=self._snapshot.jwks_uri,
                agent_url=self._snapshot.agent_url,
                fetched_at=now,
                expires_at=now + _compute_lifetime(fetched.cache_control, self._max_age),
                etag=fetched.etag or self._snapshot.etag,
            )
            return

        if fetched.data is None:
            # Defensive: status == "ok" should always carry a body.
            raise BrandJsonResolverError("invalid_body", "brand.json response missing body")

        agent = _select_agent(
            fetched.data,
            fetched.final_url,
            agent_type=self._agent_type,
            agent_id=self._agent_id,
            brand_id=self._brand_id,
        )

        if self._inner is None or (
            self._snapshot is not None and self._snapshot.jwks_uri != agent.jwks_uri
        ):
            self._inner = AsyncCachingJwksResolver(
                agent.jwks_uri,
                fetcher=self._jwks_fetcher,
                allow_private=self._allow_private,
                clock=self._clock,
            )

        now = self._clock()
        self._snapshot = _BrandSnapshot(
            jwks_uri=agent.jwks_uri,
            agent_url=agent.url,
            fetched_at=now,
            expires_at=now + _compute_lifetime(fetched.cache_control, self._max_age),
            etag=fetched.etag,
        )


# --- brand.json fetching ---


async def _fetch_brand_json(
    *,
    start_url: str,
    current_etag: str | None,
    max_redirects: int,
    allow_private: bool,
    timeout_seconds: float,
) -> _FetchedBrandJson:
    """Fetch brand.json from ``start_url``, following ``authoritative_location``
    and ``house`` redirect variants up to ``max_redirects`` hops.

    Each hop validates the URL structurally before dispatch — an
    attacker-controlled brand.json that emits
    ``{"house": "evil.com\\@victim.com"}`` or
    ``{"authoritative_location": "http://169.254.169.254/..."}`` is
    rejected at parse time rather than relying on the transport
    layer to catch every pathological shape.

    SSRF protection: ``allow_private=False`` (default) rejects
    private IPs / link-local / cloud metadata via the underlying
    httpx client. Production deployments leave it False.
    """
    seen: set[str] = set()
    url = _canonicalize_url(start_url, allow_private=allow_private)

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for hop in range(max_redirects + 1):
            if url in seen:
                raise BrandJsonResolverError("redirect_loop", "brand.json redirect loop detected")
            seen.add(url)

            headers: dict[str, str] = {"accept": "application/json"}
            # Only attach If-None-Match on the entry URL: a 304
            # short-circuits the whole chain, so revalidating a deeper
            # hop with a stale ETag would lie about the redirect target.
            if hop == 0 and current_etag is not None:
                headers["if-none-match"] = current_etag

            try:
                response = await client.get(url, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                raise BrandJsonResolverError(
                    "fetch_failed", f"brand.json fetch failed: {exc}"
                ) from exc

            if hop == 0 and response.status_code == 304:
                return _FetchedBrandJson(
                    status="not_modified",
                    final_url=url,
                    data=None,
                    etag=response.headers.get("etag"),
                    cache_control=response.headers.get("cache-control"),
                )
            if response.status_code != 200:
                raise BrandJsonResolverError(
                    "fetch_failed",
                    f"brand.json fetch returned HTTP {response.status_code}",
                )

            try:
                parsed = response.json()
            except (ValueError, httpx.DecodingError) as exc:
                raise BrandJsonResolverError(
                    "invalid_body", "brand.json response is not valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise BrandJsonResolverError("invalid_body", "brand.json response is not an object")

            authoritative = parsed.get("authoritative_location")
            house = parsed.get("house")

            if isinstance(authoritative, str):
                if hop == max_redirects:
                    raise BrandJsonResolverError(
                        "redirect_depth_exceeded",
                        "brand.json redirect depth exceeded",
                    )
                url = _canonicalize_url(authoritative, allow_private=allow_private)
                continue
            if isinstance(house, str):
                # The "house string" redirect variant: a bare domain
                # pointing at the authoritative portfolio. Reject
                # anything that isn't a bare hostname so an attacker
                # can't inject userinfo, paths, or ports via the
                # interpolation.
                if not _BARE_HOSTNAME_RE.match(house):
                    raise BrandJsonResolverError(
                        "invalid_house",
                        'brand.json "house" is not a bare hostname',
                    )
                if hop == max_redirects:
                    raise BrandJsonResolverError(
                        "redirect_depth_exceeded",
                        "brand.json redirect depth exceeded",
                    )
                url = _canonicalize_url(
                    f"https://{house}/.well-known/brand.json",
                    allow_private=allow_private,
                )
                continue

            # Narrow shape validation on the terminal document. Full
            # schema validation is stricter than we need; what we MUST
            # reject is a document whose shape would let an attacker
            # smuggle a non-string url or jwks_uri past the selector.
            _assert_brand_json_shape(parsed)

            return _FetchedBrandJson(
                status="ok",
                final_url=url,
                data=parsed,
                etag=response.headers.get("etag"),
                cache_control=response.headers.get("cache-control"),
            )

    raise BrandJsonResolverError("redirect_depth_exceeded", "brand.json redirect depth exceeded")


def _canonicalize_url(raw: str, *, allow_private: bool) -> str:
    """Structurally validate a URL and return it canonicalized.

    Rejects URLs the transport layer would later refuse anyway, but
    catching them here gives a clearer error code and prevents a
    malformed redirect target from silently bypassing the hop cap
    because its string form differed from a prior seen URL.
    """
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrandJsonResolverError("invalid_url", "brand.json URL is malformed") from exc
    if not parts.scheme or not parts.netloc:
        raise BrandJsonResolverError("invalid_url", "brand.json URL is malformed")
    if parts.username or parts.password:
        raise BrandJsonResolverError("invalid_url", "brand.json URL must not include userinfo")
    if parts.scheme != "https" and not (allow_private and parts.scheme == "http"):
        raise BrandJsonResolverError("invalid_url", "brand.json URL must use https://")
    # Strip fragments — they aren't sent on the wire and must not
    # smuggle loop-detection aliases into ``seen``.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _assert_brand_json_shape(obj: dict[str, Any]) -> None:
    """Walk every ``agents[]`` array we might consult and reject
    entries where ``url`` or ``jwks_uri`` are present but non-string.

    A permissive walk here catches a malformed document that the
    selector would otherwise silently skip (a non-string url gets
    filtered out, so an attacker who declares two agents of a type —
    one well-formed and one with a poisoned shape — couldn't change
    the selector outcome, but schema-invalid payloads are still a
    strong signal of compromise).
    """
    queues: list[Any] = [obj.get("agents")]
    house = obj.get("house")
    if isinstance(house, dict):
        queues.append(house.get("agents"))
        brands = obj.get("brands")
        if isinstance(brands, list):
            for brand in brands:
                if isinstance(brand, dict):
                    queues.append(brand.get("agents"))

    for q in queues:
        if q is None:
            continue
        if not isinstance(q, list):
            raise BrandJsonResolverError("schema_invalid", "brand.json `agents` must be an array")
        for entry in q:
            if isinstance(entry, dict):
                url = entry.get("url")
                jwks_uri = entry.get("jwks_uri")
                if url is not None and not isinstance(url, str):
                    raise BrandJsonResolverError(
                        "schema_invalid",
                        "brand.json agent.url must be a string",
                    )
                if jwks_uri is not None and not isinstance(jwks_uri, str):
                    raise BrandJsonResolverError(
                        "schema_invalid",
                        "brand.json agent.jwks_uri must be a string",
                    )


# --- agent selection ---


def _select_agent(
    data: dict[str, Any],
    final_brand_url: str,
    *,
    agent_type: BrandAgentType,
    agent_id: str | None,
    brand_id: str | None,
) -> _SelectedAgent:
    """Pick the agent matching the selector from a brand.json document.

    Resolution order on a portfolio document:
    ``brands[brand_id].agents[]`` first (when ``brand_id`` set), then
    ``house.agents[]`` as fallback. On a non-portfolio document, walks
    the top-level ``agents[]``.
    """
    house = data.get("house")
    picked: _SelectedAgent | None = None

    if isinstance(house, dict):
        if brand_id is not None:
            brands = data.get("brands")
            if isinstance(brands, list):
                brand = next(
                    (b for b in brands if isinstance(b, dict) and b.get("id") == brand_id),
                    None,
                )
                if brand is not None:
                    picked = _pick_agent(
                        brand.get("agents"),
                        final_brand_url,
                        agent_type=agent_type,
                        agent_id=agent_id,
                    )
        if picked is None:
            picked = _pick_agent(
                house.get("agents"),
                final_brand_url,
                agent_type=agent_type,
                agent_id=agent_id,
            )
    else:
        picked = _pick_agent(
            data.get("agents"),
            final_brand_url,
            agent_type=agent_type,
            agent_id=agent_id,
        )

    if picked is None:
        descriptor = _describe_selector(agent_type, agent_id, brand_id)
        raise BrandJsonResolverError(
            "agent_not_found",
            f"brand.json has no agent matching {descriptor}",
        )
    return picked


def _pick_agent(
    agents: Any,
    final_brand_url: str,
    *,
    agent_type: BrandAgentType,
    agent_id: str | None,
) -> _SelectedAgent | None:
    """Filter ``agents[]`` by the selector and return the matching entry.

    Raises :class:`BrandJsonResolverError` ``agent_ambiguous`` when
    multiple agents of the requested type exist and no ``agent_id``
    was provided.
    """
    if not isinstance(agents, list):
        return None
    matches: list[dict[str, Any]] = []
    for entry in agents:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != agent_type:
            continue
        if agent_id is not None and entry.get("id") != agent_id:
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        matches.append(entry)

    if not matches:
        return None
    if len(matches) > 1 and agent_id is None:
        choices = ", ".join(str(m.get("id", "<no-id>")) for m in matches)
        raise BrandJsonResolverError(
            "agent_ambiguous",
            (
                f"brand.json declares {len(matches)} agents of type "
                f'"{agent_type}"; pass agent_id to disambiguate '
                f"(choices: {choices})"
            ),
        )
    agent = matches[0]
    url = str(agent["url"])
    jwks_uri_raw = agent.get("jwks_uri")
    jwks_uri = (
        str(jwks_uri_raw)
        if isinstance(jwks_uri_raw, str)
        else _default_jwks_uri(url, final_brand_url)
    )
    return _SelectedAgent(url=url, jwks_uri=jwks_uri)


def _default_jwks_uri(agent_url: str, final_brand_url: str) -> str:
    """Spec fallback: when ``agent.jwks_uri`` is absent, default to
    ``<agent_origin>/.well-known/jwks.json``.

    Security: the agent origin MUST match the final brand.json origin.
    Without this check, an attacker-controlled brand.json could set
    ``agent.url: "https://victim-internal.example/"`` and force the
    verifier to treat that origin's JWKS as authoritative — a
    cross-origin trust pivot. Publishers that genuinely host their
    agent on a different origin from their brand.json MUST declare an
    explicit ``jwks_uri``.
    """
    try:
        agent_parts = urlsplit(agent_url)
    except ValueError as exc:
        raise BrandJsonResolverError("invalid_url", "agent.url is not a valid URL") from exc
    if not agent_parts.scheme or not agent_parts.netloc:
        raise BrandJsonResolverError("invalid_url", "agent.url is not a valid URL")
    brand_parts = urlsplit(final_brand_url)
    agent_origin = f"{agent_parts.scheme}://{agent_parts.netloc}"
    brand_origin = f"{brand_parts.scheme}://{brand_parts.netloc}"
    if agent_origin != brand_origin:
        raise BrandJsonResolverError(
            "jwks_origin_mismatch",
            (
                f"agent.url origin ({agent_origin}) does not match "
                f"brand.json origin ({brand_origin}); publisher must "
                "declare an explicit jwks_uri for cross-origin agents"
            ),
        )
    return f"{agent_origin}/.well-known/jwks.json"


def _describe_selector(
    agent_type: BrandAgentType,
    agent_id: str | None,
    brand_id: str | None,
) -> str:
    parts = [f"type={agent_type}"]
    if agent_id is not None:
        parts.append(f"id={agent_id}")
    if brand_id is not None:
        parts.append(f"brand={brand_id}")
    return " ".join(parts)


# --- cache-control parsing ---


_MAX_AGE_RE = re.compile(r"max-age\s*=\s*(\d+)")
_NO_STORE_OR_NO_CACHE_RE = re.compile(r"\b(no-store|no-cache)\b")


def _compute_lifetime(cache_control: str | None, max_age: float) -> float:
    """Compute a snapshot lifetime honoring the counterparty's
    ``Cache-Control`` (bounded by our configured ``max_age``)."""
    if cache_control is None:
        return max_age
    lower = cache_control.lower()
    if _NO_STORE_OR_NO_CACHE_RE.search(lower):
        return 0.0
    match = _MAX_AGE_RE.search(lower)
    if match is not None:
        try:
            server_max = float(match.group(1))
        except ValueError:
            return max_age
        return min(server_max, max_age)
    return max_age


__all__ = [
    "BrandAgentType",
    "BrandJsonJwksResolver",
    "BrandJsonResolverError",
    "BrandJsonResolverErrorCode",
    "DEFAULT_BRAND_JSON_TIMEOUT_SECONDS",
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MIN_COOLDOWN_SECONDS",
]
