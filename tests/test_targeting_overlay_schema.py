"""The beta.14 object bridge must not widen the advertised mutation contract."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter

from adcp.server.mcp_tools import (
    ADCP_TOOL_DEFINITIONS,
    _ensure_pydantic_schemas_applied,
    _inline_refs,
)
from adcp.types import TargetingOverlayInput
from adcp.types.canonical_creative import sanitize_canonical_schema

_PACKAGE_PATH = ("properties", "packages", "anyOf", 0, "items", "properties", "targeting_overlay")
_TARGETING_PATHS = {
    "create_media_buy": {_PACKAGE_PATH},
    "update_media_buy": {
        _PACKAGE_PATH,
        ("properties", "new_packages", "anyOf", 0, "items", "properties", "targeting_overlay"),
    },
    "control_media_buy": {_PACKAGE_PATH},
    "buy_products": {("properties", "purchases", "items", "properties", "targeting_overlay")},
}


def _targeting_nodes(node: Any, path: tuple[str | int, ...] = ()):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "targeting_overlay":
                yield (*path, key), value
            yield from _targeting_nodes(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _targeting_nodes(value, (*path, index))


@pytest.mark.parametrize("tool", _TARGETING_PATHS)
def test_advertised_targeting_mutations_keep_input_only_schema(tool: str) -> None:
    # Exercise the actual lazy tools/list registry, not just an isolated
    # TypeAdapter. The session fixture also invokes this with every tool.
    _ensure_pydantic_schemas_applied(_TARGETING_PATHS)
    advertised = next(t["inputSchema"] for t in ADCP_TOOL_DEFINITIONS if t["name"] == tool)
    nodes = dict(_targeting_nodes(advertised))
    assert nodes.keys() == _TARGETING_PATHS[tool], tool

    # Independent mutation-only reference. Canonical create/update schemas
    # intentionally sanitize nested creative identities across their graph.
    expected = _inline_refs(TypeAdapter(TargetingOverlayInput | None).json_schema())
    if tool in {"create_media_buy", "update_media_buy"}:
        expected = sanitize_canonical_schema(expected)
    assert len(expected["anyOf"]) == 2
    assert expected["anyOf"][0]["title"] == "TargetingOverlayInput"
    assert expected["anyOf"][1] == {"type": "null"}

    for path, node in nodes.items():
        assert set(node) == {"anyOf", "default", "description", "title"}, (tool, path)
        # The runtime bridge makes this field non-ref-shaped, so Pydantic adds
        # this annotation (30 bytes/site). Pin it rather than permitting an
        # arbitrary amount of metadata outside the mutation-only anyOf.
        assert node["title"] == "Targeting Overlay", (tool, path)
        assert len(node["anyOf"]) == 2, (tool, path)
        assert node["anyOf"] == expected["anyOf"], (tool, path)
        assert node["default"] is None
        assert isinstance(node["description"], str)
        actual_bytes = json.dumps(node, sort_keys=True).encode()
        mutation_bytes = json.dumps(expected, sort_keys=True).encode()
        # Retain a budget for the generated description as well as pinning
        # the exact metadata keys/title above.
        assert len(actual_bytes) <= len(mutation_bytes) + 2048, (tool, path)
        assert b'"title": "TargetingOverlay"' not in actual_bytes, (tool, path)
