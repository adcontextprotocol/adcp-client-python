"""Buyer read safety through the public full-read and pure-evaluation APIs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingLedger,
    ReportingReconciliationError,
    evaluate_reporting_ledger,
    load_reporting_ledger,
)
from adcp.reporting.ownership import with_revision_ownership
from adcp.types import GetReportingStatusRequest, GetReportingStatusResponse
from adcp.types.core import TaskResult, TaskStatus
from tests.test_reporting_reconciliation import (
    DIGEST,
    PERIOD,
    REVISION,
    TOTALS,
    _full_finality_scope,
    _response,
)

NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
OWNER = "obligation-billing"
REVISION_ID = "revision-august-official"
SECRET = "secret-wire-body-never-diagnostic"
ARRAYS = (
    "periods",
    "revisions",
    "materializations",
    "receipts",
    "adjustments",
    "adjustment_receipts",
    "consumer_statuses",
)


def _receipt(**changes: Any) -> dict[str, Any]:
    result = {
        "reporting_receipt_id": "receipt-accepted",
        "reporting_obligation_id": OWNER,
        "reporting_revision_id": REVISION_ID,
        "reporting_materialization_id": "materialization-billing",
        "status": "accepted",
        "verification_profile": "canonical_digest",
        "observed_row_count": 7,
        "observed_control_totals": deepcopy(TOTALS),
        "observed_canonical_content_digest": deepcopy(DIGEST),
        "observed_at": "2026-09-02T00:01:00Z",
        "received_at": "2026-09-02T00:01:01Z",
    }
    result.update(changes)
    if result["status"] == "rejected":
        result["rejection_codes"] = ["EVIDENCE_MISMATCH"]
    return result


def _adjustment() -> dict[str, Any]:
    return {
        "reporting_adjustment_id": "adjustment-correction",
        "adjusts_reporting_revision_id": REVISION_ID,
        "reason_code": "source_correction",
        "accounting_period": {"start": PERIOD["start"], "end": PERIOD["end"]},
        "control_total_deltas": [
            {"name": "spend", "value": "-1.00", "value_type": "decimal", "unit": "USD"}
        ],
        # This is seller-advertised evidence, not a claim of raw-byte hashing.
        "canonical_adjustment_sha256": "c" * 64,
        "correction_observed_at": "2026-09-02T00:01:02Z",
        "created_at": "2026-09-02T00:01:03Z",
    }


def _adjustment_receipt(**changes: Any) -> dict[str, Any]:
    result = {
        "reporting_receipt_id": "adjustment-receipt-accepted",
        "reporting_adjustment_id": "adjustment-correction",
        "adjusts_reporting_revision_id": REVISION_ID,
        "status": "accepted",
        "observed_adjustment_sha256": "c" * 64,
        "observed_at": "2026-09-02T00:01:04Z",
        "received_at": "2026-09-02T00:01:05Z",
    }
    result.update(changes)
    if result["status"] == "rejected":
        result["rejection_codes"] = ["ADJUSTMENT_DIGEST_MISMATCH"]
    return result


def _history(*, adjustments: bool = False) -> dict[str, Any]:
    raw = deepcopy(_response([_receipt()]))
    raw["ledger_as_of"] = "2026-09-02T00:02:00Z"
    raw["changes_checkpoint"] = "frozen-checkpoint"
    raw["adjustments"] = [_adjustment()] if adjustments else []
    raw["adjustment_receipts"] = [_adjustment_receipt()] if adjustments else []
    raw["consumer_statuses"] = []
    _counts(raw)
    return raw


def _with_status(raw: dict[str, Any]) -> dict[str, Any]:
    raw["consumer_statuses"] = [
        {
            "reporting_status_id": "consumer-status-current",
            "delivery_config_id": "billing-feed",
            "delivery_config_version": 1,
            "report_definition_id": "billing-v1",
            "period": deepcopy(PERIOD),
            "reporting_obligation_id": OWNER,
            "reporting_revision_id": REVISION_ID,
            "observed_revision_content_sha256": REVISION["revision_content_sha256"],
            "consumer_status": "received",
            "status_as_of": "2026-09-02T00:01:00Z",
        }
    ]
    raw["periods"][0].update(
        consumer_status_count=1, current_consumer_status_id="consumer-status-current"
    )
    _counts(raw)
    return raw


def _counts(raw: dict[str, Any]) -> None:
    """Recompute fixture counts; mutation tests deliberately run after this."""
    for obligation in raw["periods"]:
        owner = obligation["reporting_obligation_id"]
        materials = [m for m in raw["materializations"] if m["reporting_obligation_id"] == owner]
        receipts = [r for r in raw["receipts"] if r["reporting_obligation_id"] == owner]
        # These fixtures normally have one owner; multi-owner tests set their
        # explicit per-owner counts themselves.
        obligation.update(
            revision_count=len(raw["revisions"]),
            materialization_count=len(materials),
            successful_materialization_count=sum(
                m["status"] in {"available", "delivered"} for m in materials
            ),
            receipt_count=len(receipts),
            accepted_receipt_count=sum(r["status"] == "accepted" for r in receipts),
            adjustment_count=len(raw.get("adjustments", [])),
            adjustment_receipt_count=len(raw.get("adjustment_receipts", [])),
            accepted_adjustment_receipt_count=sum(
                r["status"] == "accepted" for r in raw.get("adjustment_receipts", [])
            ),
        )
    raw["pagination"] = {
        "has_more": False,
        "total_count": sum(len(raw.get(name, [])) for name in ARRAYS),
    }


def _pages(raw: dict[str, Any], *, explicit: bool = True) -> list[dict[str, Any]]:
    rows = [(name, row) for name in ARRAYS for row in raw.get(name, [])]
    pages = []
    for name, row in rows or [("periods", None)]:
        page = {key: deepcopy(value) for key, value in raw.items() if key not in ARRAYS}
        page.update({array: [] for array in ARRAYS})
        if row is not None:
            page[name] = [deepcopy(row)]
        if explicit:
            page = with_revision_ownership(
                page, {r["reporting_revision_id"]: OWNER for r in raw["revisions"]}
            )
        pages.append(page)
    for index, page in enumerate(pages):
        page["pagination"]["has_more"] = index < len(pages) - 1
        if page["pagination"]["has_more"]:
            page["pagination"]["cursor"] = f"page-{index + 1}"
    return pages


class Pages:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.requests: list[GetReportingStatusRequest] = []

    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]:
        page = self.pages[len(self.requests)]
        self.requests.append(request)
        return TaskResult(
            status=TaskStatus.COMPLETED,
            data=GetReportingStatusResponse.model_validate(deepcopy(page)),
        )


def _request(**changes: Any) -> GetReportingStatusRequest:
    return GetReportingStatusRequest.model_validate(
        {"account": {"account_id": "account-1"}, "view": "periods", **changes}
    )


async def _load(raw: dict[str, Any], *, explicit: bool = True) -> ReportingLedger:
    return await load_reporting_ledger(Pages(_pages(raw, explicit=explicit)), _request())


def _expected() -> list[ExpectedReportingPeriod]:
    return [
        ExpectedReportingPeriod(
            "billing-feed",
            1,
            "billing-v1",
            "billing",
            "billing-v1",
            ("buy-1", "buy-2"),
            PERIOD["start"],
            PERIOD["end"],
            expected_at="2026-09-02T00:00:00Z",
        )
    ]


async def test_full_read_discards_incremental_omissions_and_cursor_but_keeps_page_size() -> None:
    class IncrementalOmissions(Pages):
        async def get_reporting_status(self, request):
            if request.changes_after:
                raw = _history()
                raw.update({name: [] for name in ARRAYS})
                raw["pagination"]["total_count"] = 0
                self.requests.append(request)
                return TaskResult(
                    status=TaskStatus.COMPLETED,
                    data=GetReportingStatusResponse.model_validate(raw),
                )
            return await super().get_reporting_status(request)

    client = IncrementalOmissions(_pages(_history()))
    request = _request(
        changes_after="incremental-checkpoint",
        pagination={"cursor": "untrusted-continuation", "max_results": 1},
    )
    original = request.model_dump(mode="json")
    ledger = await load_reporting_ledger(client, request)
    result = evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW)
    assert result.definitive
    assert result.missing_expected_periods == []
    assert request.model_dump(mode="json") == original
    assert len(client.requests) == 4
    assert all(r.changes_after is None for r in client.requests)
    assert all(r.pagination.max_results == 1 for r in client.requests)
    assert client.requests[0].pagination.cursor is None


async def test_health_finality_and_exact_revision_selectors_cannot_hide_history() -> None:
    client = Pages(_pages(_history()))
    request = _request(
        view="revision",
        reporting_revision_id=REVISION_ID,
        health=["action_required"],
        finality=["official"],
    )
    await load_reporting_ledger(client, request)
    assert all(r.view.value == "periods" for r in client.requests)
    assert all(
        r.health is None and r.finality is None and r.reporting_revision_id is None
        for r in client.requests
    )


@pytest.mark.parametrize("outside", ["horizon", "generation", "media-buy", "retention"])
async def test_expected_period_outside_the_proven_scope_is_not_an_obligation_missing_claim(
    outside,
) -> None:
    raw = _history()
    raw.update({name: [] for name in ARRAYS})
    raw["pagination"]["total_count"] = 0
    expected = _expected()[0]
    if outside == "horizon":
        expected = replace(
            expected, period_start="2026-07-01T00:00:00Z", period_end=PERIOD["start"]
        )
    elif outside == "generation":
        expected = replace(expected, delivery_config_version=2)
    elif outside == "media-buy":
        expected = replace(expected, media_buy_ids=("outside-authorized-scope",))
    else:
        raw["scope"]["coverage_complete"] = False
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=[expected], now=NOW)
    assert not result.definitive
    assert result.missing_expected_periods == []


async def test_full_retained_snapshot_can_establish_a_missing_obligation() -> None:
    raw = _history()
    # The expectation has no independent finality fact. Absence proof requires
    # a denominator spanning both possible configuration requirements.
    _full_finality_scope(raw)
    raw.update({name: [] for name in ARRAYS})
    raw["pagination"]["total_count"] = 0
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert not result.definitive
    assert result.missing_expected_periods == _expected()


async def test_unproven_ledger_cannot_claim_definitive_or_missing_obligations() -> None:
    raw = _history()
    response = GetReportingStatusResponse.model_validate(raw)
    manual = ReportingLedger(
        response.ledger_snapshot_id,
        response.ledger_as_of,
        response.account_id,
        response.scope,
        [],
        [],
        [],
        [],
    )
    result = evaluate_reporting_ledger(manual, expected_periods=_expected(), now=NOW)
    assert not result.definitive
    assert result.missing_expected_periods == []
    assert not evaluate_reporting_ledger(manual, expected_periods=[], now=NOW).definitive


async def test_completed_snapshot_cannot_be_edited_into_different_evidence() -> None:
    ledger = await _load(_history())
    ledger.receipts[0].observed_row_count = 99
    with pytest.raises(ReportingReconciliationError) as error:
        evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW)
    assert error.value.code == "LEDGER_CHANGED"


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("field", ["changes_checkpoint", "next_expected_at", "health"])
async def test_frozen_metadata_is_checked_including_wholly_legacy_pages(explicit, field) -> None:
    pages = _pages(_history(), explicit=explicit)
    pages[-1][field] = {
        "changes_checkpoint": "different-checkpoint",
        "next_expected_at": "2026-10-01T00:00:00Z",
        "health": "waiting",
    }[field]
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request(), max_snapshot_restarts=0)
    assert error.value.code == "SNAPSHOT_CHANGED"


async def test_every_page_requires_the_frozen_total() -> None:
    pages = _pages(_history())
    pages[0]["pagination"].pop("total_count")
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request(), max_snapshot_restarts=0)
    assert error.value.code == "INCOMPLETE_LEDGER_PAGE"


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("periods", "health", "waiting"),
        ("revisions", "row_count", 99),
        ("materializations", "attempt", 2),
        ("receipts", "observed_row_count", 99),
        ("adjustments", "reason_detail", "changed correction"),
        ("adjustment_receipts", "observed_adjustment_sha256", "0" * 64),
        ("consumer_statuses", "consumer_commit_ref", "changed-consumer-checkpoint"),
    ],
)
async def test_every_record_kind_requires_immutable_cross_page_content(kind, field, value) -> None:
    pages = _pages(_with_status(_history(adjustments=True)))
    index = next(i for i, page in enumerate(pages) if page[kind])
    replay = deepcopy(pages[index])
    replay[kind][0][field] = value
    pages[index]["pagination"].update(has_more=True, cursor="before-conflict")
    pages.insert(index + 1, replay)
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request())
    assert error.value.code == "IMMUTABLE_RECORD_CHANGED"


async def test_optional_field_presence_is_part_of_typed_immutable_content() -> None:
    pages = _pages(_history())
    replay = deepcopy(pages[-1])
    replay["receipts"][0]["consumer_commit_ref"] = None
    pages[-1]["pagination"].update(has_more=True, cursor="before-optional-change")
    pages.append(replay)
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request())
    assert error.value.code == "IMMUTABLE_RECORD_CHANGED"


@pytest.mark.parametrize(
    "field",
    [
        "materialization_count",
        "successful_materialization_count",
        "receipt_count",
        "accepted_receipt_count",
        "consumer_status_count",
    ],
)
async def test_frozen_counts_cover_materializations_receipts_and_consumer_statuses(field) -> None:
    raw = _with_status(_history(adjustments=True))
    raw["periods"][0][field] += 1
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


async def test_positive_status_count_requires_its_exact_current_status_dependency() -> None:
    raw = _with_status(_history())
    raw["periods"][0]["current_consumer_status_id"] = "unknown-current-status"
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "INVALID_LEDGER_DEPENDENCY"


async def test_legacy_typed_status_without_owner_uses_exact_revision_binding() -> None:
    raw = _with_status(_history())
    raw["consumer_statuses"][0].pop("reporting_obligation_id")
    ledger = await _load(raw)
    assert ledger.consumer_statuses[0].reporting_obligation_id is None
    assert evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW).definitive


@pytest.mark.parametrize("bounds", [{"max_bytes": 100}, {"max_records": 4}])
async def test_byte_and_received_row_budgets_include_identical_replays(bounds) -> None:
    pages = _pages(_history())
    replay = deepcopy(pages[0])
    replay["pagination"]["cursor"] = "replayed"
    pages.insert(1, replay)
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request(), **bounds)
    assert error.value.code == "LEDGER_LIMIT_EXCEEDED"


async def test_detaches_returned_pages_before_a_client_reuses_its_models() -> None:
    models = [GetReportingStatusResponse.model_validate(p) for p in _pages(_history())]

    class ReusingClient(Pages):
        async def get_reporting_status(self, request):
            index = len(self.requests)
            if index:
                models[0].periods[0].account_id = "changed-after-return"
            self.requests.append(request)
            return TaskResult(status=TaskStatus.COMPLETED, data=models[index])

    ledger = await load_reporting_ledger(ReusingClient([]), _request())
    assert ledger.obligations[0].account_id == "account-1"
    assert evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW).definitive


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize(
    "field", ["adjustment_count", "adjustment_receipt_count", "accepted_adjustment_receipt_count"]
)
async def test_adjustment_frozen_counts_must_match_before_evaluation(explicit, field) -> None:
    raw = _history(adjustments=True)
    raw["periods"][0][field] += 1
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw, explicit=explicit)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


@pytest.mark.parametrize("kind", ["revision", "adjustment"])
@pytest.mark.parametrize(
    "topology", ["fork", "cycle", "disconnected-cycle", "gap", "roots", "accepted-predecessor"]
)
async def test_every_receipt_chain_is_exact_and_complete(kind, topology) -> None:
    raw = _history(adjustments=kind == "adjustment")
    build = _receipt if kind == "revision" else _adjustment_receipt
    array = "receipts" if kind == "revision" else "adjustment_receipts"
    first = build(reporting_receipt_id="reporting-receipt-first", status="rejected")
    second = build(
        reporting_receipt_id="reporting-receipt-second",
        supersedes_reporting_receipt_id="reporting-receipt-first",
    )
    records = [first, second]
    if topology == "fork":
        records.append(
            build(
                reporting_receipt_id="reporting-receipt-fork",
                supersedes_reporting_receipt_id="reporting-receipt-first",
            )
        )
    elif topology == "cycle":
        first["supersedes_reporting_receipt_id"] = "reporting-receipt-second"
        second.update(status="rejected", rejection_codes=["EVIDENCE_MISMATCH"])
    elif topology == "disconnected-cycle":
        records.extend(
            [
                build(
                    reporting_receipt_id="reporting-cycle-a",
                    status="rejected",
                    supersedes_reporting_receipt_id="reporting-cycle-b",
                ),
                build(
                    reporting_receipt_id="reporting-cycle-b",
                    status="rejected",
                    supersedes_reporting_receipt_id="reporting-cycle-a",
                ),
            ]
        )
    elif topology == "gap":
        second["supersedes_reporting_receipt_id"] = "missing-predecessor"
    elif topology == "roots":
        second.pop("supersedes_reporting_receipt_id")
    else:
        first.update(status="accepted")
        first.pop("rejection_codes")
    raw[array] = records
    _counts(raw)
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "INVALID_RECEIPT_CHAIN"


@pytest.mark.parametrize("kind", ["revision", "adjustment"])
async def test_rejected_predecessors_and_only_the_accepted_current_leaf_satisfy(kind) -> None:
    raw = _history(adjustments=kind == "adjustment")
    build = _receipt if kind == "revision" else _adjustment_receipt
    array = "receipts" if kind == "revision" else "adjustment_receipts"
    raw[array] = [
        build(
            reporting_receipt_id="reporting-receipt-current",
            supersedes_reporting_receipt_id="receipt-rejected",
        ),
        build(
            reporting_receipt_id="receipt-rejected",
            status="rejected",
            supersedes_reporting_receipt_id="reporting-receipt-root",
        ),
        build(reporting_receipt_id="reporting-receipt-root", status="rejected"),
    ]
    _counts(raw)
    ledger = await _load(raw)
    assert evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW).definitive
    raw[array][0].update(status="rejected", rejection_codes=["EVIDENCE_MISMATCH"])
    _counts(raw)
    ledger = await _load(raw)
    result = evaluate_reporting_ledger(ledger, expected_periods=_expected(), now=NOW)
    assert not result.definitive
    assert (
        "MISSING_MATCHING_CONSUMER_RECEIPT"
        if kind == "revision"
        else "MISSING_MATCHING_ADJUSTMENT_RECEIPT"
    ) in result.obligations[0].reasons


async def test_receipt_id_namespace_and_cross_kind_predecessors_are_not_interchangeable() -> None:
    for collision in (False, True):
        raw = _history(adjustments=True)
        raw["receipts"][0].update(status="rejected", rejection_codes=["EVIDENCE_MISMATCH"])
        key = "reporting_receipt_id" if collision else "supersedes_reporting_receipt_id"
        raw["adjustment_receipts"][0][key] = raw["receipts"][0]["reporting_receipt_id"]
        _counts(raw)
        with pytest.raises(ReportingReconciliationError) as error:
            await _load(raw)
        assert error.value.code == "INVALID_RECEIPT_CHAIN"


@pytest.mark.parametrize("kind", ["revision", "adjustment"])
async def test_receipt_predecessors_cannot_cross_exact_targets(kind) -> None:
    raw = _history(adjustments=kind == "adjustment")
    array = "receipts" if kind == "revision" else "adjustment_receipts"
    predecessor = deepcopy(raw[array][0])
    predecessor.update(
        reporting_receipt_id="receipt-other-target",
        status="rejected",
        rejection_codes=["EVIDENCE_MISMATCH"],
    )
    if kind == "revision":
        snapshot = _snapshot()
        material = deepcopy(raw["materializations"][0])
        material.update(
            reporting_revision_id=snapshot["reporting_revision_id"],
            reporting_materialization_id="snapshot-material",
        )
        raw["revisions"].append(snapshot)
        raw["materializations"].append(material)
        predecessor.update(
            reporting_revision_id=snapshot["reporting_revision_id"],
            reporting_materialization_id="snapshot-material",
        )
    else:
        adjustment = deepcopy(raw["adjustments"][0])
        adjustment["reporting_adjustment_id"] = "another-adjustment"
        raw["adjustments"].append(adjustment)
        predecessor["reporting_adjustment_id"] = "another-adjustment"
    raw[array][0]["supersedes_reporting_receipt_id"] = "receipt-other-target"
    raw[array].append(predecessor)
    _counts(raw)
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "INVALID_RECEIPT_CHAIN"


def _snapshot() -> dict[str, Any]:
    revision = deepcopy(REVISION)
    revision.update(reporting_revision_id="retained-snapshot", finality="snapshot")
    for key in ("finality_basis", "finality_policy_id", "finalized_at"):
        revision.pop(key)
    return revision


@pytest.mark.parametrize("official_artifact", [False, True])
async def test_official_precedes_snapshot_receipt_even_without_an_artifact(
    official_artifact,
) -> None:
    raw = _history()
    snapshot = _snapshot()
    raw["revisions"].insert(0, snapshot)
    snapshot_material = deepcopy(raw["materializations"][0])
    snapshot_material.update(
        reporting_materialization_id="snapshot-material", reporting_revision_id="retained-snapshot"
    )
    snapshot_receipt = _receipt(
        reporting_receipt_id="snapshot-receipt",
        reporting_revision_id="retained-snapshot",
        reporting_materialization_id="snapshot-material",
    )
    raw["materializations"] = (
        [snapshot_material, *raw["materializations"]] if official_artifact else [snapshot_material]
    )
    raw["receipts"] = [snapshot_receipt]
    _counts(raw)
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert not result.definitive
    assert result.obligations[0].reporting_revision_id == REVISION_ID
    assert "MISSING_MATCHING_CONSUMER_RECEIPT" in result.obligations[0].reasons


async def test_accepted_snapshot_alone_cannot_close_reconciled_billing() -> None:
    raw = _history()
    raw["revisions"][0] = {**_snapshot(), "reporting_revision_id": REVISION_ID}
    raw["periods"][0]["required_finality"] = "snapshot"
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert not result.definitive
    assert "FINALITY_NOT_MET" in result.obligations[0].reasons


@pytest.mark.parametrize(
    "later", ["failed", "pending", "success", "expired", "corrupt", "bad-producer-evidence"]
)
async def test_accepted_receipt_stays_bound_to_its_materialization_after_later_outcomes(
    later,
) -> None:
    raw = _history()
    newer = deepcopy(raw["materializations"][0])
    newer.update(reporting_materialization_id="later-materialization", attempt=2)
    if later in {"failed", "pending"}:
        newer.update(status=later)
        for key in ("ready_at", "verification", "resource"):
            newer.pop(key)
        if later == "failed":
            newer.update(failed_at="2026-09-02T00:01:06Z", failure_code="WRITE_FAILED")
    elif later == "expired":
        newer["resource"]["expires_at"] = "2026-09-02T23:00:00Z"
    elif later == "corrupt":
        raw["periods"][0]["health"] = "action_required"
    elif later == "bad-producer-evidence":
        newer["verification"]["row_count"] = 999
    raw["materializations"].append(newer)
    _counts(raw)
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    reasons = result.obligations[0].reasons
    assert "MISSING_MATCHING_CONSUMER_RECEIPT" not in reasons
    assert result.definitive == (later in {"failed", "pending", "success"})
    if later == "expired":
        assert "RESOURCE_EXPIRED" in reasons
    elif later == "corrupt":
        assert "OBLIGATION_ACTION_REQUIRED" in reasons
    elif later == "bad-producer-evidence":
        assert "PRODUCER_CONTROL_TOTAL_MISMATCH" in reasons


async def test_expired_accepted_artifact_does_not_invalidate_a_readable_newer_artifact() -> None:
    raw = _history()
    newer = deepcopy(raw["materializations"][0])
    newer.update(reporting_materialization_id="later-materialization", attempt=2)
    raw["materializations"][0]["resource"]["expires_at"] = "2026-09-02T23:00:00Z"
    raw["materializations"].append(newer)
    _counts(raw)
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert result.definitive
    assert result.obligations[0].reporting_materialization_id == "later-materialization"


@pytest.mark.parametrize("field", ["row_count", "digest", "obligation", "materialization"])
async def test_accepted_receipt_must_match_its_own_materialization_even_when_a_newer_one_is_good(
    field,
) -> None:
    raw = _history()
    newer = deepcopy(raw["materializations"][0])
    newer.update(reporting_materialization_id="later-materialization", attempt=2)
    raw["materializations"].append(newer)
    if field == "row_count":
        raw["materializations"][0]["verification"]["row_count"] = 999
    elif field == "digest":
        raw["receipts"][0]["observed_canonical_content_digest"]["value"] = "0" * 64
    else:
        raw["receipts"][0][f"reporting_{field}_id"] = "unavailable-or-foreign"
    _counts(raw)
    with pytest.raises(ReportingReconciliationError):
        await _load(raw)


@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("target", ["obligation", "revision", "materialization"])
async def test_all_receipt_dependencies_are_exact_in_both_ownership_modes(explicit, target) -> None:
    raw = _history()
    raw["receipts"][0][f"reporting_{target}_id"] = "missing-or-foreign"
    with pytest.raises(ReportingReconciliationError):
        await _load(raw, explicit=explicit)


async def test_legacy_history_cannot_hide_a_revision_without_any_owning_period() -> None:
    raw = _history()
    unrelated = {
        **deepcopy(REVISION),
        "reporting_revision_id": "unowned-revision",
        "report_definition_id": "different-definition",
    }
    raw["revisions"].append(unrelated)
    raw["pagination"]["total_count"] += 1
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw, explicit=False)
    assert error.value.code == "INVALID_LEDGER_DEPENDENCY"


@pytest.mark.parametrize(
    "field,value",
    [
        ("delivery_config_id", "foreign-config"),
        ("delivery_config_version", 2),
        ("destination_ref", "foreign-destination"),
        ("feed_purpose", "pacing"),
    ],
)
async def test_failed_materializations_must_still_have_exact_owning_scope(field, value) -> None:
    raw = _history()
    failed = deepcopy(raw["materializations"][0])
    failed.update(
        reporting_materialization_id="failed-materialization",
        status="failed",
        failed_at="2026-09-02T00:01:06Z",
        failure_code="WRITE_FAILED",
        attempt=2,
    )
    for key in ("ready_at", "resource", "verification"):
        failed.pop(key)
    failed[field] = value
    raw["materializations"].append(failed)
    _counts(raw)
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "INVALID_LEDGER_DEPENDENCY"


@pytest.mark.parametrize("accepted", [False, True])
async def test_pending_adjustment_count_matches_only_the_current_leaf(accepted) -> None:
    raw = _history(adjustments=True)
    if not accepted:
        raw["adjustment_receipts"][0].update(status="rejected", rejection_codes=["MISMATCH"])
    _counts(raw)
    raw["periods"][0]["pending_adjustment_count"] = int(not accepted)
    result = evaluate_reporting_ledger(await _load(raw), expected_periods=_expected(), now=NOW)
    assert result.definitive is accepted
    raw["periods"][0]["pending_adjustment_count"] = int(accepted)
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "LEDGER_COUNT_MISMATCH"


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "correction-order",
        "receipt-order",
        "future-receipt",
        "accounting-period",
        "finality",
    ],
)
async def test_adjustment_read_evidence_must_be_consistent(mutation) -> None:
    raw = _history(adjustments=True)
    adjustment = raw["adjustments"][0]
    if mutation == "digest":
        raw["adjustment_receipts"][0]["observed_adjustment_sha256"] = "0" * 64
    elif mutation == "correction-order":
        adjustment["correction_observed_at"] = "2026-09-01T00:00:00Z"
    elif mutation == "receipt-order":
        raw["adjustment_receipts"][0]["observed_at"] = "2026-09-02T00:01:02Z"
    elif mutation == "future-receipt":
        raw["adjustment_receipts"][0].pop("received_at")
        raw["adjustment_receipts"][0]["observed_at"] = "2026-09-03T00:00:00Z"
    elif mutation == "accounting-period":
        adjustment["accounting_period"]["end"] = adjustment["accounting_period"]["start"]
    else:
        raw["revisions"][0] = {**_snapshot(), "reporting_revision_id": REVISION_ID}
    with pytest.raises(ReportingReconciliationError):
        await _load(raw)


async def test_mismatching_resolved_account_is_rejected_without_disclosing_it() -> None:
    raw = _history()
    raw["account_id"] = SECRET
    for record in [*raw["periods"], *raw["revisions"]]:
        record["account_id"] = SECRET
    with pytest.raises(ReportingReconciliationError) as error:
        await _load(raw)
    assert error.value.code == "LEDGER_SCOPE_MISMATCH"
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)
    assert error.value.__context__ is None


async def test_immutable_conflict_and_ledger_repr_do_not_expose_wire_secrets() -> None:
    raw = _history()
    raw["materializations"][0]["resource"][
        "location"
    ] = f"https://user:{SECRET}@storage.example/file"
    raw["receipts"][0]["reporting_receipt_id"] = SECRET
    ledger = await _load(raw)
    assert SECRET not in repr(ledger)
    pages = _pages(raw)
    replay = deepcopy(pages[-1])
    replay["receipts"][0]["observed_row_count"] += 1
    pages[-1]["pagination"].update(has_more=True, cursor="conflicting-replay")
    pages.append(replay)
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(Pages(pages), _request())
    assert error.value.code == "IMMUTABLE_RECORD_CHANGED"
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)


async def test_transport_or_validation_failure_is_closed_and_does_not_restart() -> None:
    class PrivateFailure:
        calls = 0

        async def get_reporting_status(self, request):
            self.calls += 1
            raise ValueError(f"authorization revoked for {SECRET}")

    client = PrivateFailure()
    with pytest.raises(ReportingReconciliationError) as error:
        await load_reporting_ledger(client, _request())
    assert client.calls == 1
    assert error.value.code == "STATUS_READ_FAILED"
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)
