"""Tests for ``/.well-known/adcp-agents.json`` discovery endpoint.

Per AdCP spec (``schemas/source/adcp-agents.json``), every AdCP host
publishes an origin-scoped manifest enumerating the agents it serves.
The SDK's ``serve()`` exposes this on every HTTP transport
(``streamable-http``, ``a2a``, ``both``); ``stdio`` has no HTTP surface
and skips the route.

Coverage:

* GET on each transport returns 200 with a schema-conformant manifest.
* Non-GET methods at the discovery path do not serve the manifest.
* The :func:`build_manifest` builder is pure and validates standalone.
* agent_id normalization handles whitespace / mixed-case input names.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

starlette = pytest.importorskip("starlette")

from starlette.applications import Starlette
from starlette.testclient import TestClient

from adcp.server import ADCPHandler, ToolContext
from adcp.server.discovery import (
    DISCOVERY_PATH,
    build_manifest,
    make_discovery_route,
    resolve_base_url,
)
from adcp.server.responses import capabilities_response
from adcp.server.serve import (
    _build_mcp_and_a2a_app,
    _wrap_with_discovery,
)

# Inline copy of the AdCP discovery schema (PR #3903 / spec
# adcontextprotocol/adcp@5c3e3e626). Inlined rather than fetched so
# tests stay deterministic and offline. Update when the upstream
# schema bumps.
_DISCOVERY_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "/schemas/adcp-agents.json",
    "title": "AdCP Multi-Agent Topology Manifest",
    "type": "object",
    "properties": {
        "$schema": {"type": "string"},
        "version": {
            "type": "string",
            "pattern": r"^[0-9]+\.[0-9]+(\.[0-9]+)?$",
        },
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "pattern": r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "url": {"type": "string", "format": "uri", "minLength": 1},
                    "transport": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "specialisms": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                        "minItems": 1,
                        "maxItems": 64,
                        "uniqueItems": True,
                    },
                    "auth_hint": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                },
                "required": ["agent_id", "url", "transport", "specialisms"],
                "additionalProperties": True,
            },
            "minItems": 1,
            "maxItems": 256,
        },
        "contact": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 255},
                "email": {"type": "string", "format": "email"},
                "url": {"type": "string", "format": "uri"},
            },
            "required": ["name"],
            "additionalProperties": True,
        },
        "last_updated": {"type": "string", "format": "date-time"},
    },
    "required": ["version", "agents"],
    "additionalProperties": True,
}


def _validate_manifest(payload: dict) -> None:
    """Raise on schema-mismatch — pytest reports the JSON-Pointer."""
    jsonschema.validate(payload, _DISCOVERY_SCHEMA)


# ----- build_manifest pure builder --------------------------------------


def test_build_manifest_minimal_validates() -> None:
    """Default-args manifest matches the spec — placeholder specialism
    keeps the schema's ``minItems: 1`` constraint happy without forcing
    every adopter to declare specialisms before they can publish."""
    manifest = build_manifest(
        name="my-agent",
        transports=["mcp"],
        base_url="https://example.com",
    )
    _validate_manifest(manifest)
    assert manifest["agents"][0]["agent_id"] == "my-agent"
    assert manifest["agents"][0]["url"] == "https://example.com/mcp"
    assert manifest["agents"][0]["transport"] == "mcp"


def test_build_manifest_both_transports_unique_agent_ids() -> None:
    """When the binary serves both transports, the manifest emits two
    rows with distinct agent_ids — required by readers that key on
    agent_id, and required by the schema's no-duplicate-id contract."""
    manifest = build_manifest(
        name="seller",
        transports=["mcp", "a2a"],
        base_url="https://seller.example.com",
        specialisms=["sales-non-guaranteed"],
    )
    _validate_manifest(manifest)
    ids = [a["agent_id"] for a in manifest["agents"]]
    assert ids == ["seller-mcp", "seller-a2a"]
    transports = [a["transport"] for a in manifest["agents"]]
    assert transports == ["mcp", "a2a"]


def test_build_manifest_normalizes_agent_id() -> None:
    """Human-friendly names with spaces / mixed case become legal
    agent_ids — the schema's character class is lowercase + hyphens
    + underscores only, so we coerce eagerly rather than reject."""
    manifest = build_manifest(
        name="My Cool Seller!",
        transports=["mcp"],
        base_url="https://example.com",
    )
    _validate_manifest(manifest)
    assert manifest["agents"][0]["agent_id"] == "my-cool-seller"


def test_build_manifest_a2a_url_has_no_mcp_suffix() -> None:
    """A2A's url is the agent's base URL (the agent-card lives at
    ``<url>/.well-known/agent-card.json``); the ``/mcp`` suffix only
    applies to the MCP transport."""
    manifest = build_manifest(
        name="a2a-agent",
        transports=["a2a"],
        base_url="https://example.com",
    )
    _validate_manifest(manifest)
    assert manifest["agents"][0]["url"] == "https://example.com"


def test_build_manifest_passes_through_specialisms_and_description() -> None:
    """Operator-supplied specialisms + description appear verbatim in
    the manifest entry — buyers route on these and can't infer them."""
    manifest = build_manifest(
        name="seller",
        transports=["mcp"],
        base_url="https://example.com",
        description="A handcrafted ad agent.",
        specialisms=["sales-guaranteed", "sales-non-guaranteed"],
    )
    _validate_manifest(manifest)
    entry = manifest["agents"][0]
    assert entry["specialisms"] == ["sales-guaranteed", "sales-non-guaranteed"]
    assert entry["description"] == "A handcrafted ad agent."


def test_resolve_base_url_projects_wildcard_to_localhost() -> None:
    """``0.0.0.0`` is a wildcard bind, not a routable URL — the manifest
    uses a localhost projection so a default-config dev binary serves
    a usable URL for local testing."""
    assert resolve_base_url("0.0.0.0", 3001) == "http://127.0.0.1:3001"
    assert resolve_base_url("example.com", 8080) == "http://example.com:8080"


# ----- ASGI integration --------------------------------------------------


class _DiscoveryTestHandler(ADCPHandler[ToolContext]):
    """Minimal handler — discovery endpoint serves regardless of the
    handler's tool surface, but we need a real one to build the apps."""

    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])


def _inner_404_app() -> Starlette:
    """Bare Starlette app — Inner 404s confirm the discovery wrapper
    only intercepts the well-known path and lets everything else fall
    through unchanged."""
    return Starlette()


def test_discovery_route_serves_get_with_valid_json() -> None:
    """A standalone Route built by :func:`make_discovery_route` returns
    a schema-valid JSON document — exercises the route in isolation
    without the full transport stack."""
    route = make_discovery_route(
        name="standalone",
        transports=["mcp"],
        base_url="https://example.com",
    )
    app = Starlette(routes=[route])
    with TestClient(app) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    _validate_manifest(payload)


def test_discovery_route_rejects_post() -> None:
    """The discovery endpoint is read-only — POST returns 405,
    confirming Starlette's default method-not-allowed handler covers
    the unauthenticated public surface."""
    route = make_discovery_route(
        name="standalone",
        transports=["mcp"],
        base_url="https://example.com",
    )
    app = Starlette(routes=[route])
    with TestClient(app) as client:
        resp = client.post(DISCOVERY_PATH, json={})
    assert resp.status_code == 405


def test_wrap_with_discovery_on_streamable_http_transport() -> None:
    """The MCP streamable-http wrapper serves the manifest at the
    well-known path; non-discovery requests fall through to the inner
    app unchanged."""
    inner = _inner_404_app()
    wrapped = _wrap_with_discovery(
        inner,
        name="mcp-only",
        transports=["mcp"],
        base_url="https://mcp.example.com",
    )
    with TestClient(wrapped) as client:
        resp = client.get(DISCOVERY_PATH)
        assert resp.status_code == 200
        payload = resp.json()
        _validate_manifest(payload)
        assert payload["agents"][0]["transport"] == "mcp"

        # Non-discovery requests pass through unchanged.
        passthrough = client.get("/mcp")
        assert passthrough.status_code == 404


def test_wrap_with_discovery_on_a2a_transport() -> None:
    """The A2A wrapper serves the manifest with ``transport: "a2a"``
    and a base URL that has no ``/mcp`` suffix."""
    inner = _inner_404_app()
    wrapped = _wrap_with_discovery(
        inner,
        name="a2a-only",
        transports=["a2a"],
        base_url="https://a2a.example.com",
    )
    with TestClient(wrapped) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200
    payload = resp.json()
    _validate_manifest(payload)
    assert payload["agents"][0]["transport"] == "a2a"
    assert payload["agents"][0]["url"] == "https://a2a.example.com"


def test_discovery_endpoint_on_unified_transport() -> None:
    """``transport="both"`` exposes a manifest with two agent rows —
    one for MCP, one for A2A — at the same well-known path that
    standalone transports use."""
    app = _build_mcp_and_a2a_app(
        _DiscoveryTestHandler(),
        name="unified",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
        base_url="https://unified.example.com",
        specialisms=["sales-non-guaranteed"],
    )
    with TestClient(app) as client:
        resp = client.get(DISCOVERY_PATH)
    assert resp.status_code == 200
    payload = resp.json()
    _validate_manifest(payload)
    transports_seen = {a["transport"] for a in payload["agents"]}
    assert transports_seen == {"mcp", "a2a"}
    urls = {a["url"] for a in payload["agents"]}
    assert urls == {
        "https://unified.example.com/mcp",
        "https://unified.example.com",
    }


def test_discovery_endpoint_post_falls_through_on_unified() -> None:
    """POST to ``/.well-known/adcp-agents.json`` is not the discovery
    GET — the wrapper must NOT serve the manifest on POST, leaving
    the inner transport apps to return their own response."""
    app = _build_mcp_and_a2a_app(
        _DiscoveryTestHandler(),
        name="unified",
        port=3001,
        host="127.0.0.1",
        instructions=None,
        test_controller=None,
    )
    with TestClient(app) as client:
        resp = client.post(DISCOVERY_PATH, json={})
    # The wrapper passes through to A2A (which doesn't route this
    # path); the response MUST NOT be the manifest. Anything other
    # than 200-with-manifest-body is acceptable here.
    if resp.status_code == 200:
        body = json.loads(resp.text)
        assert "agents" not in body, (
            "POST should not return the discovery manifest — wrapper "
            "leaked GET response on POST."
        )
