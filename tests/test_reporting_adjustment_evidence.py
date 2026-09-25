"""Pure evidence/receipt contracts, independent of transport and history selection."""

from __future__ import annotations

import hashlib
import json
import traceback
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from typing import Any

import pytest
import rfc8785

from adcp.reporting.adjustment_evidence import (
    ReportingAdjustmentEvidence,
    ReportingAdjustmentEvidenceError,
    ReportingAdjustmentEvidenceLimits,
    ReportingAdjustmentReceiptContext,
    ReportingAdjustmentScope,
    build_reporting_adjustment_receipt,
    capture_reporting_adjustment_evidence,
)
from adcp.types import (
    ReportingAdjustment,
    ReportingAdjustmentReceipt,
    ReportingObligation,
    ReportingRevision,
)
from adcp.validation.schema_loader import get_named_validator

SECRET = "private-body-marker"
SCOPE = ReportingAdjustmentScope(
    "https://seller.example/agent", "account-1", "consumer-1", "obligation-1"
)
NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
CONTEXT = ReportingAdjustmentReceiptContext(
    SCOPE, "revision-official", datetime(2026, 9, 2, tzinfo=timezone.utc), (("spend", "USD"),)
)


def adjustment(**changes: Any) -> dict[str, Any]:
    raw = {
        "reporting_adjustment_id": "adjustment-1",
        "adjusts_reporting_revision_id": "revision-official",
        "reason_code": "source_correction",
        "accounting_period": {"start": "2026-09-01T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
        "control_total_deltas": [
            {"name": "spend", "value": "-0.00", "value_type": "decimal", "unit": "USD"}
        ],
        "correction_observed_at": "2026-09-02T05:00:00.000+05:00",
        "created_at": "2026-09-02T00:00:00.000000Z",
    }
    raw.update(changes)
    raw["canonical_adjustment_sha256"] = hashlib.sha256(rfc8785.dumps(raw)).hexdigest()
    return raw


def capture(raw: dict[str, Any] | None = None, **kwargs: Any) -> ReportingAdjustmentEvidence:
    raw = adjustment() if raw is None else raw
    return capture_reporting_adjustment_evidence(
        raw, typed_adjustment=ReportingAdjustment.model_validate(raw), scope=SCOPE, **kwargs
    )


def build(
    evidence: ReportingAdjustmentEvidence | None = None, **kwargs: Any
) -> ReportingAdjustmentReceipt:
    return build_reporting_adjustment_receipt(
        capture() if evidence is None else evidence,
        kwargs.pop("context", CONTEXT),
        reporting_receipt_id=kwargs.pop("reporting_receipt_id", "receipt-adjustment-0001"),
        observed_at=kwargs.pop("observed_at", NOW),
        **kwargs,
    )


def rejected(code: str, call: Any) -> None:
    with pytest.raises(ReportingAdjustmentEvidenceError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.args == (code,)
    assert SECRET not in repr(caught.value)
    assert SECRET not in "".join(traceback.format_exception(caught.type, caught.value, caught.tb))


@pytest.mark.parametrize("detail", [None, "café", "cafe\u0301", "accounting \U0001f4b0"])
def test_raw_values_and_presence_are_hashed_before_model_normalization(detail: str | None) -> None:
    raw = adjustment(**({} if detail is None else {"reason_detail": detail}))
    typed = ReportingAdjustment.model_validate(raw)
    wire = json.dumps(raw, ensure_ascii=False, indent=2).encode()
    evidence = capture_reporting_adjustment_evidence(wire, typed_adjustment=typed, scope=SCOPE)
    expected = dict(raw)
    expected.pop("canonical_adjustment_sha256")
    assert evidence.raw_json == wire
    assert evidence.canonical_json == rfc8785.dumps(expected)
    assert evidence.observed_adjustment_sha256 == raw["canonical_adjustment_sha256"]
    assert evidence.input_kind == "bytes"
    assert evidence.adjustment == typed
    normalized = typed.model_dump(mode="json", exclude_none=True)
    normalized.pop("canonical_adjustment_sha256")
    assert rfc8785.dumps(normalized) != evidence.canonical_json
    assert json.loads(evidence.canonical_json)["control_total_deltas"][0]["value"] == "-0.00"
    assert json.loads(evidence.canonical_json)["control_total_deltas"][0]["value_type"] == "decimal"


def test_key_order_is_irrelevant_but_optional_presence_unicode_and_array_order_are_not() -> None:
    raw = adjustment()
    assert (
        capture(dict(reversed(list(raw.items())))).observed_adjustment_sha256
        == capture(raw).observed_adjustment_sha256
    )
    without_unit = deepcopy(raw)
    del without_unit["control_total_deltas"][0]["unit"]
    assert (
        capture(without_unit).observed_adjustment_sha256 != capture(raw).observed_adjustment_sha256
    )
    assert (
        capture(adjustment(reason_detail="é")).observed_adjustment_sha256
        != capture(adjustment(reason_detail="e\u0301")).observed_adjustment_sha256
    )
    two = adjustment(
        control_total_deltas=[
            {"name": "spend", "value": "1", "value_type": "decimal", "unit": "USD"},
            {"name": "fee", "value": "2", "value_type": "integer"},
        ]
    )
    reversed_deltas = deepcopy(two)
    reversed_deltas["control_total_deltas"].reverse()
    assert (
        capture(two).observed_adjustment_sha256
        != capture(reversed_deltas).observed_adjustment_sha256
    )


def test_mapping_is_snapshot_with_explicit_upstream_information_loss_limit() -> None:
    raw = adjustment(reason_detail=SECRET)
    typed = ReportingAdjustment.model_validate(raw)
    evidence = capture_reporting_adjustment_evidence(raw, typed_adjustment=typed, scope=SCOPE)
    before = evidence.raw_json, evidence.canonical_json, evidence.observed_adjustment_sha256
    raw["control_total_deltas"][0]["value"] = "999"
    typed.control_total_deltas[0].root.value = "999"
    evidence.adjustment.control_total_deltas[0].root.value = "999"
    assert (
        evidence.raw_json,
        evidence.canonical_json,
        evidence.observed_adjustment_sha256,
    ) == before
    assert evidence.input_kind == "mapping"
    assert evidence.adjustment.control_total_deltas[0].value == "-0.00"
    assert SECRET not in repr(evidence)
    assert "seller.example" not in repr(evidence.scope)
    with pytest.raises(FrozenInstanceError):
        evidence.raw_json = b"{}"
    restored = ReportingAdjustmentEvidence(evidence.scope, evidence.raw_json, evidence.input_kind)
    assert restored == evidence


@pytest.mark.parametrize(
    "wire",
    [
        b'{"reason_detail":"private-body-marker","reason_detail":"second"}',
        b'{"nested":{"value":"private-body-marker","value":"second"}}',
        b'{"reason_detail":"private-body-marker","bad":NaN}',
        b'{"reason_detail":"private-body-marker","bad":Infinity}',
        b'{"reason_detail":"private-body-marker","bad":9007199254740992}',
        b'{"reason_detail":"private-body-marker","bad":1e400}',
        b'{"reason_detail":"private-body-marker","bad":"\\ud800"}',
        b'{"reason_detail":"private-body-marker","bad":"\xff"}',
        b'"private-body-marker"',
        b"[]",
        b"null",
        b'{"private-body-marker":',
    ],
)
def test_strict_byte_admission_rejects_ambiguous_or_malformed_json(wire: bytes) -> None:
    rejected("INVALID_EVIDENCE", lambda: ReportingAdjustmentEvidence(SCOPE, wire, "bytes"))


def test_decoded_mapping_cannot_recover_duplicate_keys() -> None:
    raw = adjustment(reason_detail="safe")
    wire = (
        json.dumps(raw)
        .replace('"reason_detail": "safe"', '"reason_detail": "discarded", "reason_detail": "safe"')
        .encode()
    )
    rejected("INVALID_EVIDENCE", lambda: ReportingAdjustmentEvidence(SCOPE, wire, "bytes"))
    evidence = capture(json.loads(wire))
    assert evidence.input_kind == "mapping"
    assert evidence.adjustment.reason_detail == "safe"


@pytest.mark.parametrize(
    "changes",
    [
        {"reason_detail": None},
        {"extra": SECRET},
        {"reason_code": SECRET},
        {"control_total_deltas": [{"name": "spend", "value": 1}]},
        {
            "control_total_deltas": [
                {"name": "spend", "value": "1"},
                {"name": "spend", "value": "2"},
            ]
        },
        {"accounting_period": {"start": "2026-10-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"}},
        {"created_at": "2026-09-01T00:00:00Z"},
        {"created_at": "2026-09-02T00:00:00"},
    ],
)
def test_schema_and_cross_field_admission(changes: dict[str, Any]) -> None:
    raw = adjustment(**changes)
    rejected(
        "INVALID_EVIDENCE",
        lambda: ReportingAdjustmentEvidence(SCOPE, json.dumps(raw).encode(), "bytes"),
    )


def test_missing_advertised_digest_cannot_be_accepted_as_reconciled_evidence() -> None:
    raw = adjustment()
    del raw["canonical_adjustment_sha256"]
    rejected("INVALID_EVIDENCE", lambda: capture(raw))


def test_temporal_checks_preserve_sub_microsecond_precision() -> None:
    raw = adjustment(
        correction_observed_at="2026-09-02T00:00:00.0000002Z",
        created_at="2026-09-02T00:00:00.0000001Z",
    )
    # Both become the same datetime in the model; raw evidence still rejects their order.
    typed = ReportingAdjustment.model_validate(raw)
    assert typed.correction_observed_at == typed.created_at
    rejected("INVALID_EVIDENCE", lambda: capture(raw))
    raw = adjustment(
        accounting_period={
            "start": "2026-09-01T00:00:00.0000001Z",
            "end": "2026-09-01T00:00:00.0000002Z",
        },
        created_at="2026-09-03T00:00:00.0000001Z",
    )
    evidence = capture(raw)
    rejected("INVALID_RECEIPT", lambda: build(evidence))
    assert build(evidence, observed_at=NOW.replace(microsecond=1)).status == "accepted"


def test_generated_default_does_not_repair_a_missing_required_wire_field() -> None:
    raw = adjustment()
    del raw["control_total_deltas"][0]["value_type"]
    # Generated models accept a default here; the authoritative wire schema does not.
    ReportingAdjustment.model_validate(raw)
    rejected("INVALID_EVIDENCE", lambda: capture(raw))


@pytest.mark.parametrize(
    "limits",
    [
        ReportingAdjustmentEvidenceLimits(max_bytes=100),
        ReportingAdjustmentEvidenceLimits(max_depth=2),
        ReportingAdjustmentEvidenceLimits(max_nodes=10),
    ],
)
def test_byte_and_mapping_admission_share_explicit_resource_bounds(
    limits: ReportingAdjustmentEvidenceLimits,
) -> None:
    raw = adjustment()
    rejected("EVIDENCE_LIMIT_EXCEEDED", lambda: capture(raw, limits=limits))
    rejected(
        "EVIDENCE_LIMIT_EXCEEDED",
        lambda: ReportingAdjustmentEvidence(SCOPE, json.dumps(raw).encode(), "bytes", limits),
    )


def test_cycles_invalid_runtime_types_and_bad_limits_fail_closed() -> None:
    raw = adjustment()
    raw["loop"] = raw
    rejected(
        "INVALID_EVIDENCE",
        lambda: capture_reporting_adjustment_evidence(
            raw, typed_adjustment=ReportingAdjustment.model_validate(adjustment()), scope=SCOPE
        ),
    )
    rejected("INVALID_EVIDENCE", lambda: capture(limits=None))
    rejected("INVALID_EVIDENCE", lambda: ReportingAdjustmentEvidenceLimits(max_depth=0))
    raw = adjustment()
    raw["reason_detail"] = object()
    rejected(
        "INVALID_EVIDENCE",
        lambda: capture_reporting_adjustment_evidence(
            raw, typed_adjustment=ReportingAdjustment.model_validate(adjustment()), scope=SCOPE
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [("reporting_adjustment_id", "another"), ("reason_detail", SECRET), ("created_at", "bad")],
)
def test_mutated_or_unvalidated_typed_view_cannot_disagree_with_raw(field: str, value: str) -> None:
    raw = adjustment()
    typed = ReportingAdjustment.model_validate(raw).model_copy(update={field: value})
    rejected(
        "TYPED_EVIDENCE_MISMATCH",
        lambda: capture_reporting_adjustment_evidence(raw, typed_adjustment=typed, scope=SCOPE),
    )


def test_schema_unavailability_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adcp.reporting.adjustment_evidence.get_named_validator", lambda _: None)
    rejected("SCHEMA_UNAVAILABLE", capture)


def test_resolver_failure_does_not_leak_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_: str) -> None:
        raise RuntimeError(SECRET)

    monkeypatch.setattr("adcp.reporting.adjustment_evidence.get_named_validator", broken)
    rejected("SCHEMA_UNAVAILABLE", capture)


def test_receipt_identity_time_digest_and_wire_shape_are_deterministic() -> None:
    receipt = build()
    assert receipt == build()
    wire = receipt.model_dump(mode="json", exclude_none=True)
    assert wire == {
        "reporting_receipt_id": "receipt-adjustment-0001",
        "reporting_adjustment_id": "adjustment-1",
        "adjusts_reporting_revision_id": "revision-official",
        "status": "accepted",
        "observed_adjustment_sha256": capture().observed_adjustment_sha256,
        "observed_at": "2026-09-03T00:00:00Z",
    }
    validator = get_named_validator("core/reporting-adjustment-receipt.json")
    assert validator is not None and validator.is_valid(wire)
    assert not validator.is_valid({**wire, "rejection_codes": ["ADJUSTMENT_DIGEST_MISMATCH"]})
    assert not validator.is_valid({**wire, "status": "rejected"})


def test_digest_mismatch_and_caller_semantic_rejection_use_closed_unique_codes() -> None:
    raw = adjustment()
    raw["canonical_adjustment_sha256"] = "0" * 64
    receipt = build(
        capture(raw),
        rejection_codes=("ADJUSTMENT_SEMANTIC_MISMATCH", "ADJUSTMENT_SEMANTIC_MISMATCH"),
    )
    assert str(receipt.status) == "rejected"
    assert receipt.model_dump(mode="json")["rejection_codes"] == [
        "ADJUSTMENT_DIGEST_MISMATCH",
        "ADJUSTMENT_SEMANTIC_MISMATCH",
    ]
    assert receipt.observed_adjustment_sha256 == capture().observed_adjustment_sha256
    rejected("INVALID_RECEIPT", lambda: build(rejection_codes=(SECRET,)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("seller_identity", "https://other.example/agent"),
        ("account_id", "other"),
        ("consumer_id", "other"),
        ("reporting_obligation_id", "other"),
    ],
)
def test_cross_scope_context_never_reuses_evidence(field: str, value: str) -> None:
    other = replace(CONTEXT, scope=replace(SCOPE, **{field: value}))
    rejected("ADJUSTMENT_CONTEXT_MISMATCH", lambda: build(context=other))


@pytest.mark.parametrize(
    "context",
    [
        replace(CONTEXT, reporting_revision_id="wrong"),
        replace(CONTEXT, finalized_at=NOW),
        replace(CONTEXT, control_total_units=(("spend", "EUR"),)),
        replace(CONTEXT, control_total_units=(("another", "USD"),)),
    ],
)
def test_exact_target_official_lock_and_delta_units_are_required(
    context: ReportingAdjustmentReceiptContext,
) -> None:
    rejected("ADJUSTMENT_CONTEXT_MISMATCH", lambda: build(context=context))


def test_maximum_consumer_identity_is_preserved_without_normalization() -> None:
    prefix = "https://consumer.example/"
    consumer = prefix + "x" * (2048 - len(prefix))
    scope = replace(SCOPE, consumer_id=consumer)
    evidence = capture_reporting_adjustment_evidence(
        adjustment(), typed_adjustment=ReportingAdjustment.model_validate(adjustment()), scope=scope
    )
    assert build(evidence, context=replace(CONTEXT, scope=scope)).status == "accepted"
    assert evidence.scope.consumer_id == consumer
    rejected("INVALID_CONTEXT", lambda: replace(scope, consumer_id=consumer + "x"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"observed_at": datetime(2026, 9, 3)},
        {"observed_at": datetime(2026, 9, 1, tzinfo=timezone.utc)},
        {"reporting_receipt_id": "short"},
        {"reporting_receipt_id": SECRET + " bad"},
    ],
)
def test_bad_receipt_identity_or_time_never_escapes_as_model_error(kwargs: dict[str, Any]) -> None:
    rejected("INVALID_RECEIPT", lambda: build(**kwargs))


def test_current_rejected_leaf_is_superseded_exactly_and_accepted_is_terminal() -> None:
    prior = build(rejection_codes=("ADJUSTMENT_SEMANTIC_MISMATCH",))
    receipt = build(reporting_receipt_id="receipt-adjustment-0002", current_receipt=prior)
    assert receipt.supersedes_reporting_receipt_id == prior.reporting_receipt_id
    assert receipt.status == "accepted"
    rejected(
        "RECEIPT_TERMINAL",
        lambda: build(reporting_receipt_id="receipt-adjustment-0003", current_receipt=receipt),
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"reporting_adjustment_id": "another"},
        {"adjusts_reporting_revision_id": "another"},
        {"rejection_codes": None},
        {"observed_at": datetime(2026, 9, 4, tzinfo=timezone.utc)},
    ],
)
def test_invalid_or_foreign_current_leaf_is_rejected(updates: dict[str, Any]) -> None:
    prior = build(rejection_codes=("ADJUSTMENT_SEMANTIC_MISMATCH",)).model_copy(update=updates)
    rejected(
        "INVALID_RECEIPT",
        lambda: build(reporting_receipt_id="receipt-adjustment-0002", current_receipt=prior),
    )


def test_receipt_id_cannot_supersede_itself() -> None:
    rejected(
        "INVALID_RECEIPT",
        lambda: build(current_receipt=build(rejection_codes=("ADJUSTMENT_SEMANTIC_MISMATCH",))),
    )


def selected() -> tuple[ReportingObligation, ReportingRevision]:
    # Public model fixtures remain independent of transport and ledger test helpers.
    period = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-09-01T00:00:00Z",
        "source_timezone": "UTC",
    }
    coverage = {
        "status": "full",
        "evaluated_at": period["end"],
        "media_buy_ids": ["buy-1"],
        "fully_covered_media_buy_ids": ["buy-1"],
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": [],
        "unknown_media_buy_ids": [],
        "package_ids": [],
        "covered_package_ids": [],
        "unsupported_package_ids": [],
        "unknown_package_ids": [],
        "limitations": [],
    }
    shared = {
        "report_definition_id": "billing-v1",
        "reporting_profile": "billing-v1",
        "account_id": "account-1",
        "media_buy_ids": ["buy-1"],
        "coverage": coverage,
        "period": period,
    }
    obligation = ReportingObligation.model_validate(
        {
            **shared,
            "reporting_obligation_id": "obligation-1",
            "delivery_config_id": "billing-feed",
            "delivery_config_version": 1,
            "feed_purpose": "billing",
            "scope_resolved_at": period["end"],
            "expected_at": "2026-09-02T00:00:00Z",
            "schedule": {
                "period_duration": "P1M",
                "alignment": "billing_cycle",
                "delivery_sla": "P1D",
            },
            "destination_ref": "destination-1",
            "required_finality": "official",
            "reconciliation_mode": "consumer_receipt",
            "reconciliation_status": "pending",
            "health": "waiting",
            "production_status": "published",
            "revision_count": 1,
            "materialization_count": 1,
            "successful_materialization_count": 1,
            "receipt_count": 0,
            "accepted_receipt_count": 0,
            "issues": [],
            "resource_retained_until": "2026-12-01T00:00:00Z",
        }
    )
    revision = ReportingRevision.model_validate(
        {
            **shared,
            "reporting_revision_id": "revision-official",
            "revision_content_sha256": "e" * 64,
            "report_definition_uri": "https://schemas.example/billing.json",
            "report_definition_sha256": "d" * 64,
            "schema_version": "1",
            "schema_uri": "https://schemas.example/billing.json",
            "schema_sha256": "c" * 64,
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
            "schema_ref_policy": "local_fragment_only",
            "finality": "official",
            "finality_basis": "source_final",
            "finality_policy_id": "source-final",
            "finalized_at": "2026-09-02T00:00:00Z",
            "observed_at": "2026-09-02T00:00:00Z",
            "data_through": period["end"],
            "data_through_precision": "exact",
            "row_count": 7,
            "control_totals": [
                {"name": "spend", "value": "7000.00", "value_type": "decimal", "unit": "USD"}
            ],
            "canonical_content_digest": {
                "algorithm": "sha256",
                "value": "a" * 64,
                "canonicalization_id": "rows-v1",
                "canonicalization_uri": "https://schemas.example/rows.json",
                "canonicalization_sha256": "b" * 64,
            },
            "created_at": "2026-09-02T00:00:00Z",
        }
    )
    return obligation, revision


def test_selected_context_requires_exact_ownership_and_reconciled_billing_official() -> None:
    obligation, revision = selected()
    context = ReportingAdjustmentReceiptContext.from_selection(
        SCOPE, obligation=obligation, revision=revision, revision_owner="obligation-1"
    )
    assert context == CONTEXT
    revision.control_totals[0].root.unit = "EUR"
    assert context.control_total_units == (("spend", "USD"),)
    assert build(context=context).status == "accepted"
    rejected(
        "INVALID_CONTEXT",
        lambda: ReportingAdjustmentReceiptContext.from_selection(
            SCOPE, obligation=obligation, revision=revision, revision_owner="other"
        ),
    )


@pytest.mark.parametrize(
    "kind,updates",
    [
        ("obligation", {"account_id": "other"}),
        ("revision", {"account_id": "other"}),
        ("obligation", {"feed_purpose": "performance"}),
        ("obligation", {"reconciliation_mode": "none"}),
        ("revision", {"finality": "provisional"}),
        ("revision", {"finalized_at": None}),
        ("revision", {"report_definition_id": "other"}),
        ("revision", {"reporting_profile": "other"}),
        (
            "revision",
            {
                "period": {
                    "start": "2026-07-01T00:00:00Z",
                    "end": "2026-08-01T00:00:00Z",
                    "source_timezone": "UTC",
                }
            },
        ),
    ],
)
def test_selection_binding_cannot_be_inferred_from_an_unrelated_official(
    kind: str, updates: dict[str, Any]
) -> None:
    obligation, revision = selected()
    if kind == "obligation":
        obligation = obligation.model_copy(update=updates)
    else:
        revision = revision.model_copy(update=updates)
    rejected(
        "INVALID_CONTEXT",
        lambda: ReportingAdjustmentReceiptContext.from_selection(
            SCOPE, obligation=obligation, revision=revision, revision_owner="obligation-1"
        ),
    )
