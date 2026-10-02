"""Explicit operational route dispatch, forwarding, tenancy and collisions."""

import importlib
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ServeConfig, create_a2a_server, create_mcp_server
from adcp.server.auth import BearerTokenAuth
from adcp.server.operational_routes import OperationalRoutes
from adcp.server.serve import _apply_asgi_middleware, _build_a2a_app, _build_mcp_and_a2a_app
from adcp.server.tenant_router import (
    InMemorySubdomainTenantRouter,
    SubdomainTenantMiddleware,
    Tenant,
    current_tenant,
)


class Seller(ADCPHandler):
    advertised_tools = {"get_products"}

    async def get_products(self, params, context=None):
        return {"products": []}


async def health(request):
    assert current_tenant() is None
    return JSONResponse({"status": "alive"})


async def mounted(request):
    return JSONResponse({"path": request.url.path})


def routes():
    return [
        Route("/healthz", health, methods=["GET"]),
        Mount(
            "/manage", app=Starlette(routes=[Route("/", mounted), Route("/{name:path}", mounted)])
        ),
    ]


def build(transport: str, operational, **kwargs: Any):
    if transport == "direct-mcp":
        return create_mcp_server(
            Seller(), validation=None, unauthenticated_routes=operational
        ).streamable_http_app()
    if transport == "direct-a2a":
        return create_a2a_server(Seller(), validation=None, unauthenticated_routes=operational)
    if transport == "a2a":
        return _build_a2a_app(
            Seller(),
            name="ops",
            port=3001,
            test_controller=None,
            validation=None,
            unauthenticated_routes=operational,
            **kwargs,
        )
    return _build_mcp_and_a2a_app(
        Seller(),
        name="ops",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        unauthenticated_routes=operational,
        **kwargs,
    )


@pytest.mark.parametrize("transport", ["a2a", "both", "direct-mcp", "direct-a2a"])
def test_operational_methods_mounts_and_auth(transport):
    kwargs = (
        {}
        if transport.startswith("direct-")
        else {
            "auth": BearerTokenAuth(validate_token=lambda token: None),
        }
    )
    with TestClient(
        build(transport, routes(), **kwargs), base_url="http://localhost:3001"
    ) as client:
        assert client.get("/healthz").json() == {"status": "alive"}
        assert client.head("/healthz").status_code == 200
        assert client.post("/healthz").status_code == 405
        assert client.get("/healthz/", follow_redirects=False).status_code == 307
        assert client.get("/manage", follow_redirects=False).status_code == 307
        assert client.get("/manage/sub/path").json() == {"path": "/manage/sub/path"}
        assert client.get("/management").status_code == (
            404 if transport.startswith("direct-") else 401
        )
        if not transport.startswith("direct-"):
            assert client.post("/", json={}).status_code == 401
        if transport != "direct-mcp":
            assert client.get("/.well-known/agent-card.json").status_code == 200


@pytest.mark.parametrize("transport", ["a2a", "both", "direct-mcp", "direct-a2a"])
def test_tenant_exclusions_and_tracing(transport):
    seen = []

    class Trace:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                seen.append(scope["path"])
            await self.app(scope, receive, send)

    app = build(transport, routes())
    tenants = InMemorySubdomainTenantRouter({"tenant.example": Tenant(id="tenant")})
    app = _apply_asgi_middleware(
        app, [(SubdomainTenantMiddleware, {"router": tenants}), (Trace, {})]
    )
    with TestClient(app, base_url="http://localhost:3001") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/manage").status_code == 200
        assert client.get("/manage/x").status_code == 200
        assert client.post("/healthz").status_code == 405
        assert client.get("/management").status_code == 404
    assert "/healthz" in seen
    assert "/manage/x" in seen


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/mcp",
        "/mcp/",
        "/mcp/admin",
        "/sse",
        "/messages/x",
        "/.well-known",
        "/.well-known/agent-card.json",
        "/{name:path}",
    ],
)
@pytest.mark.parametrize("mount", [False, True])
def test_protocol_discovery_collisions_fail_at_construction(path, mount):
    route = Mount(path, app=Starlette()) if mount else Route(path, health)
    with pytest.raises(ValueError, match="collides"):
        OperationalRoutes([route])


def test_same_path_different_methods_and_router_constructed_once(monkeypatch):
    module = importlib.import_module("adcp.server.operational_routes")
    original = module.Router
    created = []

    def factory(*args, **kwargs):
        router = original(*args, **kwargs)
        created.append(router)
        return router

    monkeypatch.setattr(module, "Router", factory)
    app = build(
        "both",
        [Route("/healthz", health, methods=["GET"]), Route("/healthz", mounted, methods=["POST"])],
    )
    assert len(created) == 1
    with TestClient(app, base_url="http://localhost:3001") as client:
        assert client.get("/healthz").json() == {"status": "alive"}
        assert client.post("/healthz").json() == {"path": "/healthz"}
        assert client.delete("/healthz").status_code == 405
    assert len(created) == 1


def test_malformed_route_rejected():
    with pytest.raises(TypeError, match="Route or Mount"):
        OperationalRoutes([object()])


@pytest.mark.parametrize("transport", ["streamable-http", "a2a", "both"])
def test_serve_config_forwards_operational_routes(monkeypatch, transport):
    module = importlib.import_module("adcp.server.serve")
    captured = []
    name = {"streamable-http": "_serve_mcp", "a2a": "_serve_a2a", "both": "_serve_mcp_and_a2a"}[
        transport
    ]
    monkeypatch.setattr(module, name, lambda *args, **kwargs: captured.append(kwargs))
    operational = routes()
    module.serve(
        Seller(), config=ServeConfig(transport=transport, unauthenticated_routes=operational)
    )
    assert captured[0]["unauthenticated_routes"] is operational


@pytest.mark.parametrize("transport", ["mcp", "a2a", "both"])
@pytest.mark.asyncio
async def test_public_testing_helpers_forward_routes(transport):
    from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
    from adcp.testing import build_asgi_app, build_test_client

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guaranteed"], supported_billing=("operator",)
        )
        accounts = SingletonAccounts(account_id="hello")

        def get_products(self, req, ctx):
            return {"products": []}

        def create_media_buy(self, req, ctx):
            raise NotImplementedError

        def update_media_buy(self, mid, patch, ctx):
            raise NotImplementedError

        def sync_creatives(self, req, ctx):
            raise NotImplementedError

        def get_media_buy_delivery(self, req, ctx):
            raise NotImplementedError

        def get_media_buys(self, req, ctx):
            raise NotImplementedError

    app = build_asgi_app(
        Platform(),
        transport=transport,
        allowed_hosts=["test"],
        validate_at_init=False,
        auth=BearerTokenAuth(validate_token=lambda token: None),
        unauthenticated_routes=routes(),
    )
    with TestClient(app, base_url="http://test") as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/manage/nested").status_code == 200
    async with build_test_client(
        Platform(),
        transport=transport,
        auth=BearerTokenAuth(validate_token=lambda token: None),
        unauthenticated_routes=routes(),
    ) as client:
        assert (await client.get("/healthz")).status_code == 200


def test_public_mcp_http_builders_override_operational_routes():
    mcp = create_mcp_server(
        Seller(), validation=None, unauthenticated_routes=[Route("/old", health)]
    )
    for app in [
        mcp.streamable_http_app(unauthenticated_routes=routes()),
        mcp.sse_app(unauthenticated_routes=routes()),
    ]:
        with TestClient(app, base_url="http://localhost:3001") as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/old").status_code == 404


@pytest.mark.parametrize("builder", ["streamable", "sse", "messages"])
@pytest.mark.parametrize(
    "operational", [Route("/healthz", health), Mount("/manage", app=Starlette())]
)
def test_custom_protocol_paths_cannot_collide_with_operations(builder, operational):
    mcp = create_mcp_server(Seller(), validation=None, unauthenticated_routes=[operational])
    path = "/healthz" if isinstance(operational, Route) else "/manage/protocol"
    with pytest.raises(ValueError, match="collides"):
        if builder == "streamable":
            mcp.streamable_http_app(streamable_http_path=path)
        elif builder == "sse":
            mcp.sse_app(sse_path=path)
        else:
            mcp.sse_app(message_path=path)
