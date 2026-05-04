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
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    SubdomainTenantRouter,
    Tenant,
    current_tenant,
)

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


def test_in_memory_router_satisfies_protocol() -> None:
    router = InMemorySubdomainTenantRouter(tenants={})
    assert isinstance(router, SubdomainTenantRouter)


def test_in_memory_router_hosts_returns_normalized_keys() -> None:
    """hosts() returns the normalized (lower-cased, port-stripped) keys so
    serve() can synthesize the FastMCP allowlist from the same source."""
    router = InMemorySubdomainTenantRouter(
        tenants={
            "Acme.Localhost": Tenant(id="acme", display_name="Acme"),
            "beta.localhost:8080": Tenant(id="beta", display_name="Beta"),
        }
    )
    result = sorted(router.hosts())
    assert result == ["acme.localhost", "beta.localhost"]


def test_in_memory_router_hosts_empty() -> None:
    router = InMemorySubdomainTenantRouter(tenants={})
    assert router.hosts() == []


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


# ----- helpers --------------------------------------------------------


async def _async_pass_through(scope, receive, send) -> None:
    """Inert ASGI app for tests that exercise the middleware in
    isolation. Never reached when the middleware short-circuits."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})
