"""Host/Origin enforcement at public and assembled HTTP transport boundaries."""

from typing import Any

import pytest
from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ServeConfig, create_a2a_server, create_mcp_server
from adcp.server.serve import _build_a2a_app, _build_mcp_and_a2a_app


class Seller(ADCPHandler):
    advertised_tools = {"get_products"}

    def __init__(self):
        self.calls = 0

    async def get_products(self, params, context=None):
        self.calls += 1
        return {"products": []}


HEADERS = {"accept": "application/json, text/event-stream"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "policy-test", "version": "1"},
    },
}
SEND = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
        "message": {
            "messageId": "m1",
            "role": "user",
            "parts": [
                {
                    "kind": "data",
                    "data": {
                        "skill": "get_products",
                        "parameters": {
                            "buying_mode": "brief",
                            "brief": "video",
                        },
                    },
                },
            ],
        }
    },
}


def build(transport: str, seller: Seller, **policy: Any):
    if transport == "mcp":
        return create_mcp_server(seller, validation=None, **policy).streamable_http_app()
    if transport == "direct-a2a":
        return create_a2a_server(seller, validation=None, **policy)
    if transport == "a2a":
        return _build_a2a_app(
            seller, name="test", port=3001, test_controller=None, validation=None, **policy
        )
    return _build_mcp_and_a2a_app(
        seller,
        name="test",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        validation=None,
        **policy,
    )


@pytest.mark.parametrize("transport", ["mcp", "a2a", "both", "direct-a2a"])
@pytest.mark.parametrize("protection", [True, False])
def test_policy_matrix(transport, protection):
    seller = Seller()
    app = build(
        transport,
        seller,
        allowed_hosts=["seller.example"],
        allowed_origins=["https://seller.example"],
        enable_dns_rebinding_protection=protection,
    )
    paths = ["/mcp", "/mcp/"] if transport == "mcp" else ["/"]
    if transport == "both":
        paths += ["/mcp", "/mcp/"]
    with TestClient(app, base_url="http://seller.example") as client:
        for path in paths:
            body = INIT if path.startswith("/mcp") else SEND
            assert (
                client.post(
                    path, json=body, headers={**HEADERS, "origin": "https://evil.example"}
                ).status_code
                == 403
            )
            assert client.post(
                path, json=body, headers={**HEADERS, "host": "evil.example"}
            ).status_code == (421 if protection else 200)
            assert client.post(path, json=body, headers=HEADERS).status_code == 200
            assert (
                client.post(
                    path, json=body, headers={**HEADERS, "origin": "https://seller.example"}
                ).status_code
                == 200
            )
        # Policy applies to GET/discovery/unknown operational paths as well.
        for path in ["/.well-known/agent.json", "/.well-known/agent-card.json", "/healthz"]:
            assert client.get(path, headers={"host": "evil.example"}).status_code == (
                421 if protection else (404 if transport == "mcp" or path == "/healthz" else 200)
            )
            assert client.get(path, headers={"origin": "https://evil.example"}).status_code == 403
        if transport != "mcp":
            assert seller.calls > 0
            assert client.get("/.well-known/agent-card.json").status_code == 200


@pytest.mark.parametrize("transport", ["mcp", "a2a", "both", "direct-a2a"])
def test_default_policy_loopback_native_requests(transport):
    with TestClient(build(transport, Seller()), base_url="http://localhost:3001") as client:
        path = "/mcp" if transport == "mcp" else "/.well-known/agent-card.json"
        response = (
            client.post(path, json=INIT, headers=HEADERS)
            if transport == "mcp"
            else client.get(path)
        )
        assert response.status_code == 200
        assert client.get(path, headers={"host": "evil.example"}).status_code == 421
        assert client.get(path, headers={"origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize(
    "host,origin,status",
    [
        ("tenant.example:8443", "https://tenant.example:8443", 200),
        ("tenant.example:8443", "https://tenant.example", 403),
        ("other.example:8443", "https://tenant.example:8443", 421),
        ("tenant.example.evil:8443", "https://tenant.example:8443", 421),
    ],
)
def test_port_wildcard_and_bare_host_semantics(host, origin, status):
    app = build(
        "both",
        Seller(),
        allowed_hosts=["tenant.example"],
        allowed_origins=["https://tenant.example:*"],
    )
    with TestClient(app, base_url="http://localhost:3001") as client:
        assert (
            client.get(
                "/.well-known/agent-card.json", headers={"host": host, "origin": origin}
            ).status_code
            == status
        )


def test_config_exposes_shared_policy():
    config = ServeConfig(
        transport="a2a",
        allowed_hosts=["seller.example"],
        allowed_origins=["https://seller.example"],
        enable_dns_rebinding_protection=False,
    )
    assert config.allowed_hosts == ["seller.example"]


@pytest.mark.parametrize("transport", ["mcp", "a2a", "both"])
def test_subdomain_tenancy_keeps_explicit_origin_protection(transport):
    from adcp.server.serve import _apply_asgi_middleware
    from adcp.server.tenant_router import (
        InMemorySubdomainTenantRouter,
        SubdomainTenantMiddleware,
        Tenant,
    )

    app = build(
        transport,
        Seller(),
        allowed_origins=["https://buyer.example"],
        enable_dns_rebinding_protection=False,
    )
    router = InMemorySubdomainTenantRouter({"acme.example": Tenant(id="acme")})
    app = _apply_asgi_middleware(app, [(SubdomainTenantMiddleware, {"router": router})])
    with TestClient(app, base_url="http://acme.example:3001") as client:
        path = "/mcp" if transport == "mcp" else "/"
        body = INIT if transport == "mcp" else SEND
        assert (
            client.post(
                path, json=body, headers={**HEADERS, "origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                path, json=body, headers={**HEADERS, "origin": "https://buyer.example"}
            ).status_code
            == 200
        )
        assert (
            client.post(path, json=body, headers={**HEADERS, "host": "unknown.example"}).status_code
            == 404
        )
