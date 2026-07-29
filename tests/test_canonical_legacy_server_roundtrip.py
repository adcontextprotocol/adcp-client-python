"""Legacy callers keep their exact creative tuple across canonical handlers."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

import adcp
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller

_LEGACY_FORMAT = {
    "agent_url": "https://seller.example/mcp",
    "id": "display_300x250_image",
    "width": 300,
    "height": 250,
}


def test_extended_format_tuple_is_rejected_by_normal_validation() -> None:
    with pytest.raises(ValidationError, match=r"route\.agent_url"):
        adcp.Format(
            format_kind="image",
            params={},
            route={
                "agent_url": "https://seller.example/mcp",
                "id": "publisher-format",
                "vendor_extension": "still-a-format-tuple",
            },
        )


def test_extended_format_tuple_cannot_leak_from_model_construct() -> None:
    declaration = adcp.Format.model_construct(
        format_kind=adcp.types.CanonicalFormatKind.image,
        params={},
        route={
            "nested": {
                "agent_url": "https://seller.example/mcp",
                "id": "publisher-format",
                "vendor_extension": "still-a-format-tuple",
            }
        },
    )

    dumped = declaration.model_dump(mode="json")
    assert dumped["route"] == {
        "nested": {
            "id": "publisher-format",
            "vendor_extension": "still-a-format-tuple",
        }
    }


def test_unrelated_agent_url_fields_are_preserved() -> None:
    declaration = adcp.Format(
        format_kind="image",
        params={},
        verifier={"agent_url": "https://verifier.example/mcp", "feature_id": "safety"},
    )

    assert declaration.model_dump(mode="json")["verifier"]["agent_url"] == (
        "https://verifier.example/mcp"
    )


class _RoundTripHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    async def create_media_buy(
        self, params: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        self.received = params
        return {"media_buy_id": "mb-1", "packages": params["packages"]}

    async def update_media_buy(
        self, params: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        self.received = params
        return {
            "media_buy_id": params["media_buy_id"],
            "affected_packages": params["packages"],
        }

    async def sync_creatives(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        self.received = params
        return {"creatives": params["creatives"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["create_media_buy", "update_media_buy"])
async def test_package_tuple_round_trips_through_canonical_handler(method_name: str) -> None:
    handler = _RoundTripHandler()
    caller = create_tool_caller(handler, method_name)
    request: dict[str, Any] = {
        "adcp_version": "3.0",
        "packages": [{"product_id": "p-1", "format_ids": [_LEGACY_FORMAT]}],
    }
    if method_name == "update_media_buy":
        request["media_buy_id"] = "mb-1"

    response = await caller(request)

    assert handler.received is not None
    canonical_package = handler.received["packages"][0]
    assert "format_ids" not in canonical_package
    assert canonical_package["format_option_refs"][0]["scope"] == "product"
    response_field = "packages" if method_name == "create_media_buy" else "affected_packages"
    assert response[response_field][0]["format_ids"] == [_LEGACY_FORMAT]


@pytest.mark.asyncio
async def test_creative_tuple_round_trips_through_canonical_handler() -> None:
    handler = _RoundTripHandler()
    caller = create_tool_caller(handler, "sync_creatives")

    response = await caller(
        {
            "adcp_version": "3.0",
            "creatives": [
                {
                    "creative_id": "creative-1",
                    "name": "Banner",
                    "format_id": _LEGACY_FORMAT,
                    "assets": {},
                }
            ],
        }
    )

    assert handler.received is not None
    canonical_creative = handler.received["creatives"][0]
    assert "format_id" not in canonical_creative
    assert canonical_creative["format_kind"] == "image"
    assert response["creatives"][0]["format_id"] == _LEGACY_FORMAT
