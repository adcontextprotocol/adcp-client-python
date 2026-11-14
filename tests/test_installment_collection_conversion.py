"""Installment parent identity survives conversion to the older catalog contract."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar

import pytest

from adcp.compat.versions import ConversionResult, convert_payload
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation import ValidationHookConfig, validate_response


@pytest.fixture
def installment_catalog() -> dict[str, Any]:
    return {
        "status": "completed",
        "cache_scope": "public",
        "products": [
            {
                "product_id": "collection_catalog",
                "name": "Collection catalog",
                "description": "A product spanning two collections from one publisher",
                "publisher_properties": [
                    {"publisher_domain": "publisher.example", "selection_type": "all"}
                ],
                "format_options": [
                    {
                        "format_option_id": "image",
                        "format_kind": "image",
                        "params": {"width": 300, "height": 250},
                    }
                ],
                "delivery_type": "non_guaranteed",
                "pricing_options": [
                    {"pricing_option_id": "cpm", "pricing_model": "cpm", "currency": "USD"}
                ],
                "reporting_capabilities": {
                    "available_reporting_frequencies": ["daily"],
                    "expected_delay_minutes": 0,
                    "timezone": "UTC",
                    "supports_webhooks": False,
                    "available_metrics": ["impressions"],
                    "date_range_support": "date_range",
                },
                "collections": [
                    {
                        "publisher_domain": "publisher.example",
                        "collection_ids": ["show_a", "show_b"],
                    }
                ],
                "installments": [
                    {
                        "installment_id": "episode_12",
                        "name": "Episode 12",
                        "collection_ref": {
                            "publisher_domain": "publisher.example",
                            "collection_id": "show_b",
                        },
                    }
                ],
            }
        ],
    }


def _validated_downgrade(payload: dict[str, Any]) -> ConversionResult:
    source = validate_response("get_products", payload, version="3.2")
    assert source.variant != "skipped"
    assert source.valid, source.issues
    result = convert_payload(
        "get_products", payload, direction="sync", source_version="3.2", target_version="3.1"
    )
    target = validate_response("get_products", result.payload, version="3.1")
    assert target.variant != "skipped"
    assert target.valid, target.issues
    return result


def test_single_publisher_installment_keeps_parent_collection(
    installment_catalog: dict[str, Any],
) -> None:
    original = deepcopy(installment_catalog)
    result = _validated_downgrade(installment_catalog)
    assert result.payload["products"][0]["installments"] == [
        {"installment_id": "episode_12", "name": "Episode 12", "collection_id": "show_b"}
    ]
    assert result.losses == ()
    assert installment_catalog == original


@pytest.mark.parametrize("legacy_id", ["show_b", "explicit_legacy_id"])
def test_existing_installment_shorthand_is_preserved(
    installment_catalog: dict[str, Any], legacy_id: str
) -> None:
    installment_catalog["products"][0]["installments"][0]["collection_id"] = legacy_id
    result = _validated_downgrade(installment_catalog)
    assert result.payload["products"][0]["installments"][0]["collection_id"] == legacy_id


@pytest.mark.parametrize(
    "case", ["multiple_publishers", "missing_collections", "publisher_mismatch"]
)
def test_unrepresentable_collection_reference_still_reports_loss(
    installment_catalog: dict[str, Any], case: str
) -> None:
    product = installment_catalog["products"][0]
    if case == "multiple_publishers":
        product["collections"].append(
            {"publisher_domain": "another.example", "collection_ids": ["show_b"]}
        )
    elif case == "missing_collections":
        product.pop("collections")
    else:
        product["installments"][0]["collection_ref"]["publisher_domain"] = "another.example"
    result = _validated_downgrade(installment_catalog)
    installment = result.payload["products"][0]["installments"][0]
    assert "collection_id" not in installment
    assert "collection_ref" not in installment
    assert [(loss.pointer, loss.reason) for loss in result.losses] == [
        ("/products/0/installments/0/collection_ref", "field_unavailable")
    ]


def test_installment_without_canonical_parent_stays_unchanged(
    installment_catalog: dict[str, Any],
) -> None:
    installment = installment_catalog["products"][0]["installments"][0]
    installment.pop("collection_ref")
    installment["collection_id"] = "show_a"
    result = _validated_downgrade(installment_catalog)
    assert result.payload["products"][0]["installments"] == [installment]
    assert result.losses == ()


async def test_mcp_catalog_response_keeps_installment_parent(
    installment_catalog: dict[str, Any],
) -> None:
    class Seller(ADCPHandler):
        adcp_capabilities: ClassVar[dict[str, Any]] = {
            "media_buy": {"features": {"canonical_creatives": True}}
        }

        async def get_products(
            self, params: dict[str, Any], context: ToolContext | None = None
        ) -> dict[str, Any]:
            return deepcopy(installment_catalog)

    caller = create_tool_caller(
        Seller(),
        "get_products",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    response = await caller({"adcp_version": "3.1", "buying_mode": "brief", "brief": "collections"})
    assert validate_response("get_products", response, version="3.1").valid
    assert response["products"][0]["installments"][0]["collection_id"] == "show_b"
