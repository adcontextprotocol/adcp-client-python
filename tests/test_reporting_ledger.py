"""The seller-side ledger: obligations, immutable revisions, and honest health.

Each test names an operational promise a buyer is entitled to rely on: that a
missing first report is visible, that a quiet campaign is distinguishable from a
dead feed, that an official close cannot be quietly rewritten, and that a
consumer's own statement degrades only its own view.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest

from adcp.reporting.ledger import (
    ConsumerStatusDisabledError,
    ConsumerStatusIngest,
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ProducerOfferings,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingDeliveryEscalation,
    ReportingObligationRecord,
    ReportingProducer,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
    aggregate_reporting_health,
    consumer_mismatch_issue_key,
    derive_period,
    iso_duration_to_timedelta,
    project_obligation_health,
    revision_content_sha256,
)

ACCOUNT = "acct_1"
CALLER = ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer_1")

_OFFERING: dict[str, Any] = {
    "offering_id": "HOURLY_SNAPSHOT_V1",
    "feed_purpose": "analytics",
    "report_definition_id": "daily_delivery_v1",
    "report_definition_uri": "https://contracts.example.test/reporting/daily-delivery-v1",
    "report_definition_sha256": "a" * 64,
    "reporting_profile": {
        "id": "paid_media_delivery",
        "version": "1.0.0",
        "schema_uri": "https://contracts.example.test/reporting/rows-v1.json",
        "schema_sha256": "b" * 64,
        "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "schema_ref_policy": "local_fragment_only",
        "grain": "one row per media buy per period",
        "primary_keys": ["media_buy_id"],
    },
    "schedule": {"period_duration": "PT1H", "delivery_sla": "PT1H", "alignment": "utc"},
    "supported_finality": ["snapshot"],
    "reconciliation_mode": "delivery_only",
}

HOURLY = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT1H", alignment="utc")


def _configuration(**overrides) -> ReportingConfiguration:
    defaults = dict(
        delivery_config_id="daily_reporting",
        delivery_config_version=1,
        account_id=ACCOUNT,
        report_definition_id="daily_delivery_v1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=HOURLY,
        required_finality="official",
        activated_at=datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc),
        media_buy_ids=("mb_1",),
        automated_recovery_window=timedelta(hours=6),
    )
    defaults.update(overrides)
    return ReportingConfiguration(**defaults)  # type: ignore[arg-type]


def _obligation(
    configuration: ReportingConfiguration, ordinal: int = 1, **overrides
) -> ReportingObligationRecord:
    boundary = derive_period(
        configuration.schedule, account_timezone="UTC", ordinal=_ordinal_for(configuration, ordinal)
    )
    defaults = dict(
        reporting_obligation_id=f"rpo_{ordinal}",
        account_id=configuration.account_id,
        delivery_config_id=configuration.delivery_config_id,
        delivery_config_version=configuration.delivery_config_version,
        report_definition_id=configuration.report_definition_id,
        reporting_profile=configuration.reporting_profile,
        feed_purpose=configuration.feed_purpose,
        period=boundary,
        scope_resolved_at=boundary.end,
        media_buy_ids=configuration.media_buy_ids,
        required_finality=configuration.required_finality,
        automated_recovery_deadline_at=boundary.expected_at
        + configuration.automated_recovery_window,
        schedule=configuration.schedule,
        created_at=boundary.end,
    )
    defaults.update(overrides)
    return ReportingObligationRecord(**defaults)  # type: ignore[arg-type]


def _ordinal_for(configuration: ReportingConfiguration, offset: int) -> int:
    from adcp.reporting.ledger.models import first_ordinal_after

    assert configuration.activated_at is not None
    return (
        first_ordinal_after(
            configuration.schedule,
            account_timezone="UTC",
            activated_at=configuration.activated_at,
        )
        + offset
        - 1
    )


def _revision(obligation: ReportingObligationRecord, **overrides) -> ReportingRevisionRecord:
    rows: list[dict[str, object]] = overrides.pop(
        "rows", [{"media_buy_id": "mb_1", "impressions": 5}]
    )
    revision_id = overrides.pop("reporting_revision_id", "rpr_1")
    totals = overrides.pop("control_totals", (("impressions", "5"),))
    finality = overrides.pop("finality", "official")
    defaults = dict(
        reporting_revision_id=revision_id,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality=finality,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=len(rows),
            control_totals=totals,
            reporting_rows=rows,
        ),
        row_count=len(rows),
        control_totals=totals,
        observed_at=obligation.period.end,
        data_through=obligation.period.end,
        created_at=obligation.period.expected_at,
    )
    if finality == "official":
        defaults.update(
            finality_basis="source_final",
            finality_policy_id="policy_1",
            finalized_at=obligation.period.end,
        )
    defaults.update(overrides)
    return ReportingRevisionRecord(**defaults), rows  # type: ignore[return-value,arg-type]


# -- period derivation ------------------------------------------------------


def test_both_sides_derive_the_same_period_and_expected_at() -> None:
    # The documented worked example: activated 00:20 with hourly periods, the
    # first period is [01:00, 02:00) and expected_at is 03:00.
    configuration = _configuration()
    ordinal = _ordinal_for(configuration, 1)
    boundary = derive_period(HOURLY, account_timezone="UTC", ordinal=ordinal)
    assert boundary.start == datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    assert boundary.end == datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    assert boundary.expected_at == datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)


def test_expected_at_is_always_period_end_plus_the_sla() -> None:
    schedule = ReportingScheduleSpec(period_duration="P1D", delivery_sla="PT6H")
    boundary = derive_period(schedule, account_timezone="UTC", ordinal=100)
    assert boundary.expected_at - boundary.end == timedelta(hours=6)


def test_calendar_durations_are_refused() -> None:
    # A month's length depends on which month it is, so two parties cannot
    # derive it identically -- which is the whole point of publishing a schedule.
    with pytest.raises(ValueError, match="not a supported reporting duration"):
        iso_duration_to_timedelta("P1M")


def test_periods_track_wall_clock_across_a_dst_transition() -> None:
    # US DST ends 2026-11-01. A source-local day is still one P1D period; only
    # its UTC length changes. A derivation that walked fixed 24-hour UTC
    # offsets would shift every later boundary by an hour and silently report
    # each day against the wrong window.
    schedule = ReportingScheduleSpec(
        period_duration="P1D",
        delivery_sla="PT1H",
        alignment="custom_timezone",
        period_timezone="America/New_York",
        # Anchor on a local midnight so periods are local calendar days.
        period_anchor=datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc),
    )
    spans = {
        derive_period(schedule, account_timezone="UTC", ordinal=ordinal).start: (
            derive_period(schedule, account_timezone="UTC", ordinal=ordinal).end
            - derive_period(schedule, account_timezone="UTC", ordinal=ordinal).start
        )
        for ordinal in range(4)
    }
    # 2026-11-01 local is the 25-hour day; every neighbour is 24.
    assert sorted(spans.values()) == [
        timedelta(hours=24),
        timedelta(hours=24),
        timedelta(hours=24),
        timedelta(hours=25),
    ]


# -- obligations before reports ---------------------------------------------


async def test_an_obligation_is_committed_before_any_source_data_exists() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    producer = ReportingProducer(source=_NullSource(), offerings=ProducerOfferings(), store=store)
    committed = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 3, 30, tzinfo=timezone.utc)
    )
    assert [item.period.start.isoformat() for item in committed][:2] == [
        "2026-09-01T01:00:00+00:00",
        "2026-09-01T02:00:00+00:00",
    ]
    # Committed with no revision anywhere: a missing first report is visible.
    for obligation in committed:
        assert (
            await store.list_revisions(
                account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
            )
            == ()
        )


async def test_closing_periods_twice_commits_each_obligation_once() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    producer = ReportingProducer(source=_NullSource(), offerings=ProducerOfferings(), store=store)
    now = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
    first = await producer.close_elapsed_periods(configuration, now=now)
    second = await producer.close_elapsed_periods(configuration, now=now)
    assert first and second == []


async def test_a_period_that_has_not_closed_is_not_obligated() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    producer = ReportingProducer(source=_NullSource(), offerings=ProducerOfferings(), store=store)
    # Exactly at the boundary: the obligation appears in the first snapshot
    # strictly after it, never at it.
    committed = await producer.close_elapsed_periods(
        configuration, now=datetime(2026, 9, 1, 2, 0, tzinfo=timezone.utc)
    )
    assert [item.period.end.isoformat() for item in committed] == ["2026-09-01T02:00:00+00:00"]


def test_scope_must_freeze_at_the_period_boundary() -> None:
    configuration = _configuration()
    boundary = derive_period(HOURLY, account_timezone="UTC", ordinal=_ordinal_for(configuration, 1))
    with pytest.raises(ValueError, match="scope_resolved_at must equal the period end"):
        _obligation(configuration, scope_resolved_at=boundary.end + timedelta(minutes=1))


# -- health -----------------------------------------------------------------


async def test_health_walks_waiting_to_delayed_to_action_required() -> None:
    configuration = _configuration()
    obligation = _obligation(configuration)
    expected = obligation.period.expected_at
    deadline = obligation.automated_recovery_deadline_at

    def health_at(moment: datetime) -> str:
        return project_obligation_health(
            obligation, [], ledger_as_of=moment, scope_closed=True
        ).health

    assert health_at(expected - timedelta(minutes=1)) == "waiting"
    assert health_at(expected + timedelta(minutes=1)) == "delayed"
    assert health_at(deadline + timedelta(minutes=1)) == "action_required"


async def test_a_zero_row_revision_satisfies_an_obligation() -> None:
    # The distinction the whole tier exists for: a quiet campaign is complete,
    # a dead feed is not.
    configuration = _configuration()
    obligation = _obligation(configuration)
    revision, _ = _revision(obligation, rows=[], control_totals=(("impressions", "0"),))
    projection = project_obligation_health(
        obligation,
        [revision],
        ledger_as_of=obligation.automated_recovery_deadline_at + timedelta(days=1),
        scope_closed=True,
    )
    assert projection.satisfied is True
    assert projection.health == "complete"
    assert projection.issues == ()


async def test_a_snapshot_revision_does_not_satisfy_an_official_obligation() -> None:
    configuration = _configuration(required_finality="official")
    obligation = _obligation(configuration)
    snapshot, _ = _revision(obligation, finality="snapshot")
    projection = project_obligation_health(
        obligation,
        [snapshot],
        ledger_as_of=obligation.period.expected_at + timedelta(minutes=1),
        scope_closed=False,
    )
    assert projection.satisfied is False
    assert projection.health == "delayed"
    # Something *was* published, so production is not pending.
    assert projection.production_status == "published"


async def test_an_unreadable_revision_is_action_required_not_complete() -> None:
    # Core promises retained readability; evidence nobody can read is not
    # evidence.
    configuration = _configuration()
    obligation = _obligation(configuration)
    revision, _ = _revision(obligation)
    from dataclasses import replace

    projection = project_obligation_health(
        obligation,
        [replace(revision, readable=False)],
        ledger_as_of=obligation.period.expected_at + timedelta(days=1),
        scope_closed=True,
    )
    assert projection.health == "action_required"
    assert projection.issues[0].code == "RESOURCE_EXPIRED"
    assert projection.issues[0].responsible_party == "seller"


def test_issue_identity_is_stable_across_reads() -> None:
    configuration = _configuration()
    obligation = _obligation(configuration)
    moment = obligation.period.expected_at + timedelta(minutes=5)
    first = project_obligation_health(obligation, [], ledger_as_of=moment, scope_closed=False)
    second = project_obligation_health(obligation, [], ledger_as_of=moment, scope_closed=False)
    assert first.issues[0].issue_id == second.issues[0].issue_id


def test_incomplete_coverage_makes_a_scope_action_required() -> None:
    assert (
        aggregate_reporting_health(
            ["complete", "complete"], scope_closed=True, coverage_complete=False
        )
        == "action_required"
    )
    assert (
        aggregate_reporting_health(
            ["complete", "complete"], scope_closed=True, coverage_complete=True
        )
        == "complete"
    )


def test_scope_health_is_worst_case() -> None:
    assert (
        aggregate_reporting_health(
            ["complete", "delayed"], scope_closed=True, coverage_complete=True
        )
        == "delayed"
    )
    assert (
        aggregate_reporting_health(
            ["delayed", "action_required"], scope_closed=True, coverage_complete=True
        )
        == "action_required"
    )


# -- immutability -----------------------------------------------------------


async def test_an_official_revision_is_terminal() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation, reporting_revision_id="rpr_1")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(obligation, reporting_revision_id="rpr_2")
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(second, rows2)
    assert error.value.code == "OFFICIAL_REVISION_TERMINAL"


async def test_recommitting_a_revision_id_with_new_content_is_a_conflict() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation)
    await store.commit_revision(first, rows)
    assert (await store.commit_revision(first, rows)).reporting_revision_id == "rpr_1"

    changed, changed_rows = _revision(
        obligation, rows=[{"media_buy_id": "mb_1", "impressions": 99}]
    )
    with pytest.raises(LedgerConflictError, match="different content"):
        await store.commit_revision(changed, changed_rows)


async def test_a_snapshot_restatement_supersedes_the_current_leaf() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation, reporting_revision_id="rpr_1", finality="snapshot")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(
        obligation,
        reporting_revision_id="rpr_2",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_1",
    )
    await store.commit_revision(second, rows2)

    projection = project_obligation_health(
        obligation,
        await store.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        ),
        ledger_as_of=obligation.period.expected_at,
        scope_closed=True,
    )
    assert projection.current_revision is not None
    assert projection.current_revision.reporting_revision_id == "rpr_2"


async def test_superseding_a_stale_leaf_is_refused() -> None:
    # Otherwise a successful retry could erase a recorded restatement by
    # forking the chain.
    store = InMemoryReportingLedgerStore()
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation, reporting_revision_id="rpr_1", finality="snapshot")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(
        obligation,
        reporting_revision_id="rpr_2",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_1",
    )
    await store.commit_revision(second, rows2)
    third, rows3 = _revision(
        obligation,
        reporting_revision_id="rpr_3",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_1",
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(third, rows3)
    assert error.value.code == "SUPERSEDES_STALE"


def test_an_official_revision_cannot_supersede_anything() -> None:
    configuration = _configuration()
    obligation = _obligation(configuration)
    with pytest.raises(ValueError, match="terminal"):
        _revision(obligation, supersedes_reporting_revision_id="rpr_0")


def test_an_official_revision_needs_finality_evidence() -> None:
    configuration = _configuration()
    obligation = _obligation(configuration)
    with pytest.raises(ValueError, match="finality basis"):
        _revision(obligation, finality_basis=None, finality_policy_id=None, finalized_at=None)


async def test_an_adjustment_requires_an_official_predecessor() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    snapshot, rows = _revision(obligation, finality="snapshot")
    await store.commit_revision(snapshot, rows)
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_adjustment(
            ReportingAdjustmentRecord(
                reporting_adjustment_id="rpa_1",
                account_id=ACCOUNT,
                adjusts_reporting_revision_id=snapshot.reporting_revision_id,
                reason_code="source_correction",
                accounting_period_start=obligation.period.start,
                accounting_period_end=obligation.period.end,
                control_total_deltas=(("impressions", "-1"),),
                correction_observed_at=obligation.period.end,
                created_at=obligation.period.end,
            )
        )
    assert error.value.code == "ADJUSTMENT_REQUIRES_OFFICIAL"


async def test_a_configuration_generation_is_immutable() -> None:
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(_configuration())
    with pytest.raises(LedgerConflictError, match="publish a new version"):
        await store.put_configuration(_configuration(reporting_profile="something_else"))


# -- the revision binding ---------------------------------------------------


def test_the_revision_binding_covers_exactly_the_four_bound_fields() -> None:
    rows = [{"media_buy_id": "mb_1", "impressions": 5}]
    baseline = revision_content_sha256(
        reporting_revision_id="rpr_1",
        row_count=1,
        control_totals=(("impressions", "5"),),
        reporting_rows=rows,
    )
    assert baseline == revision_content_sha256(
        reporting_revision_id="rpr_1",
        row_count=1,
        control_totals=(("impressions", "5"),),
        reporting_rows=[{"impressions": 5, "media_buy_id": "mb_1"}],
    )
    assert baseline != revision_content_sha256(
        reporting_revision_id="rpr_1",
        row_count=1,
        control_totals=(("impressions", "6"),),
        reporting_rows=rows,
    )


async def test_an_exact_revision_read_walks_a_frozen_row_set() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    rows = [{"media_buy_id": "mb_1", "day": index} for index in range(5)]
    revision, _ = _revision(obligation, rows=rows, control_totals=(("impressions", "0"),))
    await store.commit_revision(revision, rows)

    walked: list[dict[str, object]] = []
    cursor = None
    while True:
        page = await store.read_revision_rows(
            account_id=ACCOUNT,
            reporting_revision_id=revision.reporting_revision_id,
            cursor=cursor,
            limit=2,
        )
        walked.extend(page.rows)
        assert page.total_count == 5
        if not page.has_more:
            break
        cursor = page.cursor
    assert walked == rows
    assert (
        revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=len(walked),
            control_totals=revision.control_totals,
            reporting_rows=walked,
        )
        == revision.revision_content_sha256
    )


async def test_a_revision_read_is_scoped_to_its_account() -> None:
    store = InMemoryReportingLedgerStore()
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    assert (
        await store.get_revision(
            account_id="someone-else", reporting_revision_id=revision.reporting_revision_id
        )
        is None
    )


# -- status handler ---------------------------------------------------------


async def _seeded(
    *, ledger_as_of: datetime | None = None
) -> tuple[InMemoryReportingLedgerStore, ReportingObligationRecord]:
    """A store holding one obligation, with its ledger boundary pinned.

    ``ledger_as_of`` defaults well past every deadline so a test that does not
    care about the clock gets settled health rather than whatever the wall
    clock happens to say on the day the suite runs.
    """
    boundary = ledger_as_of or datetime(2026, 9, 30, tzinfo=timezone.utc)
    store = InMemoryReportingLedgerStore(clock=lambda: boundary)
    configuration = _configuration()
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    return store, obligation


async def test_summary_reports_waiting_before_anything_is_due() -> None:
    # The obligation is committed at the period close, but nothing is late
    # until expected_at.
    store, obligation = await _seeded(ledger_as_of=datetime(2026, 9, 1, 2, 1, tzinfo=timezone.utc))
    handler = ReportingStatusHandler(store)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    assert payload["health"] == "waiting"
    assert payload["obligation_counts"]["total"] == 1
    assert payload["issues"] == []


async def test_summary_reports_delayed_once_expected_at_has_passed() -> None:
    store, obligation = await _seeded(ledger_as_of=datetime(2026, 9, 1, 3, 30, tzinfo=timezone.utc))
    payload = await ReportingStatusHandler(store).handle({"view": "summary"}, caller=CALLER)
    assert payload["health"] == "delayed"
    assert payload["issues"][0]["code"] == "REPORT_OVERDUE"
    assert payload["issues"][0]["recommended_action"] == "wait_for_retry"


async def test_summary_escalates_after_the_automated_recovery_window() -> None:
    # Never park a dead feed in delayed.
    store, obligation = await _seeded(ledger_as_of=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc))
    payload = await ReportingStatusHandler(store).handle({"view": "summary"}, caller=CALLER)
    assert payload["health"] == "action_required"
    assert payload["issues"][0]["recommended_action"] == "contact_seller"


async def test_periods_view_paginates_one_snapshot() -> None:
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store, page_size=1)
    first = await handler.handle({"view": "periods"}, caller=CALLER)
    assert first["pagination"]["total_count"] == 2
    assert first["pagination"]["has_more"] is True
    second = await handler.handle(
        {"view": "periods", "pagination": {"cursor": first["pagination"]["cursor"]}},
        caller=CALLER,
    )
    assert second["ledger_snapshot_id"] == first["ledger_snapshot_id"]
    assert len(second["revisions"]) == 1


async def test_a_cursor_from_another_filter_set_is_refused() -> None:
    # A cursor is bound to caller, account, *and* filters: continuing it under
    # a different filter set would blend two ledger boundaries into one walk
    # the consumer believes is consistent.
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store, page_size=1)
    first = await handler.handle({"view": "periods"}, caller=CALLER)
    cursor = first["pagination"]["cursor"]
    with pytest.raises(LedgerConflictError) as error:
        await handler.handle(
            {
                "view": "periods",
                "delivery_config_ids": ["something_else"],
                "pagination": {"cursor": cursor},
            },
            caller=CALLER,
        )
    assert error.value.code == "CURSOR_SNAPSHOT_MISMATCH"


async def test_changes_after_replays_only_later_records() -> None:
    store, obligation = await _seeded()
    handler = ReportingStatusHandler(store)
    first = await handler.handle({"view": "periods"}, caller=CALLER)
    checkpoint = first["changes_checkpoint"]

    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    incremental = await handler.handle(
        {"view": "periods", "changes_after": checkpoint}, caller=CALLER
    )
    assert [item["reporting_revision_id"] for item in incremental["revisions"]] == ["rpr_1"]
    assert incremental["periods"] == []


async def test_an_unknown_revision_looks_the_same_as_an_unauthorized_one() -> None:
    # A distinguishable "not found" is an enumeration oracle.
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store)
    other = ReportingStatusCaller(account_id="acct_2", consumer_id="buyer_2")
    with pytest.raises(LedgerConflictError) as unknown:
        await handler.handle(
            {"view": "revision", "reporting_revision_id": "rpr_nope"}, caller=CALLER
        )
    with pytest.raises(LedgerConflictError) as unauthorized:
        await handler.handle({"view": "revision", "reporting_revision_id": "rpr_1"}, caller=other)
    assert unknown.value.code == unauthorized.value.code == "LOOKUP_UNAVAILABLE"


async def test_scope_data_through_is_the_weakest_member() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    configuration = _configuration()
    await store.put_configuration(configuration)
    early = await store.commit_obligation(_obligation(configuration, ordinal=1))
    late = await store.commit_obligation(
        _obligation(configuration, ordinal=2, reporting_obligation_id="rpo_2")
    )
    for index, obligation in enumerate((early, late)):
        revision, rows = _revision(obligation, reporting_revision_id=f"rpr_{index}")
        await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    assert payload["data_through"] == early.period.end.isoformat().replace("+00:00", "Z")


# -- consumer status preview ------------------------------------------------


def _statement(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "reporting_status_id": "status_2026_09_01_0100_01",
        "delivery_config_id": "daily_reporting",
        "delivery_config_version": 1,
        "report_definition_id": "daily_delivery_v1",
        "period": {
            "start": "2026-09-01T01:00:00Z",
            "end": "2026-09-01T02:00:00Z",
            "source_timezone": "UTC",
        },
        "consumer_status": "obligation_missing",
        "status_as_of": "2026-09-01T03:05:00Z",
    }
    payload.update(overrides)
    return payload


async def test_the_ingest_is_off_by_default() -> None:
    # sync_reporting_status is additive and opt-in: a seller that has not
    # advertised consumer_status_task must not silently accept statements.
    store, _ = await _seeded()
    with pytest.raises(ConsumerStatusDisabledError, match="opt-in extension"):
        await ConsumerStatusIngest(store).handle(
            {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1"
        )


async def test_a_statement_records_and_replays_idempotently() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    request = {"statuses": [_statement()]}
    first = await ingest.handle(request, account_id=ACCOUNT, consumer_id="buyer_1")
    assert first["results"][0]["result"] == "recorded"
    second = await ingest.handle(request, account_id=ACCOUNT, consumer_id="buyer_1")
    assert second["results"][0]["result"] == "unchanged"


async def test_obligation_missing_needs_no_seller_obligation_id() -> None:
    # Requiring one would make the first missing report invisible again.
    store = InMemoryReportingLedgerStore(clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    await store.put_configuration(_configuration())
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    result = await ingest.handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1"
    )
    assert result["results"][0]["result"] == "recorded"


async def test_the_wire_conditionals_are_enforced_from_the_schema() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    result = await ingest.handle(
        {"statuses": [_statement(consumer_status="received")]},
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert result["results"][0]["result"] == "failed"
    assert result["results"][0]["errors"][0]["code"] == "INVALID_CONSUMER_STATUS"


async def test_supersession_is_atomic_and_exact() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    await ingest.handle({"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1")

    # A second statement that does not supersede the leaf is refused.
    orphan = await ingest.handle(
        {"statuses": [_statement(reporting_status_id="status_2026_09_01_0100_02")]},
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert orphan["results"][0]["errors"][0]["code"] == "STATUS_SUPERSEDES_REQUIRED"

    # Superseding the real leaf works.
    repaired = await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_2026_09_01_0100_03",
                    supersedes_reporting_status_id="status_2026_09_01_0100_01",
                    consumer_status="revision_missing",
                    reporting_obligation_id="rpo_1",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert repaired["results"][0]["result"] == "recorded"

    # Superseding the now-stale leaf again is refused.
    stale = await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_2026_09_01_0100_04",
                    supersedes_reporting_status_id="status_2026_09_01_0100_01",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert stale["results"][0]["errors"][0]["code"] == "STATUS_SUPERSEDES_STALE"


async def test_a_batch_may_not_carry_two_updates_for_one_chain() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    result = await ingest.handle(
        {
            "statuses": [
                _statement(),
                _statement(reporting_status_id="status_2026_09_01_0100_09"),
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert result["results"][0]["result"] == "recorded"
    assert result["results"][1]["errors"][0]["code"] == "DUPLICATE_CHAIN_IN_BATCH"


async def test_an_unknown_configuration_generation_is_refused() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    result = await ingest.handle(
        {"statuses": [_statement(delivery_config_version=99)]},
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert result["results"][0]["errors"][0]["code"] == "UNKNOWN_CONFIGURATION_GENERATION"


async def test_a_conflicting_statement_degrades_only_the_submitting_caller() -> None:
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    await ingest.handle(
        {"statuses": [_statement(consumer_status="obligation_missing")]},
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)

    mine = await handler.handle({"view": "periods"}, caller=CALLER)
    assert mine["periods"][0]["health"] == "action_required"
    assert mine["periods"][0]["issues"][0]["code"] == "CONSUMER_STATUS_MISMATCH"

    theirs = await handler.handle(
        {"view": "periods"},
        caller=ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer_2"),
    )
    # Another caller's view is untouched by a statement it did not make.
    assert theirs["periods"][0]["health"] in {"healthy", "complete"}
    assert theirs["periods"][0]["issues"] == []


async def test_received_against_a_superseded_revision_is_a_mismatch() -> None:
    store = InMemoryReportingLedgerStore(clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation, reporting_revision_id="rpr_1", finality="snapshot")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(
        obligation,
        reporting_revision_id="rpr_2",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_1",
    )
    await store.commit_revision(second, rows2)

    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    await ingest.handle(
        {
            "statuses": [
                _statement(
                    consumer_status="received",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id="rpr_1",
                    observed_revision_content_sha256=first.revision_content_sha256,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    payload = await handler.handle({"view": "periods"}, caller=CALLER)
    issue = payload["periods"][0]["issues"][0]
    assert issue["code"] == "CONSUMER_STATUS_MISMATCH"
    assert "superseded restatement" in issue["message"]


async def test_missing_consumer_status_does_not_degrade_seller_health() -> None:
    # A buyer that has not integrated the loop is not evidence of anything.
    store, obligation = await _seeded()
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    payload = await handler.handle({"view": "periods"}, caller=CALLER)
    assert payload["periods"][0]["health"] in {"healthy", "complete"}
    assert payload["periods"][0]["issues"] == []
    assert "current_consumer_status_id" not in payload["periods"][0]


async def test_a_chain_filed_before_the_obligation_attaches_after_repair() -> None:
    # The repair path: obligation_missing is filed, the seller creates the
    # obligation, and the existing chain must attach rather than be lost.
    store = InMemoryReportingLedgerStore(clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    configuration = _configuration()
    await store.put_configuration(configuration)
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    await ingest.handle({"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1")
    obligation = await store.commit_obligation(_obligation(configuration))
    attached = await store.list_consumer_statuses(
        account_id=ACCOUNT,
        consumer_id="buyer_1",
        reporting_obligation_ids=[obligation.reporting_obligation_id],
    )
    assert [item.reporting_status_id for item in attached] == ["status_2026_09_01_0100_01"]


async def test_consumer_statuses_are_not_disclosed_across_callers() -> None:
    store, _ = await _seeded()
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    await ingest.handle({"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1")
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    theirs = await handler.handle(
        {"view": "periods"},
        caller=ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer_2"),
    )
    assert theirs["consumer_statuses"] == []


class _NullSource:
    """A source that is never called; period-close tests do not fetch."""

    @property
    def capabilities(self):  # pragma: no cover - never reached
        raise AssertionError("period close must not touch the source")

    async def execute(self, request, *, cancel, heartbeat=None):  # pragma: no cover
        raise AssertionError("period close must not touch the source")


# -- AdCP 3.2.0-rc.3 consumer-status hardening ---------------------------------
#
# Five behaviours, each with its own failure mode if it is wrong:
#   1. content_mismatch carries its closed mismatch_code all the way through.
#   2. A received made stale only by a restatement is delayed, not a page-out.
#   3. Later restatements must not restart that grace window.
#   4. An advertised escalation boundary beats the grace window.
#   5. Silence is counted, never escalated, and never changes health.


def _rc3_statement(**overrides) -> dict[str, object]:
    overrides.setdefault("reporting_status_id", "status_rc3_2026_09_01_0100_01")
    overrides.setdefault("consumer_status", "received")
    return _statement(**overrides)


async def _received_then_restated(
    *,
    ledger_as_of: datetime,
    restatements: int = 1,
    escalation: ReportingDeliveryEscalation | None = None,
) -> tuple[InMemoryReportingLedgerStore, ReportingObligationRecord, dict[str, Any]]:
    """Buyer reads a snapshot, seller restates it, then the caller polls.

    ``delivery_sla`` is PT1H, so the grace deadline is the first superseding
    revision's ``created_at`` plus one hour.
    """
    store = InMemoryReportingLedgerStore(clock=lambda: ledger_as_of)
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first = await store.commit_revision(
        *_revision(obligation, reporting_revision_id="rpr_snap_1", finality="snapshot")
    )
    previous = first
    for index in range(restatements):
        previous = await store.commit_revision(
            *_revision(
                obligation,
                reporting_revision_id=f"rpr_snap_{index + 2}",
                finality="snapshot",
                supersedes_reporting_revision_id=previous.reporting_revision_id,
                created_at=_utc_at(2026, 9, 1, 3, 0) + timedelta(hours=index),
            )
        )
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    posted = await ingest.handle(
        {
            "statuses": [
                _rc3_statement(
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=first.reporting_revision_id,
                    observed_revision_content_sha256=first.revision_content_sha256,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    assert posted["results"][0]["result"] == "recorded", posted
    handler = ReportingStatusHandler(store, consumer_status_enabled=True, escalation=escalation)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    return store, obligation, payload


def _utc_at(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


async def test_content_mismatch_round_trips_with_its_closed_code() -> None:
    # The code is the whole point: "the metric is missing" and "the currency is
    # wrong" are different claims, and an agent dispatches on the value rather
    # than on prose. Dropping it would leave the buyer believing it had said
    # which contract fact was contradicted.
    store, obligation = await _seeded()
    revision = await store.commit_revision(*_revision(obligation))
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    result = await ingest.handle(
        {
            "statuses": [
                _rc3_statement(
                    consumer_status="content_mismatch",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                    mismatch_code="metric_missing",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    entry = result["results"][0]
    assert entry["result"] == "recorded", entry
    assert entry["consumer_status"]["mismatch_code"] == "metric_missing"

    stored = await store.list_consumer_statuses(account_id=ACCOUNT, consumer_id="buyer_1")
    assert [item.mismatch_code for item in stored] == ["metric_missing"]

    # It conflicts with an otherwise healthy projection immediately -- there is
    # no grace window for content that arrived and is wrong.
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    assert payload["health"] == "action_required"
    issue = next(item for item in payload["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["severity"] == "action_required"
    assert issue["responsible_party"] == "seller"
    assert "metric_missing" in issue["message"]


async def test_a_content_mismatch_retry_that_changes_only_the_code_is_a_conflict() -> None:
    # Reusing a status id with different content is an idempotency conflict.
    # Without mismatch_code in the digest, changing the claim would silently
    # overwrite the recorded one under the same immutable identity.
    store, obligation = await _seeded()
    revision = await store.commit_revision(*_revision(obligation))
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)

    def payload(code: str) -> dict[str, Any]:
        return {
            "statuses": [
                _rc3_statement(
                    consumer_status="content_mismatch",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                    mismatch_code=code,
                )
            ]
        }

    first = await ingest.handle(payload("metric_missing"), account_id=ACCOUNT, consumer_id="b")
    assert first["results"][0]["result"] == "recorded"
    replay = await ingest.handle(payload("metric_missing"), account_id=ACCOUNT, consumer_id="b")
    assert replay["results"][0]["result"] == "unchanged"
    changed = await ingest.handle(payload("currency_mismatch"), account_id=ACCOUNT, consumer_id="b")
    assert changed["results"][0]["result"] == "failed"


async def test_stale_received_is_delayed_inside_the_grace_window() -> None:
    # The buyer consumed exactly what the seller then required. Paging a human
    # the instant a seller restates a provisional revision would train buyers
    # to ignore the signal, so this is the one conflict with a grace window.
    _store, _obligation, payload = await _received_then_restated(
        ledger_as_of=_utc_at(2026, 9, 1, 3, 30)
    )
    assert payload["health"] == "delayed"
    issue = next(item for item in payload["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["severity"] == "delayed"
    assert issue["recommended_action"] == "wait_for_retry"
    assert issue["opened_at"]


async def test_stale_received_escalates_at_the_grace_deadline() -> None:
    _store, _obligation, payload = await _received_then_restated(
        ledger_as_of=_utc_at(2026, 9, 1, 4, 1)
    )
    assert payload["health"] == "action_required"
    issue = next(item for item in payload["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["severity"] == "action_required"
    assert issue["recommended_action"] != "wait_for_retry"


async def test_later_restatements_do_not_restart_the_grace_window() -> None:
    # Anchored to the FIRST supersession, not the newest revision. Otherwise a
    # seller could hold a genuinely unresolved mismatch below action_required
    # forever by restating on a timer.
    _store, _obligation, payload = await _received_then_restated(
        ledger_as_of=_utc_at(2026, 9, 1, 4, 30), restatements=3
    )
    # The third restatement was created at 05:00, so a window anchored to the
    # newest revision would still be open at 04:30. It is not.
    assert payload["health"] == "action_required"


async def test_the_issue_keeps_one_id_and_opened_at_across_the_severity_change() -> None:
    # A consumer ages one work item, not two. opened_at also anchors the
    # escalation clock, so an advancing value would make escalation
    # unreachable by polling.
    store = InMemoryReportingLedgerStore(clock=lambda: _utc_at(2026, 9, 1, 3, 30))
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first = await store.commit_revision(
        *_revision(obligation, reporting_revision_id="rpr_snap_1", finality="snapshot")
    )
    await store.commit_revision(
        *_revision(
            obligation,
            reporting_revision_id="rpr_snap_2",
            finality="snapshot",
            supersedes_reporting_revision_id=first.reporting_revision_id,
            created_at=_utc_at(2026, 9, 1, 3, 0),
        )
    )
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _rc3_statement(
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=first.reporting_revision_id,
                    observed_revision_content_sha256=first.revision_content_sha256,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )

    boundary = _utc_at(2026, 9, 1, 3, 30)
    store._clock = lambda: boundary  # noqa: SLF001 - move the ledger boundary
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    delayed = await handler.handle({"view": "summary"}, caller=CALLER)
    delayed_issue = next(
        item for item in delayed["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH"
    )
    assert delayed_issue["severity"] == "delayed"

    boundary = _utc_at(2026, 9, 1, 5, 0)
    store._clock = lambda: boundary  # noqa: SLF001
    escalated = await handler.handle({"view": "summary"}, caller=CALLER)
    escalated_issue = next(
        item for item in escalated["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH"
    )
    assert escalated_issue["severity"] == "action_required"
    assert escalated_issue["issue_id"] == delayed_issue["issue_id"]
    assert escalated_issue["opened_at"] == delayed_issue["opened_at"]


async def test_an_advertised_escalation_window_beats_the_grace_window() -> None:
    # Precedence is explicit in the spec: when the two overlap the escalation
    # boundary wins, and wait_for_retry must not survive it.
    escalation = ReportingDeliveryEscalation(
        consumer_mismatch_escalation=timedelta(minutes=5),
        operations_contact_email="reporting-ops@seller.example",
    )
    store = InMemoryReportingLedgerStore(clock=lambda: _utc_at(2026, 9, 1, 3, 5))
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first = await store.commit_revision(
        *_revision(obligation, reporting_revision_id="rpr_snap_1", finality="snapshot")
    )
    await store.commit_revision(
        *_revision(
            obligation,
            reporting_revision_id="rpr_snap_2",
            finality="snapshot",
            supersedes_reporting_revision_id=first.reporting_revision_id,
            created_at=_utc_at(2026, 9, 1, 3, 0),
        )
    )
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _rc3_statement(
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=first.reporting_revision_id,
                    observed_revision_content_sha256=first.revision_content_sha256,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True, escalation=escalation)
    # Open the issue at 03:05, inside the PT1H grace window.
    opened = await handler.handle({"view": "summary"}, caller=CALLER)
    assert opened["health"] == "delayed"

    # 03:11 is still inside the grace window (deadline 04:00) but past
    # opened_at + 5 minutes, so the escalation boundary decides.
    boundary = _utc_at(2026, 9, 1, 3, 11)
    store._clock = lambda: boundary  # noqa: SLF001
    escalated = await handler.handle({"view": "summary"}, caller=CALLER)
    assert escalated["health"] == "action_required"
    issue = next(item for item in escalated["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH")
    assert issue["recommended_action"].startswith("contact_")


def test_an_escalation_window_requires_a_contact() -> None:
    # Committing to escalate with nowhere to escalate to is the failure the
    # requirement exists to prevent, and the schema makes the contact
    # mandatory when the window is advertised.
    with pytest.raises(ValueError, match="requires operations_contact"):
        ReportingDeliveryEscalation(consumer_mismatch_escalation=timedelta(hours=1))


async def test_silence_is_counted_and_changes_no_health() -> None:
    # A buyer that has not integrated the loop is not evidence about the
    # seller. The count overlaps the health counts rather than partitioning
    # them, and never becomes an issue.
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    # The seller did everything right and the scope is closed, so this reads
    # complete even though the buyer never said a word. Silence is the buyer's
    # gap, not the seller's.
    assert payload["health"] == "complete"
    counts = payload["obligation_counts"]
    assert counts["consumer_status_pending"] == 1
    # Overlapping, not partitioning: the same obligation is counted complete.
    assert counts["complete"] == 1
    assert counts["total"] == 1
    assert payload["issues"] == []


async def test_any_posted_status_clears_the_pending_count() -> None:
    store, obligation = await _seeded()
    revision = await store.commit_revision(*_revision(obligation))
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _rc3_statement(
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    assert payload["obligation_counts"]["consumer_status_pending"] == 0
    assert payload["health"] == "complete"


async def test_pending_is_absent_when_the_task_is_not_advertised() -> None:
    # The field is required only when the seller advertises consumer_status_task.
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    handler = ReportingStatusHandler(store)
    payload = await handler.handle({"view": "summary"}, caller=CALLER)
    assert "consumer_status_pending" not in payload["obligation_counts"]


async def test_a_waived_mismatch_leaves_the_view_degraded_without_publishing_it() -> None:
    # Waiving records an off-protocol agreement to stop acting, not a finding
    # that the reporting is fine. If it returned the period to healthy a seller
    # could unilaterally erase a buyer-attributed disagreement, which is the
    # one outcome this separately attributed loop exists to prevent.
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1"
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    before = await handler.handle({"view": "summary"}, caller=CALLER)
    assert before["health"] == "action_required"
    issue_key = consumer_mismatch_issue_key(
        account_id=ACCOUNT,
        consumer_id="buyer_1",
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        report_definition_id=obligation.report_definition_id,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
    )
    await store.set_issue_state(
        issue_key=issue_key,
        account_id=ACCOUNT,
        state="waived",
        at=_utc_at(2026, 9, 30),
        external_ref="OPS-1234",
    )
    after = await handler.handle({"view": "summary"}, caller=CALLER)
    assert after["health"] == "action_required"
    assert [item for item in after["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH"] == []


async def test_an_operator_cannot_resolve_a_live_mismatch() -> None:
    # resolved is reachable only when the condition actually clears. Exposing
    # it as an operator action would be exactly the unilateral erasure the
    # lifecycle rule forbids, so the store refuses the value at runtime as well
    # as in the type -- an adopter calling from untyped code gets the same
    # answer mypy gives.
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1"
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    await handler.handle({"view": "summary"}, caller=CALLER)
    issue_key = consumer_mismatch_issue_key(
        account_id=ACCOUNT,
        consumer_id="buyer_1",
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        report_definition_id=obligation.report_definition_id,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
    )
    with pytest.raises(LedgerConflictError) as caught:
        await store.set_issue_state(
            issue_key=issue_key,
            account_id=ACCOUNT,
            state="resolved",  # type: ignore[arg-type]
            at=_utc_at(2026, 9, 30),
        )
    assert caught.value.code == "ISSUE_STATE_NOT_OPERATOR_SETTABLE"


async def test_a_recurrence_after_retirement_gets_a_new_issue_id() -> None:
    store, obligation = await _seeded()
    revision = await store.commit_revision(*_revision(obligation))
    ingest = ConsumerStatusIngest(store, enabled=True, clock=store._clock)
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)

    await ingest.handle({"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1")
    first = await handler.handle({"view": "summary"}, caller=CALLER)
    first_issue = next(
        item for item in first["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH"
    )

    # The buyer supersedes with a statement that agrees, so the condition
    # clears and the occurrence is retired by the projection.
    await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_2026_09_01_0100_02",
                    supersedes_reporting_status_id="status_2026_09_01_0100_01",
                    consumer_status="received",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                    status_as_of="2026-09-01T04:00:00Z",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    cleared = await handler.handle({"view": "summary"}, caller=CALLER)
    assert [i for i in cleared["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH"] == []

    # The same condition recurs. rc.3 requires a new issue_id and a new
    # opened_at, so a consumer's work item for the old occurrence cannot be
    # silently reused for a new disagreement.
    await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_2026_09_01_0100_03",
                    supersedes_reporting_status_id="status_2026_09_01_0100_02",
                    consumer_status="revision_missing",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    status_as_of="2026-09-01T05:00:00Z",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    again = await handler.handle({"view": "summary"}, caller=CALLER)
    again_issue = next(
        item for item in again["issues"] if item["code"] == "CONSUMER_STATUS_MISMATCH"
    )
    assert again_issue["issue_id"] != first_issue["issue_id"]


async def test_reserved_authoritative_party_is_rejected_at_config_install() -> None:
    # Rejected before the generation is stored and before any obligation
    # exists. Coercing to seller would be the dangerous option: the buyer asked
    # to be the authoritative counter and would silently get the opposite.
    store = InMemoryReportingLedgerStore()
    with pytest.raises(LedgerConflictError) as caught:
        await store.put_configuration(_configuration(authoritative_party="consumer"))
    assert caught.value.code == "UNSUPPORTED_FEATURE"
    assert await store.list_configurations(account_id=ACCOUNT) == ()


async def test_authoritative_party_seller_is_the_accepted_default() -> None:
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(_configuration())
    stored = await store.list_configurations(account_id=ACCOUNT)
    assert [item.authoritative_party for item in stored] == ["seller"]


# -- review fixes ------------------------------------------------------------


def test_the_identity_digest_omits_an_absent_mismatch_code() -> None:
    # BLOCKING regression guard. canonical_json_utf8_v1 encodes None as null,
    # so carrying the key unconditionally would change the digest of every
    # statement an rc.2 SDK wrote. After an in-place upgrade a buyer's exact
    # retry would then fail with STATUS_IDENTITY_CONFLICT instead of replaying
    # as `unchanged` -- the one thing an idempotent append-only surface must
    # never do.
    from adcp.reporting.ledger.models import ConsumerStatusRecord
    from adcp.reporting.ledger.pg import _consumer_status_payload
    from adcp.reporting.ledger.store import _consumer_status_identity

    def record(**overrides):
        base = dict(
            reporting_status_id="status_digest_00000001",
            account_id=ACCOUNT,
            consumer_id="buyer_1",
            delivery_config_id="daily_reporting",
            delivery_config_version=1,
            report_definition_id="daily_delivery_v1",
            period_start=_utc_at(2026, 9, 1, 1),
            period_end=_utc_at(2026, 9, 1, 2),
            period_source_timezone="UTC",
            consumer_status="received",
            status_as_of=_utc_at(2026, 9, 1, 3),
            recorded_at=_utc_at(2026, 9, 1, 3),
        )
        base.update(overrides)
        return ConsumerStatusRecord(**base)

    without = record()
    assert "mismatch_code" not in _consumer_status_payload(without)

    # ...and the key IS present once there is a code, so a code-only change is
    # still an idempotency conflict rather than a silent overwrite.
    with_code = record(mismatch_code="metric_missing")
    assert _consumer_status_payload(with_code)["mismatch_code"] == "metric_missing"
    assert _consumer_status_identity(without) != _consumer_status_identity(with_code)


def test_issue_state_moves_only_forward() -> None:
    # Without refusing the reverse edges an operator could un-waive an issue
    # and republish the same issue_id and opened_at -- resurrecting a work item
    # both parties agreed to stop acting on, under an identity the consumer has
    # already filed away.
    from adcp.reporting.ledger import check_issue_state_transition

    for current, requested in (
        ("open", "acknowledged"),
        ("open", "waived"),
        ("acknowledged", "waived"),
        ("acknowledged", "acknowledged"),
    ):
        check_issue_state_transition(current, requested)

    for current, requested in (
        ("waived", "acknowledged"),
        ("waived", "open"),
        ("resolved", "acknowledged"),
        ("resolved", "open"),
        ("acknowledged", "open"),
    ):
        with pytest.raises(LedgerConflictError) as caught:
            check_issue_state_transition(current, requested)
        assert caught.value.code == "ISSUE_STATE_TRANSITION_INVALID"


async def test_a_waived_issue_cannot_be_un_waived_through_the_store() -> None:
    store, obligation, issue_key = await _open_mismatch_issue()
    await store.set_issue_state(
        issue_key=issue_key, account_id=ACCOUNT, state="waived", at=_utc_at(2026, 9, 30)
    )
    with pytest.raises(LedgerConflictError) as caught:
        await store.set_issue_state(
            issue_key=issue_key,
            account_id=ACCOUNT,
            state="acknowledged",
            at=_utc_at(2026, 9, 30),
        )
    assert caught.value.code == "ISSUE_STATE_TRANSITION_INVALID"
    del obligation


async def test_get_issue_returns_only_live_occurrences() -> None:
    # The Protocol docstring promises the live occurrence. Returning a retired
    # row would let the projection re-emit a resolved issue's opened_at, and it
    # made the two stores disagree.
    store, _obligation, issue_key = await _open_mismatch_issue()
    assert await store.get_issue(issue_key=issue_key, account_id=ACCOUNT) is not None
    await store.retire_issue(issue_key=issue_key, account_id=ACCOUNT, at=_utc_at(2026, 9, 30))
    assert await store.get_issue(issue_key=issue_key, account_id=ACCOUNT) is None


async def _open_mismatch_issue():
    """A store with one obligation, one conflicting statement, and its issue."""
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_1"
    )
    issue_key = consumer_mismatch_issue_key(
        account_id=ACCOUNT,
        consumer_id="buyer_1",
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        report_definition_id=obligation.report_definition_id,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
    )
    return store, obligation, issue_key


async def test_opened_at_is_minted_at_ingest_not_at_the_first_poll() -> None:
    # opened_at anchors the escalation clock. Deferring it to the first
    # get_reporting_status would tie escalation to whether anyone polled: a
    # mismatch filed and never read could never cross
    # consumer_mismatch_escalation_seconds, which is exactly the unattended
    # case the boundary exists for.
    store, _obligation, issue_key = await _open_mismatch_issue()
    live = await store.get_issue(issue_key=issue_key, account_id=ACCOUNT)
    assert live is not None
    assert live.opened_at == store._clock()
    assert live.issue_state == "open"


async def test_an_unrelated_seller_problem_does_not_reset_the_escalation_clock() -> None:
    # project_consumer_mismatch returns None both when the condition cleared
    # and when the seller is already degraded for its own reasons. Retiring on
    # the latter would mint a new issue_id and opened_at on the later repair,
    # resetting the clock on a disagreement that never went away.
    store, obligation, issue_key = await _open_mismatch_issue()
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    first = await handler.handle({"view": "summary"}, caller=CALLER)
    opened = next(i for i in first["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH")

    # The seller's own revision becomes unreadable: health leaves healthy for a
    # reason that has nothing to do with the buyer's statement.
    revision = (
        await store.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
    )[0]
    await store.set_revision_readable(
        account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id, readable=False
    )
    degraded = await handler.handle({"view": "summary"}, caller=CALLER)
    assert degraded["health"] == "action_required"
    # The occurrence stayed open, so it is still there to carry opened_at.
    assert await store.get_issue(issue_key=issue_key, account_id=ACCOUNT) is not None

    # The seller repairs. The same issue comes back with the same identity.
    await store.set_revision_readable(
        account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id, readable=True
    )
    repaired = await handler.handle({"view": "summary"}, caller=CALLER)
    again = next(i for i in repaired["issues"] if i["code"] == "CONSUMER_STATUS_MISMATCH")
    assert again["issue_id"] == opened["issue_id"]
    assert again["opened_at"] == opened["opened_at"]


async def test_a_waived_issue_is_never_moved_to_resolved_by_the_projection() -> None:
    store, obligation, issue_key = await _open_mismatch_issue()
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    await handler.handle({"view": "summary"}, caller=CALLER)
    await store.set_issue_state(
        issue_key=issue_key, account_id=ACCOUNT, state="waived", at=_utc_at(2026, 9, 30)
    )
    revision = (
        await store.list_revisions(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
    )[0]
    # The buyer supersedes with an agreeing statement, so the condition clears.
    await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_2026_09_01_0100_09",
                    supersedes_reporting_status_id="status_2026_09_01_0100_01",
                    consumer_status="received",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=revision.reporting_revision_id,
                    observed_revision_content_sha256=revision.revision_content_sha256,
                    status_as_of="2026-09-01T06:00:00Z",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    await handler.handle({"view": "summary"}, caller=CALLER)
    # Waived stays waived: overwriting it with `resolved` would replace one
    # readable act with a different one, and the forward-only lifecycle forbids
    # the edge anyway.
    async with store._lock:
        stored = store._issues[(ACCOUNT, issue_key)]
    assert stored.issue_state == "waived"


# -- ingest identity resolution ---------------------------------------------


async def test_a_statement_naming_an_unresolvable_obligation_is_refused() -> None:
    # Recording it would let a buyer dispute a period against an obligation
    # that does not exist for this caller, and have the seller project
    # action_required against itself over it.
    store, _obligation = await _seeded()
    result = await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _statement(
                    consumer_status="revision_missing",
                    reporting_obligation_id="rpo_does_not_exist",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    entry = result["results"][0]
    assert entry["result"] == "failed"
    assert entry["errors"][0]["code"] == "LOOKUP_UNAVAILABLE"


async def test_a_statement_naming_an_unresolvable_revision_is_refused() -> None:
    store, obligation = await _seeded()
    await store.commit_revision(*_revision(obligation))
    result = await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _rc3_statement(
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id="rpr_does_not_exist",
                    observed_revision_content_sha256="f" * 64,
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    entry = result["results"][0]
    assert entry["result"] == "failed"
    assert entry["errors"][0]["code"] == "LOOKUP_UNAVAILABLE"


async def test_an_obligation_id_for_a_different_period_is_refused() -> None:
    # The id resolves but describes another period. Attaching this chain to it
    # would violate the chain-immutability rule.
    store, obligation = await _seeded()
    result = await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _statement(
                    consumer_status="revision_missing",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    period={
                        "start": "2026-09-02T01:00:00Z",
                        "end": "2026-09-02T02:00:00Z",
                        "source_timezone": "UTC",
                    },
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    entry = result["results"][0]
    assert entry["result"] == "failed"
    assert entry["errors"][0]["code"] == "OBLIGATION_IDENTITY_MISMATCH"


async def test_content_mismatch_against_a_superseded_revision_is_refused() -> None:
    # content_mismatch is valid only against a revision the seller *currently
    # requires*. Against a superseded one the dispute is already moot -- the
    # seller replaced the content -- and recording it would degrade the
    # caller's own view over bytes neither party stands behind.
    store = InMemoryReportingLedgerStore(clock=lambda: _utc_at(2026, 9, 30))
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first = await store.commit_revision(
        *_revision(obligation, reporting_revision_id="rpr_snap_1", finality="snapshot")
    )
    await store.commit_revision(
        *_revision(
            obligation,
            reporting_revision_id="rpr_snap_2",
            finality="snapshot",
            supersedes_reporting_revision_id=first.reporting_revision_id,
        )
    )
    result = await ConsumerStatusIngest(store, enabled=True, clock=store._clock).handle(
        {
            "statuses": [
                _rc3_statement(
                    consumer_status="content_mismatch",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    reporting_revision_id=first.reporting_revision_id,
                    observed_revision_content_sha256=first.revision_content_sha256,
                    mismatch_code="metric_missing",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_1",
    )
    entry = result["results"][0]
    assert entry["result"] == "failed"
    assert entry["errors"][0]["code"] == "REVISION_NOT_CURRENTLY_REQUIRED"


# -- the advertised capability block ----------------------------------------


def test_the_advertised_capability_block_validates_against_the_rc3_schema() -> None:
    # consumer_status_task and revision_content_task are `const` *task names*,
    # not booleans, and the schema's allOf requires revision_content_task
    # whenever consumer_status_task is present. Emitting `true` produced a
    # block that failed its own capabilities schema.
    from adcp.reporting.ledger import ReportingProducer
    from adcp.reporting.source import ReportingSourceExecutor
    from adcp.validation.schema_loader import get_named_validator

    producer = ReportingProducer(
        source=cast(ReportingSourceExecutor, object()),
        offerings=ProducerOfferings(snapshot_offering_id="HOURLY_SNAPSHOT_V1"),
        store=InMemoryReportingLedgerStore(),
        escalation=ReportingDeliveryEscalation(
            consumer_mismatch_escalation=timedelta(hours=4),
            operations_contact_url="https://support.seller.example/reporting",
        ),
    )
    block = producer.advertised_reporting_delivery(
        consumer_status_task=True,
        offerings=[_OFFERING],
        automated_recovery_window=timedelta(hours=6),
        status_retention_days=400,
    )
    assert block["consumer_status_task"] == "sync_reporting_status"
    assert block["revision_content_task"] == "get_media_buy_delivery"
    assert block["consumer_mismatch_escalation_seconds"] == 14400
    assert block["operations_contact"] == {"url": "https://support.seller.example/reporting"}

    validator = get_named_validator("core/reporting-delivery-capabilities.json")
    assert validator is not None
    assert [error.message for error in sorted(validator.iter_errors(block), key=str)] == []


def test_a_seller_without_the_loop_omits_consumer_status_task() -> None:
    from adcp.reporting.ledger import ReportingProducer
    from adcp.reporting.source import ReportingSourceExecutor
    from adcp.validation.schema_loader import get_named_validator

    producer = ReportingProducer(
        source=cast(ReportingSourceExecutor, object()),
        offerings=ProducerOfferings(snapshot_offering_id="HOURLY_SNAPSHOT_V1"),
        store=InMemoryReportingLedgerStore(),
    )
    block = producer.advertised_reporting_delivery(
        consumer_status_task=False,
        offerings=[_OFFERING],
        automated_recovery_window=timedelta(hours=6),
        status_retention_days=400,
    )
    assert "consumer_status_task" not in block
    validator = get_named_validator("core/reporting-delivery-capabilities.json")
    assert validator is not None
    assert [error.message for error in sorted(validator.iter_errors(block), key=str)] == []


def test_an_escalation_commitment_without_the_loop_is_unpublishable() -> None:
    from adcp.reporting.ledger import ReportingProducer
    from adcp.reporting.source import ReportingSourceExecutor

    producer = ReportingProducer(
        source=cast(ReportingSourceExecutor, object()),
        offerings=ProducerOfferings(snapshot_offering_id="HOURLY_SNAPSHOT_V1"),
        store=InMemoryReportingLedgerStore(),
        escalation=ReportingDeliveryEscalation(
            consumer_mismatch_escalation=timedelta(hours=4),
            operations_contact_email="ops@seller.example",
        ),
    )
    with pytest.raises(ValueError, match="requires consumer_status_task"):
        producer.advertised_reporting_delivery(
            consumer_status_task=False,
            offerings=[_OFFERING],
            automated_recovery_window=timedelta(hours=6),
            status_retention_days=400,
        )
