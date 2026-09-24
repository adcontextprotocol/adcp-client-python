"""Captured generation forecasts agree with actual period creation (#1179)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import islice

import pytest

from adcp.reporting.ledger import ProducerOfferings, ReportingProducer, ReportingScheduleSpec
from adcp.reporting.ledger.schedule import committed_periods, next_reporting_expectation
from adcp.reporting.ledger.status import ReportingStatusCaller, ReportingStatusHandler
from adcp.types import GetReportingStatusResponse
from adcp.validation.schema_loader import get_validator

from ._generation_support import START, UncalledSource, configuration, revision_for
from ._reliable_support import reliable_factory


@pytest.fixture(
    params=[("memory", False), ("memory", True), ("postgres", False), ("postgres", True)]
)
async def schedules(request):
    backend, notifications = request.param
    async with reliable_factory(backend, notifications=notifications) as harness:
        yield harness


def hour(value):
    return START + timedelta(hours=value)


@pytest.mark.parametrize(
    "activation,deactivation,now,expected,closed",
    [
        (0, None, 0.5, 2, 0),  # mid-period, no obligation or coverage yet
        (0, None, 1, 2, 1),  # closes at 01:00, due at 02:00
        (0, None, 1.5, 2, 1),
        (0, None, 2, 3, 2),  # strictly future expectation at the exact SLA
        (0, None, 2.5, 3, 2),
        (3, None, 0.5, 5, 0),  # already committed future activation
        (0.5, None, 0.5, 3, 0),  # first full period only
        (1, None, 1, 3, 0),
        (0, 3, 0.5, 2, 0),  # future deactivation
        (0, 1, 2, None, 1),  # stop exactly at the next start
        (0, 0.5, 0.5, 2, 0),  # begun full period remains owed
        (0, 0.5, 2, None, 1),
        (1, 1, 0.5, None, 0),  # no committed active interval
        (None, None, 0.5, None, 0),
    ],
)
async def test_producer_and_public_summary_share_all_activation_and_due_boundaries(
    schedules, activation, deactivation, now, expected, closed
):
    h = schedules
    h.clock.now = hour(now)
    config = replace(
        configuration(),
        activated_at=hour(activation) if activation is not None else None,
        deactivated_at=hour(deactivation) if deactivation is not None else None,
    )
    await h.store.put_configuration(config)
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=h.store
    )
    obligations = await producer.close_elapsed_periods(config, now=h.clock())
    assert len(obligations) == closed
    production_operation_1 = await producer.close_elapsed_periods(config, now=h.clock())
    assert production_operation_1 == []
    # Completing all existing evidence must not erase tomorrow's commitment.
    for obligation in obligations:
        revision, rows = revision_for(obligation, suffix=obligation.reporting_obligation_id)
        await h.store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(h.store)
    for consumer in ("buyer-one", "buyer-two"):
        raw = await handler.handle({}, caller=ReportingStatusCaller("acct_a", consumer))
        GetReportingStatusResponse.model_validate(raw)
        validator = get_validator("get_reporting_status", "sync")
        assert validator is not None
        validator.validate(raw)
        assert raw["health"] == "complete"
        assert raw["obligation_counts"]["total"] == closed
        assert raw.get("next_expected_at") == (
            hour(expected).isoformat().replace("+00:00", "Z") if expected is not None else None
        )
        if not closed:
            assert raw["coverage"]["media_buy_ids"] == []
            assert raw["issues"] == []


async def test_nearest_generation_and_account_filters_use_captured_not_current_configuration(
    schedules,
):
    h = schedules
    h.clock.now = hour(0.5)
    first = replace(configuration(), deactivated_at=None)
    second = replace(
        first, delivery_config_version=2, schedule=replace(first.schedule, delivery_sla="PT10M")
    )
    foreign = replace(
        first, account_id="acct_b", schedule=replace(first.schedule, delivery_sla="PT0S")
    )
    await h.store.put_configuration(first)
    await h.store.put_configuration(second)
    await h.store.put_configuration(foreign)
    handler = ReportingStatusHandler(h.store)
    caller = ReportingStatusCaller("acct_a", "buyer")
    captured = await h.store.read_status_snapshot(account_id="acct_a")
    expected = hour(1) + timedelta(minutes=10)
    original = handler.render_snapshot({}, caller=caller, snapshot=captured)
    assert original["next_expected_at"] == expected.isoformat().replace("+00:00", "Z")
    await h.store.put_configuration(replace(second, deactivated_at=hour(0)))
    h.clock.now = hour(5)
    assert handler.render_snapshot({}, caller=caller, snapshot=captured) == original
    for filters in (
        {"delivery_config_ids": ["absent"]},
        {"feed_purposes": ["billing"]},
        {"media_buy_ids": ["foreign-buy"]},
        {"period": {"start": hour(-2).isoformat(), "end": hour(0).isoformat()}},
    ):
        assert "next_expected_at" not in handler.render_snapshot(
            filters, caller=caller, snapshot=captured
        )


@pytest.mark.parametrize(
    "date,hours", [("2026-03-08T05:00:00+00:00", 23), ("2026-11-01T04:00:00+00:00", 25)]
)
def test_civil_days_across_dst_preserve_the_captured_timezone_and_sla(date, hours):
    start = datetime.fromisoformat(date)
    config = replace(
        configuration(),
        activated_at=start,
        deactivated_at=start + timedelta(days=3),
        account_timezone="America/New_York",
        schedule=ReportingScheduleSpec("P1D", "PT1H", "account_timezone", period_anchor=start),
    )
    first = next(committed_periods(config))
    assert first.start == start
    assert first.end - first.start == timedelta(hours=hours)
    assert (
        next_reporting_expectation((config,), (), as_of=start + timedelta(hours=1))
        == first.expected_at
    )
    assert next_reporting_expectation((config,), (), as_of=first.expected_at) > first.expected_at


@pytest.mark.parametrize("duration", ["PT1H", "PT10M"])
def test_nonexistent_civil_slots_are_skipped_without_duplicate_or_reordered_periods(duration):
    start = datetime(2026, 3, 8, 5, tzinfo=timezone.utc)
    config = replace(
        configuration(),
        activated_at=start,
        deactivated_at=start + timedelta(hours=5),
        schedule=ReportingScheduleSpec(
            duration, "PT0S", "custom_timezone", "America/New_York", start
        ),
    )
    periods = list(committed_periods(config))
    assert all(a.end == b.start for a, b in zip(periods, periods[1:]))
    assert len({p.period_key for p in periods}) == len(periods)
    for period in periods:
        near = period.start + (period.end - period.start) / 2
        assert next_reporting_expectation((config,), (), as_of=near) == period.expected_at


def test_empty_scope_and_explicit_anchor_before_or_after_activation():
    assert next_reporting_expectation((), (), as_of=START) is None
    config = replace(
        configuration(),
        deactivated_at=None,
        schedule=replace(configuration().schedule, period_anchor=hour(10)),
    )
    assert [p.start for p in islice(committed_periods(config), 2)] == [START, hour(1)]
    offset = START.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert next_reporting_expectation((config,), (), as_of=offset) == hour(2)
