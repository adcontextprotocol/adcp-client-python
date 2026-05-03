"""Unit tests for :mod:`adcp.schemas`."""

from __future__ import annotations

import pytest

from adcp.schemas import ADCP_AGENTS, load_schema


def test_load_schema_adcp_agents_returns_dict() -> None:
    schema = load_schema(ADCP_AGENTS)
    assert isinstance(schema, dict)
    assert schema["title"] == "AdCP Multi-Agent Topology Manifest"
    assert "version" in schema.get("required", [])
    assert "agents" in schema.get("required", [])


def test_load_schema_adcp_agents_has_https_pattern() -> None:
    schema = load_schema(ADCP_AGENTS)
    url_prop = schema["properties"]["agents"]["items"]["properties"]["url"]
    assert url_prop["pattern"] == "^https://"


def test_load_schema_unknown_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError, match="not bundled"):
        load_schema("nonexistent-schema.json")


def test_load_schema_error_message_is_actionable() -> None:
    with pytest.raises(FileNotFoundError) as exc_info:
        load_schema("typo-schema.json")
    msg = str(exc_info.value)
    assert "adcp-agents.json" in msg
