"""Smoke tests for the /.well-known/adcp-agents.json discovery endpoint.

Covers all three transport modes (streamable-http, a2a, both) via the
Starlette TestClient, plus advertise_all gating and comply_test_controller
exclusion.
"""

from __future__ import annotations

import json

import pytest

starlette = pytest.importorskip("starlette")

from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ToolContext
from adcp.server.a2a_server import create_a2a_server
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_mcp_and_a2a_app, _wrap_with_adcp_agents_route, create_mcp_server

DISCOVERY_PATH = "/.well-known/adcp-agents.json"


class _MinimalHandler(ADCPHandler[ToolContext]):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])

    async def get_products(self, params, context=None):
        return {"products": []}


class _FullHandler(ADCPHandler[ToolContext]):
    """Handler that overrides multiple tools so capability list is non-trivial."""

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy", "creative"])

    async def get_products(self, params, context=None):
        return {"products": []}

    async def create_media_buy(self, params, context=None):
        return {"media_buy_id": "mb_1", "packages": []}

    async def build_creative(self, params, context=None):
        return {"creative_id": "cr_1"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mcp_test_client(handler, name="test-agent", advertise_all=False):
    mcp = create_mcp_server(handler, name=name, port=3099)
    inner = mcp.streamable_http_app()
    app = _wrap_with_adcp_agents_route(inner, handler, name, advertise_all)
    return TestClient(app, raise_server_exceptions=True)


def _a2a_test_client(handler, name="test-agent", advertise_all=False):
    a2a_app = create_a2a_server(handler, name=name, port=3099)
    app = _wrap_with_adcp_agents_route(a2a_app, handler, name, advertise_all)
    return TestClient(app, raise_server_exceptions=True)


def _both_test_client(handler, name="test-agent", advertise_all=False):
    app = _build_mcp_and_a2a_app(
        handler,
        name=name,
        port=3099,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        advertise_all=advertise_all,
    )
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# streamable-http transport
# ---------------------------------------------------------------------------


def test_mcp_discovery_returns_200():
    with _mcp_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200


def test_mcp_discovery_content_type():
    with _mcp_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    assert "application/json" in resp.headers["content-type"]


def test_mcp_discovery_body_is_valid_json():
    with _mcp_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    assert isinstance(data, dict)
    assert "agents" in data
    assert isinstance(data["agents"], list)
    assert len(data["agents"]) >= 1


def test_mcp_discovery_agent_name():
    with _mcp_test_client(_MinimalHandler(), name="my-seller") as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    assert data["agents"][0]["name"] == "my-seller"


def test_mcp_discovery_includes_adcp_version():
    with _mcp_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    assert "adcp_version" in data
    assert isinstance(data["adcp_version"], str)
    assert len(data["adcp_version"]) > 0


def test_mcp_discovery_capabilities_list():
    with _mcp_test_client(_FullHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    caps = data["agents"][0]["capabilities"]
    assert "get_products" in caps
    assert "create_media_buy" in caps
    assert "build_creative" in caps


def test_mcp_discovery_excludes_comply_test_controller_advertise_all():
    with _mcp_test_client(_MinimalHandler(), advertise_all=True) as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    caps = data["agents"][0]["capabilities"]
    assert "comply_test_controller" not in caps


def test_mcp_discovery_advertise_all_false_only_implemented_tools():
    with _mcp_test_client(_MinimalHandler(), advertise_all=False) as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    caps = data["agents"][0]["capabilities"]
    # _MinimalHandler only overrides get_adcp_capabilities + get_products
    assert "get_products" in caps
    # A non-overridden tool should not appear
    assert "create_media_buy" not in caps


def test_mcp_discovery_advertise_all_expands_capabilities():
    with_all = _mcp_test_client(_MinimalHandler(), advertise_all=True)
    without_all = _mcp_test_client(_MinimalHandler(), advertise_all=False)

    with with_all as c1, without_all as c2:
        caps_all = c1.get(DISCOVERY_PATH).json()["agents"][0]["capabilities"]
        caps_impl = c2.get(DISCOVERY_PATH).json()["agents"][0]["capabilities"]

    assert len(caps_all) >= len(caps_impl)


# ---------------------------------------------------------------------------
# A2A transport
# ---------------------------------------------------------------------------


def test_a2a_discovery_returns_200():
    with _a2a_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200


def test_a2a_discovery_body_structure():
    with _a2a_test_client(_MinimalHandler(), name="a2a-seller") as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    assert data["agents"][0]["name"] == "a2a-seller"
    assert isinstance(data["agents"][0]["capabilities"], list)


# ---------------------------------------------------------------------------
# both transport (unified MCP+A2A dispatcher)
# ---------------------------------------------------------------------------


def test_both_discovery_returns_200():
    with _both_test_client(_MinimalHandler()) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200


def test_both_discovery_body_structure():
    with _both_test_client(_MinimalHandler(), name="both-seller") as client:
        resp = client.get(DISCOVERY_PATH)
    data = resp.json()
    assert data["agents"][0]["name"] == "both-seller"


def test_both_discovery_does_not_affect_mcp_path():
    """The /mcp path still routes to FastMCP after the discovery route is added."""
    with _both_test_client(_MinimalHandler()) as client:
        disc = client.get(DISCOVERY_PATH)
        # /mcp itself routes to FastMCP; any 2xx or 4xx from FastMCP confirms routing
        mcp_resp = client.get("/mcp")
    assert disc.status_code == 200
    assert mcp_resp.status_code not in (500, 502)


# ---------------------------------------------------------------------------
# fetch_adcp_agents client helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_adcp_agents_success(monkeypatch):
    """fetch_adcp_agents returns the parsed document from /.well-known/adcp-agents.json."""
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from adcp import fetch_adcp_agents

    doc = {
        "adcp_version": "3.0",
        "agents": [{"name": "my-seller", "capabilities": ["get_products"]}],
    }
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = doc

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(httpx, "AsyncClient", lambda: mock_client)

    result = await fetch_adcp_agents("https://seller.example.com")
    assert result["agents"][0]["name"] == "my-seller"


@pytest.mark.asyncio
async def test_fetch_adcp_agents_404(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from adcp import fetch_adcp_agents
    from adcp.exceptions import AdcpAgentsNotFoundError

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 404

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(httpx, "AsyncClient", lambda: mock_client)

    with pytest.raises(AdcpAgentsNotFoundError):
        await fetch_adcp_agents("https://seller.example.com")
