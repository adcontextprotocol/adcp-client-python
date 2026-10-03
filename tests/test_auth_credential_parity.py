"""Authentication carrier policy is identical on the MCP and A2A boundaries."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.server.auth import BearerTokenAuth, Principal, current_principal
from adcp.server.serve import _wrap_a2a_with_auth, _wrap_mcp_with_auth


def middleware_app(leg: str, config: BearerTokenAuth) -> Any:
    async def endpoint(request: Request) -> JSONResponse:
        await asyncio.sleep(0)
        return JSONResponse({"principal": current_principal.get(), "body": await request.json()})

    inner = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    return (_wrap_mcp_with_auth if leg == "mcp" else _wrap_a2a_with_auth)(inner, config)


CALL = {"method": "tools/call", "params": {"name": "get_products"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("mode", ["required", "anonymous", "discovery"])
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize(
    "headers,status",
    [
        ([("Authorization", "Bearer secret-a")], 200),
        ([("x-legacy", "secret-a")], 200),
        ([("Authorization", "Bearer secret-a"), ("x-legacy", "secret-a")], 200),
        ([("Authorization", "Bearer secret-a"), ("Authorization", "bearer\tsecret-a")], 200),
        ([("x-legacy", "secret-a"), ("x-legacy", " secret-a ")], 200),
        ([("Authorization", "Bearer secret-a"), ("x-legacy", "secret-b")], 401),
        ([("Authorization", "Bearer secret-a"), ("Authorization", "Bearer secret-b")], 401),
        ([("x-legacy", "secret-a"), ("x-legacy", "secret-b")], 401),
        ([("Authorization", "")], 401),
        ([("x-legacy", "\t ")], 401),
        ([("Authorization", "Basic secret-a"), ("x-legacy", "secret-a")], 401),
        ([("Authorization", ""), ("x-legacy", "secret-a")], 401),
        ([("Authorization", "Bearer secret-a"), ("x-legacy", "")], 401),
        ([("Authorization", "Bearer secret-a"), ("Authorization", "Bearer")], 401),
        ([("Authorization", "Bearer unknown-secret")], 401),
    ],
)
async def test_accepted_carriers(
    leg: str,
    mode: str,
    custom: bool,
    headers: list[tuple[str, str]],
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    validated: list[str] = []

    def validate(token: str) -> Principal | None:
        validated.append(token)
        return Principal(token) if token in {"secret-a", "secret-b"} else None

    options = {f"{leg}_header_name": "x-legacy", f"{leg}_bearer_prefix_required": False}
    with pytest.warns(DeprecationWarning) if custom and leg == "mcp" else nullcontext():
        config = BearerTokenAuth(
            validate_token=validate,
            allow_unauthenticated=mode == "anonymous",
            **(options if custom else {"legacy_header_aliases": ["x-legacy"]}),
        )
    app = middleware_app(leg, config)
    body = CALL if mode != "discovery" else {"method": "tools/list"}
    with caplog.at_level(logging.DEBUG, logger="adcp.server.auth"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/", headers=headers, json=body)
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"principal": "secret-a", "body": body}
        assert validated == ["secret-a"]
    else:
        assert "www-authenticate" in response.headers
        if headers != [("Authorization", "Bearer unknown-secret")]:
            assert validated == []
    assert current_principal.get() is None
    for record in caplog.records:
        if record.name == "adcp.server.auth":
            assert "secret" not in record.getMessage()
            assert record.exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_alias_prefix_policy(leg: str) -> None:
    config = BearerTokenAuth(
        validate_token=Principal,
        legacy_header_aliases=["x-legacy"],
        legacy_aliases_bearer_prefix_required=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware_app(leg, config)), base_url="http://test"
    ) as client:
        for alias, status in [
            ("Bearer secret-a", 200),
            ("bearer\tsecret-a", 200),
            ("secret-a", 401),
            ("Bearer secret-b", 401),
        ]:
            response = await client.post(
                "/", json=CALL, headers={"authorization": "Bearer secret-a", "x-legacy": alias}
            )
            assert response.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_concurrent_credentials_and_anonymous_requests(leg: str) -> None:
    config = BearerTokenAuth(validate_token=Principal, allow_unauthenticated=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=middleware_app(leg, config)), base_url="http://test"
    ) as client:

        async def call(identity: str | None) -> None:
            headers = {"authorization": f"Bearer {identity}"} if identity else {}
            response = await client.post("/", headers=headers, json=CALL)
            assert response.json()["principal"] == identity

        await asyncio.gather(*(call(identity) for identity in ["a", None, "b", None] * 8))
    assert current_principal.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_validator_exception_does_not_log_credentials(
    leg: str, caplog: pytest.LogCaptureFixture
) -> None:
    def validate(token: str) -> Principal | None:
        raise RuntimeError(f"credential={token}")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=middleware_app(leg, BearerTokenAuth(validate_token=validate))
        ),
        base_url="http://test",
    ) as client:
        with caplog.at_level(logging.INFO, logger="adcp.server.auth"):
            response = await client.post(
                "/", headers={"authorization": "Bearer private-token"}, json=CALL
            )
    assert response.status_code == 401
    assert "private-token" not in caplog.text + response.text
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].reason == "validator_error"
    assert errors[0].exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize("alias,status", [("token-a", 200), ("token-b", 401)])
async def test_combined_server_credential_parity(alias: str, status: int) -> None:
    """The #1305 reproduction, through the actual combined transport server."""
    from asgi_lifespan import LifespanManager

    from adcp.server import ADCPHandler
    from adcp.server.auth import auth_context_factory
    from adcp.server.serve import _build_mcp_and_a2a_app

    seen: list[str | None] = []

    class Seller(ADCPHandler):
        advertised_tools = {"get_products"}

        async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
            seen.append(context.caller_identity)
            return {"products": []}

    app = _build_mcp_and_a2a_app(
        Seller(),
        name="seller",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        stateless_http=True,
        enable_dns_rebinding_protection=False,
        context_factory=auth_context_factory,
        auth=BearerTokenAuth(validate_token=Principal, legacy_header_aliases=["x-legacy"]),
    )
    params = {"buying_mode": "brief", "brief": "video"}
    mcp = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_products", "arguments": params},
    }
    a2a = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "m1",
                "role": "ROLE_USER",
                "parts": [{"data": {"skill": "get_products", "parameters": params}}],
            }
        },
    }
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=True
        ) as client:
            for path, body in [("/mcp", mcp), ("/", a2a)]:
                seen.clear()
                response = await client.post(
                    path,
                    json=body,
                    headers={
                        "accept": "application/json, text/event-stream",
                        "A2A-Version": "1.0",
                        "authorization": "Bearer token-a",
                        "x-legacy": alias,
                    },
                )
                assert response.status_code == status
                assert seen == (["token-a"] if status == 200 else [])
