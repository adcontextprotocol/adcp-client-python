"""Period clock shared by subscription coverage on both sides of the wire.

A reporting subscription owes the complete periods of the resolved offering's
published schedule. The boundaries come from ``core/reporting-schedule.json``
``period_generation``: ``utc`` counts from 1970-01-01T00:00:00Z, and
``account_timezone`` and ``source_timezone`` count from *local* midnight on
1970-01-01 in their zone. ``billing_cycle`` counts from its explicit anchor in
its zone.

The ledger's :class:`~adcp.reporting.ledger.models.ReportingScheduleSpec`
defaults a civil-time schedule without an anchor to the UTC epoch expressed in
local time, which for ``America/New_York`` puts every daily boundary at 19:00
local. Production admission already refuses that phase. Subscriptions never
rely on that default: every civil-time schedule built here carries the
normative local-midnight anchor explicitly, so the seller's obligations and the
buyer's expectations use one derivation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adcp.reporting.ledger.models import (
    ReportingScheduleSpec,
    _period_instants,
    first_ordinal_after,
    iso_duration_to_timedelta,
)

UTC = timezone.utc


def utc_instant(value: datetime, *, field: str = "timestamp") -> datetime:
    """Return ``value`` in UTC, refusing naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def require_zone(name: str, *, field: str = "timezone") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"{field} {name!r} is not a recognized IANA timezone") from error


def local_midnight_origin(zone_name: str) -> datetime:
    """The normative origin of a civil-time schedule: 1970-01-01T00:00 local."""
    zone = require_zone(zone_name)
    return datetime(1970, 1, 1, tzinfo=zone).astimezone(UTC)


def civil_schedule(
    *,
    period_duration: str,
    delivery_sla: str,
    zone_name: str,
    anchor: datetime | None = None,
) -> ReportingScheduleSpec:
    """Build a ledger schedule with an explicit, normative origin.

    ``zone_name == "UTC"`` without an anchor is the ``utc`` alignment.
    """
    duration = iso_duration_to_timedelta(period_duration)
    if duration <= timedelta(0):
        raise ValueError("period_duration must be positive")
    iso_duration_to_timedelta(delivery_sla)
    if anchor is None and zone_name == "UTC":
        return ReportingScheduleSpec(
            period_duration=period_duration, delivery_sla=delivery_sla, alignment="utc"
        )
    require_zone(zone_name)
    return ReportingScheduleSpec(
        period_duration=period_duration,
        delivery_sla=delivery_sla,
        alignment="custom_timezone",
        period_timezone=zone_name,
        period_anchor=(
            utc_instant(anchor, field="period_anchor")
            if anchor is not None
            else local_midnight_origin(zone_name)
        ),
    )


def _zone_name(schedule: ReportingScheduleSpec) -> str:
    return schedule.timezone_name("UTC")


def period_bounds(schedule: ReportingScheduleSpec, ordinal: int) -> tuple[datetime, datetime]:
    return _period_instants(schedule, _zone_name(schedule), ordinal)


def first_ordinal_at_or_after(schedule: ReportingScheduleSpec, instant: datetime) -> int:
    return first_ordinal_after(
        schedule, account_timezone=_zone_name(schedule), activated_at=utc_instant(instant)
    )


def first_boundary_at_or_after(schedule: ReportingScheduleSpec, instant: datetime) -> datetime:
    """The cutover instant E for an outgoing schedule at change instant T.

    E is T when T falls on a boundary, and otherwise the end of the period
    containing T: the end of the last period that starts before T.
    """
    return period_bounds(schedule, first_ordinal_at_or_after(schedule, instant))[0]


def last_boundary_at_or_before(schedule: ReportingScheduleSpec, instant: datetime) -> datetime:
    """The start of the period containing ``instant``, or ``instant`` on a boundary."""
    at = utc_instant(instant)
    ordinal = first_ordinal_at_or_after(schedule, at)
    start = period_bounds(schedule, ordinal)[0]
    if start == at:
        return start
    return period_bounds(schedule, ordinal - 1)[0]


def iter_periods(
    schedule: ReportingScheduleSpec, *, starts_from: datetime, starts_before: datetime
) -> Iterator[tuple[datetime, datetime]]:
    """Yield every nonempty period whose start lies in ``[starts_from, starts_before)``.

    A spring-forward gap can collapse a civil slot to zero elapsed time; that
    slot is not a period. A nonexistent local boundary otherwise advances by
    the gap, as ``period_generation`` requires, so the period is kept.
    """
    upper = utc_instant(starts_before)
    ordinal = first_ordinal_at_or_after(schedule, starts_from)
    while True:
        start, end = period_bounds(schedule, ordinal)
        if start >= upper:
            return
        if end > start:
            yield start, end
        ordinal += 1


def delivery_sla(schedule: ReportingScheduleSpec) -> timedelta:
    return iso_duration_to_timedelta(schedule.delivery_sla)


def iso_z(value: datetime) -> str:
    """RFC 3339 UTC with a ``Z`` suffix and no fractional seconds when whole."""
    instant = utc_instant(value)
    if instant.microsecond:
        return instant.isoformat().replace("+00:00", "Z")
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "civil_schedule",
    "delivery_sla",
    "first_boundary_at_or_after",
    "iso_z",
    "iter_periods",
    "last_boundary_at_or_before",
    "local_midnight_origin",
    "period_bounds",
    "require_zone",
    "utc_instant",
]
