"""Semantic 3.3 -> 3.2 downgrade rules (issue 1667).

The generic projection drops a field the older schema lacks. These fields
need more: a row that cannot exist below 3.3 is omitted whole, a field whose
absence would change meaning is refused, and a 3.3 field with an interim 3.2
spelling moves there. Payloads come from ``version_conversion_sweep_3_3.json``,
which the downgrade sweep also converts to 3.1 and 3.0.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp import ADCPClient
from adcp.compat.versions import (
    CONVERSION_METADATA_KEY,
    ADCPVersionConversionError,
    ConversionResult,
    convert_payload,
)
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.types.core import AgentConfig, Protocol
from adcp.validation import ValidationHookConfig, validate_request, validate_response

SOURCE = "3.3-beta.1"
_CASES = {
    case["id"]: case
    for case in json.loads(
        (Path(__file__).parent / "fixtures" / "version_conversion_sweep_3_3.json").read_text()
    )["cases"]
}


def _payload(case_id: str) -> dict[str, Any]:
    return deepcopy(_CASES[f"issue1667.{case_id}"]["payload"])


def _down(tool: str, payload: dict[str, Any], *, direction: str = "sync") -> ConversionResult:
    validate = validate_request if direction == "request" else validate_response
    assert validate(tool, payload, version=SOURCE).valid
    result = convert_payload(
        tool, payload, direction=direction, source_version=SOURCE, target_version="3.2"
    )
    assert validate(tool, result.payload, version="3.2").valid
    return result


def _losses(result: ConversionResult) -> list[tuple[str, str]]:
    return [(loss.pointer, loss.reason) for loss in result.losses]


PACKAGE = "/media_buy_deliveries/0/by_package/0"


def test_unaired_spots_are_omitted_and_aired_spots_lose_only_lifecycle_fields() -> None:
    result = _down(
        "get_media_buy_delivery",
        _payload("get_media_buy_delivery.spot_lifecycle_and_unmeasured_rows"),
    )
    package = result.payload["media_buy_deliveries"][0]["by_package"][0]
    # scheduled (s3) and preempted (s4) rows have no aired_at, which 3.2
    # requires: the rows go, never a row with an invented airing.
    assert package["by_spot"] == [
        {"spot_id": "s1", "aired_at": "2027-01-01T20:00:00Z", "grps": 1.5},
        {"spot_id": "s2", "aired_at": "2027-01-02T20:00:00Z"},
        {"spot_id": "s5", "aired_at": "2027-01-04T21:00:00Z"},
    ]
    spot_losses = [loss for loss in _losses(result) if "/by_spot/" in loss[0]]
    assert spot_losses == [
        (f"{PACKAGE}/by_spot/1/spot_status", "field_unavailable"),
        (f"{PACKAGE}/by_spot/1/scheduled_at", "field_unavailable"),
        (f"{PACKAGE}/by_spot/1/break_position", "field_unavailable"),
        (f"{PACKAGE}/by_spot/2", "row_omitted_not_aired"),
        (f"{PACKAGE}/by_spot/3", "row_omitted_not_aired"),
        (f"{PACKAGE}/by_spot/4/spot_status", "field_unavailable"),
        (f"{PACKAGE}/by_spot/4/replaces_spot_id", "field_unavailable"),
    ]


def test_unmeasured_delivery_rows_are_omitted_never_zeroed() -> None:
    result = _down(
        "get_media_buy_delivery",
        _payload("get_media_buy_delivery.spot_lifecycle_and_unmeasured_rows"),
    )
    package = result.payload["media_buy_deliveries"][0]["by_package"][0]
    assert package["by_format"] == [
        {"format_kind": "video_hosted", "impressions": 1000, "spend": 450}
    ]
    assert package["daily_breakdown"] == [{"date": "2027-01-01", "impressions": 1000, "spend": 450}]
    assert {loss for loss in _losses(result) if "/by_spot/" not in loss[0]} == {
        (f"{PACKAGE}/by_format/1", "row_omitted_impressions_not_measured"),
        (f"{PACKAGE}/daily_breakdown/1", "row_omitted_impressions_not_measured"),
    }


async def test_a_3_3_seller_omits_unmeasured_rows_for_a_3_2_buyer() -> None:
    payload = _payload("get_media_buy_delivery.spot_lifecycle_and_unmeasured_rows")

    class Seller(ADCPHandler):
        async def get_media_buy_delivery(self, params: dict[str, Any], context: ToolContext):
            return deepcopy(payload)

    context = ToolContext()
    caller = create_tool_caller(
        Seller(),
        "get_media_buy_delivery",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    response = await caller({"adcp_version": "3.2", "media_buy_ids": ["mb_1"]}, context)
    assert validate_response("get_media_buy_delivery", response, version="3.2").valid
    package = response["media_buy_deliveries"][0]["by_package"][0]
    assert [row["spot_id"] for row in package["by_spot"]] == ["s1", "s2", "s5"]
    assert all("impressions" in row for row in package["by_format"])
    reasons = {loss["reason"] for loss in context.metadata[CONVERSION_METADATA_KEY]["losses"]}
    assert {"row_omitted_not_aired", "row_omitted_impressions_not_measured"} <= reasons


def test_catalog_ingestion_is_dropped_from_3_2_capabilities() -> None:
    capabilities = _payload("get_adcp_capabilities.catalog_ingestion")
    result = _down("get_adcp_capabilities", capabilities)
    # 3.2 ``features`` holds booleans only. Coarsening to ``true`` would read
    # as an unknown vendor flag below 3.3 and fail the 3.3 model on the way
    # back up; the 3.2 declaration of the capability is catalog_management.
    assert result.payload["media_buy"]["features"] == {"catalog_management": True}
    assert _losses(result) == [("/media_buy/features/catalog_ingestion", "field_unavailable")]


async def test_a_3_2_pinned_buyer_parses_3_3_capabilities() -> None:
    # Discovery is not version-gated: a 3.3 seller may answer with its own
    # capabilities. The buyer validates and parses them without conversion.
    capabilities = _payload("get_adcp_capabilities.catalog_ingestion")
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        adcp_version="3.2",
        validation=ValidationHookConfig(requests="strict", responses="strict"),
    )
    tool_result = MagicMock(isError=False, content=[], structuredContent=capabilities)
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=tool_result)
    client.adapter._get_session = AsyncMock(return_value=session)

    parsed = await client.fetch_capabilities()

    assert parsed.media_buy is not None and parsed.media_buy.features is not None
    ingestion = parsed.media_buy.features.catalog_ingestion
    assert ingestion is not None and ingestion.item_status_reporting.value == "per_item"


@pytest.mark.parametrize(
    ("case_id", "webhook"),
    [
        ("create_media_buy.reporting_webhook_without_authentication", "reporting_webhook"),
        ("create_media_buy.artifact_webhook_without_authentication", "artifact_webhook"),
    ],
)
def test_webhook_without_authentication_fails_structured(case_id: str, webhook: str) -> None:
    request = _payload(case_id)
    assert validate_request("create_media_buy", request, version=SOURCE).valid
    with pytest.raises(ADCPVersionConversionError) as caught:
        convert_payload(
            "create_media_buy",
            request,
            direction="request",
            source_version=SOURCE,
            target_version="3.2",
        )
    # The SDK never invents a credential and never drops the webhook.
    assert caught.value.pointer == f"/{webhook}"
    assert "authentication" in caught.value.reason
    assert (caught.value.source_version, caught.value.target_version) == (SOURCE, "3.2")


def test_signing_profile_does_not_stand_in_for_3_2_authentication() -> None:
    request = _payload("create_media_buy.reporting_webhook_without_authentication")
    request["reporting_webhook"]["signing_profile"] = "adcp/webhook-signing/v1"
    assert validate_request("create_media_buy", request, version=SOURCE).valid
    with pytest.raises(ADCPVersionConversionError) as caught:
        convert_payload(
            "create_media_buy",
            request,
            direction="request",
            source_version=SOURCE,
            target_version="3.2",
        )
    assert caught.value.pointer == "/reporting_webhook"


def test_webhook_with_authentication_converts_unchanged() -> None:
    request = _payload("create_media_buy.reporting_webhook_with_authentication")
    result = _down("create_media_buy", request, direction="request")
    assert result.payload["reporting_webhook"] == request["reporting_webhook"]
    assert result.losses == ()


@pytest.mark.parametrize("direction", ["request", "sync"])
def test_minute_level_dayparts_are_refused_not_rounded(direction: str) -> None:
    if direction == "request":
        payload = _payload("create_media_buy.daypart_clock_times")
        pointer = "/packages/0/targeting_overlay/daypart_targets/1/start_time"
    else:
        payload = {
            "status": "completed",
            "media_buy_id": "mb_1",
            "revision": 1,
            "confirmed_at": "2027-01-01T00:00:00Z",
            "media_buy_status": "active",
            "packages": [
                {
                    "package_id": "pkg_1",
                    "product_id": "p1",
                    "pricing_option_id": "po1",
                    "budget": 1000,
                    "targeting_overlay": {
                        "daypart_targets": [
                            {"days": ["friday"], "start_time": "19:30", "end_time": "23:00"}
                        ]
                    },
                }
            ],
        }
        pointer = "/packages/0/targeting_overlay/daypart_targets/0/start_time"
    validate = validate_request if direction == "request" else validate_response
    assert validate("create_media_buy", payload, version=SOURCE).valid
    # A response omitting the window would read as an all-day package, so
    # both directions refuse.
    with pytest.raises(ADCPVersionConversionError) as caught:
        convert_payload(
            "create_media_buy",
            payload,
            direction=direction,
            source_version=SOURCE,
            target_version="3.2",
        )
    assert caught.value.pointer == pointer
    assert "never rounded" in caught.value.reason


def test_whole_hour_dayparts_convert_unchanged() -> None:
    request = _payload("create_media_buy.daypart_whole_hours")
    result = _down("create_media_buy", request, direction="request")
    assert result.payload["packages"] == request["packages"]


def test_age_determination_bases_move_to_the_interim_extension() -> None:
    response = _payload("get_signals.age_determination_bases")
    response["signals"][0]["ext"]["adcp"] = {"age_determination_bases": ["inferred"]}
    result = _down("get_signals", response)
    signal = result.payload["signals"][0]
    assert "age_determination_bases" not in signal
    # Other extension data is kept, and the top-level value wins.
    assert signal["ext"] == {
        "vendor": {"segment_tier": "gold"},
        "adcp": {"age_determination_bases": ["verified", "declared"]},
    }
    assert _losses(result) == [("/signals/0/age_determination_bases", "moved_to_ext_adcp")]


def test_age_determination_bases_create_the_extension_when_absent() -> None:
    response = _payload("get_signals.age_determination_bases")
    del response["signals"][0]["ext"]
    result = _down("get_signals", response)
    assert result.payload["signals"][0]["ext"] == {
        "adcp": {"age_determination_bases": ["verified", "declared"]}
    }


@pytest.mark.parametrize(
    ("top_level", "expected"),
    [(None, ["verified", "declared"]), (["inferred"], ["inferred"])],
)
def test_upgrade_reads_the_interim_extension_and_top_level_wins(
    top_level: list[str] | None, expected: list[str]
) -> None:
    signal = _payload("get_signals.age_determination_bases")["signals"][0]
    del signal["age_determination_bases"]
    signal["ext"]["adcp"] = {"age_determination_bases": ["verified", "declared"]}
    if top_level is not None:
        signal["age_determination_bases"] = top_level
    response = {"status": "completed", "cache_scope": "public", "signals": [signal]}
    result = convert_payload(
        "get_signals", response, direction="sync", source_version="3.2", target_version=SOURCE
    )
    upgraded = result.payload["signals"][0]
    assert upgraded["age_determination_bases"] == expected
    assert upgraded["ext"] == signal["ext"]
    assert validate_response("get_signals", result.payload, version=SOURCE).valid


def test_a_price_breakdown_with_an_operator_fee_is_omitted_whole() -> None:
    result = _down("get_products", _payload("get_products.operator_fee"))
    operator, seller = result.payload["products"][0]["pricing_options"]
    # Without its operator_fee row the breakdown would no longer walk
    # list_price to fixed_price; fee would misstate who keeps the money.
    assert "price_breakdown" not in operator
    assert operator["fixed_price"] == 33
    assert seller["price_breakdown"]["adjustments"][0]["kind"] == "discount"
    assert _losses(result) == [
        # The generic projection drops 3.3-only product fields.
        ("/products/0/dooh_inventory_summary", "field_unavailable"),
        ("/products/0/pricing_options/0/price_breakdown/adjustments/1/kind", "enum_unavailable"),
        ("/products/0/pricing_options/0/price_breakdown", "unrepresentable_in_target"),
    ]


def test_new_error_codes_pass_through_the_open_vocabulary() -> None:
    # 3.2 error.code is an open string: receivers decode unknown codes by
    # ``recovery``. Mapping pin drift to a 3.2 code belongs to definition pins.
    response = _payload("create_media_buy.reference_definition_changed")
    result = _down("create_media_buy", response)
    assert result.payload["errors"] == response["errors"]
    assert result.losses == ()
