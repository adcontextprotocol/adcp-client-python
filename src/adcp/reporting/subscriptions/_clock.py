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
* Sub-daily local periods (adcp#8588): under ``account_timezone``,
  ``source_timezone`` or ``billing_cycle`` alignment, a duration of hours,
  minutes and seconds only MUST divide 24 hours, and its boundaries are the
  instants whose local wall-clock time is a whole multiple of the duration
  after local midnight, counted afresh each local day. A nonexistent wall time
  yields no boundary and a repeated one yields one boundary per offset, so a
  23-hour DST day has 23 hourly periods and a 25-hour day 25. Local durations
  cannot mix date and time components.

The ledger's ``ReportingScheduleSpec`` treats every component as civil time and
defaults an anchorless civil schedule to the UTC epoch in local time; this
calendar does neither.
"""

from __future__ import annotations

import calendar as _calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = timezone.utc
_DURATION = re.compile(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_SLA = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def utc_instant(value: datetime, *, field: str = "timestamp") -> datetime:
    """Return ``value`` in UTC, refusing naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@lru_cache(maxsize=1)
def _iana_zones() -> frozenset[str]:
    return frozenset(available_timezones())


def require_zone(name: str | None, *, field: str = "timezone") -> ZoneInfo:
    """A recognized IANA zone; numeric offsets, unknown names and keys that are
    not IANA zone names (``posixrules``, ``right/UTC``, ``posix/...``) are refused."""
    if not name or name[0] in "+-" or name[0].isdigit() or name not in _iana_zones():
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
    """A non-negative ``delivery_sla`` as exact elapsed time (days are 24 hours).

    A ``delivery_sla`` with years or months, which the offering schema allows,
    has no agreed arithmetic across SDKs and is refused.
    """
    match = _SLA.fullmatch(value)
    if match is None or value in {"P", "PT"} or value.endswith("T"):
        raise ValueError(
            f"delivery_sla {value!r} is not supported: use days, hours, minutes and "
            "seconds only (years and months have no agreed arithmetic)"
        )
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
    #: Local alignment (anything but ``utc``): sub-daily durations restart at
    #: each local midnight (adcp#8588).
    local: bool = False
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
        if self.local and exact and (years or months or days):
            raise ValueError(
                f"period_duration {self.period_duration!r} mixes date and time components, "
                "which a local alignment does not allow"
            )
        if self.local and exact and not (years or months or days) and 86400 % exact:
            raise ValueError(
                f"period_duration {self.period_duration!r} must divide 24 hours "
                "under a local alignment"
            )
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
        local: bool = False,
    ) -> PeriodCalendar:
        """``anchor`` is an instant (``billing_cycle``); otherwise local midnight 1970-01-01.

        ``local`` marks an ``account_timezone``, ``source_timezone`` or
        ``billing_cycle`` schedule.
        """
        zone = require_zone(zone_name)
        origin = (
            utc_instant(anchor, field="period_anchor").astimezone(zone).replace(tzinfo=None)
            if anchor is not None
            else datetime(1970, 1, 1)
        )
        return cls(
            period_duration, zone_name, origin, parse_delivery_sla(delivery_sla), local=local
        )

    @property
    def _subdaily_local(self) -> bool:
        years, months, days, exact = self._parts
        return self.local and bool(exact) and not (years or months or days)

    def _day_boundaries(self, day: date) -> list[datetime]:
        """Every boundary on one local date: wall-clock multiples after local midnight."""
        step = self._parts[3]
        found: set[datetime] = set()
        midnight = datetime.combine(day, time())
        for index in range(86400 // step):
            wall = midnight + timedelta(seconds=index * step)
            for fold in (0, 1):
                instant = wall.replace(tzinfo=self._zone, fold=fold).astimezone(UTC)
                if instant.astimezone(self._zone).replace(tzinfo=None) == wall:
                    found.add(instant)
        return sorted(found)

    def _boundaries_from(self, instant: datetime) -> Iterator[datetime]:
        """Sub-daily local boundaries at or after ``instant``, in order."""
        at = utc_instant(instant)
        day = at.astimezone(self._zone).date() - timedelta(days=1)
        while True:
            for boundary in self._day_boundaries(day):
                if boundary >= at:
                    yield boundary
            day += timedelta(days=1)

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
        if self._subdaily_local:
            return next(self._boundaries_from(instant))
        return self.boundary(self.first_ordinal_at_or_after(instant))

    def floor(self, instant: datetime) -> datetime:
        """The last boundary at or before ``instant``."""
        at = utc_instant(instant)
        if self._subdaily_local:
            day = at.astimezone(self._zone).date() + timedelta(days=1)
            while True:
                earlier = [item for item in self._day_boundaries(day) if item <= at]
                if earlier:
                    return earlier[-1]
                day -= timedelta(days=1)
        ordinal = self.first_ordinal_at_or_after(at)
        start = self.boundary(ordinal)
        return start if start == at else self.boundary(ordinal - 1)

    def periods(
        self, *, starts_from: datetime, starts_before: datetime
    ) -> Iterator[tuple[datetime, datetime]]:
        """Every nonempty period whose start lies in ``[starts_from, starts_before)``."""
        upper = utc_instant(starts_before)
        if self._subdaily_local:
            boundaries = self._boundaries_from(starts_from)
            start = next(boundaries)
            while start < upper:
                end = next(boundaries)
                yield start, end
                start = end
            return
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
