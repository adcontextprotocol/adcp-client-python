"""Exact revision reads must not acquire aggregate-only selector defaults."""

import json
from copy import deepcopy

import pytest
from pydantic import BaseModel, ValidationError

from adcp.types import GetMediaBuyDeliveryRequest
from adcp.validation.schema_loader import get_named_validator


def exact_request():
    return {
        "account": {"account_id": "acct_a"},
        "reporting_revision_id": "rpr_public_exact_revision",
        "pagination": {"max_results": 100},
    }


@pytest.mark.parametrize("parser", ["model_validate", "model_validate_json"])
def test_exact_revision_request_survives_public_serialization(parser):
    raw = exact_request()
    before = deepcopy(raw)
    validator = get_named_validator("media-buy/get-media-buy-delivery-request.json")
    assert validator is not None and not list(validator.iter_errors(raw))
    request = getattr(GetMediaBuyDeliveryRequest, parser)(
        json.dumps(raw) if parser == "model_validate_json" else raw
    )
    encoded = request.model_dump(mode="json", exclude_none=True)
    assert "include_package_daily_breakdown" not in encoded
    assert "include_window_breakdown" not in encoded
    assert not list(validator.iter_errors(encoded))
    assert raw == before


AGGREGATE_SELECTORS = (
    "media_buy_ids",
    "start_date",
    "end_date",
    "status_filter",
    "requested_metrics",
    "reporting_dimensions",
    "attribution_window",
    "include_package_daily_breakdown",
    "time_granularity",
    "include_window_breakdown",
)


@pytest.mark.parametrize("field", AGGREGATE_SELECTORS)
def test_explicit_aggregate_selectors_are_forbidden_even_when_null(field):
    raw = {**exact_request(), field: None}
    before = deepcopy(raw)
    with pytest.raises(ValidationError, match="exact revision requests forbid aggregate selectors"):
        GetMediaBuyDeliveryRequest.model_validate(raw)
    assert raw == before


@pytest.mark.parametrize("field", ["include_package_daily_breakdown", "include_window_breakdown"])
@pytest.mark.parametrize("flag", [False, True])
def test_exact_mode_rejects_explicit_boolean_flags_without_mutation(field, flag):
    raw = {**exact_request(), field: flag}
    before = deepcopy(raw)
    with pytest.raises(ValidationError, match="exact revision requests forbid aggregate selectors"):
        GetMediaBuyDeliveryRequest.model_validate(raw)
    assert raw == before


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"include_package_daily_breakdown": False},
        {"include_window_breakdown": False},
        {"include_package_daily_breakdown": True, "include_window_breakdown": True},
    ],
)
def test_aggregate_defaults_and_explicit_flags_are_preserved(flags):
    raw = {"media_buy_ids": ["shared-media-buy"], **flags}
    request = GetMediaBuyDeliveryRequest.model_validate(raw)
    fields = set(request.model_fields_set)
    for value in (
        request.model_dump(),
        request.model_dump(mode="json"),
        json.loads(request.model_dump_json()),
    ):
        assert value["include_package_daily_breakdown"] is flags.get(
            "include_package_daily_breakdown", False
        )
        assert value["include_window_breakdown"] is flags.get("include_window_breakdown", False)
        validator = get_named_validator("media-buy/get-media-buy-delivery-request.json")
        assert not list(validator.iter_errors(value))
    assert request.model_fields_set == fields


def test_pagination_requires_an_exact_revision():
    with pytest.raises(ValidationError, match="pagination requires reporting_revision_id"):
        GetMediaBuyDeliveryRequest.model_validate({"pagination": {"max_results": 100}})


def test_exact_request_subclasses_and_nesting_preserve_the_selected_mode():
    class Exact(GetMediaBuyDeliveryRequest):
        pass

    class OrdinaryParent(BaseModel):
        request: Exact

    raw = exact_request()
    parent = OrdinaryParent.model_validate({"request": raw})
    fields = set(parent.request.model_fields_set)
    for value in (
        parent.model_dump(),
        parent.model_dump(mode="json"),
        json.loads(parent.model_dump_json()),
        {"request": parent.request.model_dump()},
        {"request": json.loads(parent.request.model_dump_json())},
    ):
        assert not set(AGGREGATE_SELECTORS).intersection(value["request"])
        assert value["request"]["pagination"]["max_results"] == 100
        assert value["request"]["pagination"].get("cursor") is None
    validator = get_named_validator("media-buy/get-media-buy-delivery-request.json")
    assert not list(
        validator.iter_errors(parent.model_dump(mode="json", exclude_none=True)["request"])
    )
    serialized = parent.request.model_dump(mode="json")
    assert GetMediaBuyDeliveryRequest.model_validate(serialized).model_dump() == serialized
    assert parent.request.model_fields_set == fields
    # Aggregate-mode defaults remain available as attributes; serialization is
    # mode-aware and never changes the model or a caller-owned input mapping.
    assert parent.request.include_package_daily_breakdown is False
    assert parent.request.include_window_breakdown is False
    assert raw == exact_request()
