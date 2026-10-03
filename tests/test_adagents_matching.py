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
    assert resolve_properties_for_agent(doc, AGENT, mode="permissive") == doc["properties"]
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
