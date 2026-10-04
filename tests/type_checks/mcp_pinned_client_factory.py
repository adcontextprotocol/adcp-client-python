"""Adopter-facing type checks for an IP-pinned MCP HTTP client factory.

``MCPAdapter(httpx_client_factory=...)`` is typed against MCP SDK v2's
``McpHttpClientFactory``, which returns an ``httpx2.AsyncClient``. An
adopter closing the DNS-rebinding TOCTOU on that path needs a pinned
transport in the same generation, so this file is the type-level
statement that one exists: the factory below satisfies the protocol
with no cast and no ``# type: ignore``.
"""

import httpx2
from mcp.shared._httpx_utils import McpHttpClientFactory

from adcp import ADCPClient
from adcp.signing import build_async_ip_pinned_transport2
from adcp.types.core import AgentConfig, Protocol

SELLER = "https://seller.example.com/mcp"


def pinned_client_factory(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=build_async_ip_pinned_transport2(SELLER),
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


factory: McpHttpClientFactory = pinned_client_factory


def hardened_transport() -> httpx2.AsyncBaseTransport:
    # Same keyword surface as the httpx builder, address relaxations included.
    return build_async_ip_pinned_transport2(
        SELLER,
        allow_private=False,
        allow_special_use=False,
        allowed_ports=frozenset({443}),
        verify=True,
    )


def client() -> ADCPClient:
    return ADCPClient(
        AgentConfig(id="seller", agent_uri=SELLER, protocol=Protocol.MCP),
        httpx_client_factory=pinned_client_factory,
    )
