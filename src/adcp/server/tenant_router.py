"""Subdomain-based tenant routing for multi-tenant deployments.

Multi-tenant AdCP sellers fronted by ``*.example.com`` use the
incoming ``Host`` header to decide which tenant the request belongs
to. salesagent ships a hand-rolled ``domain_routing.py`` (~250 LOC);
this module ships the same shape behind a typed Protocol so adopters
get the routing seam without writing the parser.

Surface
-------

* :class:`Tenant` — the resolved tenant. Adopters extend with
  ``ext`` for whatever per-tenant data their downstream stores need
  (DB shard, locale, billing entity, etc.).
* :class:`SubdomainTenantRouter` — runtime-checkable Protocol with
  one async ``resolve(host: str) -> Tenant | None`` method.
* :class:`InMemorySubdomainTenantRouter` — reference impl for
  dev/test backed by a static ``host → Tenant`` dict.
* :class:`CallableSubdomainTenantRouter` — adopter-callable router
  for DB-backed lookups. Adopter writes a single sync-or-async
  callable mapping a normalized host to a :class:`Tenant`; the
  framework owns host normalization. Optional bounded TTL cache
  for hot-path lookups. **Recommended for production multi-tenant
  deployments** — replaces ~25 LOC of adopter glue with ~5.
* :class:`SubdomainTenantMiddleware` — Starlette ASGI middleware
  that calls the router, stashes the result in a
  :class:`contextvars.ContextVar`, and ``404`` s on unknown hosts.
* :func:`current_tenant` — accessor for the threaded contextvar.
  ``context_factory`` callbacks read it to populate
  :attr:`ToolContext.tenant_id`.

Wire-up
-------

::

    from starlette.applications import Starlette
    from adcp.server import (
        InMemorySubdomainTenantRouter,
        SubdomainTenantMiddleware,
        Tenant,
        current_tenant,
    )

    router = InMemorySubdomainTenantRouter(
        tenants={
            "acme.example.com": Tenant(id="acme", display_name="Acme"),
            "beta.example.com": Tenant(id="beta", display_name="Beta"),
        }
    )

    app = Starlette()
    app.add_middleware(SubdomainTenantMiddleware, router=router)

    def build_context(meta):
        tenant = current_tenant()
        return ToolContext(
            request_id=meta.request_id,
            tenant_id=tenant.id if tenant else None,
            ...
        )

Composition
-----------

The contextvar threads down to ``AccountStore``,
``BuyerAgentRegistry``, ``TaskRegistry``, etc., letting all
downstream stores filter by tenant without explicit plumbing —
each store reads :func:`current_tenant` (or the
:attr:`ToolContext.tenant_id` set from it) and scopes accordingly.

Security
--------

* Unknown hosts return ``404 Not Found`` with no body — the
  middleware MUST NOT 200 a request to a host the router can't
  resolve. Buyers probing for tenant existence get the same
  response shape regardless of whether the host is unrecognized
  or the tenant is suspended (suspension is a per-tenant decision
  surfaced downstream).
* The ``Host`` header is the source of truth, not ``X-Forwarded-Host``
  or any reverse-proxy header — adopters terminating TLS on a
  proxy are responsible for passing the original host through
  correctly.
* The contextvar is request-scoped via the ASGI middleware's
  per-call ``set()``; ASGI doesn't reuse contexts across requests.
"""

from __future__ import annotations

import contextvars
import inspect
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Tenant:
    """The resolved tenant for a request.

    Frozen — the middleware caches resolved tenants in a contextvar
    that's read by downstream stores; mutation in-place would create
    cross-store inconsistency.

    :param id: Stable tenant identifier. Used as
        :attr:`ToolContext.tenant_id` and the scope key for
        per-tenant DB queries / cache scoping.
    :param display_name: Human-readable name for logging and admin
        UIs. Not used for routing.
    :param ext: Adopter passthrough — DB shard pointer, billing
        entity FK, locale, sandbox flag, etc.
    """

    id: str
    display_name: str = ""
    ext: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SubdomainTenantRouter(Protocol):
    """Resolves an HTTP ``Host`` header value to a :class:`Tenant`.

    Adopters back this Protocol with their tenant table — typically
    a SQL query against the deployment's tenant registry. The
    middleware calls :meth:`resolve` once per request; production
    adopters cache hot lookups in the impl since the host header is
    request-scoped.

    Returning ``None`` causes the middleware to ``404`` the
    request — unknown hosts MUST NOT pass through.
    """

    async def resolve(self, host: str) -> Tenant | None:
        """Return the :class:`Tenant` for ``host`` or ``None`` to 404.

        ``host`` is the raw ``Host`` header value (lower-cased by
        the middleware before this call). Implementations strip any
        ``:port`` suffix as needed; the middleware doesn't.
        """
        ...


class InMemorySubdomainTenantRouter:
    """Reference :class:`SubdomainTenantRouter` for dev / test.

    Backed by a static ``host → Tenant`` dict. Lookup is exact
    match on the lower-cased host (with the port suffix stripped).
    Production adopters swap to a SQL-backed impl that hits their
    tenant table.
    """

    def __init__(self, tenants: Mapping[str, Tenant]) -> None:
        # Normalize keys to lower-cased + port-stripped at construction
        # so resolve() can be a single dict lookup. Adopters who pass
        # mixed case (``Acme.Example.com``) get the obvious behavior.
        self._tenants: dict[str, Tenant] = {
            _normalize_host(host): tenant for host, tenant in tenants.items()
        }

    async def resolve(self, host: str) -> Tenant | None:
        return self._tenants.get(_normalize_host(host))


# Type alias for adopter-supplied lookup callables. Either sync (returns
# Tenant | None) or async (returns Awaitable[Tenant | None]) is accepted —
# CallableSubdomainTenantRouter awaits at call time. Receives the
# already-normalized (lower-cased + port-stripped) host so adopters don't
# reimplement the parser.
TenantResolver = Callable[[str], "Tenant | None | Awaitable[Tenant | None]"]


class CallableSubdomainTenantRouter:
    """Adopter-callable :class:`SubdomainTenantRouter` for DB-backed lookups.

    The adopter passes a single callable mapping a normalized host to a
    :class:`Tenant` (or ``None`` for 404). The framework owns host
    normalization (lower-case + port-strip), so adopters write only the
    lookup itself — typically a single SQL query against their tenant
    table.

    The callable may be sync or async; the router awaits at call time.

    Example::

        from sqlalchemy import select
        from adcp.server import CallableSubdomainTenantRouter, Tenant

        async def lookup(host: str) -> Tenant | None:
            subdomain = host.split(".", 1)[0]  # 'acme.example.com' -> 'acme'
            async with my_db.session() as s:
                row = await s.scalar(
                    select(TenantRow).filter_by(subdomain=subdomain, is_active=True)
                )
            return Tenant(id=row.tenant_id, display_name=row.name) if row else None

        router = CallableSubdomainTenantRouter(lookup)

    Optional bounded TTL cache absorbs hot-path lookups without adopters
    reimplementing — useful when the resolver hits a remote DB on every
    request. Defaults to **no caching** (``cache_size=0``); adopters opt
    in with explicit bounds:

    ::

        router = CallableSubdomainTenantRouter(
            lookup,
            cache_size=1024,           # bounded LRU; never grows beyond this
            cache_ttl_seconds=60.0,    # expire entries after 60s
        )

    Cache bounds are mandatory when caching is enabled — there is no
    "cache forever, unbounded size" mode by design. Tenants come and go
    (suspension, deactivation); long-lived caches without TTL hand
    adopters a stale-cache footgun. The ``cache_ttl_seconds`` ceiling is
    the explicit knob.

    **Negative-cache + tenant onboarding race.** When caching is enabled,
    ``None`` results are cached too (to absorb probing for unknown hosts).
    This creates a race on tenant creation: if a probe for
    ``acme.example.com`` hits at T=0 (host doesn't exist yet) and the
    tenant is provisioned at T=1, the cached ``None`` causes 404s for up
    to ``cache_ttl_seconds`` afterward. Call ``invalidate(host)`` from
    your tenant *creation* path — not only deactivation — to clear the
    negative entry immediately::

        # on tenant create / re-activate
        router.invalidate("acme.example.com")

    Memory profile
    --------------
    Without caching: zero state held by the router. Each ``resolve()``
    call awaits the adopter callable directly.

    With caching: bounded by ``cache_size`` entries. Maximum memory is
    ``cache_size × (sizeof(host_str) + sizeof(your_Tenant) + 16)``
    where ``sizeof(your_Tenant)`` depends on what you store in
    :attr:`Tenant.ext` — the router can't predict it. The cache never
    grows beyond ``cache_size`` entries regardless of payload size.
    """

    def __init__(
        self,
        resolver: TenantResolver,
        *,
        cache_size: int = 0,
        cache_ttl_seconds: float = 0.0,
    ) -> None:
        """Construct the router.

        :param resolver: Callable taking a normalized host string and
            returning ``Tenant | None`` (sync or async). Receives
            already-normalized hosts — lower-cased with any
            ``:port`` suffix stripped.
        :param cache_size: Maximum number of cached lookups. ``0``
            disables caching entirely (the adopter callable is awaited
            on every request). Must be ``>= 0``.
        :param cache_ttl_seconds: Per-entry TTL in seconds. Must be
            ``> 0`` when ``cache_size > 0``. There is no "cache forever"
            mode — see the class docstring for rationale.
        :raises ValueError: If ``cache_size > 0`` and
            ``cache_ttl_seconds <= 0`` (cache requires explicit TTL).
        """
        if cache_size < 0:
            raise ValueError(f"cache_size must be >= 0, got {cache_size}")
        if cache_size > 0 and cache_ttl_seconds <= 0:
            raise ValueError(
                "cache_ttl_seconds must be > 0 when cache_size > 0; "
                "explicit TTL prevents stale-tenant footguns. Pass a "
                "value like 60.0 (one-minute cache) to opt in."
            )
        self._resolver = resolver
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl_seconds
        # OrderedDict gives us LRU-by-move-to-end for free; bounded by
        # popitem(last=False) when over cache_size. Each entry is
        # (Tenant | None, expires_at_monotonic). Negative results are
        # cached too so DOS-style probing doesn't bypass the cache.
        self._cache: OrderedDict[str, tuple[Tenant | None, float]] = OrderedDict()

    async def resolve(self, host: str) -> Tenant | None:
        normalized = _normalize_host(host)

        if self._cache_size > 0:
            cached = self._cache_get(normalized)
            if cached is not _CACHE_MISS:
                return cached  # type: ignore[return-value]

        result = self._resolver(normalized)
        if inspect.isawaitable(result):
            result = await result

        if self._cache_size > 0:
            self._cache_put(normalized, result)

        return result

    # ----- cache internals (request-path; keep tight) ---------------------

    def _cache_get(self, host: str) -> Tenant | None | object:
        entry = self._cache.get(host)
        if entry is None:
            return _CACHE_MISS
        tenant, expires_at = entry
        if time.monotonic() > expires_at:
            # Expired — drop and miss. Don't await a fresh resolve here;
            # the caller does that. Avoids holding the entry through the
            # adopter callable's network round-trip.
            self._cache.pop(host, None)
            return _CACHE_MISS
        # LRU touch
        self._cache.move_to_end(host)
        return tenant

    def _cache_put(self, host: str, tenant: Tenant | None) -> None:
        expires_at = time.monotonic() + self._cache_ttl
        self._cache[host] = (tenant, expires_at)
        self._cache.move_to_end(host)
        # Bound size — evict oldest until under limit.
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def invalidate(self, host: str | None = None) -> None:
        """Drop a cached entry (or all entries when ``host`` is ``None``).

        Adopters call this from their tenant-creation, -deactivation, and
        -modification flows to evict stale entries before the TTL fires.
        Creation matters because negative results (``None``) are cached —
        see the class docstring for details. Safe to call even when caching
        is disabled (no-op).

        :param host: Specific host to evict (raw or normalized — the
            method normalizes internally). ``None`` clears the entire
            cache.
        """
        if host is None:
            self._cache.clear()
            return
        self._cache.pop(_normalize_host(host), None)


# Sentinel for cache miss vs. cached-None (negative result)
_CACHE_MISS: object = object()


# Module-level contextvar — request-scoped via the ASGI middleware's
# per-call `set()`. ASGI guarantees per-task context isolation, so
# concurrent requests on the same process see only their own tenant.
_current_tenant: contextvars.ContextVar[Tenant | None] = contextvars.ContextVar(
    "adcp_current_tenant",
    default=None,
)


def current_tenant() -> Tenant | None:
    """Return the resolved :class:`Tenant` for the current request.

    Returns ``None`` outside the middleware's request scope, or
    when the request isn't tenant-routed (e.g., health-check paths
    excluded from the middleware).

    Adopter ``context_factory`` callbacks read this and write the
    tenant id onto :attr:`ToolContext.tenant_id` so downstream
    framework primitives (idempotency middleware, AccountStore,
    BuyerAgentRegistry) scope by tenant.
    """
    return _current_tenant.get()


class SubdomainTenantMiddleware:
    """Starlette ASGI middleware: ``Host`` header → :class:`Tenant`.

    Wire via ``app.add_middleware(SubdomainTenantMiddleware,
    router=...)``. The middleware:

    1. Reads the ``Host`` header from the ASGI scope.
    2. Calls the router's ``resolve()`` method.
    3. On hit, sets the :data:`current_tenant` contextvar for the
       remainder of the request's lifetime.
    4. On miss, returns ``404 Not Found`` immediately — the wrapped
       app is never called.

    Non-HTTP scopes (websocket, lifespan) pass through unchanged
    so the middleware is safe on the standard Starlette stack.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        router: SubdomainTenantRouter,
        excluded_paths: frozenset[str] = frozenset(),
    ) -> None:
        """Construct the middleware.

        :param app: The wrapped ASGI app (the next layer).
        :param router: The :class:`SubdomainTenantRouter` impl.
        :param excluded_paths: HTTP paths that bypass tenant routing
            entirely — typically ``{"/healthz", "/readyz"}``.
            Requests to these paths skip the router call and the
            contextvar set; downstream code sees
            :func:`current_tenant` returning ``None``.
        """
        self._app = app
        self._router = router
        self._excluded = excluded_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._excluded:
            await self._app(scope, receive, send)
            return

        host = _extract_host_header(scope)
        if host is None:
            await _send_404(send, reason="missing-host-header")
            return

        tenant = await self._router.resolve(host)
        if tenant is None:
            await _send_404(send, reason="unknown-host")
            return

        token = _current_tenant.set(tenant)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_tenant.reset(token)


# ----- helpers -----------------------------------------------------------


def _normalize_host(host: str) -> str:
    """Lower-case and strip ``:port`` suffix.

    The ``Host`` header is case-insensitive per RFC 7230, but a
    case-sensitive dict lookup would miss legitimate variations.
    Also strips the port suffix so ``acme.example.com:443`` resolves
    the same as ``acme.example.com``.
    """
    normalized = host.strip().lower()
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0]
    return normalized


def _extract_host_header(scope: Scope) -> str | None:
    """Pull the ``Host`` header from an ASGI scope.

    ASGI normalizes header names to lower-cased bytes and stores
    them as a list of ``(name, value)`` tuples on ``scope["headers"]``.
    """
    headers = scope.get("headers") or []
    for name, value in headers:
        if name == b"host":
            decoded: str = bytes(value).decode("latin-1")
            return decoded
    return None


async def _send_404(send: Send, *, reason: str) -> None:
    """Emit a minimal ``404 Not Found`` response.

    No body — buyers probing for tenant existence get the same
    response shape regardless of why the host was rejected. The
    ``X-Adcp-Tenant-Reject-Reason`` header carries an opaque
    diagnostic for adopter logs / observability without leaking
    tenant-existence info to the buyer.
    """
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-length", b"0"),
                (b"x-adcp-tenant-reject-reason", reason.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b""})


__all__ = [
    "CallableSubdomainTenantRouter",
    "InMemorySubdomainTenantRouter",
    "SubdomainTenantMiddleware",
    "SubdomainTenantRouter",
    "Tenant",
    "TenantResolver",
    "current_tenant",
]
