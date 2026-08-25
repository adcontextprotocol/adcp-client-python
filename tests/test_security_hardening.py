from __future__ import annotations

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from adcp import ADCPClient, ADCPMultiAgentClient
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


@pytest.mark.parametrize(
    "agent_uri",
    [
        "https://user:pass@seller.example.com/mcp",
        "https://token@seller.example.com/mcp",
        "http://user:pass@127.0.0.1:8000/mcp",
    ],
)
def test_agent_uri_rejects_embedded_credentials(agent_uri: str) -> None:
    with pytest.raises(ValueError, match="agent_uri must not include credentials"):
        AgentConfig(id="remote", agent_uri=agent_uri, protocol=Protocol.MCP)


def test_extra_headers_reject_non_loopback_plaintext_http() -> None:
    with pytest.raises(ValueError, match="extra_headers require an https://"):
        AgentConfig(
            id="remote",
            agent_uri="http://seller.example.com/mcp",
            protocol=Protocol.MCP,
            extra_headers={"x-adcp-tenant": "acme"},
        )


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
async def test_mcp_unsigned_client_factory_blocks_redirects_with_headers() -> None:
    factory = _make_hardened_mcp_http_factory()
    client = factory(headers={"x-adcp-auth": "secret"}, trust_env=True, follow_redirects=True)
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
        assert client.follow_redirects is False
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
        client = factory(headers=captured.get("headers"), trust_env=True)
        try:
            assert client.trust_env is False
            assert client.follow_redirects is False
        finally:
            await client.aclose()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_mcp_sse_adapter_uses_custom_factory_without_partial_signing(monkeypatch) -> None:
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

    def fake_sse_client(_url, **kwargs):
        captured.update(kwargs)
        return FakeSseContext()

    def custom_factory(**kwargs):
        return httpx2.AsyncClient(**kwargs)

    async def signing_hook(_request) -> None:
        return None

    monkeypatch.setattr(mcp_mod, "sse_client", fake_sse_client)
    monkeypatch.setattr(mcp_mod, "_ClientSession", FakeSession)

    adapter = MCPAdapter(
        AgentConfig(
            id="custom-mcp-sse",
            agent_uri="https://seller.example.com/sse",
            protocol=Protocol.MCP,
            mcp_transport="sse",
        ),
        httpx_client_factory=custom_factory,
    )
    adapter.signing_request_hook = signing_hook
    try:
        await adapter._get_session()
        factory = captured["httpx_client_factory"]
        assert callable(factory)
        client = factory()
        try:
            assert client.trust_env is False
            assert client.follow_redirects is False
            assert signing_hook not in client.event_hooks["request"]
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


@pytest.mark.asyncio
async def test_custom_mcp_factory_composes_signing_and_existing_hooks() -> None:
    captured: dict[str, object] = {}

    async def audit_hook(_request) -> None:
        return None

    async def signing_hook(_request) -> None:
        return None

    def custom_factory(**kwargs):
        captured.update(kwargs)
        return httpx2.AsyncClient(event_hooks={"request": [audit_hook]}, **kwargs)

    client = ADCPClient(
        AgentConfig(
            id="custom-mcp",
            agent_uri="https://seller.example.com/mcp",
            protocol=Protocol.MCP,
        ),
        httpx_client_factory=custom_factory,
    )
    assert isinstance(client.adapter, MCPAdapter)
    client.adapter.signing_request_hook = signing_hook

    http_client = client.adapter._streamable_http_client_factory()(
        trust_env=True,
        follow_redirects=True,
    )
    try:
        assert captured["trust_env"] is False
        assert captured["follow_redirects"] is False
        assert http_client.event_hooks["request"] == [
            audit_hook,
            client.adapter.tracing_request_hook,
            signing_hook,
        ]
    finally:
        await http_client.aclose()


@pytest.mark.parametrize(
    ("trust_env", "follow_redirects", "message"),
    [
        (True, False, "trust_env=False"),
        (False, True, "follow_redirects=False"),
    ],
)
def test_custom_mcp_factory_rejects_insecure_returned_client(
    trust_env: bool,
    follow_redirects: bool,
    message: str,
) -> None:
    class UnsafeClient:
        event_hooks: dict[str, list[object]] = {"request": []}

        def __init__(self) -> None:
            self.trust_env = trust_env
            self.follow_redirects = follow_redirects

        def sse(self):
            return None

    adapter = MCPAdapter(
        AgentConfig(
            id="unsafe-mcp",
            agent_uri="https://seller.example.com/mcp",
            protocol=Protocol.MCP,
        ),
        httpx_client_factory=lambda **_kwargs: UnsafeClient(),
    )

    with pytest.raises(ValueError, match=message):
        adapter._streamable_http_client_factory()()


def test_custom_mcp_factory_rejects_a2a_only_client() -> None:
    with pytest.raises(TypeError, match="only supported for MCP"):
        ADCPClient(
            AgentConfig(
                id="a2a",
                agent_uri="https://seller.example.com",
                protocol=Protocol.A2A,
            ),
            httpx_client_factory=lambda **_kwargs: object(),
        )


def test_multi_agent_custom_factory_reaches_only_mcp_adapters() -> None:
    custom_factory = lambda **_kwargs: object()  # noqa: E731
    multi = ADCPMultiAgentClient(
        [
            AgentConfig(
                id="mcp",
                agent_uri="https://mcp.example.com/mcp",
                protocol=Protocol.MCP,
            ),
            AgentConfig(
                id="a2a",
                agent_uri="https://a2a.example.com",
                protocol=Protocol.A2A,
            ),
        ],
        httpx_client_factory=custom_factory,
    )

    mcp_adapter = multi.agent("mcp").adapter
    assert isinstance(mcp_adapter, MCPAdapter)
    assert mcp_adapter._httpx_client_factory is custom_factory
    assert not hasattr(multi.agent("a2a").adapter, "_httpx_client_factory")


@pytest.mark.asyncio
async def test_custom_mcp_factory_is_used_for_explicit_session_close() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        is_redirect = False

        def raise_for_status(self) -> None:
            return None

    class Client:
        trust_env = False
        follow_redirects = False
        event_hooks: dict[str, list[object]] = {"request": []}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def delete(self, url: str):
            calls.append({"url": url})
            return Response()

        def sse(self):
            return None

    def custom_factory(**kwargs):
        calls.append(kwargs)
        return Client()

    adapter = MCPAdapter(
        AgentConfig(
            id="close-mcp",
            agent_uri="https://seller.example.com/mcp",
            protocol=Protocol.MCP,
        ),
        httpx_client_factory=custom_factory,
    )

    await adapter.close_mcp_session("session-1")

    assert calls[0]["trust_env"] is False
    assert calls[0]["follow_redirects"] is False
    assert calls[1]["url"] == "https://seller.example.com/mcp"
