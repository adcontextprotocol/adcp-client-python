"""Regression contracts for protocol-agnostic agent listing and resolution."""

from copy import deepcopy
from typing import Any

import pytest

from adcp import find_authorized_agent_entries
from adcp.adagents import (
    get_properties_by_agent,
    normalize_url,
    resolve_properties_for_agent,
    validate_adagents_structure,
    verify_agent_authorization,
)

AGENT = "https://sales.example.com/mcp"


def document(url: Any = AGENT, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "authorized_agents": [
            {
                "url": url,
                "authorized_for": "Display",
                "authorization_type": "property_tags",
                "property_tags": ["all"] if tags is None else tags,
            }
        ],
        "properties": [
            {
                "property_id": "site",
                "property_type": "website",
                "name": "Site",
                "identifiers": [{"type": "domain", "value": "example.com"}],
                "tags": ["all"],
            }
        ],
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://SALES.Example.com:443/mcp/",
        "http://sales.example.com:80/mcp",
        "https://sales.example.com/mcp/?q=x#fragment",
        # #1624: stray whitespace around a listed url must not hide the agent.
        " https://sales.example.com/mcp ",
        "\thttps://sales.example.com/mcp/\n",
    ],
)
def test_normalization_retains_protocol_and_path_contract(url: str) -> None:
    assert normalize_url(url) == "sales.example.com/mcp"
    doc = document(url)
    assert find_authorized_agent_entries(doc, AGENT) == doc["authorized_agents"]
    assert get_properties_by_agent(doc, AGENT) == doc["properties"]
    assert verify_agent_authorization(doc, AGENT)


@pytest.mark.parametrize(
    "url",
    [
        "https://sales.example.com:80/mcp",
        "http://sales.example.com:443/mcp",
        "https://sales.example.com:8443/mcp",
        "https://sales.example.com/MCP",
        "https://sales.example.com/other",
    ],
)
def test_paths_and_nondefault_ports_remain_significant(url: str) -> None:
    assert find_authorized_agent_entries(document(url), AGENT) == []


@pytest.mark.parametrize(
    "url",
    [None, [], [AGENT], 42, {}, "", "   ", "https://sales.example.com:bad/mcp", "https://[bad/mcp"],
)
def test_invalid_sibling_cannot_break_lookup(url: Any) -> None:
    doc = document(url)
    valid = document()["authorized_agents"][0]
    doc["authorized_agents"].extend([None, 1, valid])
    before = deepcopy(doc)
    assert find_authorized_agent_entries(doc, AGENT) == [valid]
    assert verify_agent_authorization(doc, AGENT)
    assert get_properties_by_agent(doc, AGENT) == doc["properties"]
    assert (
        resolve_properties_for_agent(doc, AGENT, mode="permissive", allow_any_domain=True)
        == doc["properties"]
    )
    assert doc == before
    if not isinstance(url, str) or not url:
        assert any(error.kind == "missing_url" for error in validate_adagents_structure(doc).errors)


def test_listing_distinguishes_unbound_and_unlisted_and_retains_duplicates() -> None:
    unbound = document(tags=["missing"])
    assert get_properties_by_agent(unbound, AGENT) == []
    assert find_authorized_agent_entries(unbound, AGENT)
    unlisted = document("https://other.example.com/mcp")
    assert get_properties_by_agent(unlisted, AGENT) == []
    assert find_authorized_agent_entries(unlisted, AGENT) == []
    unbound["authorized_agents"].append(document()["authorized_agents"][0])
    matches = find_authorized_agent_entries(unbound, AGENT)
    assert matches == unbound["authorized_agents"]
    assert matches[0] is unbound["authorized_agents"][0]


def test_normalization_strips_surrounding_whitespace_on_either_side() -> None:
    assert normalize_url(" https://interchange.io ") == "interchange.io"
    assert normalize_url(" https://interchange.io ") == normalize_url("https://interchange.io")
    assert find_authorized_agent_entries(document(), f"  {AGENT}  ")


@pytest.mark.parametrize(
    "url",
    ["https://sales.example.com", "https://SALES.example.com/", "http://sales.example.com:80"],
)
def test_an_origin_only_entry_lists_every_agent_at_its_origin(url: str) -> None:
    """AdCP 3.3.0-beta.6 (adcontextprotocol/adcp#8862): matching only widens."""
    doc = document(url)
    assert find_authorized_agent_entries(doc, AGENT) == doc["authorized_agents"]
    assert get_properties_by_agent(doc, AGENT) == doc["properties"]
    assert verify_agent_authorization(doc, AGENT)


@pytest.mark.parametrize(
    "url",
    [
        "https://sales.example.com/?q=x",
        "https://sales.example.com:8443",
        "https://example.com",
        "https://sales.example.com.evil.test",
    ],
)
def test_only_a_bare_origin_widens(url: str) -> None:
    assert find_authorized_agent_entries(document(url), AGENT) == []


def _pinned(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"authorized_agents": list(entries)}


def _jwk() -> dict[str, Any]:
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    x = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return {"kty": "OKP", "crv": "Ed25519", "x": x}


@pytest.mark.asyncio
async def test_signing_pins_combine_every_matching_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """AdCP 3.3.0-rc.0 (adcontextprotocol/adcp#8943, #9016).

    Every matching entry authorizes the agent. It is pinned only if each one
    carries signing_keys; then the pin is their union, and copies of a key keep
    the earliest revoked_at.
    """
    from adcp import adagents

    k1, k2 = _jwk(), _jwk()
    origin = {"url": "https://sales.example.com", "signing_keys": [k1]}
    exact = {"url": AGENT, "signing_keys": [k2]}
    later = {**k1, "revoked_at": "2026-12-01T00:00:00Z"}
    earlier = {**k1, "revoked_at": "2026-11-01T00:00:00Z"}
    records = {
        "union.example": _pinned(origin, exact),
        "absent.example": _pinned(origin, {"url": AGENT}),
        "empty.example": _pinned(origin, {"url": AGENT, "signing_keys": []}),
        "revoked.example": _pinned(
            {**origin, "signing_keys": [later]}, {**exact, "signing_keys": [earlier, k1]}
        ),
        "other.example": _pinned({"url": "https://other.example.com", "signing_keys": [k1]}),
    }

    async def fetch(domain: str) -> dict[str, Any]:
        return records[domain]

    monkeypatch.setattr(adagents, "fetch_adagents", fetch)
    pins = await adagents.fetch_publisher_signing_pins(
        ["union.example", "absent.example", "empty.example", "revoked.example"], AGENT
    )
    assert pins == {
        "union.example": [k1, k2],
        "absent.example": None,
        "empty.example": None,
        "revoked.example": [earlier],
    }
    with pytest.raises(ValueError, match="matching agent"):
        await adagents.fetch_publisher_signing_pins(["other.example"], AGENT)
