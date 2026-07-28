"""Tests for MCP adapter auto-signing wiring.

The hook behavior itself (``ADCPClient._sign_outgoing_request``) is covered
by ``test_autosign_hook.py`` — both adapters share it. These tests focus
on MCP-specific plumbing: the custom ``httpx_client_factory`` that
``streamablehttp_client`` receives, the SSE-transport warning path, and
the ``current_operation`` ContextVar scope around ``session.call_tool``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from adcp.client import ADCPClient
from adcp.protocols.mcp import MCPAdapter, _make_signing_http_factory
from adcp.signing import SigningConfig, private_key_from_jwk
from adcp.signing.autosign import current_operation
from adcp.types.core import AgentConfig, Protocol

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
KEYS = json.loads((VECTORS_DIR / "keys.json").read_text())["keys"]
ED25519_KEY = next(k for k in KEYS if k["kid"] == "test-ed25519-2026")


@pytest.fixture()
def signing_config() -> SigningConfig:
    private_key = private_key_from_jwk(ED25519_KEY, d_field="_private_d_for_test_only")
    return SigningConfig(private_key=private_key, key_id=ED25519_KEY["kid"])


# -- factory ------------------------------------------------------------


async def _dummy_hook(_request: httpx.Request) -> None:
    return None


def test_factory_disables_follow_redirects() -> None:
    factory = _make_signing_http_factory(_dummy_hook)
    client = factory()
    assert client.follow_redirects is False


def test_factory_installs_request_event_hook() -> None:
    factory = _make_signing_http_factory(_dummy_hook)
    client = factory()
    hooks = client.event_hooks.get("request") or []
    assert hooks == [_dummy_hook]


def test_factory_forwards_headers_timeout_auth() -> None:
    factory = _make_signing_http_factory(_dummy_hook)
    timeout = httpx.Timeout(7.5)
    client = factory(
        headers={"X-Buyer": "b1"},
        timeout=timeout,
        auth=None,
    )
    assert client.headers["X-Buyer"] == "b1"
    # httpx normalizes the timeout to an internal object; reading back the
    # connect component is enough to prove it was threaded through.
    assert client.timeout.connect == 7.5


def test_factory_accepts_none_args() -> None:
    # MCP's default factory is called with all-None sometimes; ours should
    # accept that shape without raising.
    factory = _make_signing_http_factory(_dummy_hook)
    client = factory()
    assert isinstance(client, httpx.AsyncClient)


# -- SSE transport warning ----------------------------------------------


def _make_mcp_adapter(transport: str) -> MCPAdapter:
    agent = AgentConfig(
        id="mcp-seller",
        agent_uri="https://seller.example.com/mcp",
        protocol=Protocol.MCP,
        mcp_transport=transport,
    )
    return MCPAdapter(agent)


async def test_sse_transport_with_signing_logs_warning_and_skips(
    signing_config: SigningConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = _make_mcp_adapter("sse")
    adapter.signing_request_hook = AsyncMock()

    # Capture the warning emitted during session setup. We short-circuit
    # the actual HTTP attempt by patching sse_client to raise immediately,
    # so we only exercise the code path up to the warning.
    with patch("adcp.protocols.mcp.sse_client") as mock_sse:
        mock_sse.side_effect = RuntimeError("stop here")
        with caplog.at_level(logging.WARNING, logger="adcp.protocols.mcp"):
            with pytest.raises(Exception):
                await adapter._get_session()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "RFC 9421 auto-signing is not supported on MCP SSE" in r.getMessage() for r in warnings
    ), [r.getMessage() for r in warnings]


async def test_streamable_http_with_signing_wires_factory(
    signing_config: SigningConfig,
) -> None:
    adapter = _make_mcp_adapter("streamable_http")
    adapter.signing_request_hook = AsyncMock()

    # Patch streamablehttp_client so we can assert on the kwargs it
    # receives — specifically that httpx_client_factory is present.
    captured_kwargs: dict[str, Any] = {}

    def _fake_streamable_http(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        raise RuntimeError("stop here")  # bail before any real I/O

    with patch("adcp.protocols.mcp.streamablehttp_client", side_effect=_fake_streamable_http):
        with pytest.raises(Exception):
            await adapter._get_session()

    assert "httpx_client_factory" in captured_kwargs
    factory = captured_kwargs["httpx_client_factory"]
    # Sanity check: the factory produces an AsyncClient with the signing hook.
    client = factory()
    try:
        assert client.follow_redirects is False
        assert client.event_hooks["request"] == [adapter.signing_request_hook]
    finally:
        await client.aclose()


async def test_streamable_http_without_signing_wires_hardened_factory() -> None:
    adapter = _make_mcp_adapter("streamable_http")
    # No signing_request_hook installed → hardened unsigned factory still
    # prevents auth headers from following ambient proxy environment settings.

    captured_kwargs: dict[str, Any] = {}

    def _fake_streamable_http(*args: Any, **kwargs: Any) -> Any:
        captured_kwargs.update(kwargs)
        raise RuntimeError("stop here")

    with patch("adcp.protocols.mcp.streamablehttp_client", side_effect=_fake_streamable_http):
        with pytest.raises(Exception):
            await adapter._get_session()

    assert "httpx_client_factory" in captured_kwargs
    factory = captured_kwargs["httpx_client_factory"]
    client = factory(trust_env=True)
    try:
        assert client.trust_env is False
        assert client.follow_redirects is True
    finally:
        await client.aclose()

    client = factory(headers={"x-adcp-auth": "secret"}, trust_env=True, follow_redirects=True)
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        await client.aclose()


# -- ContextVar scope around call_tool ----------------------------------


async def test_current_operation_set_around_call_tool(
    signing_config: SigningConfig,
) -> None:
    """Verify the ContextVar is set during call_tool and reset on the way out."""
    observed: list[str | None] = []

    async def _capture(*_args: Any, **_kwargs: Any) -> Any:
        observed.append(current_operation.get())
        result = MagicMock()
        result.isError = False
        result.content = []
        result.structuredContent = None
        return result

    adapter = _make_mcp_adapter("streamable_http")
    fake_session = MagicMock()
    fake_session.call_tool = _capture
    adapter._get_session = AsyncMock(return_value=fake_session)  # type: ignore[method-assign]

    # Before the call, ContextVar is unset.
    assert current_operation.get() is None
    await adapter._call_mcp_tool("create_media_buy", {})
    # After the call, ContextVar is back to unset (token reset).
    assert current_operation.get() is None
    # Inside the call, it was set to the operation name.
    assert observed == ["create_media_buy"]


async def test_context_var_reset_on_exception() -> None:
    """If call_tool raises, the ContextVar still resets."""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    adapter = _make_mcp_adapter("streamable_http")
    fake_session = MagicMock()
    fake_session.call_tool = _boom
    adapter._get_session = AsyncMock(return_value=fake_session)  # type: ignore[method-assign]

    assert current_operation.get() is None
    result = await adapter._call_mcp_tool("create_media_buy", {})
    # _call_mcp_tool catches broad exceptions and returns a failed TaskResult,
    # so it shouldn't re-raise here; the point is that ContextVar still reset.
    assert current_operation.get() is None
    assert result.success is False


# -- ADCPClient wires hook into adapter --------------------------------


def test_adcp_client_mcp_adapter_receives_hook(signing_config: SigningConfig) -> None:
    agent = AgentConfig(
        id="mcp-seller",
        agent_uri="https://seller.example.com/mcp",
        protocol=Protocol.MCP,
    )
    client = ADCPClient(agent, signing=signing_config)
    hook = client.adapter.signing_request_hook
    assert hook is not None
    # Bound methods compare equal even though each access creates a new
    # descriptor, so == is the right check here (not `is`).
    assert hook == client._sign_outgoing_request
    assert hook.__self__ is client  # type: ignore[union-attr]
