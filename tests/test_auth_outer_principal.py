"""Trusted outer identity survives the unauthenticated MCP gate (#1304a)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from adcp.server.auth import (
    REQUEST_STATE_PRINCIPAL,
    REQUEST_STATE_PRINCIPAL_METADATA,
    REQUEST_STATE_TENANT,
    BearerTokenAuth,
    auth_context_factory,
    current_principal,
    current_principal_metadata,
    current_tenant,
)
from adcp.server.serve import RequestMetadata, _wrap_a2a_with_auth, _wrap_mcp_with_auth


class TrustedIdentity:
    """Test auth layer; only the test's trusted request flag creates identity."""

    def __init__(self, app: Any, carrier: str, identity: str | None) -> None:
        self.app = app
        self.carrier = carrier
        self.identity = identity

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or (b"x-trusted-test", b"yes") not in scope["headers"]:
            await self.app(scope, receive, send)
            return
        tokens = []
        if self.carrier in {"state", "both"}:
            scope.setdefault("state", {}).update(
                {
                    REQUEST_STATE_PRINCIPAL: self.identity,
                    REQUEST_STATE_TENANT: "tenant-42",
                    REQUEST_STATE_PRINCIPAL_METADATA: {"role": "buyer"},
                }
            )
        if self.carrier in {"context", "both"}:
            tokens = [
                current_principal.set(self.identity),
                current_tenant.set("tenant-42"),
                current_principal_metadata.set({"role": "buyer"}),
            ]
        try:
            await self.app(scope, receive, send)
        finally:
            if tokens:
                current_principal.reset(tokens[0])
                current_tenant.reset(tokens[1])
                current_principal_metadata.reset(tokens[2])


def outer_app(leg: str, *, fail: bool = False, allow: bool = True) -> Any:
    async def endpoint(request: Request) -> JSONResponse:
        await asyncio.sleep(0)
        ctx = auth_context_factory(
            RequestMetadata(
                tool_name="get_products",
                transport=leg,
                request_id="test",
                request_context=request,
            )
        )
        if fail:
            raise RuntimeError("handler failed")
        return JSONResponse(
            {
                "principal": ctx.caller_identity,
                "tenant": ctx.tenant_id,
                "role": ctx.metadata.get("role"),
            }
        )

    inner = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    config = BearerTokenAuth(validate_token=lambda token: None, allow_unauthenticated=allow)
    return (_wrap_mcp_with_auth if leg == "mcp" else _wrap_a2a_with_auth)(inner, config)


CALL = {"method": "tools/call", "params": {"name": "get_products"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("carrier", ["state", "context", "both"])
@pytest.mark.parametrize("identity", ["buyer-42", "", None])
async def test_preserve_outer_identity(leg: str, carrier: str, identity: str | None) -> None:
    app = TrustedIdentity(outer_app(leg), carrier, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        identified = await client.post("/", json=CALL, headers={"x-trusted-test": "yes"})
        assert identified.json() == {"principal": identity, "tenant": "tenant-42", "role": "buyer"}
        anonymous = await client.post("/", json=CALL)
        assert anonymous.json() == {"principal": None, "tenant": None, "role": None}
    assert current_principal.get() is None
    assert current_tenant.get() is None
    assert current_principal_metadata.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
async def test_reset_even_when_handler_raises(leg: str) -> None:
    app = TrustedIdentity(outer_app(leg, fail=True), "both", "buyer-42")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="handler failed"):
            await client.post("/", json=CALL, headers={"x-trusted-test": "yes"})
    assert current_principal.get() is None
    assert current_tenant.get() is None
    assert current_principal_metadata.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["mcp", "a2a"])
@pytest.mark.parametrize("allow", [False, True])
async def test_caller_identity_header_is_not_trusted(leg: str, allow: bool) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=outer_app(leg, allow=allow)), base_url="http://test"
    ) as client:
        response = await client.post("/", json=CALL, headers={"x-principal-id": "forged"})
    assert response.status_code == (200 if allow else 401)
    if allow:
        assert response.json()["principal"] is None
