"""Reporting selectors preserve the caller's scope through public typed models."""

import json
from copy import deepcopy
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ValidationError

from adcp.types import ReportingDeliveryConfiguration, SyncAccountsRequest
from adcp.validation.schema_loader import get_named_validator


def onboarding_request(scope):
    return {
        "adcp_version": "3.2-rc.4",
        "idempotency_key": "scope-onboarding-regression-0001",
        "accounts": [
            {
                "account": {"account_id": "usd"},
                "reporting_delivery_configs": [
                    {
                        "delivery_config_id": "shared-config",
                        "delivery_config_version": 1,
                        "offering_id": "fixture-official-USD",
                        "active": False,
                        "feed_purpose": "billing",
                        "report_definition_id": "reference-report-v1",
                        "reporting_profile": "paid_media_delivery",
                        "scope": deepcopy(scope),
                        "coverage_requirement": "full",
                        "required_finality": "official",
                        "reconciliation_mode": "consumer_receipt",
                        "authoritative_party": "seller",
                        "schedule": {
                            "period_duration": "PT1H",
                            "alignment": "utc",
                            "delivery_sla": "PT1H",
                        },
                        "method": {
                            "pattern": "warehouse_materialization",
                            "transport": "fixture-sql",
                            "orchestration": "producer_managed",
                            "destination": {
                                "mode": "provision",
                                "provider": {"domain": "fixture.example.test"},
                                "location": "reporting/shared-destination",
                            },
                        },
                    }
                ],
            }
        ],
        "delete_missing": False,
        "dry_run": False,
    }


def test_explicit_media_buy_selector_survives_public_request_serialization():
    raw = onboarding_request({"media_buy_ids": ["shared-media-buy"]})
    before = deepcopy(raw)
    validator = get_named_validator("account/sync-accounts-request.json")
    assert validator is not None
    assert not list(validator.iter_errors(raw))
    request = SyncAccountsRequest.model_validate(raw)
    encoded = request.model_dump(mode="json", exclude_none=True)
    assert encoded["accounts"][0]["reporting_delivery_configs"][0]["scope"] == {
        "media_buy_ids": ["shared-media-buy"]
    }
    assert not list(validator.iter_errors(encoded))
    assert raw == before


@pytest.mark.parametrize(
    "scope", [{}, {"all_media_buys": True}, {"media_buy_ids": ["shared-media-buy"]}]
)
def test_scope_modes_round_trip_without_mutating_inputs_or_field_presence(scope):
    raw = onboarding_request(scope)
    before = deepcopy(raw)
    request = SyncAccountsRequest.model_validate(raw)
    selected = request.accounts[0].reporting_delivery_configs[0].scope
    expected = scope or {"all_media_buys": True}
    fields = set(selected.model_fields_set)
    assert selected.all_media_buys is (True if "all_media_buys" in expected else None)
    validator = get_named_validator("account/sync-accounts-request.json")
    for encoded in (
        request.model_dump(),
        request.model_dump(mode="json"),
        json.loads(request.model_dump_json()),
    ):
        assert encoded["accounts"][0]["reporting_delivery_configs"][0]["scope"] == expected
        assert not list(validator.iter_errors(encoded))
        assert SyncAccountsRequest.model_validate(encoded).model_dump(
            mode="json"
        ) == request.model_dump(mode="json")
    assert raw == before
    assert selected.model_fields_set == fields


@pytest.mark.parametrize(
    "scope",
    [
        {"all_media_buys": False},
        {"all_media_buys": 1},
        {"all_media_buys": "true"},
        {"all_media_buys": None},
        {"media_buy_ids": None},
        {"media_buy_ids": []},
        {"media_buy_ids": [""]},
        {"media_buy_ids": ["shared-media-buy", "shared-media-buy"]},
        {"all_media_buys": True, "media_buy_ids": ["shared-media-buy"]},
        {"all_media_buys": True, "media_buy_ids": None},
        {"all_media_buys": None, "media_buy_ids": ["shared-media-buy"]},
        {"unexpected_selector": True},
    ],
)
def test_malformed_or_conflicting_explicit_scopes_fail_closed(scope):
    raw = onboarding_request(scope)
    before = deepcopy(raw)
    with pytest.raises(ValidationError):
        SyncAccountsRequest.model_validate(raw)
    assert raw == before


@pytest.mark.parametrize(
    "scope", [{}, {"all_media_buys": True}, {"media_buy_ids": ["shared-media-buy"]}]
)
def test_scope_subclasses_inside_an_ordinary_pydantic_parent(scope):
    scope_type = ReportingDeliveryConfiguration.model_fields["scope"].annotation

    class CustomScope(scope_type):
        pass

    class OrdinaryParent(BaseModel):
        scope: CustomScope

    parent = OrdinaryParent.model_validate({"scope": scope})
    expected = scope or {"all_media_buys": True}
    for encoded in (
        parent.model_dump(),
        parent.model_dump(mode="json"),
        json.loads(parent.model_dump_json()),
    ):
        assert encoded == {"scope": expected}
    assert parent.scope.all_media_buys is (True if "all_media_buys" in expected else None)


@pytest.mark.parametrize(
    "scope", [{}, {"media_buy_ids": ["shared-media-buy"]}, {"all_media_buys": True}]
)
def test_read_only_scope_mappings_follow_the_same_selector_contract(scope):
    scope_type = ReportingDeliveryConfiguration.model_fields["scope"].annotation
    wrapped = MappingProxyType(scope)
    selected = scope_type.model_validate(wrapped)
    assert selected.model_dump(mode="json") == (scope or {"all_media_buys": True})
    assert dict(wrapped) == scope
