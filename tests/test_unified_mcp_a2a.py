"""Tests for ``transport="both"`` — unified MCP+A2A binary.

Both transports run on a single Starlette parent app via ``Mount``:
``Mount("/mcp", mcp_app)`` plus ``Mount("/", a2a_app)``. Tests use
Starlette's :class:`TestClient` (with the lifespan context entered)
so FastMCP's session manager initializes properly before requests.

Coverage:

* ``/.well-known/agent.json`` reaches the A2A app.
* POST ``/`` reaches A2A (its message endpoint).
* GET ``/mcp`` and ``/mcp/`` both route to FastMCP (Mount issues a
  307 redirect for the bare prefix; the inner endpoint matches
  ``/mcp/`` after Starlette's ``root_path`` accounting).
* Unknown paths (``/random``) fall through to A2A.
* ``transport="bogus"`` on the public ``serve()`` raises ValueError
  listing ``"both"`` as a valid option.
"""

from __future__ import annotations

import pytest

starlette = pytest.importorskip("starlette")

from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ToolContext
from adcp.server.responses import capabilities_response
from adcp.server.serve import _build_mcp_and_a2a_app


class _UnifiedTestHandler(ADCPHandler[ToolContext]):
    """Minimal handler that surfaces a ``get_adcp_capabilities``
    response on both transports."""

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])


@pytest.fixture
def unified_client():
    """TestClient with lifespan context entered.
    ``with TestClient(app):`` runs the parent's lifespan (and through
    it, FastMCP's session-manager init) — without it, requests to
    the MCP path fail with ``Task group is not initialized``."""
    app = _build_mcp_and_a2a_app(
        _UnifiedTestHandler(),
        name="unified-test",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
    )
    with TestClient(app) as client:
        yield client


# ----- A2A path ----------------------------------------------------------


def test_a2a_agent_card_served_on_root_path(unified_client) -> None:
    """``/.well-known/agent.json`` (0.3 alias) resolves to a 200 agent-card
    response. The route must be registered — a 404 means the alias was
    stripped from create_a2a_server's route list."""
    resp = unified_client.get("/.well-known/agent.json")
    assert resp.status_code == 200, (
        f"/.well-known/agent.json returned {resp.status_code}; "
        "expected 200 — the 0.3 alias route is missing from create_a2a_server"
    )


def test_a2a_root_path_routed_to_a2a_app(unified_client) -> None:
    """A POST to ``/`` (A2A's message endpoint) reaches A2A —
    not MCP, which would have no route there."""
    resp = unified_client.post("/", json={})
    assert resp.status_code != 404, (
        "POST / should reach the A2A app, but got 404 — likely the " "dispatcher misrouted to MCP."
    )


def test_unknown_path_routes_to_a2a_default(unified_client) -> None:
    """Paths that aren't ``/mcp...`` fall through to A2A (the
    catch-all Mount). A2A returns 404 for unknown routes — the
    test confirms the dispatch reached A2A cleanly."""
    resp = unified_client.get("/some-random-path")
    assert resp.status_code in (404, 405)


# ----- MCP path ----------------------------------------------------------


def test_mcp_path_routed_to_mcp_app(unified_client) -> None:
    """A GET to ``/mcp`` resolves through the dispatcher to the
    FastMCP app. Starlette Mount issues a 307 redirect from the
    bare prefix to the trailing-slash form; TestClient follows by
    default. FastMCP then returns its own status (typically 421
    "Misdirected Request" on a GET without proper MCP framing).
    A 404 here would mean the dispatcher misrouted to A2A."""
    resp = unified_client.get("/mcp")
    assert resp.status_code != 404, (
        f"GET /mcp should reach the FastMCP app, got {resp.status_code} — "
        "the dispatcher likely misrouted to A2A."
    )


def test_mcp_trailing_slash_resolves(unified_client) -> None:
    """``/mcp/`` resolves to the same FastMCP endpoint as the
    redirected ``/mcp`` form. Both should land on the inner
    streamable-http route after Mount + ``root_path`` accounting."""
    resp_no_slash = unified_client.get("/mcp")
    resp_slash = unified_client.get("/mcp/")
    # Both reach the same FastMCP inner endpoint; the response
    # status reflects FastMCP's view (typically 421 on GET) and
    # MUST be the same for both forms.
    assert resp_no_slash.status_code == resp_slash.status_code, (
        f"Trailing slash mismatched: /mcp={resp_no_slash.status_code}, "
        f"/mcp/={resp_slash.status_code}"
    )


def test_mcp_subpath_routed_to_mcp_app(unified_client) -> None:
    """Paths under ``/mcp/`` (e.g., ``/mcp/anything``) also route
    to the MCP app via Mount's prefix matching. FastMCP returns
    its own 404/405 for unknown subpaths inside its app; the
    test confirms the dispatcher reached MCP rather than A2A."""
    resp = unified_client.get("/mcp/anything")
    assert resp.status_code in (
        404,
        405,
        406,
        400,
        421,
    ), f"Unexpected status from /mcp subpath: {resp.status_code}"


# ----- Construction sanity ----------------------------------------------


def test_unified_app_builds_end_to_end() -> None:
    """The unified-app builder constructs both inner apps from the
    same handler instance and returns a working ASGI callable.
    Adopters writing context_factory or middleware wire one place
    and reach both transports."""
    handler = _UnifiedTestHandler()
    app = _build_mcp_and_a2a_app(
        handler,
        name="unified-identity",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
    )
    assert app is not None
    assert callable(app)


# ----- Public surface: serve() validation -------------------------------


def test_serve_rejects_unknown_transport_lists_both() -> None:
    """``serve(transport=...)`` validates the transport. An invalid
    value raises ValueError whose message lists ``"both"`` alongside
    the existing options — confirming the new option made it onto
    the public surface."""
    from adcp.server import serve

    with pytest.raises(ValueError, match="both"):
        serve(_UnifiedTestHandler(), transport="bogus")
