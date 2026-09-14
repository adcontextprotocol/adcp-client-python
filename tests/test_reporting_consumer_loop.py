"""Buyer side of the AdCP 3.2.0-rc.3 consumer-status loop.

Each test names the wrong answer it exists to prevent, because every failure
mode here is silent: a buyer that misclassifies a mismatch sends the seller a
code it cannot act on, and a buyer that posts only at scope close is invisible
in ``consumer_status_pending`` for the whole flight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.reporting import (
    ConsumerLoopView,
    ReportingContentReading,
    ReportingOperationsContactView,
    ReportingPinnedDefinition,
    classify_content_mismatch,
    plan_consumer_statuses,
)
from adcp.types import ReportingObligation, ReportingRevision

PERIOD_START = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
EXPECTED_AT = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
RECOVERY = timedelta(hours=2)

#: Full coverage over the frozen denominator. Spelled out rather than
#: defaulted because the coverage partition is what ``coverage_short`` is
#: measured against -- a fixture that left it implicit would not test anything.
_COVERAGE: dict[str, Any] = {
    "status": "full",
    "evaluated_at": PERIOD_END.isoformat().replace("+00:00", "Z"),
    "media_buy_ids": ["mb_1", "mb_2"],
    "fully_covered_media_buy_ids": ["mb_1", "mb_2"],
    "partially_covered_media_buy_ids": [],
    "unsupported_media_buy_ids": [],
    "unknown_media_buy_ids": [],
    "package_ids": ["pkg_1", "pkg_2"],
    "covered_package_ids": ["pkg_1", "pkg_2"],
    "unsupported_package_ids": [],
    "unknown_package_ids": [],
    "limitations": [],
}


def _obligation(**overrides: Any) -> ReportingObligation:
    payload: dict[str, Any] = {
        "reporting_obligation_id": "rpo_1",
        "account_id": "acct_1",
        "delivery_config_id": "daily",
        "delivery_config_version": 1,
        "report_definition_id": "daily_v1",
        "reporting_profile": "paid_media_delivery",
        "feed_purpose": "analytics",
        "period": {
            "start": PERIOD_START.isoformat().replace("+00:00", "Z"),
            "end": PERIOD_END.isoformat().replace("+00:00", "Z"),
            "source_timezone": "UTC",
        },
        "expected_at": EXPECTED_AT.isoformat().replace("+00:00", "Z"),
        "scope_resolved_at": PERIOD_END.isoformat().replace("+00:00", "Z"),
        "media_buy_ids": ["mb_1", "mb_2"],
        "required_finality": "snapshot",
        "health": "healthy",
        "production_status": "published",
        "reconciliation_mode": "delivery_only",
        "schedule": {"period_duration": "PT1H", "delivery_sla": "PT1H", "alignment": "utc"},
        "coverage": _COVERAGE,
        "issues": [],
        "revision_count": 1,
        "reconciliation_status": "not_required",
    }
    payload.update(overrides)
    return ReportingObligation.model_validate(payload)


def _revision(**overrides: Any) -> ReportingRevision:
    payload: dict[str, Any] = {
        "reporting_revision_id": "rpr_1",
        "account_id": "acct_1",
        "report_definition_id": "daily_v1",
        "reporting_profile": "paid_media_delivery",
        "media_buy_ids": ["mb_1", "mb_2"],
        "period": {
            "start": PERIOD_START.isoformat().replace("+00:00", "Z"),
            "end": PERIOD_END.isoformat().replace("+00:00", "Z"),
            "source_timezone": "UTC",
        },
        "finality": "snapshot",
        "revision_content_sha256": "a" * 64,
        "row_count": 2,
        "control_totals": [{"name": "impressions", "value": "5"}],
        "observed_at": PERIOD_END.isoformat().replace("+00:00", "Z"),
        "created_at": EXPECTED_AT.isoformat().replace("+00:00", "Z"),
        "coverage": _COVERAGE,
        "data_through": PERIOD_END.isoformat().replace("+00:00", "Z"),
        "data_through_precision": "exact",
        "report_definition_uri": "https://contracts.example.test/daily-v1",
        "report_definition_sha256": "b" * 64,
        "schema_uri": "https://contracts.example.test/rows-v1",
        "schema_sha256": "c" * 64,
        "schema_version": "1.0.0",
    }
    payload.update(overrides)
    return ReportingRevision.model_validate(payload)


def _reading(**overrides: Any) -> ReportingContentReading:
    defaults: dict[str, Any] = {
        "reporting_revision_id": "rpr_1",
        "media_buy_ids": ("mb_1", "mb_2"),
        "package_ids": ("pkg_1", "pkg_2"),
        "metric_names": ("impressions", "spend"),
        "metric_units": {"spend": "USD"},
        "control_total_units": {"impressions": "count"},
        "observed_revision_content_sha256": "a" * 64,
    }
    defaults.update(overrides)
    return ReportingContentReading(**defaults)


DEFINITION = ReportingPinnedDefinition(
    metric_names=("impressions", "spend"),
    metric_units={"spend": "USD"},
    control_total_units={"impressions": "count"},
)


# -- classification ---------------------------------------------------------


def test_a_conforming_revision_is_not_a_mismatch() -> None:
    # The negative case matters most: a classifier that always finds something
    # would have every buyer filing content_mismatch against healthy feeds.
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(),
            definition=DEFINITION,
        )
        is None
    )


def test_a_frozen_media_buy_with_no_rows_is_scope_media_buy_missing() -> None:
    # Without an explicit zero the revision cannot distinguish zero delivery
    # from an omitted buy, which is exactly why this code exists.
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(media_buy_ids=("mb_1",)),
            definition=DEFINITION,
        )
        == "scope_media_buy_missing"
    )


def test_an_explicit_zero_row_covers_a_frozen_media_buy() -> None:
    # A buy present only as an explicit zero is reported, not omitted. Treating
    # it as missing would make every honest zero-delivery period a dispute.
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(media_buy_ids=("mb_1", "mb_2")),
            definition=DEFINITION,
        )
        is None
    )


def test_fewer_packages_than_the_frozen_coverage_is_coverage_short() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(package_ids=("pkg_1",)),
            definition=DEFINITION,
        )
        == "coverage_short"
    )


def test_a_promised_metric_that_is_absent_is_metric_missing() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(metric_names=("impressions",)),
            definition=DEFINITION,
        )
        == "metric_missing"
    )


def test_metric_missing_wins_over_schema_nonconformant() -> None:
    # The spec is explicit: a metric that is simply absent uses metric_missing
    # even when the pinned schema declares it required. Letting structural
    # validation win would collapse every missing metric into a generic schema
    # failure and lose the one detail that tells the seller what to fix.
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(metric_names=("impressions",), schema_conformant=False),
            definition=DEFINITION,
        )
        == "metric_missing"
    )


def test_rows_that_fail_the_pinned_schema_are_schema_nonconformant() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(schema_conformant=False),
            definition=DEFINITION,
        )
        == "schema_nonconformant"
    )


def test_a_metric_unit_that_disagrees_with_the_pin_is_currency_mismatch() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(metric_units={"spend": "EUR"}),
            definition=DEFINITION,
        )
        == "currency_mismatch"
    )


def test_a_control_total_unit_that_disagrees_is_currency_mismatch() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(control_total_units={"impressions": "millions"}),
            definition=DEFINITION,
        )
        == "currency_mismatch"
    )


@pytest.mark.parametrize(
    "value",
    [
        PERIOD_START - timedelta(seconds=1),
        # Half-open: a value exactly at the end belongs to the next period.
        PERIOD_END,
        PERIOD_END + timedelta(hours=1),
    ],
)
def test_a_time_value_outside_the_half_open_period_is_period_mismatch(value: datetime) -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(time_dimension_values=(value,)),
            definition=DEFINITION,
        )
        == "period_mismatch"
    )


def test_a_time_value_at_the_period_start_is_inside_the_window() -> None:
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(),
            reading=_reading(time_dimension_values=(PERIOD_START,)),
            definition=DEFINITION,
        )
        is None
    )


def test_classification_never_looks_at_counts() -> None:
    # The one thing content_mismatch must never be used for. A revision whose
    # row count and control totals are wildly different from what the buyer
    # measured is still conforming: that is a measurement dispute for
    # measurement_terms and makegood_policy, not this loop.
    assert (
        classify_content_mismatch(
            obligation=_obligation(),
            revision=_revision(
                row_count=1_000_000, control_totals=[{"name": "impressions", "value": "999999"}]
            ),
            reading=_reading(),
            definition=DEFINITION,
        )
        is None
    )


# -- the posting deadline ---------------------------------------------------


def test_nothing_is_due_before_the_deadline() -> None:
    # Filing revision_missing before the seller is late would be a false
    # accusation this loop exists to prevent.
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + timedelta(minutes=1),
        automated_recovery_window=RECOVERY,
    )
    assert plan == []


def test_a_period_past_its_deadline_with_no_reading_is_revision_missing() -> None:
    # rc.3 moved the duty from "before scope close" to the deadline. A
    # still-retrying buyer posts now and supersedes later rather than staying
    # silent until the campaign ends.
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )
    assert len(plan) == 1
    intent = plan[0]
    assert intent.consumer_status == "revision_missing"
    assert intent.due_at == EXPECTED_AT + RECOVERY
    assert intent.overdue
    wire = intent.to_wire()
    assert wire["consumer_status"] == "revision_missing"
    assert wire["reporting_obligation_id"] == "rpo_1"
    # revision_missing forbids a revision id and a digest; the keys must be
    # absent, because an explicit null still satisfies JSON Schema's required.
    assert "reporting_revision_id" not in wire
    assert "observed_revision_content_sha256" not in wire
    assert "mismatch_code" not in wire


def test_a_conforming_reading_past_the_deadline_is_received() -> None:
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading()},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
    )
    wire = plan[0].to_wire()
    assert wire["consumer_status"] == "received"
    assert wire["observed_revision_content_sha256"] == "a" * 64
    assert "mismatch_code" not in wire


def test_a_violating_reading_becomes_content_mismatch_with_its_code() -> None:
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading(metric_names=("impressions",))},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
    )
    wire = plan[0].to_wire()
    assert wire["consumer_status"] == "content_mismatch"
    assert wire["mismatch_code"] == "metric_missing"
    # content_mismatch requires all three, so a seller can bind the claim to
    # exactly the bytes the buyer read.
    assert wire["reporting_obligation_id"] == "rpo_1"
    assert wire["reporting_revision_id"] == "rpr_1"
    assert wire["observed_revision_content_sha256"] == "a" * 64


def test_a_replanned_identical_claim_reuses_its_status_id() -> None:
    # An interrupted buyer that re-plans must get an idempotent replay, not a
    # supersession conflict. Changing the claim changes the id, which is
    # exactly when a new immutable statement is required.
    def plan_for(**kwargs: Any) -> str:
        plan = plan_consumer_statuses(
            [_obligation()],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            **kwargs,
        )
        return plan[0].reporting_status_id

    first = plan_for()
    assert plan_for() == first
    changed = plan_for(
        readings={"rpo_1": _reading()}, definition=DEFINITION, revisions={"rpr_1": _revision()}
    )
    assert changed != first


def test_a_plan_supersedes_the_sellers_reported_leaf() -> None:
    # Omitting a known leaf fails atomically at the seller rather than forking
    # the chain. The conflict is correct; this is how a buyer avoids provoking it.
    plan = plan_consumer_statuses(
        [_obligation(current_consumer_status_id="status_existing_leaf_01")],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )
    assert plan[0].supersedes_reporting_status_id == "status_existing_leaf_01"
    assert plan[0].to_wire()["supersedes_reporting_status_id"] == "status_existing_leaf_01"


def test_status_ids_satisfy_the_schemas_minimum_length() -> None:
    # The schema requires 16-255 characters matching a restricted class. A
    # shorter derived id would be rejected by every seller.
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )
    status_id = plan[0].reporting_status_id
    assert 16 <= len(status_id) <= 255
    assert all(char.isalnum() or char in "_.:-" for char in status_id)


# -- surfacing what the seller said -----------------------------------------


def test_the_loop_view_separates_no_loop_from_nothing_owed() -> None:
    # None is "this seller has no loop"; 0 is "you owe nothing". Conflating
    # them would make a buyer think it was caught up on a seller that never
    # asked for status at all.
    assert ConsumerLoopView(consumer_status_pending=None).consumer_status_pending is None
    assert ConsumerLoopView(consumer_status_pending=0).consumer_status_pending == 0


def test_the_loop_view_selects_only_this_buyers_mismatches() -> None:
    from adcp.types import ReportingStatusIssue

    overdue = ReportingStatusIssue.model_validate(
        {
            "issue_id": "iss_overdue",
            "code": "REPORT_OVERDUE",
            "severity": "action_required",
            "responsible_party": "seller",
            "recommended_action": "contact_seller",
        }
    )
    mismatch = ReportingStatusIssue.model_validate(
        {
            "issue_id": "iss_mismatch",
            "code": "CONSUMER_STATUS_MISMATCH",
            "severity": "delayed",
            "responsible_party": "buyer",
            "recommended_action": "wait_for_retry",
            "opened_at": "2026-09-01T03:30:00Z",
            "issue_state": "open",
            "external_ref": "OPS-1234",
            "reporting_status_id": "status_lifecycle_0000001",
        }
    )
    view = ConsumerLoopView(consumer_status_pending=2, issues=(overdue, mismatch))
    assert [i.issue_id for i in view.mismatch_issues] == ["iss_mismatch"]
    # The lifecycle fields a buyer needs to age one work item and correlate it
    # with its own tracker.
    only = view.mismatch_issues[0]
    assert only.opened_at is not None
    assert (
        str(only.issue_state.value if hasattr(only.issue_state, "value") else only.issue_state)
        == "open"
    )
    assert only.external_ref == "OPS-1234"
    # Not escalated: wait_for_retry is outside the contact_ family.
    assert view.escalated() == ()


def test_escalation_is_read_from_recommended_action_not_severity() -> None:
    # A seller past its advertised window MUST switch recommended_action to a
    # contact_ value, and wait_for_retry cannot survive that boundary. Reading
    # severity instead would confuse an escalated mismatch with an ordinary
    # action_required one.
    from adcp.types import ReportingStatusIssue

    escalated = ReportingStatusIssue.model_validate(
        {
            "issue_id": "iss_escalated",
            "code": "CONSUMER_STATUS_MISMATCH",
            "severity": "action_required",
            "responsible_party": "seller",
            "recommended_action": "contact_seller",
            "opened_at": "2026-09-01T03:30:00Z",
            "reporting_status_id": "status_lifecycle_0000001",
        }
    )
    view = ConsumerLoopView(consumer_status_pending=0, issues=(escalated,))
    assert [i.issue_id for i in view.escalated()] == ["iss_escalated"]


def test_operations_contact_is_read_from_the_capability_block() -> None:
    contact = ReportingOperationsContactView.from_capability(
        {
            "consumer_mismatch_escalation_seconds": 3600,
            "operations_contact": {
                "url": "https://support.seller.example/reporting",
                "email": "reporting-ops@seller.example",
            },
        }
    )
    assert contact is not None
    assert contact.url == "https://support.seller.example/reporting"
    assert contact.email == "reporting-ops@seller.example"


def test_a_seller_without_a_contact_yields_none() -> None:
    assert ReportingOperationsContactView.from_capability(None) is None
    assert (
        ReportingOperationsContactView.from_capability({"reliable_reporting_version": "1.0"})
        is None
    )
