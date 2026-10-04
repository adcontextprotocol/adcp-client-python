"""Typed operational Route/Mount configuration on all public builders."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from adcp.decisioning import DecisioningPlatform
from adcp.server import (
    ADCPHandler,
    ServeConfig,
    ToolContext,
    create_a2a_server,
    create_mcp_server,
    serve,
)
from adcp.testing import build_asgi_app, build_test_client


async def alive(request: Request) -> Response:
    return JSONResponse({"status": "alive"})


def configure(handler: ADCPHandler[ToolContext], platform: DecisioningPlatform) -> None:
    routes: list[Route | Mount] = [Route("/healthz", alive), Mount("/manage", app=Starlette())]
    serve(handler, config=ServeConfig(transport="both", unauthenticated_routes=routes))
    create_mcp_server(handler, unauthenticated_routes=routes)
    create_a2a_server(handler, unauthenticated_routes=routes)
    build_asgi_app(platform, transport="a2a", unauthenticated_routes=routes)


async def check(platform: DecisioningPlatform) -> None:
    async with build_test_client(
        platform, transport="both", unauthenticated_routes=[Route("/healthz", alive)]
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
