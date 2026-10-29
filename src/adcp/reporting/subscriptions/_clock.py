"""The period calendar shared by subscription coverage on both sides of the wire.

``core/reporting-schedule.json`` ``period_generation``: every boundary is
calculated directly from the schedule's origin and the interval ordinal by
multiplying each ISO 8601 duration component by the ordinal and applying
years, months, days, hours, minutes, then seconds.

* Origins: ``utc`` counts from 1970-01-01T00:00:00Z; ``account_timezone`` and
  ``source_timezone`` from local midnight on 1970-01-01 in their zone;
  ``billing_cycle`` from its explicit ``period_anchor`` expressed in its zone.
* Years, months and days are local civil arithmetic in the zone. The day is
  clamped from the origin's day (a March 31 anchor gives February 28, then
  March 31 again). A nonexistent local time advances by the gap; an ambiguous
  one takes the earlier instant.
* Hours, minutes and seconds are exact elapsed time added after the civil part,
  so ``PT24H`` differs from ``P1D`` across a DST change. The spec does not say
  whether time components are civil; this matches the TypeScript SDK
  (adcontextprotocol/adcp-client#3505), so both SDKs derive identical periods.
* A zero-length period (a skipped civil date) is never a period.

The ledger's ``ReportingScheduleSpec`` treats every component as civil time and
defaults an anchorless civil schedule to the UTC epoch in local time; this
calendar does neither.
"""

from __future__ import annotations

import calendar as _calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
_DURATION = re.compile(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_SLA = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def utc_instant(value: datetime, *, field: str = "timestamp") -> datetime:
    """Return ``value`` in UTC, refusing naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def require_zone(name: str | None, *, field: str = "timezone") -> ZoneInfo:
    """A recognized IANA zone; numeric offsets and unknown names are refused."""
    if not name or name[0] in "+-" or name[0].isdigit():
        raise ValueError(f"{field} {name!r} is not a recognized IANA timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"{field} {name!r} is not a recognized IANA timezone") from error


def parse_period_duration(value: str) -> tuple[int, int, int, int]:
    """``(years, months, days, seconds)`` of a strictly positive duration."""
    match = _DURATION.fullmatch(value)
    if match is None or value in {"P", "PT"} or value.endswith("T"):
        raise ValueError(f"{value!r} is not an ISO 8601 period duration")
    years, months, days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    exact = hours * 3600 + minutes * 60 + seconds
    if not (years or months or days or exact):
        raise ValueError("period_duration must be positive")
    return years, months, days, exact


def parse_delivery_sla(value: str) -> timedelta:
    """A non-negative ``delivery_sla`` as exact elapsed time (days are 24 hours)."""
    match = _SLA.fullmatch(value)
    if match is None or value in {"P", "PT"} or value.endswith("T"):
        raise ValueError(f"{value!r} is not a supported delivery_sla")
    days, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _resolve(local: datetime, zone: ZoneInfo) -> datetime:
    """Local civil time to an instant: a gap advances, a fold takes the earlier."""
    return local.replace(tzinfo=zone, fold=0).astimezone(UTC)


def _add_months(local: datetime, months: int, origin_day: int) -> datetime:
    index = local.year * 12 + (local.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(origin_day, _calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class PeriodCalendar:
    """Ordered half-open periods of one schedule in one zone."""

    period_duration: str
    zone_name: str
    origin: datetime  # naive local civil time
    delivery_sla: timedelta = timedelta(0)
    _parts: tuple[int, int, int, int] = field(init=False, repr=False, compare=False)
    _zone: ZoneInfo = field(init=False, repr=False, compare=False)
    _approx: float = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parts = parse_period_duration(self.period_duration)
        object.__setattr__(self, "_parts", parts)
        object.__setattr__(self, "_zone", require_zone(self.zone_name))
        if self.origin.tzinfo is not None:
            raise ValueError("origin is local civil time and must be naive")
        years, months, days, exact = parts
        approx = years * 365.2425 * 86400 + months * 30.436875 * 86400 + days * 86400 + exact
        object.__setattr__(self, "_approx", approx)

    @classmethod
    def for_schedule(
        cls,
        *,
        period_duration: str,
        delivery_sla: str,
        zone_name: str,
        anchor: datetime | None = None,
    ) -> PeriodCalendar:
        """``anchor`` is an instant (``billing_cycle``); otherwise local midnight 1970-01-01."""
        zone = require_zone(zone_name)
        origin = (
            utc_instant(anchor, field="period_anchor").astimezone(zone).replace(tzinfo=None)
            if anchor is not None
            else datetime(1970, 1, 1)
        )
        return cls(period_duration, zone_name, origin, parse_delivery_sla(delivery_sla))

    def boundary(self, ordinal: int) -> datetime:
        years, months, days, exact = self._parts
        local = self.origin
        if years or months:
            local = _add_months(local, (years * 12 + months) * ordinal, self.origin.day)
        if days:
            local = local + timedelta(days=days * ordinal)
        return _resolve(local, self._zone) + timedelta(seconds=exact * ordinal)

    def bounds(self, ordinal: int) -> tuple[datetime, datetime]:
        return self.boundary(ordinal), self.boundary(ordinal + 1)

    def first_ordinal_at_or_after(self, instant: datetime) -> int:
        at = utc_instant(instant)
        origin = self.boundary(0)
        ordinal = int((at - origin).total_seconds() // self._approx)
        while self.boundary(ordinal) < at:
            ordinal += 1
        while self.boundary(ordinal - 1) >= at:
            ordinal -= 1
        return ordinal

    def ceil(self, instant: datetime) -> datetime:
        """The first boundary at or after ``instant`` (E for a cutover at it)."""
        return self.boundary(self.first_ordinal_at_or_after(instant))

    def floor(self, instant: datetime) -> datetime:
        """The last boundary at or before ``instant``."""
        at = utc_instant(instant)
        ordinal = self.first_ordinal_at_or_after(at)
        start = self.boundary(ordinal)
        return start if start == at else self.boundary(ordinal - 1)

    def periods(
        self, *, starts_from: datetime, starts_before: datetime
    ) -> Iterator[tuple[datetime, datetime]]:
        """Every nonempty period whose start lies in ``[starts_from, starts_before)``."""
        upper = utc_instant(starts_before)
        ordinal = self.first_ordinal_at_or_after(starts_from)
        start = self.boundary(ordinal)
        while start < upper:
            end = self.boundary(ordinal + 1)
            if end > start:
                yield start, end
            ordinal += 1
            start = end


def iso_z(value: datetime) -> str:
    """RFC 3339 UTC with a ``Z`` suffix and no fractional seconds when whole."""
    instant = utc_instant(value)
    if instant.microsecond:
        return instant.isoformat().replace("+00:00", "Z")
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "PeriodCalendar",
    "iso_z",
    "parse_delivery_sla",
    "parse_period_duration",
    "require_zone",
    "utc_instant",
]
