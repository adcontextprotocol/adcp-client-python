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
import json
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
import idna

from adcp.signing._bounded_http import ResponseTooLargeError, async_read_limited_bytes
from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.jwks import (
    AsyncCachingJwksResolver,
    AsyncJwksFetcher,
    SSRFValidationError,
    async_default_jwks_fetcher,
)

#: Test seam: a callable that returns an :class:`httpx.AsyncClient`
#: (or any async-context-manager wrapping one) for a given URL. The
#: production path constructs an IP-pinned client; tests inject a
#: factory that returns a client wired to a mock transport so they
#: don't have to monkeypatch ``AsyncClient.__init__`` globally.
_ClientFactory = Callable[[str], AbstractAsyncContextManager[httpx.AsyncClient]]

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
# security.mdx:1103 @ AdCP 3.1.8: successful freshness plus bounded
# stale-on-error service must not mask key rotation beyond the 30-minute
# revocation polling ceiling. Split that total budget evenly by default.
DEFAULT_MAX_AGE_SECONDS = 900.0
DEFAULT_MAX_STALE_SECONDS = 900.0
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_BRAND_JSON_TIMEOUT_SECONDS = 10.0

#: brand.json bodies are tiny by design (a single brand portfolio with a
#: handful of agents). Cap at 256 KiB so a counterparty serving an
#: adversarial multi-megabyte body can't OOM the verifier. Buffered
#: into memory after read; no streaming-parse path. Larger legitimate
#: brand directories (1000+ brands in a portfolio) would need a higher
#: cap — file an issue if you hit it.
DEFAULT_MAX_BRAND_JSON_BYTES = 256 * 1024

#: Default ports stripped during URL canonicalization so loop detection
#: and origin equality see the same string for ``https://x`` and
#: ``https://x:443``.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# Bare hostname pattern for the ``house`` redirect variant. Matches
# the regex in brand.json's schema (``schemas/cache/brand.json``)
# exactly so cross-language conformance behavior stays identical.
_BARE_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class BrandJsonResolverError(Exception):
    """Typed error surfaced by the resolver pipeline."""

    def __init__(self, code: BrandJsonResolverErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: BrandJsonResolverErrorCode = code


@dataclass(frozen=True)
class _BrandJsonSnapshot:
    """One cached brand.json document — full parsed body + final URL
    (after redirects) + cache metadata.

    Frozen so consumers can hold a reference without worrying about
    mid-flight mutation by a concurrent refresh; refresh swaps in a new
    snapshot atomically.
    """

    data: dict[str, Any]
    final_url: str
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


class _BrandJsonFetcher:
    """Shared brand.json fetcher with TTL cache + single-flight refresh.

    Composed by :class:`BrandJsonJwksResolver` and (forthcoming)
    ``BrandAuthorizationResolver`` so both surfaces share one snapshot
    per brand.json URL instead of double-fetching. The fetcher owns:

    * the raw brand.json body (parsed dict) and final URL after redirects
    * cache metadata (ETag, fetched_at, expires_at)
    * single-flight refresh dedup across concurrent callers

    Consumers layer their own selector-output caches on top.

    Cooldown semantics live in the *consumer*, not here. The fetcher's
    :meth:`refresh` always issues a fetch (subject to in-flight dedup).
    Callers that want "only refresh if stale and past cooldown" should
    inspect :attr:`snapshot` and gate the call themselves — same shape
    as the existing JWKS resolver, just moved up one layer.
    """

    def __init__(
        self,
        brand_json_url: str,
        *,
        min_cooldown_seconds: float = DEFAULT_MIN_COOLDOWN_SECONDS,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_body_bytes: int = DEFAULT_MAX_BRAND_JSON_BYTES,
        allow_private_destinations: bool = False,
        timeout_seconds: float = DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
        clock: Callable[[], float] | None = None,
        _client_factory: _ClientFactory | None = None,
    ) -> None:
        self._url = brand_json_url
        self._min_cooldown = min_cooldown_seconds
        self._max_age = max_age_seconds
        self._max_stale = max_stale_seconds
        self._max_redirects = max_redirects
        self._max_body_bytes = max_body_bytes
        self._allow_private = allow_private_destinations
        self._timeout = timeout_seconds
        self._clock = clock or time.time
        self._client_factory = _client_factory

        self._snapshot: _BrandJsonSnapshot | None = None
        self._last_attempt_at: float | None = None
        self._last_error: BrandJsonResolverError | None = None
        # In-flight refresh future for single-flighting concurrent
        # callers — N tasks hitting a cold cache do ONE fetch, not N.
        # ``asyncio.Lock`` would also work but SERIALIZES (waiter N+1
        # fetches AFTER waiter N's fetch returns), which is what we
        # want to avoid.
        self._refresh_in_flight: asyncio.Future[None] | None = None

    @property
    def brand_json_url(self) -> str:
        """The configured entry URL (pre-redirect)."""
        return self._url

    @property
    def min_cooldown_seconds(self) -> float:
        return self._min_cooldown

    @property
    def snapshot(self) -> _BrandJsonSnapshot | None:
        """Current cached snapshot, or None on cold cache. No IO."""
        return self._snapshot

    def is_stale(self, snapshot: _BrandJsonSnapshot | None = None) -> bool:
        """Return True if ``snapshot`` (or the current one) has expired."""
        snap = snapshot if snapshot is not None else self._snapshot
        if snap is None:
            return True
        return self._clock() > snap.expires_at

    def can_refresh(self, snapshot: _BrandJsonSnapshot | None = None) -> bool:
        """Return True if a refresh is allowed by the cooldown gate.

        Cold cache always allows. Otherwise the snapshot must be past
        ``min_cooldown_seconds`` since its ``fetched_at``.
        """
        snap = snapshot if snapshot is not None else self._snapshot
        if snap is None:
            return True
        reference = max(snap.fetched_at, self._last_attempt_at or snap.fetched_at)
        return self._clock() - reference >= self._min_cooldown

    def can_serve_stale(self, snapshot: _BrandJsonSnapshot | None = None) -> bool:
        """Return whether an expired authorization snapshot is within grace."""
        snap = snapshot if snapshot is not None else self._snapshot
        if snap is None:
            return False
        stale_deadline = min(
            snap.expires_at + self._max_stale,
            # The default trust ceiling is over total snapshot age. Shorter
            # explicit cache lifetimes may still use bounded stale-on-error
            # grace, but no configuration extends trust past 30 minutes.
            snap.fetched_at + DEFAULT_MAX_AGE_SECONDS,
        )
        return self._clock() <= stale_deadline

    @property
    def last_error(self) -> BrandJsonResolverError | None:
        return self._last_error

    def clear(self) -> None:
        """Drop the cached snapshot. Next refresh will be unconditional."""
        self._snapshot = None

    async def refresh(self) -> _BrandJsonSnapshot:
        """Single-flighted brand.json refresh.

        Concurrent callers share one in-flight fetch via
        ``_refresh_in_flight``. ``asyncio.shield`` protects the in-flight
        task from a waiter's cancellation propagating into the shared
        fetch.

        On 304 (Not Modified) the snapshot's lifetime is extended in
        place; on 2xx the snapshot is replaced. Raises
        :class:`BrandJsonResolverError` on fetch/parse failure WITHOUT
        clearing the prior snapshot — callers that want stale-on-error
        get it for free.
        """
        if self._refresh_in_flight is not None:
            await asyncio.shield(self._refresh_in_flight)
            assert self._snapshot is not None  # noqa: S101 - invariant after shared refresh
            return self._snapshot

        loop = asyncio.get_running_loop()
        self._refresh_in_flight = loop.create_future()
        try:
            try:
                snap = await self._do_refresh()
            except BaseException as exc:
                if not self._refresh_in_flight.done():
                    self._refresh_in_flight.set_exception(exc)
                raise
            else:
                if not self._refresh_in_flight.done():
                    self._refresh_in_flight.set_result(None)
                return snap
        finally:
            self._refresh_in_flight = None

    async def _do_refresh(self) -> _BrandJsonSnapshot:
        self._last_attempt_at = self._clock()
        try:
            fetched = await _fetch_brand_json(
                start_url=self._url,
                current_etag=self._snapshot.etag if self._snapshot is not None else None,
                max_redirects=self._max_redirects,
                allow_private=self._allow_private,
                timeout_seconds=self._timeout,
                max_body_bytes=self._max_body_bytes,
                client_factory=self._client_factory,
            )
        except BrandJsonResolverError as exc:
            self._last_error = exc
            raise
        self._last_error = None

        now = self._clock()
        if fetched.status == "not_modified" and self._snapshot is not None:
            self._snapshot = _BrandJsonSnapshot(
                data=self._snapshot.data,
                final_url=self._snapshot.final_url,
                fetched_at=now,
                expires_at=now + _compute_lifetime(fetched.cache_control, self._max_age),
                etag=fetched.etag or self._snapshot.etag,
            )
            return self._snapshot

        if fetched.data is None:
            # Defensive: status == "ok" should always carry a body.
            raise BrandJsonResolverError("invalid_body", "brand.json response missing body")

        self._snapshot = _BrandJsonSnapshot(
            data=fetched.data,
            final_url=fetched.final_url,
            fetched_at=now,
            expires_at=now + _compute_lifetime(fetched.cache_control, self._max_age),
            etag=fetched.etag,
        )
        return self._snapshot


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

    The :attr:`jwks_source` class attribute is the discriminant the
    request-signing verifier consults to decide whether
    :func:`adcp.signing.check_key_origin_consistency` applies for
    this resolver. Per ADCP #3690 §step 7, the
    ``identity.key_origins`` consistency check is mandatory only when
    the JWKS source for the (agent, purpose, role) tuple was the
    operator brand.json — and skipped for publisher-pinned tuples
    (where the JWKS origin is the publisher's domain by design).
    A resolver that always sources via brand.json declares
    ``jwks_source = "brand_json"`` so the verifier engages the check;
    a publisher-pin resolver either omits the attribute or declares
    ``"publisher_pin"`` so the verifier skips it.
    """

    #: Discriminant for the verifier-side key_origin consistency
    #: check (see class docstring).
    jwks_source: ClassVar[Literal["brand_json"]] = "brand_json"

    def __init__(
        self,
        brand_json_url: str,
        *,
        agent_type: BrandAgentType,
        agent_id: str | None = None,
        brand_id: str | None = None,
        min_cooldown_seconds: float = DEFAULT_MIN_COOLDOWN_SECONDS,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        max_stale_seconds: float = DEFAULT_MAX_STALE_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_body_bytes: int = DEFAULT_MAX_BRAND_JSON_BYTES,
        allow_private_destinations: bool = False,
        jwks_fetcher: AsyncJwksFetcher | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: float = DEFAULT_BRAND_JSON_TIMEOUT_SECONDS,
        _client_factory: _ClientFactory | None = None,
        _fetcher: _BrandJsonFetcher | None = None,
    ) -> None:
        self._agent_type = agent_type
        self._agent_id = agent_id
        self._brand_id = brand_id
        self._allow_private = allow_private_destinations
        self._jwks_fetcher = jwks_fetcher or async_default_jwks_fetcher
        self._clock = clock or time.time

        # The brand.json fetcher is the shared transport+cache layer.
        # Constructing one here means single-tenant resolvers get the
        # same behavior as before; passing ``_fetcher=`` lets the
        # forthcoming BrandAuthorizationResolver share a snapshot to
        # avoid double-fetching brand.json.
        self._fetcher = _fetcher or _BrandJsonFetcher(
            brand_json_url,
            min_cooldown_seconds=min_cooldown_seconds,
            max_age_seconds=max_age_seconds,
            max_stale_seconds=max_stale_seconds,
            max_redirects=max_redirects,
            max_body_bytes=max_body_bytes,
            allow_private_destinations=allow_private_destinations,
            timeout_seconds=timeout_seconds,
            clock=self._clock,
            _client_factory=_client_factory,
        )

        # Derived selector state. Recomputed for every successful body
        # refresh; ETags are optional and may be reused incorrectly.
        self._selected: _SelectedAgent | None = None
        self._selected_for: tuple[str, str | None] | None = None
        self._inner: AsyncCachingJwksResolver | None = None

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
        snap = self._fetcher.snapshot
        if snap is None or self._inner is None:
            await self._refresh()
        elif self._fetcher.is_stale(snap) and self._fetcher.can_refresh(snap):
            try:
                await self._refresh()
            except BrandJsonResolverError:
                if not self._fetcher.can_serve_stale(snap):
                    self._selected = None
                    self._inner = None
                    return None
        elif self._fetcher.is_stale(snap) and not self._fetcher.can_serve_stale(snap):
            self._selected = None
            self._inner = None
            return None

        if self._inner is None:
            return None

        hit = await self._inner(kid)
        if hit is not None:
            return hit

        # Cascade: refresh brand.json in case jwks_uri rotated.
        if self._fetcher.snapshot is not None and self._fetcher.can_refresh():
            try:
                await self._refresh()
            except BrandJsonResolverError:
                return None
            return await self._inner(kid)
        return None

    @property
    def agent_url(self) -> str | None:
        """The agent URL we resolved ``jwks_uri`` from. Populated
        after the first successful refresh; useful for verifier
        result attribution."""
        return self._selected.url if self._selected is not None else None

    @property
    def jwks_uri(self) -> str | None:
        """The JWKS URI selected from brand.json's ``agents[]`` for
        this resolver's ``(agent_type, agent_id, brand_id)`` tuple.
        Populated after the first successful refresh; ``None`` on
        cold cache."""
        return self._selected.jwks_uri if self._selected is not None else None

    async def force_refresh(self) -> None:
        """Force refetch of both brand.json and inner JWKS, bypassing
        the cooldown.

        Race semantics match the JS port: state is cleared, then
        ``_refresh`` is called. If another task is already mid-refresh,
        we await its completion rather than starting a fresh one — the
        in-flight task will populate the snapshot we just cleared.
        ``force_refresh`` therefore means "ensure a fresh fetch is
        either in progress or just completed", not "always issue a new
        fetch even when one is pending."
        """
        self._fetcher.clear()
        self._selected = None
        self._selected_for = None
        self._inner = None
        await self._refresh()

    async def _refresh(self) -> None:
        """Refresh brand.json + recompute selector + (re)build inner JWKS."""
        snap = await self._fetcher.refresh()
        self._sync_selector(snap)

    def _sync_selector(self, snap: _BrandJsonSnapshot) -> None:
        """Reselect the agent from the current body, independent of validators."""
        identity = (snap.final_url, snap.etag)
        try:
            agent = _select_agent(
                snap.data,
                snap.final_url,
                agent_type=self._agent_type,
                agent_id=self._agent_id,
                brand_id=self._brand_id,
            )
        except BrandJsonResolverError:
            self._selected = None
            self._selected_for = identity
            self._inner = None
            raise

        if self._inner is None or (
            self._selected is not None and self._selected.jwks_uri != agent.jwks_uri
        ):
            self._inner = AsyncCachingJwksResolver(
                agent.jwks_uri,
                fetcher=self._jwks_fetcher,
                allow_private=self._allow_private,
                clock=self._clock,
            )

        self._selected = agent
        self._selected_for = identity


# --- brand.json fetching ---


async def _fetch_brand_json(
    *,
    start_url: str,
    current_etag: str | None,
    max_redirects: int,
    allow_private: bool,
    timeout_seconds: float,
    max_body_bytes: int = DEFAULT_MAX_BRAND_JSON_BYTES,
    client_factory: _ClientFactory | None = None,
) -> _FetchedBrandJson:
    """Fetch brand.json from ``start_url``, following ``authoritative_location``
    and ``house`` redirect variants up to ``max_redirects`` hops.

    Each hop validates the URL structurally before dispatch — an
    attacker-controlled brand.json that emits
    ``{"house": "evil.com\\@victim.com"}`` or
    ``{"authoritative_location": "http://169.254.169.254/..."}`` is
    rejected at parse time rather than relying on the transport
    layer to catch every pathological shape.

    SSRF protection (matches the ``adcp.signing.jwks`` posture):
    each hop's URL is sent through an :func:`build_async_ip_pinned_transport`,
    which resolves and validates the host once and pins the connect
    to that IP — closing the DNS-rebinding TOCTOU. ``trust_env=False``
    so a misconfigured ``HTTPS_PROXY`` env var cannot tunnel the
    fetch through an attacker-chosen egress. ``allow_private=False``
    (default) rejects RFC1918 / link-local / cloud-metadata IPs.

    Body cap: each response is bounded to ``max_body_bytes`` (default
    256 KiB). brand.json is small by design; an adversarial
    multi-megabyte body is stopped during streaming, before JSON parsing.

    ``client_factory`` is the test seam — production callers pass
    ``None`` to use the IP-pinned client; tests inject a factory that
    returns a client wired to a mock transport.
    """
    from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

    seen: set[str] = set()
    url = _canonicalize_url(start_url, allow_private=allow_private)

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

        # Build a fresh IP-pinned client per hop — each redirect target
        # is a different host whose IP must be resolved + validated
        # independently. Mirrors how the JWKS fetcher constructs a
        # transport per call.
        if client_factory is not None:
            client_cm = client_factory(url)
        else:
            # The transport builder resolves + validates the host up
            # front, so it — not the later request — is where an SSRF
            # refusal surfaces. It must sit inside the same handler as
            # the request or the refusal escapes the resolver's
            # documented error contract as a raw SSRFValidationError.
            try:
                transport = build_async_ip_pinned_transport(url, allow_private=allow_private)
            except SSRFValidationError as exc:
                raise BrandJsonResolverError(
                    "fetch_failed", f"brand.json URL failed SSRF check: {exc}"
                ) from exc
            client_cm = httpx.AsyncClient(
                transport=transport,
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )

        try:
            async with client_cm as client:
                try:
                    request_cm = client.stream(
                        "GET", url, headers={**headers, "Accept-Encoding": "identity"}
                    )
                    async with request_cm as response:
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
                            body = await async_read_limited_bytes(response, limit=max_body_bytes)
                        except ResponseTooLargeError as exc:
                            raise BrandJsonResolverError(
                                "invalid_body", f"brand.json {exc}"
                            ) from exc

                        try:
                            parsed = json.loads(body)
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise BrandJsonResolverError(
                                "invalid_body", "brand.json response is not valid JSON"
                            ) from exc

                        etag = response.headers.get("etag")
                        cache_control = response.headers.get("cache-control")
                except SSRFValidationError as exc:
                    raise BrandJsonResolverError(
                        "fetch_failed", f"brand.json URL failed SSRF check: {exc}"
                    ) from exc
                except (httpx.HTTPError, OSError) as exc:
                    raise BrandJsonResolverError(
                        "fetch_failed", f"brand.json fetch failed: {exc}"
                    ) from exc

        except BrandJsonResolverError:
            raise

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
            etag=etag,
            cache_control=cache_control,
        )

    raise BrandJsonResolverError("redirect_depth_exceeded", "brand.json redirect depth exceeded")


def _canonicalize_url(raw: str, *, allow_private: bool) -> str:
    """Structurally validate a URL and return it canonicalized.

    Rejects URLs the transport layer would later refuse anyway, but
    catching them here gives a clearer error code AND closes the
    redirect-loop detector against trivial aliasing attacks
    (case-mismatched host, default-port elision).

    Canonicalization mirrors the JS port's ``new URL(...)`` semantics:

    * Scheme lowercased.
    * Host lowercased (``urlsplit`` does NOT do this — we do it).
    * Trailing FQDN-root dot stripped (``brand.example.`` and
      ``brand.example`` are the same host).
    * Unicode U-labels encoded to A-labels (``bücher.example`` →
      ``xn--bcher-kva.example``), via the package-wide UTS#46
      convention in :mod:`adcp.signing._idna_canonicalize`. A host the
      IDNA encoder refuses (e.g. an underscore label) is rejected with
      ``invalid_url`` rather than passed through.
    * IP literals normalized to their canonical form, and IPv6
      literals re-bracketed. ``urlsplit(...).hostname`` returns IPv6
      hosts *de-bracketed*, so rebuilding the authority from it
      without re-adding brackets emits ``https://::1/x`` — a string
      with no parseable host, and with a non-default port a string
      whose port can no longer be separated from the address.
    * Default port (443 for https, 80 for http) stripped.
    * Fragments stripped — they aren't sent on the wire and must not
      smuggle loop-detection aliases into ``seen``.

    Without these the loop detector sees ``https://X.example/`` and
    ``https://x.example/`` as distinct strings; the JS-side resolver
    canonicalizes both via ``new URL``, so a Python-only deployment
    would fail open where JS fails closed.

    The returned string is required to be a URL the transport layer
    accepts: it is fed straight to ``build_async_ip_pinned_transport``
    and ``client.get`` on every redirect hop.
    """
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrandJsonResolverError("invalid_url", "brand.json URL is malformed") from exc
    if not parts.scheme or not parts.netloc:
        raise BrandJsonResolverError("invalid_url", "brand.json URL is malformed")
    if parts.username or parts.password:
        raise BrandJsonResolverError("invalid_url", "brand.json URL must not include userinfo")
    scheme = parts.scheme.lower()
    if scheme != "https" and not (allow_private and scheme == "http"):
        raise BrandJsonResolverError("invalid_url", "brand.json URL must use https://")
    raw_host = parts.hostname or ""
    if not raw_host:
        raise BrandJsonResolverError("invalid_url", "brand.json URL has no host")
    try:
        host = canonicalize_host(raw_host)
    except (idna.IDNAError, UnicodeError) as exc:
        raise BrandJsonResolverError(
            "invalid_url", f"brand.json URL host is not a valid IDNA name: {exc}"
        ) from exc
    # ``canonicalize_host`` returns IPv6 literals UNBRACKETED (its step
    # 3 short-circuits to ``str(ipaddress.ip_address(...))``); putting
    # the brackets back is the caller's job. A canonicalized DNS name
    # never contains ':', so this is an unambiguous IPv6 test.
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


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


def _canonical_origin(raw: str, label: str) -> str:
    """`scheme://host[:port]` in the same canonical form on both sides.

    Shares its host handling with :func:`_canonicalize_url` -- same
    `canonicalize_host`, same re-bracketing, same default-port elision -- so an
    origin equality test cannot be defeated by spelling. Deliberately built
    from `.hostname` rather than `.netloc`: `.netloc` is the one accessor that
    retains userinfo, and `https://user@brand.example` must compare equal to
    `https://brand.example` rather than pivoting trust onto a different string.
    """
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise BrandJsonResolverError("invalid_url", f"{label} is not a valid URL") from exc
    if not parts.scheme or not parts.netloc:
        raise BrandJsonResolverError("invalid_url", f"{label} is not a valid URL")
    raw_host = parts.hostname or ""
    if not raw_host:
        raise BrandJsonResolverError("invalid_url", f"{label} has no host")
    try:
        host = canonicalize_host(raw_host)
    except (idna.IDNAError, UnicodeError) as exc:
        raise BrandJsonResolverError(
            "invalid_url", f"{label} host is not a valid IDNA name: {exc}"
        ) from exc
    if ":" in host:  # canonicalize_host returns IPv6 unbracketed
        host = f"[{host}]"
    scheme = parts.scheme.lower()
    port = parts.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    return f"{scheme}://{host}" if port is None else f"{scheme}://{host}:{port}"


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
    agent_origin = _canonical_origin(agent_url, "agent.url")
    # ``final_brand_url`` has already been through ``_canonicalize_url``, but
    # canonicalizing it again is required rather than merely tidy: the two
    # sides of this comparison MUST be produced by the same function or the
    # check compares a canonical string to a raw one. That asymmetry is the
    # defect -- a publisher spelling the same origin identically on both sides
    # (a U-label, a trailing root dot, a default port) got told their agent was
    # on a different origin from their brand.json, which it was not.
    # Re-canonicalizing is idempotent, so this costs nothing.
    brand_origin = _canonical_origin(final_brand_url, "brand.json URL")
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
    "DEFAULT_MAX_STALE_SECONDS",
    "DEFAULT_MIN_COOLDOWN_SECONDS",
]
