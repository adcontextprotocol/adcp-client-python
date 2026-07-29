"""Python 7 canonical boundary and TypeScript 13.0.0-rc.3 parity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import adcp
from adcp.canonical_formats import (
    CanonicalFormatLegacyResolutionError,
    CreativeDialect,
    CreativeDialectError,
    build_catalog_index,
    migrated_format_option_id,
    normalize_legacy_creative_request,
    project_canonical_response_to_legacy,
    project_legacy_format_id,
    project_legacy_product,
    projection_adapters_from_catalog_snapshots,
    resolve_creative_dialect,
    resolve_legacy_format_refs,
)
from adcp.types.canonical_creative import PRIMARY_CANONICAL_MODELS
from adcp.types.generated_poc.core.media_buy_features import MediaBuyFeatures
from adcp.utils import get_format_assets

_GOLDEN = Path(__file__).parent / "fixtures/canonical/typescript-13.0.0-rc.3-golden.json"


def _legacy_property_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            for key in properties:
                if key in {"format_id", "format_ids", "v1_format_ref"}:
                    found.append(f"{path}.properties.{key}")
                if key == "agent_url" and "format" in str(value.get("title", "")).lower():
                    found.append(f"{path}.properties.agent_url")
        for key, item in value.items():
            found.extend(_legacy_property_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_legacy_property_paths(item, f"{path}[{index}]"))
    return found


def _legacy_value_paths(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"format_id", "format_ids", "v1_format_ref"}:
                found.append(f"{path}.{key}")
            found.extend(_legacy_value_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_legacy_value_paths(item, f"{path}[{index}]"))
    return found


def test_root_surface_is_canonical_and_legacy_is_explicit() -> None:
    assert not hasattr(adcp, "FormatId")
    assert adcp.Format is adcp.ProductFormatDeclaration
    assert adcp.LegacyFormatId.__name__ == "LegacyFormatId"
    assert "format_kind" in adcp.Format.model_fields
    assert "format_id" not in adcp.Format.model_fields
    assert "format_ids" not in adcp.Product.model_fields
    assert "format_ids" not in adcp.CreativeFilters.model_fields
    assert not hasattr(adcp.types, "ListCreativeFormatsRequest")
    assert not hasattr(adcp.types, "FormatReferenceStructuredObject")
    assert adcp.types.aliases.GetProductsSuccessResponse is adcp.GetProductsResponse
    assert adcp.types.aliases.UpdateMediaBuyPackagesRequest is adcp.UpdateMediaBuyRequest


def test_31_capability_evidence_survives_typed_parsing() -> None:
    features = MediaBuyFeatures.model_validate({"canonical_creatives": True})
    assert features.model_dump()["canonical_creatives"] is True


@pytest.mark.parametrize("model", PRIMARY_CANONICAL_MODELS, ids=lambda model: model.__name__)
def test_primary_model_schema_recursively_excludes_legacy_identity(model: type[Any]) -> None:
    assert _legacy_property_paths(model.model_json_schema()) == []
    assert _legacy_property_paths(TypeAdapter(model).json_schema()) == []


def test_primary_dump_cannot_emit_legacy_identity_from_private_state_or_extras() -> None:
    with pytest.raises(ValidationError, match="legacy creative identity"):
        adcp.Format(
            format_kind="image",
            params={"width": 300, "height": 250},
            format_id={"agent_url": "https://seller.example/mcp", "id": "mrec"},
        )

    declaration = adcp.Format.model_construct(
        format_kind=adcp.types.CanonicalFormatKind.image,
        format_option_id="mrec",
        params={"width": 300, "height": 250},
        vendor={"format_ids": [{"agent_url": "https://seller.example/mcp", "id": "hidden"}]},
        extension_tuple={"agent_url": "https://seller.example/mcp", "id": "hidden"},
    )
    assert _legacy_value_paths(declaration.model_dump(mode="json")) == []
    assert _legacy_value_paths(json.loads(declaration.model_dump_json())) == []
    assert "agent_url" not in declaration.model_dump(mode="json")["extension_tuple"]

    class Container(BaseModel):
        declaration: adcp.Format

    assert _legacy_value_paths(TypeAdapter(adcp.Format).dump_python(declaration)) == []
    assert _legacy_value_paths(Container(declaration=declaration).model_dump()) == []


def test_asset_helpers_read_canonical_slots_without_legacy_format_models() -> None:
    declaration = adcp.Format(
        format_kind="image",
        params={"slots": [{"asset_group_id": "hero", "asset_type": "image"}]},
    )
    assert get_format_assets(declaration) == [{"asset_group_id": "hero", "asset_type": "image"}]


def test_rc3_golden_inputs_produce_exact_outputs_and_original_tuples() -> None:
    golden = json.loads(_GOLDEN.read_text())
    for case in golden["cases"]:
        result = project_legacy_format_id(case["input"], product_id="golden", field="format_ids[0]")
        assert result.diagnostic is None
        assert result.declaration is not None
        assert result.declaration.model_dump(mode="json") == case["output"]
        assert [ref.model_dump(mode="json") for ref in result.declaration.legacy_format_refs] == [
            case["input"]
        ]


def test_migrated_option_ids_are_stable_under_seller_reordering() -> None:
    mrec = {"agent_url": "https://salesagent.voxmedia.com/mcp", "id": "display_300x250_image"}
    board = {"agent_url": "https://salesagent.voxmedia.com/mcp", "id": "display_728x90_image"}
    first = {item["id"]: migrated_format_option_id(item) for item in [mrec, board]}
    reordered = {item["id"]: migrated_format_option_id(item) for item in [board, mrec]}
    assert first == reordered


def test_migrated_option_id_matches_javascript_integer_number_serialization() -> None:
    assert (
        migrated_format_option_id(
            {
                "agent_url": "https://x.example/",
                "id": "a",
                "duration_ms": 30000,
            }
        )
        == "migrated_14b2c31e0e29b1411851bf3c7d781b2e"
    )


def test_exact_owner_and_id_wins_and_bare_id_collision_fails_closed() -> None:
    catalog = build_catalog_index(
        [
            {
                "format_id": {"agent_url": "https://one.example/formats", "id": "shared"},
                "canonical": {"kind": "image"},
            },
            {
                "format_id": {"agent_url": "https://two.example/formats", "id": "shared"},
                "canonical": {"kind": "display_tag"},
            },
        ]
    )
    exact = project_legacy_format_id(
        {"agent_url": "https://one.example/formats", "id": "shared"},
        product_id="p",
        field="f",
        catalog=catalog,
    )
    assert exact.declaration is not None
    assert exact.declaration.format_kind.value == "image"

    ambiguous = project_legacy_format_id(
        {"agent_url": "https://seller.example/formats", "id": "shared"},
        product_id="p",
        field="f",
        catalog=catalog,
    )
    assert ambiguous.declaration is None
    assert ambiguous.diagnostic is not None
    assert ambiguous.diagnostic.resolution_failure == "no_match"


def test_converter_overrides_unique_bare_id_compatibility_inference() -> None:
    source = {"agent_url": "https://custom.example/mcp", "id": "display_300x250_image"}
    result = project_legacy_format_id(
        source,
        product_id="p",
        field="f",
        legacy_format_converter=lambda _: {
            "format_kind": "display_tag",
            "format_option_id": "intentional-reuse",
            "params": {"width": 300, "height": 250},
        },
    )
    assert result.declaration is not None
    assert result.declaration.format_kind.value == "display_tag"
    assert [ref.model_dump(mode="json") for ref in result.declaration.legacy_format_refs] == [
        source
    ]


@pytest.mark.parametrize(
    "owner",
    [
        "http://public.example/creative",
        "https://127.0.0.1/creative",
        "https://169.254.169.254/latest/meta-data",
        "https://user@creative.adcontextprotocol.org/",
    ],
)
def test_unsafe_owners_fail_closed(owner: str) -> None:
    result = project_legacy_format_id(
        {"agent_url": owner, "id": "display_300x250_image"},
        product_id="p",
        field="f",
    )
    assert result.declaration is None
    assert result.diagnostic is not None
    assert result.diagnostic.resolution_failure == "no_match"


def test_contradictory_catalog_parameters_fail_closed() -> None:
    result = project_legacy_format_id(
        {
            "agent_url": "https://salesagent.voxmedia.com/mcp",
            "id": "display_300x250_image",
            "width": 728,
            "height": 90,
        },
        product_id="p",
        field="f",
    )
    assert result.declaration is None
    assert result.diagnostic is not None
    assert result.diagnostic.resolution_failure == "catalog_requirement_conflict"


def test_partial_product_is_retained_and_wholly_unmappable_product_is_omitted() -> None:
    base = {
        "product_id": "p",
        "name": "Product",
        "description": "Product",
        "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example"}],
        "delivery_type": "non_guaranteed",
        "pricing_options": [
            {"pricing_model": "cpm", "pricing_option_id": "cpm", "currency": "USD"}
        ],
        "reporting_capabilities": {
            "available_reporting_frequencies": ["daily"],
            "expected_delay_minutes": 0,
            "timezone": "UTC",
            "supports_webhooks": False,
            "available_metrics": ["impressions"],
            "date_range_support": "date_range",
        },
    }
    partial = project_legacy_product(
        {
            **base,
            "format_ids": [
                {"agent_url": "https://seller.example", "id": "display_300x250_image"},
                {"agent_url": "https://seller.example", "id": "unknown"},
            ],
        }
    )
    assert partial.product is not None
    assert len(partial.product.format_options) == 1
    assert len(partial.diagnostics) == 1

    omitted = project_legacy_product(
        {**base, "format_ids": [{"agent_url": "https://seller.example", "id": "unknown"}]}
    )
    assert omitted.product is None
    assert omitted.diagnostics[0].code == "FORMAT_PROJECTION_FAILED"


def test_process_boundary_requires_durable_resolver() -> None:
    legacy = {
        "agent_url": "https://formats.vox.example/mcp",
        "id": "vox_mrec_html",
        "width": 300,
        "height": 250,
    }
    snapshots = [
        {
            "source": "configured",
            "publisher_domain": "vox.example",
            "formats": [
                {
                    "format_kind": "display_tag",
                    "format_option_id": "vox_mrec_html",
                    "publisher_domain": "vox.example",
                    "params": {"width": 300, "height": 250},
                    "v1_format_ref": [legacy],
                }
            ],
        }
    ]
    adapters = projection_adapters_from_catalog_snapshots(snapshots)
    projected = project_legacy_format_id(
        legacy,
        product_id="vox-homepage",
        field="f",
        legacy_format_converter=adapters.legacy_format_converter,
    )
    assert projected.declaration is not None

    persisted = adcp.Format.model_validate(json.loads(projected.declaration.model_dump_json()))
    with pytest.raises(CanonicalFormatLegacyResolutionError):
        resolve_legacy_format_refs(persisted)
    assert [
        ref.model_dump(mode="json")
        for ref in resolve_legacy_format_refs(
            persisted,
            resolver=adapters.canonical_format_legacy_resolver,
            product_id="vox-homepage",
        )
    ] == [legacy]


def test_creative_dialect_matrix_fails_closed_for_ambiguous_31() -> None:
    assert resolve_creative_dialect("3.0") is CreativeDialect.LEGACY
    assert resolve_creative_dialect("3.2") is CreativeDialect.CANONICAL
    assert (
        resolve_creative_dialect(
            "3.1", capabilities={"media_buy": {"features": {"canonical_creatives": True}}}
        )
        is CreativeDialect.CANONICAL
    )
    assert (
        resolve_creative_dialect("3.1", request={"format_ids": [{"id": "x"}]})
        is CreativeDialect.LEGACY
    )
    with pytest.raises(CreativeDialectError):
        resolve_creative_dialect("3.1")
    with pytest.raises(CreativeDialectError):
        resolve_creative_dialect(
            "3.2", capabilities={"media_buy": {"features": {"canonical_creatives": False}}}
        )


def test_server_request_normalizer_and_same_process_response_preserve_tuple() -> None:
    legacy = {
        "agent_url": "https://seller.example/mcp",
        "id": "display_300x250_image",
    }
    normalized = normalize_legacy_creative_request(
        {
            "packages": [{"product_id": "p", "format_ids": [legacy]}],
            "fields": ["product_id", "format_ids"],
        }
    )
    assert normalized["fields"] == ["product_id", "format_options"]
    assert normalized["packages"][0]["format_option_refs"][0]["scope"] == "product"
    assert _legacy_value_paths(normalized) == []

    product = project_legacy_product({**_minimal_product(), "format_ids": [legacy]}).product
    assert product is not None
    wire = project_canonical_response_to_legacy({"products": [product]})
    assert wire["products"][0]["format_ids"] == [legacy]
    assert "format_options" not in wire["products"][0]


def test_server_downgrade_projects_package_and_creative_refs() -> None:
    legacy = {"agent_url": "https://seller.example/mcp", "id": "display_300x250_image"}
    product = project_legacy_product({**_minimal_product(), "format_ids": [legacy]}).product
    assert product is not None
    option_id = product.format_options[0].format_option_id
    wire = project_canonical_response_to_legacy(
        {
            "products": [product],
            "packages": [
                {
                    "package_id": "pkg",
                    "product_id": "p",
                    "format_option_refs": [{"scope": "product", "format_option_id": option_id}],
                    "creatives": [
                        {
                            "creative_id": "cr",
                            "format_option_ref": {
                                "scope": "product",
                                "format_option_id": option_id,
                            },
                        }
                    ],
                }
            ],
        }
    )
    assert wire["packages"][0]["format_ids"] == [legacy]
    assert wire["packages"][0]["creatives"][0]["format_id"] == legacy


def _minimal_product() -> dict[str, Any]:
    return {
        "product_id": "p",
        "name": "Product",
        "description": "Product",
        "publisher_properties": [{"selection_type": "all", "publisher_domain": "pub.example"}],
        "delivery_type": "non_guaranteed",
        "pricing_options": [
            {"pricing_model": "cpm", "pricing_option_id": "cpm", "currency": "USD"}
        ],
        "reporting_capabilities": {
            "available_reporting_frequencies": ["daily"],
            "expected_delay_minutes": 0,
            "timezone": "UTC",
            "supports_webhooks": False,
            "available_metrics": ["impressions"],
            "date_range_support": "date_range",
        },
    }
