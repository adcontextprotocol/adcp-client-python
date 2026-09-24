"""Reporting defaults stay explicit through canonical and bundled model shapes."""

import json

import pytest
from pydantic import BaseModel

from adcp.decisioning.capabilities import MediaBuy
from adcp.types import NotificationConfig
from adcp.types.base import AdCPBaseModel
from tests.test_reporting_ledger import _OFFERING


@pytest.mark.parametrize("model_type", [MediaBuy, MediaBuy.__bases__[0]])
@pytest.mark.parametrize("explicit", [False, True])
def test_reporting_defaults_are_explicit_in_actual_bundled_and_canonical_models(
    model_type, explicit
):
    supplied = {
        "supported": True,
        "offerings": [_OFFERING],
        "automated_recovery_window_seconds": 0,
        "status_retention_days": 30,
    }
    if explicit:
        supplied.update(reliable_reporting_version="1.0", managed_delivery=False)
    model = model_type.model_validate({"reporting_delivery": supplied})
    before = set(model.reporting_delivery.model_fields_set)
    default_status_task = model.reporting_delivery.status_task
    for wire in (model.model_dump(mode="json"), json.loads(model.model_dump_json())):
        value = wire["reporting_delivery"]
        assert ("reliable_reporting_version" in value) is explicit
        assert ("managed_delivery" in value) is explicit
        assert not any(key.endswith(("_task", "_notification")) for key in value)
        assert "supports_webhook_activity" not in value
        if explicit:
            assert value["reliable_reporting_version"] == "1.0"
            assert value["managed_delivery"] is False
    assert model.reporting_delivery.model_fields_set == before
    assert model.reporting_delivery.status_task == default_status_task


@pytest.mark.parametrize("product_event,explicit", [(False, False), (False, True), (True, False)])
def test_nested_notification_defaults_follow_event_and_field_provenance(product_event, explicit):
    class Envelope(BaseModel):
        config: NotificationConfig

    supplied = {
        "subscriber_id": "test",
        "url": "https://buyer.example/events",
        "event_types": ["product.updated" if product_event else "reporting.status_changed"],
    }
    if explicit:
        supplied["product_payload_view"] = "legacy"
    model = Envelope.model_validate({"config": supplied})
    before = set(model.config.model_fields_set)
    for wire in (model.model_dump(mode="json"), json.loads(model.model_dump_json())):
        assert ("product_payload_view" in wire["config"]) is (product_event or explicit)
    assert model.config.model_fields_set == before


def test_unrelated_model_keeps_its_defaults():
    class Unrelated(AdCPBaseModel):
        managed_delivery: bool = False
        supports_webhook_activity: bool = False
        product_payload_view: str = "legacy"

    model = Unrelated()
    expected = {
        "managed_delivery": False,
        "supports_webhook_activity": False,
        "product_payload_view": "legacy",
    }
    assert model.model_dump() == expected
    assert json.loads(model.model_dump_json()) == expected
