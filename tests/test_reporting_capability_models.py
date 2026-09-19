"""Public reporting promises are nullable, explicit and tier-consistent (#1180)."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from adcp.types import GetAdcpCapabilitiesResponse, ReportingDeliveryCapabilities
from tests.test_reporting_ledger import _OFFERING

PROMISES = {
    "receipt_task": "sync_reporting_receipts",
    "readiness_notification": "reporting.delivery_ready",
    "status_notification": "reporting.status_changed",
    "ledger_notification": "reporting.ledger_changed",
}


def capability(**fields):
    return {
        "supported": True,
        "offerings": [_OFFERING],
        "automated_recovery_window_seconds": 0,
        "status_retention_days": 7,
        **fields,
    }


def response(raw):
    return {
        "status": "completed",
        "adcp": {
            "major_versions": [3],
            "idempotency": {"supported": True, "replay_ttl_seconds": 3600},
        },
        "supported_protocols": ["media_buy"],
        "media_buy": {"reporting_delivery": raw},
    }


@pytest.fixture(params=["public", "bundled"])
def graph(request):
    if request.param == "public":
        return ReportingDeliveryCapabilities, GetAdcpCapabilitiesResponse
    # Codegen's self-contained clone is deliberately tested as a separate graph.
    module = importlib.import_module(
        "adcp.types.generated_poc.bundled.protocol.get_adcp_capabilities_response"
    )
    return module.ReportingDelivery, module.GetAdcpCapabilitiesResponse


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"managed_delivery": False},
        {"reconciled_billing": False},
        {"managed_delivery": False, "reconciled_billing": False},
    ],
)
def test_core_attributes_schema_and_json_round_trip_do_not_synthesize_promises(graph, flags):
    model, envelope = graph
    value = model.model_validate(capability(**flags))
    fields = set(value.model_fields_set)
    schema = model.model_json_schema()
    for name in PROMISES:
        field = model.model_fields[name]
        assert field.default is None
        assert type(None) in get_args(field.annotation)
        assert getattr(value, name) is None
        assert schema["properties"][name]["default"] is None
        assert {"type": "null"} in schema["properties"][name]["anyOf"]
    for raw in (
        value.model_dump(),
        value.model_dump(mode="json"),
        json.loads(value.model_dump_json()),
    ):
        assert not PROMISES.keys() & raw.keys()
        assert model.model_validate(raw).model_dump(mode="json") == value.model_dump(mode="json")
    restored = model.model_validate_json(value.model_dump_json())
    assert restored.model_dump(mode="json") == value.model_dump(mode="json")
    nested = envelope.model_validate(response(capability(**flags)))
    for raw in (nested.model_dump(mode="json"), json.loads(nested.model_dump_json())):
        assert not PROMISES.keys() & raw["media_buy"]["reporting_delivery"].keys()
        restored = envelope.model_validate_json(json.dumps(raw))
        assert restored.media_buy.reporting_delivery.receipt_task is None
        assert restored.media_buy.reporting_delivery.readiness_notification is None
    assert fields == value.model_fields_set


@pytest.mark.parametrize(
    "field,tier",
    [("readiness_notification", "managed_delivery"), ("receipt_task", "reconciled_billing")],
)
@pytest.mark.parametrize("flag", [None, False])
def test_inverse_tier_validation_runs_in_both_standalone_and_nested_graphs(
    graph, field, tier, flag
):
    model, envelope = graph
    raw = capability(**{field: PROMISES[field], **({tier: flag} if flag is not None else {})})
    with pytest.raises(ValidationError, match=f"{field} requires {tier}"):
        model.model_validate(raw)
    with pytest.raises(ValidationError, match=f"{field} requires {tier}"):
        envelope.model_validate(response(raw))


@pytest.mark.parametrize("managed", [None, False])
def test_reconciled_is_cumulative_even_without_a_receipt_task(graph, managed):
    model, envelope = graph
    raw = capability(reconciled_billing=True, managed_delivery=managed)
    for cls, body in ((model, raw), (envelope, response(raw))):
        with pytest.raises(ValidationError, match="reconciled_billing requires managed_delivery"):
            cls.model_validate(body)


@pytest.mark.parametrize("field", PROMISES)
def test_each_explicit_supported_literal_survives_without_implying_other_promises(graph, field):
    model, envelope = graph
    flags = {"managed_delivery": True} if field == "readiness_notification" else {}
    if field == "receipt_task":
        flags = {"managed_delivery": True, "reconciled_billing": True}
    raw = capability(**flags, **{field: PROMISES[field]})
    for cls, body in ((model, raw), (envelope, response(raw))):
        value = cls.model_validate(body)
        for output in (value.model_dump(mode="json"), json.loads(value.model_dump_json())):
            block = output if cls is model else output["media_buy"]["reporting_delivery"]
            assert {key: block[key] for key in PROMISES if key in block} == {field: PROMISES[field]}
        with pytest.raises(ValidationError):
            cls.model_validate(
                {**raw, field: "unsupported"}
                if cls is model
                else response({**raw, field: "unsupported"})
            )


def test_subclasses_inside_a_non_sdk_parent_omit_absent_promises():
    class Reporting(ReportingDeliveryCapabilities):
        pass

    class OrdinaryParent(BaseModel):
        reporting: Reporting

    value = OrdinaryParent.model_validate({"reporting": capability()})
    assert all(getattr(value.reporting, name) is None for name in PROMISES)
    for raw in (value.model_dump(), json.loads(value.model_dump_json())):
        assert not PROMISES.keys() & raw["reporting"].keys()
    value = OrdinaryParent.model_validate(
        {"reporting": capability(status_notification=PROMISES["status_notification"])}
    )
    assert value.model_dump()["reporting"]["status_notification"] == PROMISES["status_notification"]


def test_post_generation_repair_is_idempotent_for_both_actual_model_layouts(tmp_path, monkeypatch):
    from scripts import post_generate_fixes

    root = Path(__file__).parents[1] / "src/adcp/types/generated_poc"
    targets = (
        "core/reporting_delivery_capabilities.py",
        "bundled/protocol/get_adcp_capabilities_response.py",
    )
    for relative in targets:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    monkeypatch.setattr(post_generate_fixes, "OUTPUT_DIR", tmp_path)
    before = [(tmp_path / name).read_bytes() for name in targets]
    post_generate_fixes.fix_reporting_capability_defaults()
    assert before == [(tmp_path / name).read_bytes() for name in targets]
