"""Property names that look like annotation keywords survive MCP schema stripping (#1900).

``_strip_schema_annotations`` once read the keys of a nested ``properties`` map
as schema keywords, so a property literally named ``description`` was dropped
while its object stayed closed (``additionalProperties: false``). A strict MCP
client then refused a valid ``list_products`` response.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema.validators import validator_for

from adcp.server import ADCPHandler, mcp_tools
from adcp.validation import get_mcp_schema
from adcp.validation import schema_loader as loader
from adcp.validation.schema_loader import _strip_schema_annotations

ANNOTATION_NAMES = ("description", "title", "examples", "$comment")

CANONICAL_PRODUCT = "external:core/canonical-product.json"


@pytest.fixture(autouse=True)
def isolated_loader():
    loader._reset_for_tests()
    yield
    loader._reset_for_tests()


def _annotated(name: str) -> dict[str, Any]:
    """A schema for a property that itself carries every annotation keyword."""
    return {
        "type": "string",
        "title": f"{name} title",
        "description": f"{name} description",
        "examples": ["x"],
        "$comment": "note",
    }


# -- unit ---------------------------------------------------------------------


def test_nested_properties_named_like_annotations_survive() -> None:
    schema = {
        "type": "object",
        "properties": {
            "product": {
                "type": "object",
                "additionalProperties": False,
                "properties": {name: _annotated(name) for name in ANNOTATION_NAMES},
            }
        },
    }

    out = _strip_schema_annotations(schema)

    nested = out["properties"]["product"]["properties"]
    assert set(nested) == set(ANNOTATION_NAMES)
    for name in ANNOTATION_NAMES:
        assert nested[name] == {"type": "string"}
    assert out["properties"]["product"]["additionalProperties"] is False


@pytest.mark.parametrize("keyword", ["patternProperties", "$defs", "definitions"])
def test_name_map_keywords_keep_annotation_named_keys(keyword: str) -> None:
    schema = {
        "type": "object",
        "properties": {
            "holder": {
                "type": "object",
                keyword: {name: _annotated(name) for name in ANNOTATION_NAMES},
            }
        },
    }

    out = _strip_schema_annotations(schema)

    entries = out["properties"]["holder"][keyword]
    assert set(entries) == set(ANNOTATION_NAMES)
    for name in ANNOTATION_NAMES:
        assert entries[name] == {"type": "string"}


def test_root_level_defs_keep_annotation_named_keys() -> None:
    schema = {"$defs": {name: _annotated(name) for name in ANNOTATION_NAMES}}

    out = _strip_schema_annotations(schema)

    assert out["$defs"] == {name: {"type": "string"} for name in ANNOTATION_NAMES}


def test_const_default_and_enum_values_are_copied_verbatim() -> None:
    instance = {"title": "t", "description": "d", "examples": [1], "$comment": "c"}
    schema = {
        "type": "object",
        "properties": {
            "a": {"const": dict(instance)},
            "b": {"default": dict(instance)},
            "c": {"enum": [dict(instance), {"description": "only"}]},
        },
    }

    out = _strip_schema_annotations(schema)

    assert out["properties"]["a"]["const"] == instance
    assert out["properties"]["b"]["default"] == instance
    assert out["properties"]["c"]["enum"] == [instance, {"description": "only"}]


def test_root_direct_properties_keep_description_nested_do_not() -> None:
    schema = {
        "type": "object",
        "description": "root description",
        "properties": {
            "top": {
                "type": "object",
                "description": "top description",
                "properties": {"inner": {"type": "string", "description": "inner description"}},
            }
        },
    }

    out = _strip_schema_annotations(schema)

    assert out["description"] == "root description"
    assert out["properties"]["top"]["description"] == "top description"
    assert "description" not in out["properties"]["top"]["properties"]["inner"]


def test_annotation_keywords_are_still_removed() -> None:
    schema = {
        "type": "object",
        "title": "root title",
        "examples": [{}],
        "$comment": "root comment",
        "_bundled": True,
        "properties": {
            "top": {
                "type": "object",
                "title": "t",
                "examples": [1],
                "$comment": "c",
                "description": "kept at the root's direct property",
                "properties": {
                    "inner": {"type": "string", "description": "d", "title": "t"},
                },
            }
        },
        "items": {"type": "string", "description": "nested", "title": "t"},
    }

    out = _strip_schema_annotations(schema)

    assert set(out) == {"type", "properties", "items"}
    assert out["items"] == {"type": "string"}
    assert out["properties"]["top"] == {
        "type": "object",
        "description": "kept at the root's direct property",
        "properties": {"inner": {"type": "string"}},
    }


def test_root_property_named_title_survives_root_title_keyword_dropped() -> None:
    schema = {
        "type": "object",
        "title": "Root",
        "properties": {"title": {"type": "string", "title": "T", "description": "kept"}},
    }

    out = _strip_schema_annotations(schema)

    assert "title" not in {k for k in out if k != "properties"}
    assert out["properties"]["title"] == {"type": "string", "description": "kept"}


# -- integration --------------------------------------------------------------


@pytest.mark.parametrize("version", ["3.2", None])
def test_list_products_keeps_product_description_property(version: str | None) -> None:
    schema = get_mcp_schema("list_products", "sync", version=version)
    assert schema is not None

    product = schema["$defs"][CANONICAL_PRODUCT]

    assert product["properties"]["description"] == {"type": "string"}
    if version == "3.2":
        # The 3.2 product is closed, so a dropped ``description`` property would
        # make every product carrying it invalid. The default pin's is open.
        assert product["additionalProperties"] is False


# -- end to end through the served schema -------------------------------------


class _PinnedHandler(ADCPHandler):
    def __init__(self, version: str) -> None:
        self.version = version

    def get_adcp_version(self) -> str:
        return self.version

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        return {"products": []}


_PRODUCT: dict[str, Any] = {
    "product_id": "p1",
    "name": "Product 1",
    "description": "A test product",
    "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example.com"}],
    "format_options": [
        {
            "format_option_id": "p1-display",
            "format_kind": "image",
            "params": {"sizes": [{"width": 300, "height": 250}]},
        }
    ],
    "delivery_type": "non_guaranteed",
    "pricing_options": [{"pricing_model": "cpm", "pricing_option_id": "po1", "currency": "USD"}],
    "reporting_capabilities": {
        "available_reporting_frequencies": ["daily"],
        "expected_delay_minutes": 60,
        "timezone": "UTC",
        "supports_webhooks": False,
        "available_metrics": ["impressions"],
        "date_range_support": "date_range",
    },
}


def test_served_get_products_output_schema_accepts_product_description() -> None:
    # The SDK's name for the discovery tool a 3.2 handler advertises is
    # ``get_products``; ``list_products`` is the bundle's sync-schema name.
    tools = mcp_tools.get_tools_for_handler(_PinnedHandler("3.2.0-rc.6"))
    tool = next(t for t in tools if t["name"] == "get_products")
    output_schema = tool["outputSchema"]

    product_def = output_schema["$defs"]["external:core/product.json"]
    assert "description" in product_def["properties"]

    validator = validator_for(output_schema)(output_schema)
    errors = [
        e.message for e in validator.iter_errors({"status": "completed", "products": [_PRODUCT]})
    ]

    assert errors == []


def test_served_list_products_output_schema_accepts_product_description() -> None:
    # Unlike ``get_products``, the ``list_products`` output schema is the
    # compact sync schema, where #1900 dropped ``description`` from a closed
    # product and made this valid response fail.
    tools = mcp_tools.get_tools_for_handler(ADCPHandler(), advertise_all=True, adcp_version="3.2")
    tool = next(t for t in tools if t["name"] == "list_products")
    output_schema = tool["outputSchema"]

    product_def = output_schema["$defs"][CANONICAL_PRODUCT]
    assert product_def["properties"]["description"] == {"type": "string"}
    assert product_def["additionalProperties"] is False

    response = {
        "outcome": "listed",
        "products": [_PRODUCT],
        "feed_version": "v1",
        "cache_scope": "public",
    }
    validator = validator_for(output_schema)(output_schema)

    assert [e.message for e in validator.iter_errors(response)] == []
