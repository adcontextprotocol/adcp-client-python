"""Unit tests for :mod:`adcp.schemas`."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

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


@pytest.mark.parametrize("traversal", [
    "../../schemas/cache/adagents.json",
    "../validation/__init__.py",
    "/etc/passwd",
    "a\\b.json",
    "..json",
])
def test_load_schema_rejects_path_traversal(traversal: str) -> None:
    with pytest.raises(FileNotFoundError, match="invalid"):
        load_schema(traversal)


def test_load_schema_corrupted_schema_raises_file_not_found(tmp_path: Path) -> None:
    """A corrupted bundled file should surface as FileNotFoundError, not JSONDecodeError."""

    @contextmanager
    def _bad_as_file(resource: object) -> Iterator[Path]:
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        yield bad

    with patch("adcp.schemas.as_file", _bad_as_file):
        with pytest.raises(FileNotFoundError):
            load_schema(ADCP_AGENTS)
