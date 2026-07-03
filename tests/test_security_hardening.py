from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp import ADCPClient
from adcp.decisioning.upstream import NoAuth, UpstreamHttpClient
from adcp.protocols import mcp as mcp_mod
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import (
    MCPAdapter,
    _make_hardened_mcp_http_factory,
    _make_signing_http_factory,
)
from adcp.registry import RegistryClient
from adcp.signing.autosign import SigningConfig
from adcp.types.core import AgentConfig, Protocol


def test_auth_token_rejects_non_loopback_plaintext_http() -> None:
    with pytest.raises(ValueError, match="auth_token requires an https://"):
        AgentConfig(
            id="remote",
            agent_uri="http://seller.example.com/mcp",
            protocol=Protocol.MCP,
            auth_token="secret",
        )


def test_auth_token_allows_loopback_plaintext_http() -> None:
    cfg = AgentConfig(
        id="local",
        agent_uri="http://127.0.0.1:8000/mcp",
        protocol=Protocol.MCP,
        auth_token="secret",
    )
    assert cfg.agent_uri.startswith("http://127.0.0.1")


def test_request_signing_rejects_non_loopback_plaintext_http() -> None:
    signing = SigningConfig(
        private_key=ed25519.Ed25519PrivateKey.generate(),
        key_id="buyer-test-key",
    )
    cfg = AgentConfig(
        id="remote",
        agent_uri="http://seller.example.com/mcp",
        protocol=Protocol.MCP,
    )

    with pytest.raises(ValueError, match="request signing requires an https://"):
        ADCPClient(cfg, signing=signing)


@pytest.mark.asyncio
async def test_a2a_owned_client_ignores_proxy_environment() -> None:
    adapter = A2AAdapter(
        AgentConfig(id="a2a", agent_uri="https://seller.example.com", protocol=Protocol.A2A)
    )
    try:
        client = await adapter._get_httpx_client()
        assert client.trust_env is False
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_registry_owned_client_ignores_proxy_environment() -> None:
    registry = RegistryClient("https://registry.example.com")
    try:
        client = await registry._get_client()
        assert client.trust_env is False
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_upstream_owned_client_ignores_proxy_environment() -> None:
    upstream = UpstreamHttpClient(base_url="https://upstream.example.com", auth=NoAuth())
    try:
        client = await upstream._get_client()
        assert client.trust_env is False
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_mcp_unsigned_client_factory_ignores_proxy_environment() -> None:
    factory = _make_hardened_mcp_http_factory()
    client = factory(trust_env=True, follow_redirects=False)
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_unsigned_streamable_http_adapter_uses_hardened_factory() -> None:
    adapter = MCPAdapter(
        AgentConfig(id="mcp", agent_uri="https://seller.example.com/mcp", protocol=Protocol.MCP)
    )
    factory = adapter._streamable_http_client_factory()
    client = factory(trust_env=True)
    try:
        assert client.trust_env is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_sse_adapter_passes_hardened_factory(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSseContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *args):
            return None

    class FakeSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def initialize(self):
            return None

    def fake_sse_client(url, **kwargs):
        captured.update(kwargs)
        return FakeSseContext()

    monkeypatch.setattr(mcp_mod, "sse_client", fake_sse_client)
    monkeypatch.setattr(mcp_mod, "_ClientSession", FakeSession)

    adapter = MCPAdapter(
        AgentConfig(
            id="mcp-sse",
            agent_uri="https://seller.example.com/sse",
            protocol=Protocol.MCP,
            mcp_transport="sse",
            auth_token="secret",
        )
    )
    try:
        await adapter._get_session()
        factory = captured["httpx_client_factory"]
        assert callable(factory)
        client = factory(trust_env=True)
        try:
            assert client.trust_env is False
        finally:
            await client.aclose()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_mcp_signing_client_factory_ignores_proxy_environment() -> None:
    async def hook(request):
        return None

    factory = _make_signing_http_factory(hook)
    client = factory(trust_env=True, follow_redirects=True, event_hooks={})
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.event_hooks["request"] == [hook]
    finally:
        await client.aclose()
