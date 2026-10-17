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


#: What allOf member 3 requires of a managed_delivery tier: the two retention
#: windows, and an offering naming the delivery method.
MANAGED = {
    "resource_retention_days": 90,
    "authorization_revocation_seconds": 3600,
    "offerings": [
        {
            **_OFFERING,
            "method": {
                "pattern": "file_transfer",
                "transport": "s3",
                "orchestration": "producer_managed",
                "destination_modes": ["existing"],
                "provider": {"domain": "aws.amazon.com"},
                "format": "jsonl",
            },
        }
    ],
}

#: allOf member 9 of the response: a seller emitting a reporting notification signs
#: its outbound webhooks.
WEBHOOK_SIGNING = {
    "supported": True,
    "profile": "adcp/webhook-signing/v1",
    "algorithms": ["ed25519"],
    "legacy_hmac_fallback": False,
}


def _reconciled_offering():
    """The managed offering under the reconciled-billing tier.

    allOf member 4 of the capabilities requires a consumer_receipt offering, and
    member 1 of core/reporting-delivery-offering.json then requires that offering's
    profile to state the whole canonical-digest contract.
    """
    offering = dict(MANAGED["offerings"][0])
    offering["reconciliation_mode"] = "consumer_receipt"
    offering["reporting_profile"] = {
        **offering["reporting_profile"],
        "canonicalization_id": "rows-v1",
        "canonicalization_contract_version": "1.0",
        "canonicalization_media_type": "application/vnd.adcp.reporting-canonicalization+json",
        "canonicalization_uri": "https://contracts.example.test/canonicalization/rows-v1.json",
        "canonicalization_sha256": "c" * 64,
    }
    return offering


def capability(**fields):
    if fields.get("managed_delivery") is True:
        fields = {**MANAGED, **fields}
    if fields.get("reconciled_billing") is True and fields.get("managed_delivery") is True:
        fields = {**fields, "offerings": [_reconciled_offering()]}
    return {
        "supported": True,
        # core/reporting-delivery-capabilities.json requires both tasks at its root;
        # neither is a tier promise, so neither belongs in PROMISES.
        "configuration_task": "sync_accounts",
        "status_task": "get_reporting_status",
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
        # A seller declaring media_buy.reporting_delivery declares the experimental
        # feature that names it (protocol/get-adcp-capabilities-response.json).
        "experimental_features": ["media_buy.reporting_delivery"],
        "webhook_signing": dict(WEBHOOK_SIGNING),
        "media_buy": {"reporting_delivery": raw},
    }


@pytest.fixture(params=["public", "bundled"])
def graph(request):
    """``(reporting-delivery model, envelope, carries the derived root constraints)``.

    Only the public model does. The clone's reporting_delivery is an inline nested
    object inside the bundled response file, and the reader compiles a schema's ROOT
    constraints, so the clone is graded by the response's own conjuncts plus the
    authored tier validator. The two graphs therefore refuse the same documents with
    different refusals, and a test naming one has to say which graph it is in.
    """
    if request.param == "public":
        return ReportingDeliveryCapabilities, GetAdcpCapabilitiesResponse, True
    # Codegen's self-contained clone is deliberately tested as a separate graph.
    module = importlib.import_module(
        "adcp.types.domains.bundled.protocol.get_adcp_capabilities_response"
    )
    return module.ReportingDelivery, module.GetAdcpCapabilitiesResponse, False


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
    model, envelope, derived = graph
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
    "field,tier,derived_refusal",
    [
        # core/reporting-delivery-capabilities.json states this one in its root
        # allOf, so where the derived constraint exists it names it and arrives first.
        (
            "readiness_notification",
            "managed_delivery",
            r"root conjunct 7\) over managed_delivery, readiness_notification",
        ),
        # The schema root states no inverse for this one, so the authored validator
        # is the only owner in either graph.
        ("receipt_task", "reconciled_billing", None),
    ],
    ids=["readiness_notification-managed_delivery", "receipt_task-reconciled_billing"],
)
@pytest.mark.parametrize("flag", [None, False])
def test_inverse_tier_validation_runs_in_both_standalone_and_nested_graphs(
    graph, field, tier, flag, derived_refusal
):
    model, envelope, derived = graph
    refusal = derived_refusal if derived and derived_refusal else f"{field} requires {tier}"
    raw = capability(**{field: PROMISES[field], **({tier: flag} if flag is not None else {})})
    with pytest.raises(ValidationError, match=refusal):
        model.model_validate(raw)
    with pytest.raises(ValidationError, match=refusal):
        envelope.model_validate(response(raw))


@pytest.mark.parametrize("managed", [None, False])
def test_reconciled_is_cumulative_even_without_a_receipt_task(graph, managed):
    model, envelope, derived = graph
    # The tier is not free-standing: allOf member 4 requires a receipt_task and a
    # consumer_receipt offering, and that offering's own method then requires
    # managed_delivery through member 5, so the cumulative rule is the schema's own.
    # The clone, carrying no derived root constraint, refuses through the authored one.
    refusal = (
        r"root conjunct 4\) over offerings, receipt_task, reconciled_billing"
        if derived
        else "reconciled_billing requires managed_delivery"
    )
    raw = capability(reconciled_billing=True, managed_delivery=managed)
    for cls, body in ((model, raw), (envelope, response(raw))):
        with pytest.raises(ValidationError, match=refusal):
            cls.model_validate(body)


@pytest.mark.parametrize("field", PROMISES)
def test_each_explicit_supported_literal_survives_without_implying_other_promises(graph, field):
    model, envelope, derived = graph
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

    root = Path(__file__).parents[1] / "src/adcp/types/domains"
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
