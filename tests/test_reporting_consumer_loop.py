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
    ConsumerStatusPlanError,
    ReportingContentReading,
    ReportingOperationsContactView,
    ReportingPinnedDefinition,
    classify_content_mismatch,
    plan_consumer_statuses,
)
from adcp.reporting._reconcile import ExpectedReportingPeriod
from adcp.types import ReportingConsumerStatus, ReportingObligation, ReportingRevision

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


def _leaf(intent: Any) -> ReportingConsumerStatus:
    """The stored statement a seller would return for a posted intent.

    Built from the intent's own wire shape so the planner is compared against
    what the seller actually echoes back, not against a hand-written guess at
    it.
    """
    payload = dict(intent.to_wire())
    payload["recorded_at"] = payload["status_as_of"]
    return ReportingConsumerStatus.model_validate(payload)


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


def test_a_period_past_its_deadline_with_no_revision_is_revision_missing() -> None:
    # rc.3 moved the duty from "before scope close" to the deadline. A
    # still-retrying buyer posts now and supersedes later rather than staying
    # silent until the campaign ends.
    #
    # revision_count=0 is what makes this revision_missing: the claim is "the
    # obligation existed but no required revision was available", which is only
    # honest when the seller has in fact published none.
    plan = plan_consumer_statuses(
        [_obligation(revision_count=0)],
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


def test_a_replanned_identical_claim_derives_the_same_status_id() -> None:
    # An interrupted buyer that re-plans must get an idempotent replay, not a
    # supersession conflict.
    def plan_for(**kwargs: Any) -> str:
        plan = plan_consumer_statuses(
            [_obligation(revision_count=0)],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            **kwargs,
        )
        return plan[0].reporting_status_id

    assert plan_for() == plan_for()


def test_a_changed_claim_gets_a_different_status_id() -> None:
    # The id must cover the whole statement, not just (obligation, status).
    # Two different `content_mismatch` claims, and two `received` statements
    # naming different revisions, are different immutable statements: sharing
    # an id makes the second an idempotency conflict at the seller and loses it.
    def status_id(reading: ReportingContentReading, revision: Any) -> str:
        plan = plan_consumer_statuses(
            [_obligation()],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            readings={"rpo_1": reading},
            definition=DEFINITION,
            revisions={revision.reporting_revision_id: revision},
        )
        return plan[0].reporting_status_id

    metric_missing = status_id(_reading(metric_names=("impressions",)), _revision())
    currency = status_id(_reading(metric_units={"spend": "EUR"}), _revision())
    assert metric_missing != currency

    first = status_id(_reading(), _revision())
    second = status_id(
        _reading(
            reporting_revision_id="rpr_2",
            observed_revision_content_sha256="b" * 64,
        ),
        _revision(reporting_revision_id="rpr_2"),
    )
    assert first != second


def test_an_unchanged_claim_is_skipped_rather_than_superseding_itself() -> None:
    # With content in the id, re-planning an already-posted claim derives the
    # *same* id as the current leaf. Emitting it would produce a statement that
    # names its own id in supersedes_reporting_status_id -- which no seller can
    # apply -- so the planner drops it.
    reading = _reading()
    revision = _revision()
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": reading},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
    )
    assert len(plan) == 1
    posted = plan[0]

    leaf = _leaf(posted)
    again = plan_consumer_statuses(
        [_obligation(current_consumer_status_id=posted.reporting_status_id)],
        now=EXPECTED_AT + RECOVERY + timedelta(hours=1),
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": reading},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
        current_statuses=[leaf],
    )
    assert again == []


def test_a_changed_claim_supersedes_the_current_leaf() -> None:
    reading = _reading()
    revision = _revision()
    posted = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": reading},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
    )[0]

    changed = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY + timedelta(hours=1),
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading(metric_names=("impressions",))},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
        current_statuses=[_leaf(posted)],
    )
    assert len(changed) == 1
    assert changed[0].consumer_status == "content_mismatch"
    assert changed[0].reporting_status_id != posted.reporting_status_id
    assert changed[0].supersedes_reporting_status_id == posted.reporting_status_id


def test_a_plan_supersedes_the_sellers_reported_leaf() -> None:
    # Omitting a known leaf fails atomically at the seller rather than forking
    # the chain. The conflict is correct; this is how a buyer avoids provoking it.
    existing = ReportingConsumerStatus.model_validate(
        {
            "reporting_status_id": "status_existing_leaf_01",
            "delivery_config_id": "daily",
            "delivery_config_version": 1,
            "report_definition_id": "daily_v1",
            "period": {
                "start": PERIOD_START.isoformat().replace("+00:00", "Z"),
                "end": PERIOD_END.isoformat().replace("+00:00", "Z"),
                "source_timezone": "UTC",
            },
            "consumer_status": "obligation_missing",
            "status_as_of": EXPECTED_AT.isoformat().replace("+00:00", "Z"),
        }
    )
    plan = plan_consumer_statuses(
        [_obligation(revision_count=0)],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        current_statuses=[existing],
    )
    assert plan[0].supersedes_reporting_status_id == "status_existing_leaf_01"
    assert plan[0].to_wire()["supersedes_reporting_status_id"] == "status_existing_leaf_01"


def test_status_ids_satisfy_the_schemas_minimum_length() -> None:
    # The schema requires 16-255 characters matching a restricted class. A
    # shorter derived id would be rejected by every seller.
    plan = plan_consumer_statuses(
        [_obligation(revision_count=0)],
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


# -- obligation_missing: the status only the buyer can derive -----------------


def _expected_period(**overrides: Any) -> ExpectedReportingPeriod:
    defaults: dict[str, Any] = {
        "delivery_config_id": "daily",
        "delivery_config_version": 1,
        "report_definition_id": "daily_v1",
        "feed_purpose": "analytics",
        "reporting_profile": "paid_media_delivery",
        "media_buy_ids": ("mb_1", "mb_2"),
        "period_start": PERIOD_START.isoformat().replace("+00:00", "Z"),
        "period_end": PERIOD_END.isoformat().replace("+00:00", "Z"),
        "source_timezone": "UTC",
        "expected_at": EXPECTED_AT.isoformat().replace("+00:00", "Z"),
    }
    defaults.update(overrides)
    return ExpectedReportingPeriod(**defaults)


def test_a_period_absent_from_the_ledger_becomes_obligation_missing() -> None:
    # The status no seller can derive for itself, and the whole reason the
    # buyer keeps its own denominator. Previously unreachable: the planner
    # iterated only the seller's obligations, so a period the seller omitted
    # produced no statement at all and the omission stayed invisible.
    plan = plan_consumer_statuses(
        [],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        missing_expected_periods=[_expected_period()],
    )
    assert len(plan) == 1
    wire = plan[0].to_wire()
    assert wire["consumer_status"] == "obligation_missing"
    assert wire["period"]["source_timezone"] == "UTC"
    # Keyed without a seller obligation id -- requiring one would make the
    # first missing report invisible again. The schema forbids carrying any of
    # these on obligation_missing, so the keys must be absent.
    assert "reporting_obligation_id" not in wire
    assert "reporting_revision_id" not in wire
    assert "observed_revision_content_sha256" not in wire


def test_obligation_missing_is_not_filed_before_expected_at() -> None:
    # Valid only at or after the obligation's expected_at. Filing earlier
    # accuses the seller of omitting a period that is not due.
    assert (
        plan_consumer_statuses(
            [],
            now=EXPECTED_AT - timedelta(minutes=1),
            automated_recovery_window=RECOVERY,
            missing_expected_periods=[_expected_period()],
        )
        == []
    )


def test_obligation_missing_status_as_of_is_at_or_after_expected_at() -> None:
    plan = plan_consumer_statuses(
        [],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        missing_expected_periods=[_expected_period()],
    )
    assert plan[0].status_as_of >= EXPECTED_AT


def test_a_missing_period_without_a_derived_expected_at_is_skipped() -> None:
    # Without the buyer's own expected_at there is no defensible deadline and
    # no way to satisfy "valid only at or after expected_at". Skipping beats
    # inventing one.
    assert (
        plan_consumer_statuses(
            [],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            missing_expected_periods=[_expected_period(expected_at=None)],
        )
        == []
    )


# -- revision_missing vs unreadable ------------------------------------------


def test_a_failed_read_is_unreadable_with_its_failure_code() -> None:
    # unreadable was unreachable: the reading had nowhere to carry a
    # failure_code, so a buyer that could see a revision but not read it had no
    # way to say so and the planner filed `received` or `revision_missing`.
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={
            "rpo_1": _reading(
                failure_code="access_denied",
                observed_revision_content_sha256=None,
            )
        },
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
    )
    wire = plan[0].to_wire()
    assert wire["consumer_status"] == "unreadable"
    assert wire["failure_code"] == "access_denied"
    assert wire["reporting_revision_id"] == "rpr_1"
    # unreadable forbids the observed digest: the buyer never read the bytes.
    assert "observed_revision_content_sha256" not in wire
    assert "mismatch_code" not in wire


@pytest.mark.parametrize(
    "code",
    [
        "access_denied",
        "resource_not_found",
        "integrity_mismatch",
        "reader_incompatible",
        "transport_failed",
    ],
)
def test_every_closed_failure_code_round_trips(code: str) -> None:
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading(failure_code=code, observed_revision_content_sha256=None)},
        revisions={"rpr_1": _revision()},
    )
    assert plan[0].to_wire()["failure_code"] == code


def test_an_obligation_with_a_revision_and_no_reading_refuses_to_guess() -> None:
    # Defaulting to revision_missing accused the seller of publishing nothing
    # it demonstrably published. Both possible defaults are false claims
    # attributed to the buyer, so refuse.
    with pytest.raises(ConsumerStatusPlanError, match="no reading was supplied"):
        plan_consumer_statuses(
            [_obligation(revision_count=1)],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            obligation_revisions={"rpo_1": [_revision()]},
        )


@pytest.mark.parametrize(
    "history,count",
    [(None, 1), ([], 1), ([_revision()], 2), ([_revision(), _revision()], 2)],
)
def test_an_incomplete_obligation_partition_names_obligation_revisions(history, count) -> None:
    # The missing input is the obligation's retained history, not a reading:
    # a message that says "reading" sends the adopter to fix the wrong
    # argument and fail again on the next turn.
    with pytest.raises(ConsumerStatusPlanError, match="obligation_revisions") as caught:
        plan_consumer_statuses(
            [_obligation(revision_count=count)],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            obligation_revisions=None if history is None else {"rpo_1": history},
        )
    assert "rpo_1" in str(caught.value)
    assert "no reading was supplied" not in str(caught.value)


def test_required_finality_decides_whether_a_revision_counts() -> None:
    # An obligation needing `official` is not satisfied by snapshots, so a
    # snapshot-only ledger really has no required revision.
    plan = plan_consumer_statuses(
        [_obligation(required_finality="official")],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        obligation_revisions={"rpo_1": [_revision(finality="snapshot")]},
    )
    assert plan[0].consumer_status == "revision_missing"

    with pytest.raises(ConsumerStatusPlanError):
        plan_consumer_statuses(
            [_obligation(required_finality="official")],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            obligation_revisions={"rpo_1": [_revision(finality="official")]},
        )


def test_a_reading_naming_an_absent_revision_fails_loudly() -> None:
    # Filing `received` would bind the statement to a revision the seller
    # cannot resolve, and the seller must reject it. Say so where it is
    # fixable.
    with pytest.raises(ConsumerStatusPlanError, match="not .*in this ledger snapshot"):
        plan_consumer_statuses(
            [_obligation()],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            readings={"rpo_1": _reading(reporting_revision_id="rpr_unknown")},
            revisions={},
        )


# -- status_as_of ------------------------------------------------------------


def test_received_status_as_of_is_when_the_revision_became_consumable() -> None:
    # Sellers use it as buyer-attributed arrival evidence. Planning time would
    # date every statement to whenever the loop happened to run.
    consumable = EXPECTED_AT + timedelta(minutes=7)
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY + timedelta(days=2),
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading(first_consumable_at=consumable)},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
    )
    assert plan[0].status_as_of == consumable


def test_status_as_of_never_goes_backwards_from_the_superseded_leaf() -> None:
    # "status_as_of MUST be no earlier than the superseded statement's
    # status_as_of." A re-read that became consumable before the previous
    # statement was made would otherwise travel back in time.
    late_leaf = ReportingConsumerStatus.model_validate(
        {
            "reporting_status_id": "status_late_leaf_000001",
            "delivery_config_id": "daily",
            "delivery_config_version": 1,
            "report_definition_id": "daily_v1",
            "period": {
                "start": PERIOD_START.isoformat().replace("+00:00", "Z"),
                "end": PERIOD_END.isoformat().replace("+00:00", "Z"),
                "source_timezone": "UTC",
            },
            "consumer_status": "revision_missing",
            "reporting_obligation_id": "rpo_1",
            "status_as_of": (EXPECTED_AT + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        }
    )
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading(first_consumable_at=EXPECTED_AT + timedelta(minutes=1))},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
        current_statuses=[late_leaf],
    )
    assert plan[0].status_as_of == EXPECTED_AT + timedelta(hours=5)


def test_a_naive_timestamp_is_refused_rather_than_guessed() -> None:
    # astimezone on a naive value assumes the *local* zone, so a buyer in a
    # non-UTC container would stamp every statement hours off.
    with pytest.raises(ValueError, match="naive"):
        plan_consumer_statuses(
            [_obligation(revision_count=0)],
            now=datetime(2026, 9, 1, 5, 0),
            automated_recovery_window=RECOVERY,
        )


# -- checkpoints are read, not only written ----------------------------------


@pytest.mark.asyncio
async def test_a_plan_without_a_ledger_read_supersedes_from_the_checkpoint() -> None:
    # A buyer that plans from checkpoints alone (no periods view this cycle)
    # must still name the current leaf. Without the read, every such statement
    # was rejected with STATUS_SUPERSEDES_REQUIRED -- surfaced rather than
    # silently wrong, but a whole planning mode that never worked.
    from adcp.reporting import (
        ConsumerStatusCheckpoint,
        InMemoryConsumerStatusCheckpoints,
        consumer_status_chain_key,
        resolve_checkpointed_leaves,
    )

    first = plan_consumer_statuses(
        [_obligation(revision_count=0)],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )[0]

    checkpoints = InMemoryConsumerStatusCheckpoints()
    await checkpoints.put(
        consumer_status_chain_key(first, account_id="acct_1"),
        ConsumerStatusCheckpoint(reporting_status_id=first.reporting_status_id),
    )

    leaves = await resolve_checkpointed_leaves(
        checkpoints, account_id="acct_1", obligations=[_obligation(revision_count=0)]
    )
    changed = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY + timedelta(hours=1),
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading()},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
        checkpointed_leaves=leaves,
        account_id="acct_1",
    )
    assert changed[0].consumer_status == "received"
    assert changed[0].supersedes_reporting_status_id == first.reporting_status_id


@pytest.mark.asyncio
async def test_a_lost_response_retry_reproduces_the_original_statement() -> None:
    # The response was lost, so the buyer does not know the statement landed.
    # Re-planning must reproduce it byte for byte -- same id *and* same
    # supersedes -- or the seller's replay fingerprint sees a different
    # statement and answers with an identity conflict instead of `unchanged`.
    from adcp.reporting import (
        ConsumerStatusCheckpoint,
        InMemoryConsumerStatusCheckpoints,
        consumer_status_chain_key,
        resolve_checkpointed_leaves,
    )

    # first_consumable_at is what makes the retry reproducible: without it
    # status_as_of falls back to planning time, and a re-plan an hour later is
    # genuinely a different statement rather than a retry of the same one.
    reading = _reading(first_consumable_at=EXPECTED_AT + timedelta(minutes=3))
    revision = _revision()
    posted = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": reading},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
        account_id="acct_1",
        checkpointed_leaves={
            consumer_status_chain_key(
                plan_consumer_statuses(
                    [_obligation(revision_count=0)],
                    now=EXPECTED_AT + RECOVERY,
                    automated_recovery_window=RECOVERY,
                )[0],
                account_id="acct_1",
            ): ConsumerStatusCheckpoint(reporting_status_id="status_prior_leaf_0001")
        },
    )[0]
    assert posted.supersedes_reporting_status_id == "status_prior_leaf_0001"

    checkpoints = InMemoryConsumerStatusCheckpoints()
    await checkpoints.put(
        consumer_status_chain_key(posted, account_id="acct_1"),
        ConsumerStatusCheckpoint(
            reporting_status_id=posted.reporting_status_id,
            supersedes_reporting_status_id=posted.supersedes_reporting_status_id,
        ),
    )
    leaves = await resolve_checkpointed_leaves(
        checkpoints, account_id="acct_1", obligations=[_obligation()]
    )

    retry = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY + timedelta(hours=3),
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": reading},
        definition=DEFINITION,
        revisions={"rpr_1": revision},
        checkpointed_leaves=leaves,
        account_id="acct_1",
    )[0]
    assert retry.reporting_status_id == posted.reporting_status_id
    # The critical bit: it must NOT supersede itself, and must not have lost
    # the supersedes it originally carried.
    assert retry.supersedes_reporting_status_id == "status_prior_leaf_0001"
    assert retry.to_wire() == posted.to_wire()


@pytest.mark.asyncio
async def test_a_ledger_read_wins_over_a_stale_checkpoint() -> None:
    # The seller is authoritative. A checkpoint that has fallen behind must not
    # make the buyer supersede a statement the seller no longer considers the
    # leaf.
    from adcp.reporting import ConsumerStatusCheckpoint, consumer_status_chain_key

    stale = plan_consumer_statuses(
        [_obligation(revision_count=0)],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )[0]
    real_leaf = ReportingConsumerStatus.model_validate(
        {
            "reporting_status_id": "status_real_leaf_000001",
            "delivery_config_id": "daily",
            "delivery_config_version": 1,
            "report_definition_id": "daily_v1",
            "period": {
                "start": PERIOD_START.isoformat().replace("+00:00", "Z"),
                "end": PERIOD_END.isoformat().replace("+00:00", "Z"),
                "source_timezone": "UTC",
            },
            "consumer_status": "revision_missing",
            "reporting_obligation_id": "rpo_1",
            "status_as_of": EXPECTED_AT.isoformat().replace("+00:00", "Z"),
        }
    )
    plan = plan_consumer_statuses(
        [_obligation()],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
        readings={"rpo_1": _reading()},
        definition=DEFINITION,
        revisions={"rpr_1": _revision()},
        current_statuses=[real_leaf],
        account_id="acct_1",
        checkpointed_leaves={
            consumer_status_chain_key(stale, account_id="acct_1"): ConsumerStatusCheckpoint(
                reporting_status_id="status_stale_checkpoint_1"
            )
        },
    )
    assert plan[0].supersedes_reporting_status_id == "status_real_leaf_000001"


@pytest.mark.asyncio
async def test_two_accounts_sharing_a_config_id_do_not_share_a_checkpoint() -> None:
    # The spec's chain identity is account-scoped. A checkpoint store shared
    # across sellers -- or across accounts on one seller -- would otherwise let
    # two chains with the same delivery_config_id overwrite each other's
    # leaves, and the buyer would supersede the wrong statement or none.
    from adcp.reporting import consumer_status_chain_key

    intent = plan_consumer_statuses(
        [_obligation(revision_count=0)],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )[0]
    first = consumer_status_chain_key(intent, account_id="acct_1")
    second = consumer_status_chain_key(intent, account_id="acct_2")
    assert first != second

    # The consumer scope separates identities within one account too.
    assert consumer_status_chain_key(
        intent, account_id="acct_1", consumer_id="buyer_a"
    ) != consumer_status_chain_key(intent, account_id="acct_1", consumer_id="buyer_b")


@pytest.mark.asyncio
async def test_a_checkpoint_written_by_the_poster_is_readable_by_the_planner() -> None:
    # The two derive the key independently. If they ever disagreed, every
    # lookup would miss *silently* and every statement would go out with no
    # supersedes -- which the seller rejects. Pin that they agree.
    from adcp.reporting import (
        InMemoryConsumerStatusCheckpoints,
        post_consumer_statuses,
        resolve_checkpointed_leaves,
    )

    intent = plan_consumer_statuses(
        [_obligation(revision_count=0)],
        now=EXPECTED_AT + RECOVERY,
        automated_recovery_window=RECOVERY,
    )[0]

    class _AcceptingClient:
        async def sync_reporting_status(self, request: Any) -> Any:
            from types import SimpleNamespace

            payload = request.model_dump(mode="json", exclude_none=True)
            return SimpleNamespace(
                success=True,
                error=None,
                data={
                    "status": "completed",
                    "results": [
                        {"result": "recorded", "consumer_status": statement}
                        for statement in payload["statuses"]
                    ],
                },
            )

    checkpoints = InMemoryConsumerStatusCheckpoints()
    result = await post_consumer_statuses(
        _AcceptingClient(),
        [intent],
        account_id="acct_1",
        consumer_id="buyer_a",
        checkpoints=checkpoints,
    )
    assert result.ok and len(result.recorded) == 1

    leaves = await resolve_checkpointed_leaves(
        checkpoints,
        account_id="acct_1",
        consumer_id="buyer_a",
        obligations=[_obligation(revision_count=0)],
    )
    assert len(leaves) == 1
    assert next(iter(leaves.values())).reporting_status_id == intent.reporting_status_id

    # A different account reads nothing, which is the isolation being asserted.
    assert (
        await resolve_checkpointed_leaves(
            checkpoints,
            account_id="acct_2",
            consumer_id="buyer_a",
            obligations=[_obligation(revision_count=0)],
        )
        == {}
    )


@pytest.mark.asyncio
async def test_checkpoints_without_an_account_id_fail_loudly() -> None:
    # A mismatched key misses silently, so refuse rather than quietly degrade
    # to "no supersedes" on every statement.
    from adcp.reporting import ConsumerStatusCheckpoint

    with pytest.raises(ConsumerStatusPlanError, match="requires account_id"):
        plan_consumer_statuses(
            [_obligation(revision_count=0)],
            now=EXPECTED_AT + RECOVERY,
            automated_recovery_window=RECOVERY,
            checkpointed_leaves={"rpcc_whatever": ConsumerStatusCheckpoint("status_x_0000001")},
        )
