"""JWKS fetching and caching for the AdCP request-signing profile.

Production deployments MUST validate JWKS URIs against SSRF per the AdCP
webhook-URL rules: reject reserved IP ranges (loopback, private, link-local,
multicast, reserved) and known cloud metadata endpoints. This module enforces
those rules at resolution time and provides a per-URL cache with a 30-second
refetch cooldown between fetches — the cooldown blocks attack-driven cache
invalidation (where an attacker forces a verifier to hammer the signer's
`jwks_uri` on every rejection).

DNS-rebinding-resistant transport (resolve-then-connect with IP pinning) is
tracked in #190 and not implemented here — the current design is vulnerable to
a TOCTOU where DNS resolves to an allowed IP during validation and a blocked
IP at connect time.

Naming conventions
------------------
* Classes use the ``Async`` CapWords prefix (``AsyncCachingJwksResolver``).
* Free functions use the ``async_`` snake_case prefix
  (``async_default_jwks_fetcher``).
* Methods use the ``a`` prefix (``aclose``, ``aprime``) — matches the
  ``httpx`` / ``anyio`` ecosystem.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import idna

from adcp.signing.errors import (
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_JWKS_UNTRUSTED,
    SignatureVerificationError,
)

DEFAULT_JWKS_COOLDOWN_SECONDS = 30.0
DEFAULT_JWKS_TIMEOUT_SECONDS = 10.0

# Cloud metadata endpoints that MUST be blocked even if somehow marked non-private
BLOCKED_METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean, Alibaba
        "fd00:ec2::254",  # AWS IPv6
        "100.100.100.200",  # Alibaba
        "192.0.0.192",  # Oracle Cloud
    }
)

# Recommended destination ports for hardened SSRF-validated outbound HTTP
# deployments. AdCP itself does not constrain ``pushNotificationConfig.url``
# ports (see ``schemas/cache/core/push-notification-config.json``), so the
# default port-allowlist is permissive — adopters who want a hardening posture
# pass ``allowed_ports=DEFAULT_ALLOWED_PORTS`` (or a custom set) explicitly.
# Rejecting non-standard ports closes a smuggle vector for buyers bouncing
# traffic to internal services on the same routable IP — :25 (SMTP relay),
# :6379 (Redis), :11211 (Memcached), etc. — but that's an operator choice,
# not a framework default that breaks legitimate :9443 / :4443 buyers.
DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({443, 8443})

# Upper bound on the number of resolved addresses examined per validation call.
# A malicious DNS server can return thousands of records as a mild amplification
# vector against the validator's inner loop.
_MAX_RESOLVED_ADDRESSES = 32


class SSRFValidationError(Exception):
    """Raised when a URL resolves to an IP in a reserved or blocked range."""


class JwksFetcher(Protocol):
    """A callable that fetches and parses a JWKS document from a URL."""

    def __call__(self, uri: str, *, allow_private: bool = False) -> dict[str, Any]: ...


class AsyncJwksFetcher(Protocol):
    """Async variant of :class:`JwksFetcher`."""

    async def __call__(self, uri: str, *, allow_private: bool = False) -> dict[str, Any]: ...


class JwksResolver(Protocol):
    """Resolves a keyid to a JWK, or returns None if unknown.

    The canonical Protocol used by the sync RFC 9421 verifier and the
    sync JWS document verifier. Implementations include
    :class:`StaticJwksResolver` (in-memory, for tests) and
    :class:`CachingJwksResolver` (fetches + caches from a URI).

    Async callers use :class:`AsyncJwksResolver` instead.
    """

    def __call__(self, keyid: str) -> dict[str, Any] | None: ...


class AsyncJwksResolver(Protocol):
    """Async variant of :class:`JwksResolver`.

    Used by the async JWS document verifier and the async revocation
    checker so JWKS cache-misses don't block the event loop.
    Implementations: :class:`AsyncCachingJwksResolver`. For tests,
    :class:`StaticJwksResolver` doubles as an async resolver if you wrap
    it in a thin async callable — there's no async work, just a dict
    lookup — but typically you'll just use the static one directly where
    an :class:`AsyncJwksResolver` is expected via :func:`as_async`.
    """

    async def __call__(self, keyid: str) -> dict[str, Any] | None: ...


def validate_jwks_uri(
    uri: str,
    *,
    allow_private: bool = False,
    allowed_ports: frozenset[int] | None = None,
) -> None:
    """Raise SSRFValidationError on blocked IP, bad scheme, or disallowed port.

    Standalone no-return helper for callers that only want validation —
    :func:`resolve_and_validate_host` returns the accepted IP when the
    caller needs it for IP-pinned connects.
    """
    resolve_and_validate_host(uri, allow_private=allow_private, allowed_ports=allowed_ports)


def resolve_and_validate_host(
    uri: str,
    *,
    allow_private: bool = False,
    allowed_ports: frozenset[int] | None = None,
) -> tuple[str, str, int]:
    """Resolve the URI's hostname once and return ``(hostname, ip, port)``.

    Runs the full SSRF validation — reserved-range rejection + cloud-
    metadata blocklist — and returns the first IP that passes. Callers
    that connect by IP (see :class:`adcp.signing.IpPinnedTransport`)
    use the returned IP to close the DNS-rebinding TOCTOU: they resolve
    ONCE through this helper, then pin subsequent connects to that IP.

    The returned IP is always ASCII (no IPv6 scope id, no IPv4-mapped
    IPv6 wrapping) so it can be handed verbatim to
    :func:`socket.create_connection`.

    Parameters
    ----------
    uri:
        A full URL. Only ``http`` and ``https`` schemes are accepted.
    allow_private:
        Skip the reserved-range check. For tests only; cloud-metadata
        IPs remain blocked unconditionally.
    allowed_ports:
        Optional destination-port allowlist. ``None`` (default) imposes
        no port filter — the URL's port is unrestricted. Hardened
        deployments pass :data:`DEFAULT_ALLOWED_PORTS` (`{443, 8443}`)
        or a custom set; the validator then rejects URIs whose port
        is outside the set. AdCP doesn't constrain webhook ports in
        the spec, so this is operator policy, not a framework default.

    Returns
    -------
    tuple[str, str, int]
        ``(hostname, ip, port)`` — hostname and port are parsed from
        the URI; IP is the validated resolution.

    Raises
    ------
    SSRFValidationError
        Scheme is not ``http``/``https``, ``allowed_ports`` is set and
        the URI's port is outside it, the hostname doesn't resolve, or
        every resolved IP is in a blocked range.
    """
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https"):
        raise SSRFValidationError(
            f"unsupported URI scheme for SSRF-validated fetch: "
            f"{parts.scheme!r} (only http/https allowed)"
        )
    host = parts.hostname
    if host is None or host == "":
        raise SSRFValidationError(f"URI has no host: {uri!r}")
    # Strip a single trailing dot (FQDN form) so the pin matches what
    # httpx / httpcore pass on subsequent requests. Without this, a
    # caller who constructs with ``https://host./`` and then requests
    # ``https://host/`` (or vice versa) sees the backend's
    # hostname-match fail and falls through to unpinned resolution.
    if host.endswith("."):
        host = host[:-1]
    # IDNA-encode so Unicode hostnames match the ASCII form httpx
    # produces before calling into httpcore. urlsplit preserves the
    # raw Unicode; httpx encodes it. A mismatch here breaks the
    # hostname-match in the backend override and silently reopens
    # the TOCTOU for IDN hosts.
    #
    # IDNA-2008 (UTS#46, transitional_processing=False) via the PyPI
    # ``idna`` package — stdlib ``encodings.idna`` is IDNA-2003 and
    # mismaps Eszett (``ß`` → ``ss``) and final-sigma. The
    # package-wide IDNA convention is IDNA-2008; all four callsites
    # (here, ``ip_pinned_transport``, ``revocation_fetcher``,
    # ``key_origins``) share this encoding so canonicalization
    # results compare byte-equal across the verifier pipeline.
    try:
        host = idna.encode(host, uts46=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError, UnicodeEncodeError) as exc:
        raise SSRFValidationError(f"URI host {host!r} is not IDNA-valid: {exc}") from exc
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    if allowed_ports is not None and port not in allowed_ports:
        raise SSRFValidationError(
            f"port {port} not allowed for SSRF-validated fetch "
            f"(allowed: {sorted(allowed_ports) if allowed_ports else '<empty>'})"
        )

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SSRFValidationError(f"cannot resolve host {host!r}: {exc}") from exc

    accepted_ip: str | None = None
    last_rejection: str | None = None
    for _family, _, _, _, sockaddr in infos[:_MAX_RESOLVED_ADDRESSES]:
        ip_raw = sockaddr[0]
        ip_str = str(ip_raw)
        # Strip IPv6 scope id if present
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip_str)
        # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d). On Python 3.10 the direct
        # flag checks (is_loopback, is_private, etc.) on the mapped form return
        # False — the fix landed in 3.11.4 via bpo-44269. The SDK targets 3.10+
        # per pyproject.toml so we unwrap explicitly.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if str(ip) in BLOCKED_METADATA_IPS:
            raise SSRFValidationError(f"cloud metadata IP {ip} blocked")
        if not allow_private and (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            last_rejection = f"resolved IP {ip} is in a reserved range"
            # Historical behavior of validate_jwks_uri was to raise on
            # ANY reserved IP in the result list, not to skip-and-try-
            # the-next-one. Preserve that: reject immediately so a host
            # with mixed public + private results doesn't silently pin
            # the public one.
            raise SSRFValidationError(last_rejection)
        if accepted_ip is None:
            accepted_ip = str(ip)

    if accepted_ip is None:
        # Shouldn't happen — getaddrinfo with results + no raise means
        # at least one entry passed. Belt-and-braces.
        raise SSRFValidationError(
            f"host {host!r} resolved but no usable IP ({last_rejection or 'unknown'})"
        )
    return host, accepted_ip, port


def default_jwks_fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
    """Validate + resolve the URI once, then GET the JWKS over an IP-pinned
    transport.

    Pinning closes the DNS-rebinding TOCTOU that would otherwise let a
    ``TTL=0`` attacker pass SSRF validation with one IP and connect to
    a different one. See :mod:`adcp.signing.ip_pinned_transport`.
    """
    # Import lazily to avoid a module-load cycle with the transport module
    # (which imports from this file).
    from adcp.signing.ip_pinned_transport import build_ip_pinned_transport

    transport = build_ip_pinned_transport(uri, allow_private=allow_private)
    # follow_redirects=False: a 302 to a different hostname would
    # bypass the pin. trust_env=False: httpx's default True picks up
    # HTTPS_PROXY / HTTP_PROXY from the environment and routes the
    # request through an HTTPProxy pool that ignores our pinned
    # backend entirely — a process with HTTPS_PROXY set to an
    # attacker-controlled endpoint would bypass the TOCTOU fix.
    with httpx.Client(
        transport=transport,
        timeout=DEFAULT_JWKS_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = client.get(uri, headers={"Accept": "application/json"})
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or "keys" not in body:
        raise ValueError(f"JWKS document at {uri!r} has no 'keys' array")
    return body


class CachingJwksResolver:
    """JWKS resolver with per-URI cache and refetch cooldown.

    Behavior:
    - On a lookup miss, refresh if the cooldown has elapsed since the last
      refresh (success or failure). This prevents attacker-driven
      request-amplification on the signer's JWKS endpoint.
    - Cache keyed on `kid`. Unknown keyids return None (verifier converts to
      `request_signature_key_unknown`).
    - SSRF failures surface as `request_signature_jwks_untrusted`; network
      failures surface as `request_signature_jwks_unavailable`.
    """

    def __init__(
        self,
        jwks_uri: str,
        *,
        fetcher: JwksFetcher | None = None,
        cooldown_seconds: float = DEFAULT_JWKS_COOLDOWN_SECONDS,
        allow_private: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._fetcher = fetcher or default_jwks_fetcher
        self._cooldown = cooldown_seconds
        self._allow_private = allow_private
        self._clock = clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_attempt: float | None = None
        self._primed = False

    def __call__(self, keyid: str) -> dict[str, Any] | None:
        if keyid in self._cache:
            return self._cache[keyid]
        now = self._clock()
        if not self._primed or (
            self._last_attempt is not None and now - self._last_attempt >= self._cooldown
        ):
            self._refresh(now)
        return self._cache.get(keyid)

    def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            jwks = self._fetcher(self._jwks_uri, allow_private=self._allow_private)
        except SSRFValidationError as exc:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_JWKS_UNTRUSTED,
                step=7,
                message=f"JWKS URI failed SSRF check: {exc}",
            ) from exc
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
                step=7,
                message=f"JWKS fetch failed: {exc}",
            ) from exc
        self._primed = True
        self._cache = {jwk["kid"]: jwk for jwk in jwks.get("keys", []) if "kid" in jwk}


class StaticJwksResolver:
    """Resolves keyids from a fixed in-memory JWKS — convenient for tests."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._keys = {jwk["kid"]: jwk for jwk in jwks.get("keys", []) if "kid" in jwk}

    def __call__(self, keyid: str) -> dict[str, Any] | None:
        return self._keys.get(keyid)


# ---------------------------------------------------------------------------
# Async variants
# ---------------------------------------------------------------------------


async def async_default_jwks_fetcher(uri: str, *, allow_private: bool = False) -> dict[str, Any]:
    """Async counterpart to :func:`default_jwks_fetcher`.

    Uses :class:`httpx.AsyncClient` with an IP-pinned transport so
    callers on an asyncio event loop don't block the loop on JWKS
    fetches AND the DNS-rebinding TOCTOU stays closed. Same SSRF +
    follow-redirects rules as the sync version.
    """
    from adcp.signing.ip_pinned_transport import build_async_ip_pinned_transport

    transport = build_async_ip_pinned_transport(uri, allow_private=allow_private)
    # See default_jwks_fetcher for why trust_env=False matters.
    async with httpx.AsyncClient(
        transport=transport,
        timeout=DEFAULT_JWKS_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.get(uri, headers={"Accept": "application/json"})
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or "keys" not in body:
        raise ValueError(f"JWKS document at {uri!r} has no 'keys' array")
    return body


class AsyncCachingJwksResolver:
    """Async JWKS resolver with per-URI cache and refetch cooldown.

    Identical semantics to :class:`CachingJwksResolver` — per-``kid``
    cache, cooldown-gated refresh on miss, SSRF errors surface as
    ``request_signature_jwks_untrusted``, network errors as
    ``request_signature_jwks_unavailable`` — but awaitable and backed by
    :class:`httpx.AsyncClient`.

    Concurrency: a single :class:`asyncio.Lock` serializes refreshes so
    N parallel verifying tasks all driving the first miss don't fire N
    fetches.
    """

    def __init__(
        self,
        jwks_uri: str,
        *,
        fetcher: AsyncJwksFetcher | None = None,
        cooldown_seconds: float = DEFAULT_JWKS_COOLDOWN_SECONDS,
        allow_private: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jwks_uri = jwks_uri
        self._fetcher = fetcher or async_default_jwks_fetcher
        self._cooldown = cooldown_seconds
        self._allow_private = allow_private
        self._clock = clock
        self._cache: dict[str, dict[str, Any]] = {}
        self._last_attempt: float | None = None
        self._primed = False
        # Construct the lock eagerly. Lazy init was racy: two tasks both
        # seeing ``self._lock is None`` would each construct a separate
        # Lock and proceed in parallel without serialization. In Python
        # 3.10+ ``asyncio.Lock()`` no longer requires a running loop at
        # construction time — it binds to whatever loop is running the
        # first ``async with`` call. Instances are per-loop: don't share
        # across ``asyncio.run`` boundaries.
        self._lock: asyncio.Lock = asyncio.Lock()

    async def __call__(self, keyid: str) -> dict[str, Any] | None:
        if keyid in self._cache:
            return self._cache[keyid]
        now = self._clock()
        if not self._primed or (
            self._last_attempt is not None and now - self._last_attempt >= self._cooldown
        ):
            async with self._lock:
                # Re-check after acquiring: another task may have refreshed.
                if keyid in self._cache:
                    return self._cache[keyid]
                now = self._clock()
                if not self._primed or (
                    self._last_attempt is not None and now - self._last_attempt >= self._cooldown
                ):
                    await self._refresh(now)
        return self._cache.get(keyid)

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            jwks = await self._fetcher(self._jwks_uri, allow_private=self._allow_private)
        except SSRFValidationError as exc:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_JWKS_UNTRUSTED,
                step=7,
                message=f"JWKS URI failed SSRF check: {exc}",
            ) from exc
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
                step=7,
                message=f"JWKS fetch failed: {exc}",
            ) from exc
        self._primed = True
        self._cache = {jwk["kid"]: jwk for jwk in jwks.get("keys", []) if "kid" in jwk}


def as_async_resolver(resolver: JwksResolver) -> AsyncJwksResolver:
    """Wrap a sync :class:`JwksResolver` so it satisfies :class:`AsyncJwksResolver`.

    Useful for tests: pass a :class:`StaticJwksResolver` through
    :func:`as_async_resolver` to plug it into an async-verifier
    pipeline that types ``AsyncJwksResolver``. There's no real async
    work (just a dict lookup); the wrapper is a shape adapter.
    """

    async def resolve(keyid: str) -> dict[str, Any] | None:
        return resolver(keyid)

    return resolve


__all__ = [
    "BLOCKED_METADATA_IPS",
    "AsyncCachingJwksResolver",
    "AsyncJwksFetcher",
    "AsyncJwksResolver",
    "CachingJwksResolver",
    "DEFAULT_ALLOWED_PORTS",
    "DEFAULT_JWKS_COOLDOWN_SECONDS",
    "DEFAULT_JWKS_TIMEOUT_SECONDS",
    "JwksFetcher",
    "JwksResolver",
    "SSRFValidationError",
    "StaticJwksResolver",
    "as_async_resolver",
    "async_default_jwks_fetcher",
    "default_jwks_fetcher",
    "resolve_and_validate_host",
    "validate_jwks_uri",
]
