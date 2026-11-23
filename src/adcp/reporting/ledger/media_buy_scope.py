"""Period-close media-buy membership for ``scope: {all_media_buys: true}``.

A generation with ``media_buy_scope="all_media_buys"`` has no fixed buy list.
At each period close the producer asks the seller's
:data:`MediaBuyScopeResolver` which buys the period owes: every
caller-authorized buy on the account whose effective flight overlaps the
half-open period and that the seller knew of by ``period.end``. That frozen set
becomes the obligation's ``media_buy_ids`` and its coverage denominator, and is
never re-resolved.

``CompleteMediaBuyScope(())`` is a proven zero-buy period: the obligation
closes with an empty denominator and full coverage. When membership at the
cutoff cannot be proven, the resolver returns
:class:`MissingHistoricalMediaBuyScope`; the period stays open and is retried
on a later turn. An empty set is never a stand-in for "unknown".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from adcp.reporting.ledger.models import ReportingConfiguration, ReportingPeriodBoundary
from adcp.reporting.ledger.store import LedgerConflictError


@dataclass(frozen=True, slots=True)
class CompleteMediaBuyScope:
    """The complete buy set a period owes; ``()`` is a proven zero-buy period."""

    media_buy_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not str or not value for value in self.media_buy_ids):
            raise ValueError("media buy scope IDs must be nonempty strings")
        object.__setattr__(self, "media_buy_ids", tuple(sorted(set(self.media_buy_ids))))


@dataclass(frozen=True, slots=True)
class MissingHistoricalMediaBuyScope:
    """Membership at ``period.end`` cannot be proven; the period stays open and retries."""

    reason: str


MediaBuyScopeResolver = Callable[
    [ReportingConfiguration, ReportingPeriodBoundary],
    Awaitable[CompleteMediaBuyScope | MissingHistoricalMediaBuyScope],
]


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReportingScopeBuy:
    """One buy as the seller knew it at a period's cutoff.

    ``known_at`` is when the seller first knew the buy (confirmed or created).
    Its effective flight as of ``period.end`` is one or more half-open
    intervals, like the period: pass ``start_time``/``end_time`` for a single
    flight, or ``flights`` for package flights with gaps between them. With
    ``flights``, ``start_time``/``end_time`` are filled with their envelope.
    """

    media_buy_id: str
    known_at: datetime
    start_time: datetime | None = None
    end_time: datetime | None = None
    flights: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        if type(self.media_buy_id) is not str or not self.media_buy_id:
            raise ValueError("media_buy_id must be a nonempty string")
        _aware(self.known_at, "known_at")
        single = (self.start_time, self.end_time)
        if self.flights:
            if single != (None, None):
                raise ValueError("pass either start_time/end_time or flights, not both")
            flights = tuple((start, end) for start, end in self.flights)
        elif None in single:
            raise ValueError("a buy needs start_time and end_time, or flights")
        else:
            assert self.start_time is not None and self.end_time is not None
            flights = ((self.start_time, self.end_time),)
        for start, end in flights:
            if _aware(end, "flight end") < _aware(start, "flight start"):
                raise ValueError("a buy's flight cannot end before it starts")
        object.__setattr__(self, "flights", flights)
        object.__setattr__(self, "start_time", min(start for start, _ in flights))
        object.__setattr__(self, "end_time", max(end for _, end in flights))

    def overlaps(self, period: ReportingPeriodBoundary) -> bool:
        """Whether any flight interval overlaps the half-open period."""
        start, end = _aware(period.start, "period.start"), _aware(period.end, "period.end")
        return any(
            _aware(flight_start, "flight start") < end and _aware(flight_end, "flight end") > start
            for flight_start, flight_end in self.flights
        )


def freeze_all_media_buys(
    buys: Iterable[ReportingScopeBuy], period: ReportingPeriodBoundary
) -> CompleteMediaBuyScope:
    """Freeze ``all_media_buys`` membership for one period.

    A buy belongs to the period when the seller knew of it at the period's
    cutoff instant, ``known_at <= period.end`` (the spec's "known at that
    instant"), and any of its flight intervals overlaps the half-open
    ``[period.start, period.end)``. Pass only buys the configuration's caller
    is authorized to see on the configuration's account.
    """
    end = _aware(period.end, "period.end")
    return CompleteMediaBuyScope(
        tuple(
            buy.media_buy_id
            for buy in buys
            if _aware(buy.known_at, "known_at") <= end and buy.overlaps(period)
        )
    )


def reject_unsupported_media_buy_scope(
    configuration: ReportingConfiguration, *, resolver_wired: bool
) -> None:
    """Refuse an ``all_media_buys`` generation this seller cannot honour, before admission.

    Raises ``UNSUPPORTED_FEATURE`` when no :data:`MediaBuyScopeResolver` is
    wired (membership could never be frozen), and for ``required_finality``
    ``official``: such a generation can freeze a period to zero buys, and a
    zero-buy official close needs a seller attestation hook that does not
    exist yet, so that obligation could never be fulfilled.
    """
    if configuration.media_buy_scope != "all_media_buys":
        return
    if configuration.media_buy_ids:
        raise LedgerConflictError(
            "INVALID_REQUEST", "an all_media_buys generation has no fixed media_buy_ids"
        )
    if not resolver_wired:
        raise LedgerConflictError(
            "UNSUPPORTED_FEATURE", "all_media_buys reporting requires a media buy scope resolver"
        )
    if configuration.required_finality == "official":
        raise LedgerConflictError(
            "UNSUPPORTED_FEATURE",
            "all_media_buys reporting at official finality is unsupported: a zero-buy official "
            "close needs a seller attestation hook that does not exist yet",
        )


__all__ = [
    "CompleteMediaBuyScope",
    "MediaBuyScopeResolver",
    "MissingHistoricalMediaBuyScope",
    "ReportingScopeBuy",
    "freeze_all_media_buys",
    "reject_unsupported_media_buy_scope",
]
