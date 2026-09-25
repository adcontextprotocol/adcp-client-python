"""One captured-generation clock for producer obligations and status forecasts.

A generation owes full periods starting at/after activation and strictly before
deactivation. Deactivation after a period starts keeps that entire period and
its original SLA. Forecasting never creates an obligation or changes health.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    _period_instants,
    _schedule_clock,
    derive_period,
    first_ordinal_after,
    iso_duration_to_timedelta,
)


def committed_periods(
    configuration: ReportingConfiguration,
    *,
    near: datetime | None = None,
    near_start: datetime | None = None,
) -> Iterator[ReportingPeriodBoundary]:
    """Yield full committed periods, optionally seeking near an expected-at time.

    ``near`` is an optimization, not a filter: the caller still compares exact
    instants. Two predecessor civil slots retain the period containing the
    requested instant, including its DST fold. No wall clock is consulted.
    ``near_start`` seeks around a period start independently of its SLA.
    """
    activated = configuration.activated_at
    if activated is None:
        return
    schedule, timezone = configuration.schedule, configuration.account_timezone
    zone, duration, anchor = _schedule_clock(schedule, timezone)
    ordinal = first_ordinal_after(schedule, account_timezone=timezone, activated_at=activated)
    seek = near_start
    if seek is None and near is not None:
        seek = near - iso_duration_to_timedelta(schedule.delivery_sla)
    if seek is not None:
        candidate = first_ordinal_after(
            schedule,
            account_timezone=timezone,
            activated_at=seek,
        )
        ordinal = max(ordinal, candidate - 2)
    while True:
        start, end = _period_instants(schedule, timezone, ordinal)
        if configuration.deactivated_at is not None and start >= configuration.deactivated_at:
            return
        local_start = anchor + duration * ordinal
        if end > start and start.astimezone(zone).replace(tzinfo=None) == local_start:
            yield derive_period(schedule, account_timezone=timezone, ordinal=ordinal)
        ordinal += 1


def next_reporting_expectation(
    configurations: Sequence[ReportingConfiguration],
    obligations: Sequence[ReportingObligationRecord],
    *,
    as_of: datetime,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> datetime | None:
    """Nearest future due instant in the selected frozen schedule/period scope.

    Existing obligations remain commitments even if their registry generation
    has aged out. They supplement, rather than replace, captured schedules.
    This value is independent of current health and of record pagination.
    """
    future = [
        item.period.expected_at
        for item in obligations
        if item.period.expected_at > as_of
        and (period_start is None or item.period.end > period_start)
        and (period_end is None or item.period.start < period_end)
    ]
    for configuration in configurations:
        # A requested historical horizon does not create an unbounded walk.
        # Seek close to the later of the SLA cutoff and its lower period edge.
        near = as_of
        if period_start is not None:
            near = max(
                near, period_start + iso_duration_to_timedelta(configuration.schedule.delivery_sla)
            )
        for period in committed_periods(configuration, near=near):
            if period_end is not None and period.start >= period_end:
                break
            if period.expected_at <= as_of or (
                period_start is not None and period.end <= period_start
            ):
                continue
            future.append(period.expected_at)
            break
    return min(future) if future else None


def next_reporting_period_start(
    configurations: Sequence[ReportingConfiguration], *, as_of: datetime
) -> datetime | None:
    """rc.6 complete-summary forecast outside the closed evaluated horizon.

    Only captured committed generations supply this forecast. A completed
    obligation's due time is not a period start, and a future forecast does
    not create, count or lease an obligation. The producer still uses the
    identical full-period activation/deactivation and civil-time boundaries.
    """
    future = []
    for configuration in configurations:
        for period in committed_periods(configuration, near_start=as_of):
            if period.start > as_of:
                future.append(period.start)
                break
    return min(future) if future else None
