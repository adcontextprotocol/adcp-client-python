"""Explicit unauthenticated HTTP routes with Starlette matching semantics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import Match, Mount, Route, Router
from starlette.types import ASGIApp, Receive, Scope, Send

OPERATIONAL_ROUTE_SCOPE_KEY = "adcp.operational_route"
# Reserve entire protocol/discovery namespaces, and the A2A RPC root.
_RESERVED = ("/mcp", "/sse", "/messages", "/.well-known")


class OperationalRoutes:
    """Construct and validate the router once, outside request dispatch."""

    def __init__(self, routes: Sequence[Route | Mount]) -> None:
        for route in routes:
            if not isinstance(route, (Route, Mount)):
                raise TypeError("unauthenticated_routes accepts only Starlette Route or Mount")
            path = route.path.rstrip("/") or "/"
            if (
                path == "/"
                or "{" in path.split("/")[1]
                or any(
                    path == reserved
                    or path.startswith(reserved + "/")
                    or (isinstance(route, Mount) and reserved.startswith(path + "/"))
                    for reserved in _RESERVED
                )
            ):
                raise ValueError(
                    f"Operational route {route.path!r} collides with protocol/discovery"
                )
            # Detect dynamic routes that can match a reserved path as well.
            for reserved in (
                "/",
                *_RESERVED,
                "/.well-known/agent.json",
                "/.well-known/agent-card.json",
                "/.well-known/adcp-agents.json",
            ):
                scope: Scope = {"type": "http", "path": reserved, "root_path": "", "method": "GET"}
                if route.matches(scope)[0] != Match.NONE:
                    raise ValueError(
                        f"Operational route {route.path!r} collides with protocol/discovery"
                    )
        self.router = Router(routes=list(routes))
        self.app = ExceptionMiddleware(self.router)

    def validate_protocol_paths(self, paths: Sequence[str]) -> None:
        """Reject collisions with public builders' custom MCP/SSE paths."""
        for route in self.router.routes:
            assert isinstance(route, (Route, Mount))
            prefix = route.path.rstrip("/") or "/"
            for protocol_path in paths:
                path = protocol_path.rstrip("/") or "/"
                scope: Scope = {"type": "http", "path": path, "root_path": "", "method": "GET"}
                if (
                    prefix == path
                    or prefix.startswith(path + "/")
                    or (isinstance(route, Mount) and path.startswith(prefix + "/"))
                    or route.matches(scope)[0] != Match.NONE
                    or route.matches({**scope, "path": path + "/"})[0] != Match.NONE
                ):
                    raise ValueError(
                        f"Operational route {route.path!r} collides with protocol/discovery"
                    )

    def handles(self, scope: Scope) -> bool:
        if scope["type"] != "http":
            return False
        if any(route.matches(scope)[0] != Match.NONE for route in self.router.routes):
            return True
        # Delegate Starlette's slash redirect to the operational router, so
        # a bare Mount prefix cannot fall into protocol authentication.
        alternate = dict(scope)
        path = scope.get("path", "")
        alternate["path"] = path.rstrip("/") if path.endswith("/") else path + "/"
        return any(route.matches(alternate)[0] != Match.NONE for route in self.router.routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def prepare_operational_routes(
    routes: Sequence[Route | Mount] | None,
) -> OperationalRoutes | None:
    return OperationalRoutes(routes) if routes else None


class OperationalRoutesMiddleware:
    def __init__(self, app: ASGIApp, *, routes: OperationalRoutes) -> None:
        self.app = app
        self.operational_routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.operational_routes.handles(scope):
            await self.operational_routes(scope, receive, send)
            return
        await self.app(scope, receive, send)


class OperationalRouteMarker:
    """Mark known operational paths before operator middleware/tenant routing."""

    def __init__(self, app: ASGIApp, routes: OperationalRoutes) -> None:
        self.app = app
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.routes.handles(scope):
            scope = dict(scope)
            scope[OPERATIONAL_ROUTE_SCOPE_KEY] = True
        await self.app(scope, receive, send)


def find_operational_routes(app: Any) -> OperationalRoutes | None:
    while app is not None:
        if isinstance(app, OperationalRoutesMiddleware):
            return app.operational_routes
        state = getattr(app, "state", None)
        routes = getattr(state, "adcp_operational_routes", None)
        if isinstance(routes, OperationalRoutes):
            return routes
        app = getattr(app, "app", None)
    return None


def wrap_operational_routes(app: Any, routes: Sequence[Route | Mount] | None) -> Any:
    prepared = prepare_operational_routes(routes)
    return OperationalRoutesMiddleware(app, routes=prepared) if prepared is not None else app
