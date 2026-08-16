"""Tests for :mod:`adcp.server.tenant_router`.

Covers the ASGI middleware integration via Starlette's
:class:`TestClient`:

* Known host → 200 + tenant available via :func:`current_tenant`
* Unknown host → 404 with reject-reason header
* Missing Host header → 404
* Port suffix is stripped before router lookup
* Case-insensitive host match
* Excluded paths bypass routing entirely
* Non-HTTP scopes pass through (websocket / lifespan)
* ContextVar is reset after each request (no cross-request leak)
"""

from __future__ import annotations

import asyncio

import pytest

starlette = pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from adcp.server import (  # noqa: E402
    CallableSubdomainTenantRouter,
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    SubdomainTenantRouter,
    Tenant,
    current_tenant,
)
from adcp.server.tenant_router import normalize_host_key  # noqa: E402

# ----- handler that surfaces the resolved tenant ------------------------


def _build_app(router: SubdomainTenantRouter, **mw_kwargs) -> Starlette:
    async def whoami(request: Request) -> JSONResponse:
        tenant = current_tenant()
        return JSONResponse(
            {
                "tenant_id": tenant.id if tenant else None,
                "display_name": tenant.display_name if tenant else None,
            }
        )

    app = Starlette(routes=[Route("/whoami", whoami), Route("/healthz", whoami)])
    app.add_middleware(SubdomainTenantMiddleware, router=router, **mw_kwargs)
    return app


# ----- Tenant dataclass ------------------------------------------------


def test_tenant_is_frozen() -> None:
    """Tenant is frozen — middleware caches resolved tenants in a
    contextvar that downstream stores read; mutation in-place would
    break per-request isolation."""
    t = Tenant(id="acme", display_name="Acme")
    with pytest.raises((AttributeError, Exception)):
        t.id = "beta"  # type: ignore[misc]


def test_tenant_carries_ext_passthrough() -> None:
    t = Tenant(id="acme", display_name="Acme", ext={"db_shard": "us-east"})
    assert t.ext == {"db_shard": "us-east"}


# ----- InMemorySubdomainTenantRouter ----------------------------------


def test_in_memory_router_resolves_known_host() -> None:
    router = InMemorySubdomainTenantRouter(
        tenants={"acme.example.com": Tenant(id="acme", display_name="Acme")}
    )
    result = asyncio.run(router.resolve("acme.example.com"))
    assert result is not None
    assert result.id == "acme"


def test_in_memory_router_returns_none_for_unknown_host() -> None:
    router = InMemorySubdomainTenantRouter(tenants={})
    result = asyncio.run(router.resolve("unknown.example.com"))
    assert result is None


def test_in_memory_router_normalizes_case_insensitive() -> None:
    """Host header is case-insensitive per RFC 7230 — adopter passing
    mixed case at construction OR receiving mixed case at request
    time both resolve correctly."""
    router = InMemorySubdomainTenantRouter(
        tenants={"Acme.Example.com": Tenant(id="acme", display_name="Acme")}
    )
    result = asyncio.run(router.resolve("ACME.example.COM"))
    assert result is not None
    assert result.id == "acme"


def test_in_memory_router_strips_port_suffix() -> None:
    router = InMemorySubdomainTenantRouter(
        tenants={"acme.example.com": Tenant(id="acme", display_name="Acme")}
    )
    result = asyncio.run(router.resolve("acme.example.com:8080"))
    assert result is not None
    assert result.id == "acme"


def test_in_memory_router_resolves_ipv6_literal_host() -> None:
    """A bracketed IPv6 Host header resolves to its own tenant only.

    The old first-colon split collapsed any host whose first colon
    follows ``[`` to the key ``'['``, so an unrelated IPv6 literal
    matched the loopback tenant instead of 404ing.
    """
    router = InMemorySubdomainTenantRouter(
        tenants={"[::1]": Tenant(id="loopback", display_name="Loopback")}
    )
    result = asyncio.run(router.resolve("[::1]:8080"))
    assert result is not None
    assert result.id == "loopback"

    # Different address, no tenant registered for it -> must 404.
    assert asyncio.run(router.resolve("[::2]")) is None
    assert asyncio.run(router.resolve("[2001:db8::1]")) is None


def test_in_memory_router_distinct_ipv6_tenants_do_not_collide() -> None:
    """Two IPv6 tenants must occupy two registration keys, not one.

    Registration keys are normalized at construction, so a normalizer
    that truncates at the first colon merges every IPv6 tenant into a
    single dict slot — last write wins and one tenant's host resolves
    to the other tenant.
    """
    router = InMemorySubdomainTenantRouter(
        tenants={
            "[::1]": Tenant(id="loopback", display_name="Loopback"),
            "[::2]": Tenant(id="other", display_name="Other"),
        }
    )
    assert len(router._tenants) == 2

    loopback = asyncio.run(router.resolve("[::1]"))
    assert loopback is not None
    assert loopback.id == "loopback"

    other = asyncio.run(router.resolve("[::2]"))
    assert other is not None
    assert other.id == "other"


def test_in_memory_router_satisfies_protocol() -> None:
    router = InMemorySubdomainTenantRouter(tenants={})
    assert isinstance(router, SubdomainTenantRouter)


# ----- CallableSubdomainTenantRouter ---------------------------------------


def test_callable_router_passes_normalized_host_to_resolver() -> None:
    """Adopter callable receives the lower-cased + port-stripped host."""
    received: list[str] = []

    async def lookup(host: str) -> Tenant | None:
        received.append(host)
        return Tenant(id="acme", display_name="Acme") if host == "acme.example.com" else None

    router = CallableSubdomainTenantRouter(lookup)
    result = asyncio.run(router.resolve("ACME.Example.COM:8080"))

    assert received == ["acme.example.com"]
    assert result is not None
    assert result.id == "acme"


def test_callable_router_supports_sync_callables() -> None:
    """Adopter may pass a plain sync function — no `async def` required."""

    def lookup(host: str) -> Tenant | None:
        return Tenant(id="acme") if host == "acme.example.com" else None

    router = CallableSubdomainTenantRouter(lookup)
    result = asyncio.run(router.resolve("acme.example.com"))
    assert result is not None
    assert result.id == "acme"


def test_callable_router_returns_none_for_unknown_host() -> None:
    async def lookup(host: str) -> Tenant | None:
        return None

    router = CallableSubdomainTenantRouter(lookup)
    assert asyncio.run(router.resolve("unknown.example.com")) is None


def test_callable_router_satisfies_protocol() -> None:
    async def lookup(host: str) -> Tenant | None:
        return None

    router = CallableSubdomainTenantRouter(lookup)
    assert isinstance(router, SubdomainTenantRouter)


def test_callable_router_default_no_caching() -> None:
    """Default ``cache_size=0`` — every resolve calls the resolver."""
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return Tenant(id="acme")

    router = CallableSubdomainTenantRouter(lookup)
    asyncio.run(router.resolve("acme.example.com"))
    asyncio.run(router.resolve("acme.example.com"))
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 3


def test_callable_router_caching_dedupes_within_ttl() -> None:
    """Within ``cache_ttl_seconds`` the resolver is only called once per host."""
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return Tenant(id="acme")

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("acme.example.com"))
    asyncio.run(router.resolve("acme.example.com"))
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 1


def test_callable_router_caching_negative_results_too() -> None:
    """Cached ``None`` is honored — DOS-style probing for unknown hosts
    doesn't bypass the cache."""
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return None

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("attacker.example.com"))
    asyncio.run(router.resolve("attacker.example.com"))
    assert call_count == 1


def test_callable_router_caching_evicts_after_ttl(monkeypatch) -> None:
    """Entries older than ``cache_ttl_seconds`` re-query the resolver."""
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return Tenant(id="acme")

    fake_clock = [1000.0]

    def fake_monotonic() -> float:
        return fake_clock[0]

    monkeypatch.setattr("adcp.server.tenant_router.time.monotonic", fake_monotonic)

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("acme.example.com"))
    fake_clock[0] += 30  # within TTL
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 1

    fake_clock[0] += 31  # past TTL (61s total)
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 2


def test_callable_router_cache_bounded_by_size() -> None:
    """``cache_size`` is a hard ceiling — oldest entries evicted on overflow."""

    def lookup(host: str) -> Tenant | None:
        return Tenant(id=host.split(".")[0])

    router = CallableSubdomainTenantRouter(lookup, cache_size=2, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("a.example.com"))
    asyncio.run(router.resolve("b.example.com"))
    asyncio.run(router.resolve("c.example.com"))  # evicts 'a'
    # Cache still bounded — never grows beyond cache_size
    assert len(router._cache) == 2  # noqa: SLF001 — testing bound directly
    assert "a.example.com" not in router._cache
    assert "b.example.com" in router._cache
    assert "c.example.com" in router._cache


def test_callable_router_invalidate_specific_host() -> None:
    """``invalidate(host)`` drops a cached entry; next call re-queries."""
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return Tenant(id="acme")

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("acme.example.com"))
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 1

    router.invalidate("ACME.Example.COM:8080")  # any-case + port form works
    asyncio.run(router.resolve("acme.example.com"))
    assert call_count == 2


def test_callable_router_invalidate_all() -> None:
    """``invalidate()`` with no arg clears every entry."""

    def lookup(host: str) -> Tenant | None:
        return Tenant(id=host.split(".")[0])

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("a.example.com"))
    asyncio.run(router.resolve("b.example.com"))
    assert len(router._cache) == 2  # noqa: SLF001

    router.invalidate()
    assert len(router._cache) == 0  # noqa: SLF001


def test_callable_router_invalidate_no_op_without_caching() -> None:
    """Invalidating a router with caching disabled is a safe no-op."""

    async def lookup(host: str) -> Tenant | None:
        return None

    router = CallableSubdomainTenantRouter(lookup)  # cache_size=0
    router.invalidate("anything.example.com")
    router.invalidate()
    # No exception — cache stays empty
    assert len(router._cache) == 0  # noqa: SLF001


def test_callable_router_case_and_port_variants_share_cache_entry() -> None:
    """Case variants and port suffix all normalize to the same cache key.

    ``Acme.localhost:3001`` and ``acme.localhost`` must hit the resolver
    exactly once — a second probe after the cache is warm must not call
    the resolver again, regardless of how the host was presented.
    """
    call_count = 0

    async def lookup(host: str) -> Tenant | None:
        nonlocal call_count
        call_count += 1
        return Tenant(id="acme")

    router = CallableSubdomainTenantRouter(lookup, cache_size=8, cache_ttl_seconds=60.0)
    asyncio.run(router.resolve("Acme.localhost:3001"))
    asyncio.run(router.resolve("acme.localhost"))
    assert call_count == 1


def test_callable_router_rejects_cache_without_ttl() -> None:
    """Cache requires explicit TTL — no 'cache forever' mode."""
    with pytest.raises(ValueError, match="TTL"):
        CallableSubdomainTenantRouter(
            lambda host: None,
            cache_size=8,
            # cache_ttl_seconds defaults to 0 — invalid when caching enabled
        )


def test_callable_router_rejects_negative_cache_size() -> None:
    with pytest.raises(ValueError, match="cache_size"):
        CallableSubdomainTenantRouter(lambda host: None, cache_size=-1)


def test_callable_router_through_middleware() -> None:
    """End-to-end: callable router behind the standard middleware."""

    async def lookup(host: str) -> Tenant | None:
        if host == "acme.example.com":
            return Tenant(id="acme", display_name="Acme")
        return None

    router = CallableSubdomainTenantRouter(lookup)
    client = TestClient(_build_app(router))

    resp = client.get("/whoami", headers={"Host": "acme.example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": "acme", "display_name": "Acme"}

    resp = client.get("/whoami", headers={"Host": "unknown.example.com"})
    assert resp.status_code == 404


# ----- middleware: known host happy path ------------------------------


def test_middleware_resolves_known_host() -> None:
    router = InMemorySubdomainTenantRouter(
        tenants={
            "acme.example.com": Tenant(id="acme", display_name="Acme"),
            "beta.example.com": Tenant(id="beta", display_name="Beta"),
        }
    )
    client = TestClient(_build_app(router))

    resp = client.get("/whoami", headers={"Host": "acme.example.com"})
    assert resp.status_code == 200
    assert resp.json() == {"tenant_id": "acme", "display_name": "Acme"}


def test_middleware_per_request_tenant_isolation() -> None:
    """Sequential requests on different hosts each see only their
    own tenant — the contextvar resets between requests."""
    router = InMemorySubdomainTenantRouter(
        tenants={
            "acme.example.com": Tenant(id="acme", display_name="Acme"),
            "beta.example.com": Tenant(id="beta", display_name="Beta"),
        }
    )
    client = TestClient(_build_app(router))

    r1 = client.get("/whoami", headers={"Host": "acme.example.com"})
    r2 = client.get("/whoami", headers={"Host": "beta.example.com"})
    assert r1.json()["tenant_id"] == "acme"
    assert r2.json()["tenant_id"] == "beta"


def test_middleware_clears_contextvar_after_request() -> None:
    """Outside the request scope, ``current_tenant()`` returns None
    even after the middleware resolved a tenant for a request."""
    router = InMemorySubdomainTenantRouter(
        tenants={"acme.example.com": Tenant(id="acme", display_name="Acme")}
    )
    client = TestClient(_build_app(router))
    client.get("/whoami", headers={"Host": "acme.example.com"})
    assert current_tenant() is None


# ----- middleware: unknown host / missing host → 404 ------------------


def test_middleware_404s_unknown_host() -> None:
    router = InMemorySubdomainTenantRouter(tenants={})
    client = TestClient(_build_app(router))

    resp = client.get("/whoami", headers={"Host": "stranger.example.com"})
    assert resp.status_code == 404
    assert resp.headers.get("x-adcp-tenant-reject-reason") == "unknown-host"
    assert resp.content == b""  # no body — same shape as missing-host


def test_middleware_404s_missing_host_header() -> None:
    """Lower-level test: an ASGI scope with no Host header returns
    404. Buyer probing for tenant existence can't distinguish missing
    vs unknown."""
    router = InMemorySubdomainTenantRouter(tenants={})
    middleware = SubdomainTenantMiddleware(_async_pass_through, router=router)

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/whoami",
        "headers": [],  # No Host header.
    }
    asyncio.run(middleware(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 404
    headers = dict(start["headers"])
    assert headers[b"x-adcp-tenant-reject-reason"] == b"missing-host-header"


# ----- middleware: excluded paths -------------------------------------


def test_middleware_bypasses_excluded_paths() -> None:
    """Health-check endpoints typically don't carry a tenant Host —
    the middleware skips routing for paths in ``excluded_paths``."""
    router = InMemorySubdomainTenantRouter(tenants={})  # no tenants
    client = TestClient(_build_app(router, excluded_paths=frozenset({"/healthz"})))

    # Excluded path with unknown host still 200s — bypass.
    resp = client.get("/healthz", headers={"Host": "stranger.example.com"})
    assert resp.status_code == 200
    # Tenant contextvar wasn't set for the bypass.
    assert resp.json()["tenant_id"] is None


def test_middleware_routes_non_excluded_paths_normally() -> None:
    """Same router, same handler — non-excluded paths still 404
    on unknown host."""
    router = InMemorySubdomainTenantRouter(tenants={})
    client = TestClient(_build_app(router, excluded_paths=frozenset({"/healthz"})))

    resp = client.get("/whoami", headers={"Host": "stranger.example.com"})
    assert resp.status_code == 404


# ----- middleware: non-HTTP scopes pass through -----------------------


def test_middleware_passes_websocket_scope_through() -> None:
    """ASGI middleware must be safe on non-HTTP scopes (websocket,
    lifespan) — the router never gets called."""
    sentinel: list[str] = []

    async def app(scope, receive, send):
        sentinel.append(scope["type"])

    class _BombRouter:
        async def resolve(self, host: str) -> Tenant | None:
            raise AssertionError("router must not be called on non-HTTP scope")

    middleware = SubdomainTenantMiddleware(app, router=_BombRouter())

    async def noop_receive():
        return {"type": "websocket.connect"}

    async def noop_send(_message):
        pass

    asyncio.run(middleware({"type": "websocket"}, noop_receive, noop_send))
    asyncio.run(middleware({"type": "lifespan"}, noop_receive, noop_send))

    assert sentinel == ["websocket", "lifespan"]


# ----- normalize_host_key ---------------------------------------------


def test_normalize_host_key_never_raises_on_hostile_input() -> None:
    """The lookup-key helper must fail soft on any Host header value.

    The Host header is attacker-controlled and reaches this helper
    before any tenant is resolved. A raise here would turn today's
    404 into a 500, so every hostile shape must return a string.
    Deliberately no try/except: a raise fails the test with the
    exception itself.
    """
    hostile = [
        "under_score.example.com",
        "a" * 100 + ".example.com",
        "",
        " ",
        "[::1",
        "]::1[",
        "acme.example.com:abc",
        "%00.example.com",
        "http://",
        "@",
    ]
    for value in hostile:
        assert isinstance(normalize_host_key(value), str)


def test_normalize_host_key_folds_trailing_root_dot() -> None:
    """``acme.example.com.`` and ``acme.example.com`` are one tenant."""
    assert normalize_host_key("acme.example.com.") == "acme.example.com"


# ----- helpers --------------------------------------------------------


async def _async_pass_through(scope, receive, send) -> None:
    """Inert ASGI app for tests that exercise the middleware in
    isolation. Never reached when the middleware short-circuits."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.parametrize(
    ("with_port", "bare"),
    [
        ("[2001:DB8::0:1]:443", "2001:db8::0:1"),
        ("[::1]:8080", "::1"),
        ("[2001:db8:0:0:0:0:0:1]:9000", "2001:db8::1"),
    ],
    ids=["uncompressed-upper", "loopback", "fully-expanded"],
)
def test_non_canonical_ipv6_with_port_keys_the_same_as_the_bare_form(
    with_port: str, bare: str
) -> None:
    """A port must not change which tenant an IPv6 host resolves to.

    The bare-literal short-circuit only sees the raw input. Once a port is
    attached, ``urlsplit`` is what removes the brackets, and the address
    arrived downstream uncompressed -- so ``[2001:DB8::0:1]:443`` keyed to
    ``2001:db8::0:1`` while the bare form keyed to ``2001:db8::1``. A tenant
    registered under one was unreachable from the other, and the function was
    not idempotent over its own output.

    Idempotency is load-bearing rather than tidy: ``InMemorySubdomainTenantRouter``
    normalizes registration keys at construction and normalizes the Host again
    at lookup, so a non-idempotent key silently fails to match itself.
    """
    key = normalize_host_key(with_port)
    assert key == normalize_host_key(bare)
    assert normalize_host_key(key) == key, "normalize_host_key must be idempotent"


def test_ipv6_tenant_is_reachable_with_and_without_a_port() -> None:
    """The end-to-end consequence: same tenant, either Host spelling."""
    router = InMemorySubdomainTenantRouter(
        tenants={"[2001:DB8::0:1]": Tenant(id="v6", display_name="IPv6 Tenant")}
    )
    for host in ("[2001:DB8::0:1]", "[2001:db8::1]:443", "2001:db8::0:1"):
        resolved = asyncio.run(router.resolve(host))
        assert resolved is not None and resolved.id == "v6", f"unreachable via {host!r}"
