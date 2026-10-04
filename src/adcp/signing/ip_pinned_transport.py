"""IP-pinned httpx transports that close the DNS-rebinding TOCTOU.

The default signing fetchers (JWKS + revocation-list, sync + async)
resolve the target hostname via :func:`resolve_and_validate_host`,
then hand the URL back to httpx — which resolves the hostname a
second time at connect. A malicious origin with ``TTL=0`` can return
a safe IP on the first lookup (passing SSRF validation) and a
private IP or cloud-metadata address on the second.

This module closes that gap. :func:`build_ip_pinned_transport`
resolves once, picks the first IP that passes the SSRF validator,
and returns an :class:`httpx.HTTPTransport` wired to a custom
:mod:`httpcore` network backend that translates the pinned
hostname → IP at connect time. TLS certificate validation still
runs against the original hostname (httpcore passes it separately
as ``server_hostname`` during the TLS handshake), so cert CN/SAN
matching is unaffected.

The transport is single-host-scoped. Reusing it for a DIFFERENT
hostname would bypass the pin and either connect to the wrong IP
or fail SSRF re-resolution. Build one transport per hostname you
need to reach; the existing fetchers do this per-call.

Two httpx generations
---------------------

``mcp>=2.0`` runs on ``httpx2``, and this package requires that
MCP line, so every install has both ``httpx`` + ``httpcore`` and
``httpx2`` + ``httpcore2``. The two are unrelated packages: an
``httpx`` transport inside an ``httpx2`` client is accepted at
construction and then fails on the first request with a bare
``AssertionError`` from ``httpx/_transports/default.py``.

So the pin ships for both generations. The ``2``-suffixed names
are the ``httpx2`` ones — :class:`IpPinnedTransport2`,
:class:`AsyncIpPinnedTransport2`,
:func:`build_ip_pinned_transport2`,
:func:`build_async_ip_pinned_transport2` — and they are what an
MCP ``httpx_client_factory`` needs. The unsuffixed names stay on
``httpx``. Pick by the client you pass the transport to.

The pin itself is generation-independent: both httpcore
generations expose ``ConnectionPool(network_backend=...)`` and
``connect_tcp`` with the same signatures, so :class:`_IpPin`
implements the hostname→IP swap once and the four backends differ
only in the base class they extend.

Naming conventions
------------------

* Classes use the ``Async`` CapWords prefix
  (:class:`AsyncIpPinnedTransport`).
* Factory functions that BUILD an async transport use
  ``build_async_*`` (:func:`build_async_ip_pinned_transport`). The
  factory itself is synchronous — it returns an async transport.
* A trailing ``2`` marks the ``httpx2`` generation, matching the
  distribution names ``httpx2`` / ``httpcore2``.
* The legacy ``abuild_*`` alias remains for backward-compatibility
  but is deprecated.

Dependency on httpcore internals
--------------------------------

We reach into httpcore at two points, in both generations:

1. ``httpcore.ConnectionPool(network_backend=...)`` — public API.
2. ``httpcore._backends.sync.SyncBackend`` /
   ``httpcore._backends.anyio.AnyIOBackend`` — underscore-prefixed
   path, nominally private. The backend classes are the documented
   default-backend implementations, and the ``network_backend`` kwarg
   is the sanctioned extension point, but the stability of the
   backend class names themselves isn't guaranteed.

Mitigations:

* ``pyproject.toml`` pins ``httpcore>=1.0,<2.0`` and
  ``httpcore2>=2.5,<3.0``.
* :class:`adcp.signing.ip_pinned_transport` exports the backend
  signatures from a contract test that fails on import if upstream
  changes them, in either generation — see
  ``tests/conformance/signing/test_ip_pinned_transport.py``.
"""

from __future__ import annotations

import ssl
import warnings
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import httpcore
import httpcore2
import httpx
import httpx2
import idna

# Private but documented-as-the-default-backend implementations. The
# underscore prefix is a stability hazard; the contract test in
# tests/conformance/signing/test_ip_pinned_transport.py fails if the
# signatures we rely on change, so a silent upstream break becomes a
# CI failure instead of a latent security regression.
from httpcore._backends.anyio import AnyIOBackend as _AnyIOBackend
from httpcore._backends.sync import SyncBackend as _SyncBackend
from httpcore2._backends.anyio import AnyIOBackend as _AnyIOBackend2
from httpcore2._backends.sync import SyncBackend as _SyncBackend2

from adcp.signing._idna_canonicalize import canonicalize_host
from adcp.signing.jwks import resolve_and_validate_host

if TYPE_CHECKING:
    from httpcore._backends.base import SOCKET_OPTION
    from httpcore2._backends.base import SOCKET_OPTION as SOCKET_OPTION2


__all__ = [
    "AsyncIpPinnedTransport",
    "AsyncIpPinnedTransport2",
    "IpPinnedTransport",
    "IpPinnedTransport2",
    "abuild_ip_pinned_transport",  # deprecated alias; remove next release
    "build_async_ip_pinned_transport",
    "build_async_ip_pinned_transport2",
    "build_ip_pinned_transport",
    "build_ip_pinned_transport2",
]

_PoolT = TypeVar("_PoolT")


def _build_ssl_context() -> ssl.SSLContext:
    """Standard cert-validating TLS context. ``check_hostname`` stays True
    so the hostname-in-cert-SAN match runs against the URL's original
    host (the hostname httpcore passes as ``server_hostname`` during
    the handshake), not the pinned IP.
    """
    return ssl.create_default_context()


def _normalize_pin_host(host: str) -> str:
    """Normalize a hostname for byte-equal comparison.

    Delegates to :func:`canonicalize_host` — strips a single trailing
    dot, ASCII-lowercases, short-circuits IP literals (v4 and v6,
    bracketed or not) before IDNA, and otherwise encodes via
    IDNA-2008 (UTS#46 with ``transitional_processing=False``).
    Matches the JWKS fetcher's ``resolve_and_validate_host`` so a pin
    set on ``straße.de`` collapses to the same A-label httpx will
    pass to httpcore at connect time.

    Falls back to the raw input on IDNA encode failure so the
    comparison just fails cleanly instead of raising inside
    connect_tcp.
    """
    try:
        return canonicalize_host(host)
    except (idna.IDNAError, UnicodeError, UnicodeEncodeError):
        return host.lower().rstrip(".")


class _IpPin:
    """One pinned hostname→IP pair, shared by all four backends.

    The pin carries no httpcore state, so it works the same in both
    generations: a backend subclasses its own generation's
    ``SyncBackend`` / ``AnyIOBackend`` and swaps the host argument
    through :meth:`_pinned_host` before delegating to the parent's
    ``connect_tcp``. All other methods (``connect_unix_socket``)
    pass through unchanged.

    **Fails closed on wrong-host reuse.** If the caller reuses a
    transport for a DIFFERENT hostname (stored in a dict keyed by
    origin, for example), :meth:`_pinned_host` raises instead of
    letting the parent run an unpinned ``connect_tcp`` — that
    fall-through is exactly the TOCTOU the pin exists to close.
    Build a new transport per host.
    """

    #: Transport the refusal names, so the message says what refused.
    _transport_name: ClassVar[str]
    #: Factory the refusal points at for the per-host rebuild.
    _builder_name: ClassVar[str]

    def __init__(self, *, hostname: str, resolved_ip: str) -> None:
        super().__init__()
        self._hostname = _normalize_pin_host(hostname)
        self._resolved_ip = resolved_ip

    def _pinned_host(self, host: str) -> str:
        """Return the pinned IP for ``host``, or raise if ``host`` isn't the pin."""
        if _normalize_pin_host(host) != self._hostname:
            raise RuntimeError(
                f"{self._transport_name} is pinned to {self._hostname!r}; "
                f"refusing connect to {host!r} — build a new transport per host "
                f"(see {self._builder_name})"
            )
        return self._resolved_ip


class _IpPinnedSyncBackend(_IpPin, _SyncBackend):
    """httpcore sync backend that connects by IP for one pinned hostname."""

    _transport_name = "IpPinnedTransport"
    _builder_name = "build_ip_pinned_transport"

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> Any:
        return super().connect_tcp(
            host=self._pinned_host(host),
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _IpPinnedAsyncBackend(_IpPin, _AnyIOBackend):
    """httpcore async backend that connects by IP for one pinned hostname."""

    _transport_name = "AsyncIpPinnedTransport"
    _builder_name = "build_async_ip_pinned_transport"

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> Any:
        return await super().connect_tcp(
            host=self._pinned_host(host),
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _IpPinnedSyncBackend2(_IpPin, _SyncBackend2):
    """httpcore2 sync backend that connects by IP for one pinned hostname."""

    _transport_name = "IpPinnedTransport2"
    _builder_name = "build_ip_pinned_transport2"

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION2] | None = None,
    ) -> Any:
        return super().connect_tcp(
            host=self._pinned_host(host),
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _IpPinnedAsyncBackend2(_IpPin, _AnyIOBackend2):
    """httpcore2 async backend that connects by IP for one pinned hostname."""

    _transport_name = "AsyncIpPinnedTransport2"
    _builder_name = "build_async_ip_pinned_transport2"

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION2] | None = None,
    ) -> Any:
        return await super().connect_tcp(
            host=self._pinned_host(host),
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def _resolve_ssl_context(*, verify: bool, transport_name: str) -> ssl.SSLContext:
    """Return the TLS context for a pinned transport.

    ``verify=False`` is a test affordance against local origins and
    warns, because a pinned transport with cert validation off trusts
    whatever answers at the pinned IP.
    """
    if verify:
        return _build_ssl_context()

    warnings.warn(
        f"{transport_name} constructed with verify=False — TLS cert "
        "validation is disabled. Use only for tests against local "
        "origins; NEVER in production.",
        stacklevel=3,
    )
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def _build_pinned_pool(
    pool_cls: Callable[..., _PoolT],
    *,
    ssl_context: ssl.SSLContext,
    network_backend: Any,
    retries: int,
    max_connections: int | None,
    max_keepalive_connections: int | None,
) -> _PoolT:
    """Build the pool a pinned transport owns, in either httpcore generation.

    The transports build their pool here (rather than calling
    ``super().__init__`` and reassigning ``._pool``) so the TLS +
    backend config is set up atomically and no transport briefly owns
    a vanilla, unpinned pool. The limits match httpx's defaults
    explicitly — httpcore's own ``ConnectionPool`` default is 10,
    which would be a surprise downgrade for callers who expect
    httpx-shaped pool sizing.
    """
    return pool_cls(
        ssl_context=ssl_context,
        network_backend=network_backend,
        http1=True,
        http2=False,
        retries=retries,
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive_connections,
    )


class IpPinnedTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` that connects by pre-resolved IP.

    Preserves normal httpx ergonomics — pass this to
    ``httpx.Client(transport=...)`` and everything else works
    unchanged. The TLS handshake uses the original hostname for
    SNI + cert validation; only the TCP destination is rewritten.

    Construct via :func:`build_ip_pinned_transport` unless you've
    already resolved the hostname yourself. For an ``httpx2.Client``
    use :class:`IpPinnedTransport2`.
    """

    def __init__(
        self,
        *,
        hostname: str,
        resolved_ip: str,
        verify: bool = True,
        retries: int = 0,
        max_connections: int | None = 100,
        max_keepalive_connections: int | None = 20,
    ) -> None:
        self._pool = _build_pinned_pool(
            httpcore.ConnectionPool,
            ssl_context=_resolve_ssl_context(verify=verify, transport_name="IpPinnedTransport"),
            network_backend=_IpPinnedSyncBackend(hostname=hostname, resolved_ip=resolved_ip),
            retries=retries,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )


class AsyncIpPinnedTransport(httpx.AsyncHTTPTransport):
    """Async counterpart to :class:`IpPinnedTransport`.

    For an ``httpx2.AsyncClient`` — which is what MCP SDK v2 and its
    ``httpx_client_factory`` use — build an
    :class:`AsyncIpPinnedTransport2` instead.
    """

    def __init__(
        self,
        *,
        hostname: str,
        resolved_ip: str,
        verify: bool = True,
        retries: int = 0,
        max_connections: int | None = 100,
        max_keepalive_connections: int | None = 20,
    ) -> None:
        self._pool = _build_pinned_pool(
            httpcore.AsyncConnectionPool,
            ssl_context=_resolve_ssl_context(
                verify=verify, transport_name="AsyncIpPinnedTransport"
            ),
            network_backend=_IpPinnedAsyncBackend(hostname=hostname, resolved_ip=resolved_ip),
            retries=retries,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )


class IpPinnedTransport2(httpx2.HTTPTransport):
    """:class:`IpPinnedTransport` for the ``httpx2`` generation.

    Same pin, same fail-closed wrong-host refusal, built on
    ``httpcore2``. Pass this to ``httpx2.Client(transport=...)``.
    """

    def __init__(
        self,
        *,
        hostname: str,
        resolved_ip: str,
        verify: bool = True,
        retries: int = 0,
        max_connections: int | None = 100,
        max_keepalive_connections: int | None = 20,
    ) -> None:
        self._pool = _build_pinned_pool(
            httpcore2.ConnectionPool,
            ssl_context=_resolve_ssl_context(verify=verify, transport_name="IpPinnedTransport2"),
            network_backend=_IpPinnedSyncBackend2(hostname=hostname, resolved_ip=resolved_ip),
            retries=retries,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )


class AsyncIpPinnedTransport2(httpx2.AsyncHTTPTransport):
    """Async counterpart to :class:`IpPinnedTransport2`.

    This is the transport an MCP ``httpx_client_factory`` needs::

        def factory(headers=None, timeout=None, auth=None):
            return httpx2.AsyncClient(
                transport=build_async_ip_pinned_transport2(url),
                headers=headers,
                timeout=timeout,
                auth=auth,
                follow_redirects=False,
                trust_env=False,
            )
    """

    def __init__(
        self,
        *,
        hostname: str,
        resolved_ip: str,
        verify: bool = True,
        retries: int = 0,
        max_connections: int | None = 100,
        max_keepalive_connections: int | None = 20,
    ) -> None:
        self._pool = _build_pinned_pool(
            httpcore2.AsyncConnectionPool,
            ssl_context=_resolve_ssl_context(
                verify=verify, transport_name="AsyncIpPinnedTransport2"
            ),
            network_backend=_IpPinnedAsyncBackend2(hostname=hostname, resolved_ip=resolved_ip),
            retries=retries,
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )


def _resolve_pin(
    uri: str,
    *,
    allow_private: bool,
    allow_special_use: bool,
    allowed_ports: frozenset[int] | None,
) -> tuple[str, str]:
    """Resolve ``uri`` once under the shared SSRF policy; return ``(hostname, ip)``.

    The one address decision behind all four builders, so the ``httpx``
    and ``httpx2`` generations cannot drift apart: ``allow_private``
    admits RFC 1918, RFC 4193 unique-local and loopback destinations,
    ``allow_special_use`` admits the IANA special-use ranges, and cloud
    metadata addresses are refused under both. See
    :func:`adcp.signing.jwks.validate_resolved_ip`.
    """
    hostname, resolved_ip, _port = resolve_and_validate_host(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
    )
    return hostname, resolved_ip


def build_ip_pinned_transport(
    uri: str,
    *,
    allow_private: bool = False,
    allow_special_use: bool = False,
    allowed_ports: frozenset[int] | None = None,
    verify: bool = True,
) -> IpPinnedTransport:
    """Resolve ``uri`` once and return a transport pinned to the validated IP.

    Raises :class:`SSRFValidationError` if the URI's scheme isn't
    ``http``/``https``, ``allowed_ports`` is set and the URI's port is
    outside it, the host doesn't resolve, or every resolved IP is in a
    blocked range.

    ``allowed_ports`` defaults to ``None`` (no port filter — AdCP
    doesn't constrain webhook ports). Hardened deployments pass
    :data:`adcp.signing.jwks.DEFAULT_ALLOWED_PORTS` (`{443, 8443}`)
    or a custom set.

    ``allow_private`` admits RFC 1918, RFC 4193 unique-local and
    loopback destinations; ``allow_special_use`` admits the IANA
    special-use ranges. They are independent, both default off, and
    neither admits a cloud metadata address. The ``httpx2`` builders
    take the same keywords and apply the same policy.

    Typical use inside a fetcher::

        transport = build_ip_pinned_transport(uri)
        with httpx.Client(transport=transport, timeout=10.0) as client:
            response = client.get(uri)
    """
    hostname, resolved_ip = _resolve_pin(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
    )
    return IpPinnedTransport(hostname=hostname, resolved_ip=resolved_ip, verify=verify)


def build_async_ip_pinned_transport(
    uri: str,
    *,
    allow_private: bool = False,
    allow_special_use: bool = False,
    allowed_ports: frozenset[int] | None = None,
    verify: bool = True,
) -> AsyncIpPinnedTransport:
    """Build an :class:`AsyncIpPinnedTransport` for ``uri``.

    Resolve + validate run synchronously (``socket.getaddrinfo``); this
    function itself is not awaitable. The returned transport plugs
    into :class:`httpx.AsyncClient`.

    ``allowed_ports`` defaults to ``None`` (no port filter); see
    :func:`build_ip_pinned_transport` for the hardening kwarg
    semantics.
    """
    hostname, resolved_ip = _resolve_pin(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
    )
    return AsyncIpPinnedTransport(hostname=hostname, resolved_ip=resolved_ip, verify=verify)


def build_ip_pinned_transport2(
    uri: str,
    *,
    allow_private: bool = False,
    allow_special_use: bool = False,
    allowed_ports: frozenset[int] | None = None,
    verify: bool = True,
) -> IpPinnedTransport2:
    """Build an :class:`IpPinnedTransport2` for ``uri`` — the ``httpx2`` pin.

    Identical resolve-and-validate semantics to
    :func:`build_ip_pinned_transport`; the transport plugs into
    :class:`httpx2.Client`.
    """
    hostname, resolved_ip = _resolve_pin(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
    )
    return IpPinnedTransport2(hostname=hostname, resolved_ip=resolved_ip, verify=verify)


def build_async_ip_pinned_transport2(
    uri: str,
    *,
    allow_private: bool = False,
    allow_special_use: bool = False,
    allowed_ports: frozenset[int] | None = None,
    verify: bool = True,
) -> AsyncIpPinnedTransport2:
    """Build an :class:`AsyncIpPinnedTransport2` for ``uri`` — the ``httpx2`` pin.

    Resolve + validate run synchronously (``socket.getaddrinfo``); this
    function itself is not awaitable. The returned transport plugs into
    :class:`httpx2.AsyncClient`, so an MCP ``httpx_client_factory``
    pins its connection with it.
    """
    hostname, resolved_ip = _resolve_pin(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
    )
    return AsyncIpPinnedTransport2(hostname=hostname, resolved_ip=resolved_ip, verify=verify)


def abuild_ip_pinned_transport(
    uri: str,
    *,
    allow_private: bool = False,
    allow_special_use: bool = False,
    allowed_ports: frozenset[int] | None = None,
    verify: bool = True,
) -> AsyncIpPinnedTransport:
    """Deprecated alias for :func:`build_async_ip_pinned_transport`.

    The ``a``-prefix convention in this package means "awaitable
    coroutine" (``averify_detached_jws`` etc.) — but this factory is
    synchronous. Renamed during PR #206 review; kept for one release
    so downstream callers have time to migrate.
    """
    warnings.warn(
        "abuild_ip_pinned_transport is deprecated; use "
        "build_async_ip_pinned_transport (factory is sync, returns "
        "an AsyncIpPinnedTransport).",
        DeprecationWarning,
        stacklevel=2,
    )
    return build_async_ip_pinned_transport(
        uri,
        allow_private=allow_private,
        allow_special_use=allow_special_use,
        allowed_ports=allowed_ports,
        verify=verify,
    )
