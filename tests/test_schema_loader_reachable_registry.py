"""Compact task stores preserve the full registry's reference behavior."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft7Validator

from adcp.validation import schema_loader

BASE = "file:///bundle/request.json"
ROOT = "https://example.test/request.json"
CHILD = "https://example.test/child.json"
LEAF = "https://example.test/leaf.json"


def _validator(schema: dict[str, Any], store: dict[str, dict[str, Any]]) -> Draft7Validator:
    return Draft7Validator(
        schema,
        resolver=schema_loader._ref_resolver_from_store(BASE, schema, store, bundle_key="test"),
    )


def test_external_fragments_keep_local_and_transitive_refs_and_all_aliases() -> None:
    schema = {
        "$id": ROOT,
        "properties": {"item": {"$ref": f"{CHILD}#/$defs/Entry"}},
        "required": ["item"],
    }
    child = {
        "$id": "https://example.test/canonical-child.json",
        "$defs": {
            "Entry": {
                "properties": {"value": {"$ref": "#/$defs/Leaf"}},
                "required": ["value"],
            },
            "Leaf": {"$ref": LEAF},
            "Cycle": {"$ref": ROOT},
        },
    }
    leaf = {"$id": LEAF, "type": "integer", "minimum": 1}
    unrelated = {"type": "boolean"}
    registry = {
        ROOT: schema,
        CHILD: child,
        "HTTPS://example.test/child.json#": child,
        "file:///bundle/child.json": child,
        LEAF: leaf,
        "https://example.test/unrelated.json": unrelated,
    }
    compact = schema_loader._reachable_registry_store(registry, BASE, schema)
    assert list(compact) == list(registry)[:-1]
    for payload, expected in (({"item": {"value": 1}}, True), ({"item": {"value": 0}}, False)):
        assert _validator(schema, registry).is_valid(payload) is expected
        assert _validator(schema, compact).is_valid(payload) is expected


def test_document_id_collision_preserves_last_document_winner() -> None:
    shared_id = "https://example.test/shared.json"
    schema = {"$id": ROOT, "$ref": CHILD}
    child = {
        "$id": shared_id,
        "properties": {"value": {"$ref": "#/$defs/Value"}},
        "$defs": {"Value": {"type": "integer"}},
    }
    winner = {"$id": shared_id, "$defs": {"Value": {"type": "string"}}}
    registry = {
        CHILD: child,
        "https://example.test/winner.json": winner,
        "https://example.test/unrelated.json": {"type": "null"},
    }
    compact = schema_loader._reachable_registry_store(registry, BASE, schema)
    assert list(compact) == list(registry)[:-1]
    for payload, expected in (({"value": "valid"}, True), ({"value": 1}, False)):
        assert _validator(schema, registry).is_valid(payload) is expected
        assert _validator(schema, compact).is_valid(payload) is expected


def test_root_id_fragment_keeps_the_defragmented_document() -> None:
    schema = {
        "$id": f"{ROOT}#root",
        "properties": {"item": {"$ref": "#/$defs/Value"}},
        "$defs": {"Value": {"type": "integer"}},
    }
    registry = {ROOT: {"$defs": {"Value": {"type": "string"}}}, LEAF: {"type": "integer"}}
    compact = schema_loader._reachable_registry_store(registry, BASE, schema)
    assert compact == registry
    for payload, expected in (({"item": "valid"}, True), ({"item": 1}, False)):
        assert _validator(schema, registry).is_valid(payload) is expected
        assert _validator(schema, compact).is_valid(payload) is expected


@pytest.mark.parametrize(
    "child",
    [
        {"$ref": "relative.json"},
        {"$ref": "file:///bundle/leaf.json"},
        {"$id": "/schemas/test/child.json", "type": "integer"},
        {"$id": f"{CHILD}#child", "type": "integer"},
        {"$defs": {"Scoped": {"$id": "nested.json", "type": "integer"}}},
        {"$ref": "https://example.test/missing.json"},
    ],
)
def test_scope_dependent_or_missing_refs_keep_the_full_registry(child: dict[str, Any]) -> None:
    schema = {"$id": ROOT, "$ref": CHILD}
    registry = {CHILD: child, LEAF: {"type": "integer"}}
    assert schema_loader._reachable_registry_store(registry, BASE, schema) == registry


def test_missing_reference_still_fails_at_validation_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {"$id": ROOT, "$ref": "https://example.test/missing.json"}
    registry = {CHILD: {"type": "integer"}}
    compact = schema_loader._reachable_registry_store(registry, BASE, schema)

    def unexpected_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("validation attempted a network request")

    monkeypatch.setattr("requests.get", unexpected_network)
    with pytest.raises(Exception, match="schema reference is not in bundle"):
        _validator(schema, compact).is_valid(1)
