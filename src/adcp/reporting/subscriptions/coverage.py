"""Which reporting periods a subscription owes one account: the buyer's rule.

This is the pure, plain-data form of the coverage rule in
``core/reporting-subscription.json`` (``x-adcp-validation.coverage`` and
``cutover``). A buyer runs it from its own records; the seller runtime runs the
same function over the same facts to decide which obligations it owes, so the
two sides agree period by period by construction.

For each covered buy and resolved offering, the owed periods are the complete
periods of the offering's published schedule that

1. start on or after the first boundary at or after the later of the
   subscription version's ``accepted_at`` and the account's ``authorized_at``,
   and end after the buy's ``confirmed_at`` (adcp#8593: the confirmation
   period is owed in full); and
2. overlap the buy's flight from ``start_time`` to its stop time, the earliest
   of ``end_time``, the cancellation's delivery stop (``cancellation_effective_at``,
   or ``cancellation.canceled_at`` when it is absent) and ``completed_at``, with
   the flight taken as of the period's ``scope_resolved_at`` (its end) from the
   buy's ``flight_history``.

Periods are never clipped: the period containing the stop time is owed in
full. Every change that hands periods from an outgoing schedule to an incoming
one at an instant T is a cutover: the outgoing schedule owes its periods that
start before T in full, E is the end of the last of them, and the incoming
schedule owes its periods that start at or after E. Version changes,
deactivation, resolution changes and account-timezone changes all use it.
When the incoming offering is a billing feed and E is not one of its
boundaries, the stretch from E to its first boundary is owed as one stub
period (``stub``), so no billable time goes unreported (adcp#8617).

Exceptions only remove periods: each ``excluded[]`` entry and each ``gaps[]``
entry removes the periods whose start falls in ``[from, until)``. Lost
authority removes periods the same way unless ``backfill_on_regrant`` keeps
them.

Rules settled by adcp#8585 (identical in the TypeScript SDK)
    Time is split per buy at every version ``accepted_at`` / ``superseded_at``,
    deactivation and reactivation instant, offering ``available_from`` /
    ``withdrawn_at`` and account timezone cutover. Each stretch with an active,
    in-scope version and an unchanged resolution (the union over the buy's
    packages) is a regime. An offering continuing into the next regime hands
    off at E of its own outgoing calendar; a newly resolved offering starts at
    the latest E across everything the previous regime resolved; after a regime
    that resolved nothing, or no active version, the ordinary start rule applies.
    A re-grant before ``unauthorized_at + status_retention_days`` continues the
    authority chain; a later one starts a new chain. Reactivation is a fresh
    start (``inactive_intervals``), and ``gaps[]`` and ``timezone_cutovers[]``
    entries with an ``offering_id`` apply to that offering only.

Rules settled for 3.3.0-beta.3 (adcp#8617, #8618, #8619, #8622)
    Resolution is per package and sticky: a package resolves when its (buy,
    version) pair first does, at the later of the version's ``accepted_at``
    and the buy's ``confirmed_at``, among the offerings available then, and
    keeps that result until a resolved offering is withdrawn, when it
    re-resolves among the offerings available at ``withdrawn_at``. A newly
    available offering never re-resolves it. Reactivation resumes at the first
    boundary at or after the later of the reactivation instant and the end of
    the last period owed before the deactivation, so a change made while
    inactive never owes a period twice.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from adcp.reporting._reconcile import ExpectedReportingPeriod
from adcp.reporting.subscriptions._clock import (
    CoverageInputError,
    PeriodCalendar,
    iso_z,
    require_zone,
    utc_instant,
)
from adcp.reporting.subscriptions.resolution import (
    SubscriptionOffering,
    SubscriptionSelector,
    _text,
    _value,
    resolve_reporting_subscription_package,
)

#: Exclusion reasons that remove periods. ``package_ids`` entries never do.
_REASONS = frozenset(
    {"offering_unsupported", "capacity", "overridden", "excluded_by_buyer", "action_required"}
)


def _instant(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        from adcp.reporting._timestamp import aware_timestamp

        # Wire instants carry 1-6 fractional digits, which Python 3.10's ISO
        # parser refuses.
        try:
            value = aware_timestamp(value)
        except ValueError as error:
            raise CoverageInputError(
                "invalid_instant", f"{field} {value!r} is not an RFC 3339 instant"
            ) from error
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime or an RFC 3339 string")
    return utc_instant(value, field=field)


def _require_instant(value: Any, *, field: str) -> datetime:
    instant = _instant(value, field=field)
    if instant is None:
        raise CoverageInputError("invalid_input", f"{field} is required")
    return instant


# -- plain-data inputs ------------------------------------------------------


#: Owed (buy, period) pairs one coverage call may return before failing closed.
DEFAULT_MAX_PERIODS = 100_000
#: Period boundaries one coverage call may examine before failing closed.
DEFAULT_MAX_ITERATIONS = 2_000_000


@dataclass(frozen=True)
class SubscriptionBuyFlight:
    """A flight a buy had before a later change superseded it at ``superseded_at``.

    Periods whose ``scope_resolved_at`` (end) is before ``superseded_at`` were
    resolved under this flight, so an extension or reinstatement never makes
    an elapsed period owed after the fact. Build it from a ``get_media_buys``
    ``flight_history[]`` entry with :meth:`from_flight_history`.
    """

    superseded_at: datetime
    start_time: datetime
    end_time: datetime
    canceled_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("superseded_at", "start_time", "end_time", "canceled_at", "completed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_instant(value, field=name))

    @property
    def stop_time(self) -> datetime:
        return _stop(self.end_time, self.canceled_at, self.completed_at)

    @classmethod
    def from_flight_history(cls, entry: Any) -> SubscriptionBuyFlight:
        """One ``core/media-buy.json`` ``flight_history[]`` entry (adcp#8618).

        The entry records a retroactive flight edit at ``recorded_at`` with the
        ``start_time`` and ``stop_time`` in force before it.
        """
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump(mode="json", exclude_none=True)
        return cls(
            superseded_at=_require_instant(
                _value(entry, "recorded_at"), field="flight_history.recorded_at"
            ),
            start_time=_require_instant(
                _value(entry, "start_time"), field="flight_history.start_time"
            ),
            end_time=_require_instant(_value(entry, "stop_time"), field="flight_history.stop_time"),
        )


def _stop(
    end_time: datetime, canceled_at: datetime | None, completed_at: datetime | None
) -> datetime:
    return min(item for item in (end_time, canceled_at, completed_at) if item is not None)


@dataclass(frozen=True)
class SubscriptionBuyPackage:
    """One package of a buy and its product's ``reporting_delivery_offering_ids``."""

    package_id: str
    reporting_delivery_offering_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reporting_delivery_offering_ids",
            tuple(self.reporting_delivery_offering_ids),
        )


@dataclass(frozen=True)
class SubscriptionBuy:
    """One media buy the caller can see through ``get_media_buys``, as plain data.

    The top-level flight is the buy's current one. ``superseded_flights``
    records earlier flights and when a change replaced each (the buy's
    ``flight_history``). ``cancellation_effective_at`` is when delivery stops
    under a cancellation (adcp#8617); when present it, not ``canceled_at``,
    is the cancellation's term in the stop time. Each package carries the
    offering set fixed at confirmation (``packages[].reporting_delivery_offering_ids``,
    adcp#8618). Without ``packages``, ``reporting_delivery_offering_ids`` is the
    buy's products' offering ids, resolved as one product. Build it from a
    ``get_media_buys`` media buy with :meth:`from_media_buy`.
    """

    media_buy_id: str
    confirmed_at: datetime | None
    start_time: datetime
    end_time: datetime
    canceled_at: datetime | None = None
    completed_at: datetime | None = None
    packages: tuple[SubscriptionBuyPackage, ...] = ()
    reporting_delivery_offering_ids: tuple[str, ...] = ()
    superseded_flights: tuple[SubscriptionBuyFlight, ...] = ()
    cancellation_effective_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.media_buy_id:
            raise CoverageInputError("invalid_input", "media_buy_id is required")
        for name in (
            "confirmed_at",
            "start_time",
            "end_time",
            "canceled_at",
            "completed_at",
            "cancellation_effective_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_instant(value, field=name))
        object.__setattr__(self, "packages", tuple(self.packages))
        object.__setattr__(
            self, "reporting_delivery_offering_ids", tuple(self.reporting_delivery_offering_ids)
        )
        flights = tuple(sorted(self.superseded_flights, key=lambda item: item.superseded_at))
        object.__setattr__(self, "superseded_flights", flights)

    @property
    def stop_time(self) -> datetime:
        delivery_stop = (
            self.cancellation_effective_at
            if self.cancellation_effective_at is not None
            else self.canceled_at
        )
        return _stop(self.end_time, delivery_stop, self.completed_at)

    @classmethod
    def from_media_buy(
        cls,
        media_buy: Any,
        *,
        package_offering_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> SubscriptionBuy:
        """Project a ``get_media_buys`` media buy (model or wire mapping).

        Reads ``confirmed_at``, the flight, ``cancellation.canceled_at``,
        ``cancellation_effective_at``, ``completed_at``, ``flight_history[]``
        and each package's ``reporting_delivery_offering_ids``.
        ``package_offering_ids`` supplies a package's offering set when the
        seller's response does not carry it.
        """
        if hasattr(media_buy, "model_dump"):
            media_buy = media_buy.model_dump(mode="json", exclude_none=True)
        if not isinstance(media_buy, Mapping):
            raise TypeError("media_buy must be a MediaBuy or a mapping")
        overrides = {str(key): tuple(value) for key, value in (package_offering_ids or {}).items()}
        packages = []
        for package in _value(media_buy, "packages") or ():
            package_id = _text(_value(package, "package_id", ""))
            offering_ids = _value(package, "reporting_delivery_offering_ids")
            if offering_ids is None:
                offering_ids = overrides.get(package_id, ())
            packages.append(
                SubscriptionBuyPackage(package_id, tuple(_text(item) for item in offering_ids))
            )
        cancellation = _value(media_buy, "cancellation") or {}
        return cls(
            media_buy_id=_text(_value(media_buy, "media_buy_id", "")),
            confirmed_at=_instant(_value(media_buy, "confirmed_at"), field="confirmed_at"),
            start_time=_require_instant(_value(media_buy, "start_time"), field="start_time"),
            end_time=_require_instant(_value(media_buy, "end_time"), field="end_time"),
            canceled_at=_instant(_value(cancellation, "canceled_at"), field="canceled_at"),
            cancellation_effective_at=_instant(
                _value(media_buy, "cancellation_effective_at"), field="cancellation_effective_at"
            ),
            completed_at=_instant(_value(media_buy, "completed_at"), field="completed_at"),
            packages=tuple(packages),
            superseded_flights=tuple(
                SubscriptionBuyFlight.from_flight_history(entry)
                for entry in _value(media_buy, "flight_history") or ()
            ),
        )

    def flight_at(self, resolved_at: datetime) -> tuple[datetime, datetime]:
        """``(start_time, stop_time)`` as of a period's ``scope_resolved_at``.

        A change effective exactly at ``resolved_at`` is visible.
        """
        for flight in self.superseded_flights:
            if resolved_at < flight.superseded_at:
                return flight.start_time, flight.stop_time
        return self.start_time, self.stop_time

    def product_offering_ids(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if self.packages:
            return tuple(
                (package.package_id, package.reporting_delivery_offering_ids)
                for package in self.packages
            )
        return (("", self.reporting_delivery_offering_ids),)

    def _earliest_start(self) -> datetime:
        return min((self.start_time, *(flight.start_time for flight in self.superseded_flights)))

    def _latest_stop(self) -> datetime:
        return max((self.stop_time, *(flight.stop_time for flight in self.superseded_flights)))


@dataclass(frozen=True)
class SubscriptionAuthorization:
    """One grant of account authority to the caller, ``[authorized_at, unauthorized_at)``.

    Pass the account's grant history, oldest first. A loss followed by a
    re-grant within ``status_retention_days`` continues coverage, with a gap
    unless the seller backfills; a later re-grant is a fresh coverage start.
    A grant whose ``authorized_at`` is at or before the previous loss, such as
    one listed with the chain-start ``authorized_at`` that ``list_accounts``
    keeps after a re-grant inside retention (adcp#8585), continues the chain
    at an unknown instant: the seller's ``gaps[]`` say which periods it removed.
    """

    authorized_at: datetime
    unauthorized_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "authorized_at", utc_instant(self.authorized_at, field="authorized_at")
        )
        if self.unauthorized_at is not None:
            object.__setattr__(
                self,
                "unauthorized_at",
                utc_instant(self.unauthorized_at, field="unauthorized_at"),
            )
            if self.unauthorized_at <= self.authorized_at:
                raise CoverageInputError(
                    "invalid_input", "unauthorized_at must be after authorized_at"
                )


@dataclass(frozen=True)
class AccountTimezoneChange:
    """An account timezone change at ``changed_at`` (the instant T of the cutover).

    ``offering_id`` limits it to one offering, as a ``timezone_cutovers[]``
    entry with ``offering_id`` does; ``previous_timezone`` is the zone before
    the change, which every seller entry carries and the coverage rule
    requires.
    """

    changed_at: datetime
    timezone: str
    offering_id: str | None = None
    previous_timezone: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed_at", utc_instant(self.changed_at, field="changed_at"))


@dataclass(frozen=True)
class SubscriptionException:
    """One period-removing exception: periods starting in ``[from_, until)``.

    ``media_buy_id`` scopes it to one buy; otherwise it applies to the account.
    """

    reason: str
    from_: datetime
    until: datetime | None = None
    media_buy_id: str | None = None
    #: An entry for one offering's boundaries (``excluded[]`` or ``gaps[]``
    #: ``offering_id``); ``None`` is every offering.
    offering_id: str | None = None
    #: A package-level entry (``package_ids``, with ``media_buy_id``): it removes
    #: those packages from the buy's coverage while open, never periods. An
    #: ``overridden`` entry names the packages a lower ``subscription_id`` owes.
    package_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_", utc_instant(self.from_, field="from"))
        if self.until is not None:
            object.__setattr__(self, "until", utc_instant(self.until, field="until"))
        object.__setattr__(self, "package_ids", tuple(self.package_ids))

    def removes_package(self, at: datetime, media_buy_id: str, package_id: str) -> bool:
        """Whether this package-level entry takes the package out of coverage at ``at``."""
        return (
            bool(self.package_ids)
            and package_id in self.package_ids
            and (self.media_buy_id is None or self.media_buy_id == media_buy_id)
            and self.from_ <= at
            and (self.until is None or at < self.until)
        )

    def removes(self, start: datetime, media_buy_id: str, offering_id: str | None = None) -> bool:
        if self.package_ids:
            return False  # removes packages from coverage, not periods
        if self.media_buy_id is not None and self.media_buy_id != media_buy_id:
            return False
        if self.offering_id is not None and offering_id is not None:
            if self.offering_id != offering_id:
                return False
        return self.from_ <= start and (self.until is None or start < self.until)


def subscription_exceptions(
    excluded: Iterable[Any] = (), gaps: Iterable[Any] = ()
) -> tuple[SubscriptionException, ...]:
    """Normalize ``subscription_coverage`` ``excluded[]`` and ``gaps[]`` entries.

    An entry naming ``package_ids`` (``offering_unsupported``, or
    ``overridden`` by a lower ``subscription_id`` resolving the same offering,
    adcp#8619) removes those packages from the buy's coverage while it is
    open, not periods.
    """
    result: list[SubscriptionException] = []
    for entry in excluded:
        if isinstance(entry, SubscriptionException):
            result.append(entry)
            continue
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump(mode="json", exclude_none=True, by_alias=True)
        reason = _text(_value(entry, "reason"))
        if reason not in _REASONS:
            raise CoverageInputError(
                "invalid_input", f"unknown subscription exclusion reason {reason!r}"
            )
        media_buy_id = _value(entry, "media_buy_id")
        offering_id = _value(entry, "offering_id")
        result.append(
            SubscriptionException(
                reason=reason,
                from_=_require_instant(_value(entry, "from", _value(entry, "from_")), field="from"),
                until=_instant(_value(entry, "until"), field="until"),
                media_buy_id=None if media_buy_id is None else _text(media_buy_id),
                # An offering-scoped entry (for example capacity under match
                # "all" across offerings with different boundaries) removes
                # only that offering's periods.
                offering_id=None if offering_id is None else _text(offering_id),
                package_ids=tuple(_text(item) for item in _value(entry, "package_ids") or ()),
            )
        )
    for gap in gaps:
        if hasattr(gap, "model_dump"):
            gap = gap.model_dump(mode="json", exclude_none=True, by_alias=True)
        offering_id = _value(gap, "offering_id")
        result.append(
            SubscriptionException(
                reason="unauthorized",
                from_=_require_instant(_value(gap, "from", _value(gap, "from_")), field="from"),
                until=_require_instant(_value(gap, "until"), field="until"),
                offering_id=None if offering_id is None else _text(offering_id),
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class SubscriptionVersionWindow:
    """One subscription version and the instants it governs: ``[accepted_at, ends_at)``.

    ``ends_at`` is the next version's ``accepted_at`` (``superseded_at``) or the
    ``deactivated_at`` of the last version; ``None`` while it is current.
    """

    subscription_version: int
    accepted_at: datetime
    ends_at: datetime | None
    configuration: Any
    #: ``(from, until)`` stretches during which the version was inactive in
    #: place (``active: false`` then ``true`` without a new version); ``until``
    #: ``None`` is still inactive. Reactivation is a fresh start at ``until``.
    inactive_intervals: tuple[tuple[datetime, datetime | None], ...] = ()
    #: The pattern and transport of the destination this version names.
    destination: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_at", utc_instant(self.accepted_at, field="accepted_at"))
        if self.ends_at is not None:
            object.__setattr__(self, "ends_at", utc_instant(self.ends_at, field="ends_at"))
            if self.ends_at < self.accepted_at:
                raise CoverageInputError(
                    "invalid_input", "a version cannot end before it was accepted"
                )
        intervals = []
        for start, until in self.inactive_intervals:
            start = utc_instant(start, field="inactive_intervals.from")
            if until is not None:
                until = utc_instant(until, field="inactive_intervals.until")
                if until <= start:
                    raise CoverageInputError(
                        "invalid_input", "an inactive interval must end after it starts"
                    )
            intervals.append((start, until))
        object.__setattr__(self, "inactive_intervals", tuple(intervals))

    def active_at(self, instant: datetime) -> bool:
        if not (self.accepted_at <= instant and (self.ends_at is None or instant < self.ends_at)):
            return False
        return not any(
            start <= instant and (until is None or instant < until)
            for start, until in self.inactive_intervals
        )

    def contains(self, instant: datetime) -> bool:
        return self.active_at(instant)


def subscription_version_windows(
    subscription: Any,
    *,
    version_configurations: Mapping[int, Any] | None = None,
    require_prior_configurations: bool = False,
) -> tuple[SubscriptionVersionWindow, ...]:
    """Derive version windows from a ``core/reporting-subscription-state.json`` readback.

    Each prior version's configuration comes from ``version_configurations``
    when given, else from the readback's ``prior_versions[].configuration``
    (adcp#8588). When a readback omits it, the current configuration is
    assumed, unless ``require_prior_configurations`` makes the gap an error.
    A version accepted while the subscription was already inactive has an
    empty window.
    """
    if hasattr(subscription, "model_dump"):
        subscription = subscription.model_dump(mode="json", exclude_none=True)
    configurations = dict(version_configurations or {})
    current_version = int(_value(subscription, "subscription_version"))
    current_configuration = _value(subscription, "configuration")
    if current_configuration is None:
        raise CoverageInputError(
            "missing_version_configuration", "subscription state has no configuration"
        )
    state = _text(_value(subscription, "state", "active"))
    deactivated_at = _instant(_value(subscription, "deactivated_at"), field="deactivated_at")
    if state == "inactive" and deactivated_at is None:
        raise CoverageInputError(
            "invalid_input", "an inactive subscription must carry deactivated_at"
        )
    accepted_at = _require_instant(_value(subscription, "accepted_at"), field="accepted_at")
    windows = [
        SubscriptionVersionWindow(
            subscription_version=current_version,
            accepted_at=accepted_at,
            ends_at=(
                max(accepted_at, deactivated_at)
                if state == "inactive" and deactivated_at is not None
                else None
            ),
            configuration=configurations.get(current_version, current_configuration),
        )
    ]
    for prior in _value(subscription, "prior_versions", ()) or ():
        version = int(_value(prior, "subscription_version"))
        configuration = configurations.get(version, _value(prior, "configuration"))
        if configuration is None:
            if require_prior_configurations:
                raise CoverageInputError(
                    "missing_version_configuration",
                    f"no configuration is known for prior subscription_version {version}",
                )
            configuration = current_configuration
        windows.append(
            SubscriptionVersionWindow(
                subscription_version=version,
                accepted_at=_require_instant(_value(prior, "accepted_at"), field="accepted_at"),
                ends_at=_require_instant(_value(prior, "superseded_at"), field="superseded_at"),
                configuration=configuration,
            )
        )
    inactive = tuple(
        (
            _require_instant(_value(item, "deactivated_at"), field="deactivated_at"),
            _instant(_value(item, "reactivated_at"), field="reactivated_at"),
        )
        for item in _value(subscription, "inactive_intervals", ()) or ()
    )
    if inactive:
        # The readback lists them per subscription; they apply to whichever
        # version was current, so every window carries them.
        windows = [replace(window, inactive_intervals=inactive) for window in windows]
    windows.sort(key=lambda item: (item.accepted_at, item.subscription_version))
    for earlier, later in zip(windows, windows[1:]):
        if earlier.subscription_version >= later.subscription_version:
            raise CoverageInputError(
                "invalid_input", "subscription versions must increase with accepted_at"
            )
        if earlier.ends_at != later.accepted_at:
            raise CoverageInputError(
                "invalid_input", "superseded_at must equal the next version's accepted_at"
            )
    return tuple(windows)


def subscription_covers_account(
    configuration: Any,
    *,
    account_id: str,
    account_operator: str | None = None,
    membership_intervals: bool = False,
) -> bool:
    """Whether a subscription version's scope selects the account.

    ``exclude_account_ids`` and ``operator_domains`` narrow the scope; the
    seller also reports an excluded account as ``excluded_by_buyer``. With
    ``operator_domains``, an account whose operator is unknown is not selected.

    ``membership_intervals``: the account's ``excluded_by_buyer`` entries
    record its membership edits (adcp#8619), which apply in place within a
    version, so ``account_ids`` and ``exclude_account_ids`` do not decide it;
    the entries remove the periods outside its membership.
    """
    if hasattr(configuration, "model_dump"):
        configuration = configuration.model_dump(mode="json", exclude_none=True)
    scope = _value(configuration, "scope") or {}
    account_ids = _value(scope, "account_ids")
    if account_ids is not None:
        if not membership_intervals and account_id not in {_text(item) for item in account_ids}:
            return False
    elif _value(scope, "all_authorized_accounts") is not True:
        return False
    excluded = _value(configuration, "exclude_account_ids") or ()
    if not membership_intervals and account_id in {_text(item) for item in excluded}:
        return False
    domains = _value(configuration, "operator_domains")
    if domains:
        return account_operator is not None and account_operator in {
            _text(item) for item in domains
        }
    return True


# -- output -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class OwedSubscriptionPeriod(ExpectedReportingPeriod):
    """One owed period of one (subscription version, resolved offering).

    An :class:`~adcp.reporting.ExpectedReportingPeriod`, so it feeds
    :func:`~adcp.reporting.evaluate_reporting_ledger` and the consumer-status
    planner unchanged. ``delivery_config_id`` is the ``subscription_id`` and
    ``delivery_config_version`` the ``subscription_version``. ``media_buy_ids``
    are the buys owed this period under this offering, the denominator the
    seller freezes at ``scope_resolved_at``.

    ``withheld`` marks a period owed before a loss of authority that is not
    due while authority is absent; its ``expected_at`` is ``None``. After a
    re-grant it is due at the re-grant time (``authorization.regranted_at``)
    plus the offering's ``delivery_sla``, never earlier than its original
    ``expected_at``.

    ``stub`` marks a billing cutover stub (``core/reporting-obligation.json``
    ``stub``, adcp#8617): the stretch from a cutover's E to the incoming billing
    schedule's first boundary, owed as one period.
    """

    account_id: str
    subscription_id: str
    offering_id: str
    withheld: bool = False
    stub: bool = False


# -- the rule ----------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    offering: SubscriptionOffering
    calendar: PeriodCalendar

    @property
    def key(self) -> tuple[str, str, datetime]:
        return (self.offering.offering_id, self.calendar.zone_name, self.calendar.origin)


@dataclass
class _Regime:
    start: datetime
    until: datetime | None
    version: int
    resolved: tuple[_Resolved, ...]
    #: Per offering: the instant from which this regime owes periods (E, or
    #: the regime start). Carried so a later cutover inside the same outgoing
    #: period never owes a period twice.
    handoffs: dict[str, datetime] = field(default_factory=dict)
    #: Whether this regime took over from one that resolved offerings: a
    #: cutover, after which a billing offering owes a stub (adcp#8617).
    cutover: bool = False


@dataclass
class _Chain:
    """Continuous authority: a first grant, retained interruptions, an optional end."""

    start: datetime
    #: (lost, regranted); regranted is None when only the chain-start
    #: authorized_at says authority returned (the re-grant instant is unknown).
    interruptions: list[tuple[datetime, datetime | None]] = field(default_factory=list)
    end: datetime | None = None
    #: The re-grant that started this chain after an expired entry, if any.
    after_expiry_of: datetime | None = None


@dataclass
class _Owed:
    version: int
    offering: SubscriptionOffering
    zone: str
    start: datetime
    end: datetime
    expected_at: datetime | None
    withheld: bool
    stub: bool = False
    media_buy_ids: set[str] = field(default_factory=set)


def authorization_chain_start(
    authorizations: Sequence[SubscriptionAuthorization], *, status_retention_days: int
) -> datetime | None:
    """The ``authorization.authorized_at`` a seller serves for a grant history.

    The start of the current authority chain: a re-grant before
    ``unauthorized_at + status_retention_days`` continues the chain, so the
    value never moves on such a re-grant; a later re-grant starts a new chain.
    ``None`` while the caller holds no grant. The same rule coverage applies.
    """
    if not authorizations or authorizations[-1].unauthorized_at is not None:
        return None
    chains = _chains(authorizations, timedelta(days=status_retention_days))
    return chains[-1].start


def authorization_regranted_at(
    authorizations: Sequence[SubscriptionAuthorization], *, status_retention_days: int
) -> datetime | None:
    """The ``authorization.regranted_at`` a seller serves for a grant history (adcp#8618).

    The latest re-grant within the current authority chain: withheld periods
    are due at it plus ``delivery_sla``, and an unbackfilled gap ends at the
    first boundary at or after it. ``None`` while the caller holds no grant or
    the chain was never interrupted.
    """
    if not authorizations or authorizations[-1].unauthorized_at is not None:
        return None
    chains = _chains(authorizations, timedelta(days=status_retention_days))
    regrants = [regranted for _lost, regranted in chains[-1].interruptions if regranted]
    return max(regrants) if regrants else None


def _chains(
    authorizations: Sequence[SubscriptionAuthorization], retention: timedelta | None
) -> list[_Chain]:
    chains: list[_Chain] = []
    previous_end: datetime | None = None
    for index, grant in enumerate(authorizations):
        if index > 0 and previous_end is None:
            raise CoverageInputError("invalid_input", "only the last grant may be unrevoked")
        if chains and previous_end is not None and grant.authorized_at <= previous_end:
            # adcp#8585 keeps authorized_at at the chain start after a re-grant
            # inside retention, so a grant listed with it continues the chain
            # at an unknown instant; the seller's gaps[] says what it removed.
            chains[-1].interruptions.append((previous_end, None))
            chains[-1].end = None
        elif chains and previous_end is not None:
            if retention is None:
                raise CoverageInputError(
                    "missing_status_retention_days",
                    "status_retention_days is required to evaluate a re-grant",
                )
            if grant.authorized_at < previous_end + retention:
                chains[-1].interruptions.append((previous_end, grant.authorized_at))
                chains[-1].end = None
            else:
                chains.append(_Chain(grant.authorized_at, after_expiry_of=previous_end))
        else:
            chains.append(_Chain(grant.authorized_at))
        if grant.unauthorized_at is not None:
            chains[-1].end = grant.unauthorized_at
        previous_end = grant.unauthorized_at
    return chains


def _authorized(chains: Sequence[_Chain], start: datetime, end: datetime, backfill: bool) -> bool:
    """Whether the period ``[start, end)`` falls under the caller's authority.

    It starts at or after a chain's start (the first boundary at or after
    ``authorized_at``), ends by the chain's end (nothing is owed for periods
    ending after ``unauthorized_at``), and, without backfill, does not overlap
    an interruption: the periods that end after ``unauthorized_at`` and start
    before the re-grant are a gap. Stated on the period, not on a calendar, so
    a billing stub is judged like any other period.
    """
    if not chains:
        return True  # authorized_at is not advertised and authority never lapsed
    for chain in chains:
        if start < chain.start:
            continue
        if chain.end is not None and end > chain.end:
            continue
        if not backfill and any(
            end > lost and start < regranted
            for lost, regranted in chain.interruptions
            if regranted is not None
        ):
            continue
        return True
    return False


def _expected_at(
    chains: Sequence[_Chain],
    calendar: PeriodCalendar,
    start: datetime,
    end: datetime,
    backfill: bool,
) -> tuple[datetime | None, bool]:
    """When a period is due, and whether it is withheld for lost authority.

    A period owed before a loss and not yet due is due at the re-grant
    (``authorization.regranted_at``) plus ``delivery_sla``; so is a period
    backfilled across the interruption.
    """
    sla = calendar.delivery_sla
    expected: datetime = end + sla
    for chain in chains:
        for lost, regranted in chain.interruptions:
            if regranted is None:
                continue  # an undated re-grant: the original deadline stands
            pending = end <= lost and expected > lost
            backfilled = backfill and end > lost and start < regranted
            if pending or backfilled:
                expected = max(expected, regranted + sla)
        if chain.end is not None and end <= chain.end and expected > chain.end:
            return None, True
    return expected, False


def _timezone_at(
    instant: datetime,
    current: str | None,
    changes: Sequence[AccountTimezoneChange],
    offering_id: str,
) -> str | None:
    """The zone an offering's account-timezone periods use at ``instant``.

    Only changes that apply to the offering count. Before the first of them
    the zone is that change's ``previous_timezone``; with none, it is the
    account's current zone throughout (adcp#8585).
    """
    applicable = [item for item in changes if item.offering_id in {None, offering_id}]
    if not applicable:
        return current
    zone = applicable[0].previous_timezone
    for change in applicable:
        if change.changed_at <= instant:
            zone = change.timezone
    return zone


def _offering_zone(
    item: SubscriptionOffering,
    instant: datetime,
    account_timezone: str | None,
    changes: Sequence[AccountTimezoneChange],
    period_timezone: str | None,
) -> str | None:
    """The account zone an offering's calendar reads at ``instant``, validated.

    Only an ``account_timezone`` offering without a ``period_timezone``
    override reads it; for any other the zone is irrelevant and never checked.
    """
    if item.alignment != "account_timezone" or period_timezone is not None:
        return account_timezone
    applicable = [change for change in changes if change.offering_id in {None, item.offering_id}]
    for change in applicable:
        if change.previous_timezone is None:
            raise CoverageInputError(
                "missing_previous_timezone",
                "a timezone change needs previous_timezone: the account's current timezone "
                "already shows the new zone",
            )
        require_zone(change.previous_timezone, field="timezone_cutovers.previous_timezone")
        require_zone(change.timezone, field="timezone_cutovers.timezone")
    zone = _timezone_at(instant, account_timezone, changes, item.offering_id)
    if zone is not None and not applicable:
        require_zone(zone, field="account_timezone")
    return zone


def _windows(
    subscription: Any, version_configurations: Mapping[int, Any] | None
) -> tuple[SubscriptionVersionWindow, ...]:
    if isinstance(subscription, Sequence) and not isinstance(subscription, (str, bytes, Mapping)):
        windows = tuple(subscription)
        for earlier, later in zip(windows, windows[1:]):
            if later.subscription_version <= earlier.subscription_version or (
                earlier.ends_at != later.accepted_at
            ):
                raise CoverageInputError(
                    "invalid_input", "versions must be ordered and each superseded at the next"
                )
        return windows
    return subscription_version_windows(subscription, version_configurations=version_configurations)


@dataclass
class _CoverageContext:
    """The normalized input one coverage call evaluates: shared by the owed rule and
    :mod:`~adcp.reporting.subscriptions.explain`, so both read the same facts."""

    windows: tuple[SubscriptionVersionWindow, ...]
    subscription_id: str
    chains: list[_Chain]
    changes: list[AccountTimezoneChange]
    removals: tuple[SubscriptionException, ...]
    package_removals: tuple[SubscriptionException, ...]
    membership_intervals: bool
    ordered: list[datetime]
    selectors: dict[int, SubscriptionSelector]
    in_scope: dict[int, bool]
    offerings: tuple[SubscriptionOffering, ...]
    destinations: Mapping[str, Any] | None
    account_timezone: str | None
    account_id: str
    account_operator: str | None

    def regimes(self, buy: SubscriptionBuy) -> list[_Regime | None]:
        return _regimes(
            buy,
            self.ordered,
            self.windows,
            self.selectors,
            self.in_scope,
            self.offerings,
            self.destinations,
            self.account_timezone,
            self.changes,
            account_id=self.account_id,
            account_operator=self.account_operator,
            package_removals=self.package_removals,
        )


def _coverage_context(
    *,
    subscription: Any,
    account_id: str,
    buys: Sequence[SubscriptionBuy],
    offerings: Sequence[SubscriptionOffering],
    account_timezone: str | None,
    authorizations: Sequence[SubscriptionAuthorization] = (),
    exceptions: Sequence[Any] = (),
    gaps: Sequence[Any] = (),
    coverage: Any | None = None,
    status_retention_days: int | None = None,
    destinations: Mapping[str, Any] | None = None,
    account_operator: str | None = None,
    version_configurations: Mapping[int, Any] | None = None,
    timezone_changes: Sequence[AccountTimezoneChange] = (),
) -> _CoverageContext | None:
    """Validate and normalize a coverage input; ``None`` when there is no version."""
    windows = _windows(subscription, version_configurations)
    if not windows:
        return None
    subscription_id = _subscription_id(subscription, windows)
    retention = None if status_retention_days is None else timedelta(days=status_retention_days)
    chains = _chains(authorizations, retention)
    excluded = list(exceptions)
    gap_entries = list(gaps)
    changes = list(timezone_changes)
    if coverage is not None:
        excluded.extend(_value(coverage, "excluded") or ())
        gap_entries.extend(_value(coverage, "gaps") or ())
        for entry in _value(coverage, "timezone_cutovers") or ():
            offering_id = _value(entry, "offering_id")
            previous = _value(entry, "previous_timezone")
            changes.append(
                AccountTimezoneChange(
                    _require_instant(_value(entry, "effective_at"), field="effective_at"),
                    _text(_value(entry, "timezone")),
                    offering_id=None if offering_id is None else _text(offering_id),
                    previous_timezone=None if previous is None else _text(previous),
                )
            )
    # error-codes.json: a zone is refused only when a schedule reads it, so the
    # account zone and each cutover are validated where an account_timezone
    # calendar is built (_offering_zone), never up front.
    changes.sort(key=lambda item: item.changed_at)
    every_removal = subscription_exceptions(excluded, gap_entries)
    removals = tuple(item for item in every_removal if not item.package_ids)
    package_removals = tuple(item for item in every_removal if item.package_ids)
    membership_intervals = any(item.reason == "excluded_by_buyer" for item in every_removal)
    ids = [item.offering_id for item in offerings]
    if len(set(ids)) != len(ids):
        raise CoverageInputError("invalid_input", "offering_id values must be unique")
    buy_ids = [item.media_buy_id for item in buys]
    if len(set(buy_ids)) != len(buy_ids):
        raise CoverageInputError("invalid_input", "media_buy_id values must be unique")

    points: set[datetime] = set()
    for window in windows:
        points.add(window.accepted_at)
        if window.ends_at is not None:
            points.add(window.ends_at)
        for start, until in window.inactive_intervals:
            points.add(start)
            if until is not None:
                points.add(until)
    for item in offerings:
        if item.available_from is not None:
            points.add(item.available_from)
        if item.withdrawn_at is not None:
            points.add(item.withdrawn_at)
    points.update(change.changed_at for change in changes)
    for removal in package_removals:
        points.add(removal.from_)
        if removal.until is not None:
            points.add(removal.until)
    ordered = sorted(points)
    selectors = {
        window.subscription_version: SubscriptionSelector.from_subscription(window.configuration)
        for window in windows
    }
    in_scope = {
        window.subscription_version: subscription_covers_account(
            window.configuration,
            account_id=account_id,
            account_operator=account_operator,
            membership_intervals=membership_intervals,
        )
        for window in windows
    }

    return _CoverageContext(
        windows=windows,
        subscription_id=subscription_id,
        chains=chains,
        changes=changes,
        removals=removals,
        package_removals=package_removals,
        membership_intervals=membership_intervals,
        ordered=ordered,
        selectors=selectors,
        in_scope=in_scope,
        offerings=tuple(offerings),
        destinations=destinations,
        account_timezone=account_timezone,
        account_id=account_id,
        account_operator=account_operator,
    )


def owed_subscription_periods(
    *,
    subscription: Any,
    account_id: str,
    buys: Sequence[SubscriptionBuy],
    offerings: Sequence[SubscriptionOffering],
    account_timezone: str | None,
    now: datetime,
    authorizations: Sequence[SubscriptionAuthorization] = (),
    exceptions: Sequence[Any] = (),
    gaps: Sequence[Any] = (),
    coverage: Any | None = None,
    backfill_on_regrant: bool = False,
    status_retention_days: int | None = None,
    destinations: Mapping[str, Any] | None = None,
    account_operator: str | None = None,
    version_configurations: Mapping[int, Any] | None = None,
    timezone_changes: Sequence[AccountTimezoneChange] = (),
    include_unresolved: bool = False,
    max_periods: int = DEFAULT_MAX_PERIODS,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> list[OwedSubscriptionPeriod]:
    """Every period one subscription owes one account, from plain data.

    Fails closed rather than return a partial set: more than ``max_periods``
    owed (buy, period) pairs, or more than ``max_iterations`` boundaries
    examined, raises :class:`CoverageInputError` with code
    ``COVERAGE_BUDGET_EXCEEDED`` (the TypeScript SDK's ``max_periods`` and
    ``max_iterations``, with the same defaults).

    :param subscription: the ``get_principal`` readback
        (``core/reporting-subscription-state.json``) as a model or mapping, or
        a sequence of :class:`SubscriptionVersionWindow`.
    :param buys: the confirmed buys in the account the caller can see.
        A buy without ``confirmed_at`` is not covered.
    :param offerings: offerings with the seller's dimension mapping.
    :param account_timezone: the account's current timezone, as
        ``list_accounts`` shows it. Before a timezone change the zone is that
        change's ``previous_timezone``. Required when an ``account_timezone``
        offering resolves without a ``period_timezone`` and no change applies.
    :param now: periods are owed once resolved, at their end; a period ending
        after ``now`` is returned only with ``include_unresolved``.
    :param authorizations: the account's grant history. Empty means the seller
        does not advertise ``authorized_at`` and authority never lapsed.
    :param exceptions: ``subscription_coverage[].excluded`` entries, or
        :class:`SubscriptionException` values.
    :param gaps: ``subscription_coverage[].gaps`` entries.
    :param coverage: a whole ``subscription_coverage[]`` entry; its
        ``excluded``, ``gaps`` and ``timezone_cutovers`` are applied too.
    :param backfill_on_regrant: the seller's ``subscriptions.backfill_on_regrant``.
    :param status_retention_days: the seller's status retention; required when
        a grant was revoked and re-granted.
    :param destinations: the caller's ``reporting_destinations`` by
        ``destination_id``, needed when a version names a destination.
    :param account_operator: the account's operator, for ``operator_domains``.
    :param timezone_changes: account timezone changes, each a cutover at its
        ``changed_at`` (``timezone_cutovers[].effective_at``).
    """
    now = utc_instant(now, field="now")
    for name, budget in (("max_periods", max_periods), ("max_iterations", max_iterations)):
        if type(budget) is not int or budget < 1:
            raise CoverageInputError("invalid_input", f"{name} must be a positive integer")
    iterations = pairs = 0
    context = _coverage_context(
        subscription=subscription,
        account_id=account_id,
        buys=buys,
        offerings=offerings,
        account_timezone=account_timezone,
        authorizations=authorizations,
        exceptions=exceptions,
        gaps=gaps,
        coverage=coverage,
        status_retention_days=status_retention_days,
        destinations=destinations,
        account_operator=account_operator,
        version_configurations=version_configurations,
        timezone_changes=timezone_changes,
    )
    if context is None:
        return []
    subscription_id = context.subscription_id
    chains = context.chains
    removals = context.removals

    owed: dict[tuple[int, str, str, datetime], _Owed] = {}
    for buy in buys:
        if buy.confirmed_at is None:
            continue
        confirmed_at = buy.confirmed_at
        regimes = context.regimes(buy)
        flights_start = buy._earliest_start()
        latest_stop = buy._latest_stop()
        for index, regime in enumerate(regimes):
            if regime is None:
                continue
            for resolved in regime.resolved:
                calendar = resolved.calendar
                handoff = regime.handoffs[resolved.offering.offering_id]
                lower = max(
                    _first_owed_start(calendar, handoff, buy.confirmed_at),
                    calendar.floor(flights_start),
                )
                upper = latest_stop if regime.until is None else min(regime.until, latest_stop)
                if not include_unresolved:
                    upper = min(upper, now)
                candidates: Iterable[tuple[datetime, datetime, bool]] = (
                    (start, end, False)
                    for start, end in calendar.periods(starts_from=lower, starts_before=upper)
                )
                stub = _billing_stub(regime, resolved, handoff)
                if stub is not None:
                    candidates = itertools.chain((stub,), candidates)
                elif lower >= upper:
                    continue
                for start, end, is_stub in candidates:
                    iterations += 1
                    if iterations > max_iterations:
                        raise CoverageInputError.budget_exceeded(
                            "max_iterations",
                            f"Coverage would examine more than max_iterations ({max_iterations}) "
                            f"period boundaries for subscription {subscription_id!r} on account "
                            f"{account_id!r}. Narrow the input or raise max_iterations.",
                        )
                    if not include_unresolved and end > now:
                        break
                    if is_stub and (
                        end <= confirmed_at or (regime.until is not None and start >= regime.until)
                    ):
                        continue  # the stub's own side and confirmation checks
                    flight_start, flight_stop = buy.flight_at(end)
                    # An empty flight (stopped at or before it started)
                    # overlaps no period.
                    if flight_stop <= flight_start or not (
                        start < flight_stop and end > flight_start
                    ):
                        continue
                    if not _authorized(chains, start, end, backfill_on_regrant):
                        continue
                    if any(
                        item.removes(start, buy.media_buy_id, resolved.offering.offering_id)
                        for item in removals
                    ):
                        continue
                    key = (regime.version, resolved.offering.offering_id, calendar.zone_name, start)
                    entry = owed.get(key)
                    if entry is None:
                        expected_at, withheld = _expected_at(
                            chains, calendar, start, end, backfill_on_regrant
                        )
                        entry = owed[key] = _Owed(
                            regime.version,
                            resolved.offering,
                            calendar.zone_name,
                            start,
                            end,
                            expected_at,
                            withheld,
                            is_stub,
                        )
                    if buy.media_buy_id not in entry.media_buy_ids:
                        entry.media_buy_ids.add(buy.media_buy_id)
                        pairs += 1
                        if pairs > max_periods:
                            raise CoverageInputError.budget_exceeded(
                                "max_periods",
                                f"Subscription {subscription_id!r} owes account {account_id!r} "
                                f"more than max_periods ({max_periods}) periods. Narrow the "
                                "input or raise max_periods.",
                            )

    periods = [
        OwedSubscriptionPeriod(
            delivery_config_id=subscription_id,
            delivery_config_version=item.version,
            report_definition_id=item.offering.report_definition_id,
            feed_purpose=item.offering.feed_purpose,
            reporting_profile=item.offering.reporting_profile,
            media_buy_ids=tuple(sorted(item.media_buy_ids, key=lambda value: value.encode())),
            period_start=iso_z(item.start),
            period_end=iso_z(item.end),
            source_timezone=item.zone,
            expected_at=None if item.expected_at is None else iso_z(item.expected_at),
            account_id=account_id,
            subscription_id=subscription_id,
            offering_id=item.offering.offering_id,
            withheld=item.withheld,
            stub=item.stub,
        )
        for item in owed.values()
    ]
    periods.sort(
        key=lambda item: (
            utc_instant(datetime.fromisoformat(item.period_start.replace("Z", "+00:00"))),
            item.delivery_config_version,
            item.offering_id.encode("utf-8"),
        )
    )
    return periods


def _regimes(
    buy: SubscriptionBuy,
    ordered: Sequence[datetime],
    windows: Sequence[SubscriptionVersionWindow],
    selectors: Mapping[int, SubscriptionSelector],
    in_scope: Mapping[int, bool],
    offerings: Sequence[SubscriptionOffering],
    destinations: Mapping[str, Any] | None,
    account_timezone: str | None,
    changes: Sequence[AccountTimezoneChange],
    *,
    account_id: str = "",
    account_operator: str | None = None,
    package_removals: Sequence[SubscriptionException] = (),
) -> list[_Regime | None]:
    """Maximal stretches with one active, in-scope version and one resolution."""
    regimes: list[_Regime | None] = []
    for index, start in enumerate(ordered):
        until = ordered[index + 1] if index + 1 < len(ordered) else None
        window = next((item for item in windows if item.active_at(start)), None)
        if window is None or not in_scope[window.subscription_version]:
            if regimes and regimes[-1] is not None:
                regimes.append(None)
            continue
        selector = selectors[window.subscription_version]
        # adcp#8619: an account routed by destination_overrides resolves
        # against its own destination's method.
        destination_id = selector.destination_for(account_id, account_operator)
        destination = window.destination if destination_id == selector.destination_id else None
        if destination is None and destination_id is not None:
            if destinations is None or destination_id not in destinations:
                raise CoverageInputError(
                    "missing_destination_method",
                    f"version {window.subscription_version} names destination "
                    f"{destination_id!r}; pass its pattern and transport",
                )
            destination = destinations[destination_id]
        # adcp#8619: each package resolves when its (buy, version) pair first
        # does and keeps that result; a buy owes the union of its packages'.
        first_resolved = window.accepted_at
        if buy.confirmed_at is not None:
            first_resolved = max(first_resolved, buy.confirmed_at)
        found: dict[str, SubscriptionOffering] = {}
        for package_id, offering_ids in buy.product_offering_ids():
            if any(
                item.removes_package(start, buy.media_buy_id, package_id)
                for item in package_removals
            ):
                continue
            for item in resolve_reporting_subscription_package(
                selector,
                offerings,
                product_offering_ids=offering_ids,
                destination=destination,
                resolves_at=first_resolved,
                at=start,
            ):
                found[item.offering_id] = item
        resolved = tuple(
            _Resolved(
                item,
                item.calendar(
                    _offering_zone(
                        item, start, account_timezone, changes, selector.period_timezone
                    ),
                    selector.period_timezone,
                ),
            )
            for item in sorted(found.values(), key=lambda value: value.offering_id.encode())
        )
        previous = regimes[-1] if regimes else None
        if (
            previous is not None
            and previous.version == window.subscription_version
            and [item.key for item in previous.resolved] == [item.key for item in resolved]
        ):
            previous.until = until
            continue
        regime = _Regime(start, until, window.subscription_version, resolved)
        predecessor = regimes[-1] if regimes else None
        before_deactivation = (
            _regime_before_deactivation(regimes, window, start) if predecessor is None else None
        )
        if before_deactivation is not None:
            deactivated_at, last = before_deactivation
            # Reactivation (adcp#8622): resume at the first boundary at or after
            # the later of the reactivation instant and the end of the last
            # period owed before the deactivation, computed like a cutover's E
            # at the deactivation, so nothing is owed twice.
            regime.handoffs = {
                item.offering.offering_id: max(start, _cutover_end(deactivated_at, last, item))
                for item in resolved
            }
        else:
            regime.handoffs = {
                item.offering.offering_id: _cutover_end(start, predecessor, item)
                for item in resolved
            }
            regime.cutover = predecessor is not None and bool(predecessor.resolved)
        regimes.append(regime)
    return regimes


def _regime_before_deactivation(
    regimes: Sequence[_Regime | None], window: SubscriptionVersionWindow, start: datetime
) -> tuple[datetime, _Regime] | None:
    """For a reactivation at ``start``: the deactivation instant and the regime in force
    just before it, when that regime resolved offerings."""
    deactivated_at = next(
        (begin for begin, until in window.inactive_intervals if until == start), None
    )
    if deactivated_at is None:
        return None
    last = next((item for item in reversed(regimes) if item is not None), None)
    if last is None or not last.resolved or last.until != deactivated_at:
        return None
    return deactivated_at, last


def _billing_stub(
    regime: _Regime, resolved: _Resolved, handoff: datetime
) -> tuple[datetime, datetime, bool] | None:
    """The stub ``[E, first incoming boundary)`` a cutover owes a billing feed (adcp#8617).

    Pacing and analytics feeds leave that stretch unowed; it is empty when E
    is already an incoming boundary.
    """
    if not regime.cutover or resolved.offering.feed_purpose != "billing":
        return None
    first = resolved.calendar.ceil(handoff)
    if first <= handoff:
        return None
    return handoff, first, True


def _first_owed_start(
    calendar: PeriodCalendar, handoff: datetime, confirmed_at: datetime
) -> datetime:
    """The earliest start a buy's owed period may have under one regime.

    adcp#8593 (owner decision for 3.3.0-beta.2): the start bound is the first
    boundary at or after the later of the version's ``accepted_at`` (or the
    cutover E, the regime's handoff) and the account's ``authorized_at``,
    which the authority chains apply. A buy is then owed every period that
    ends after its ``confirmed_at``, so the period containing the
    confirmation is owed in full. This is the one place the confirmation rule
    lives.
    """
    return max(calendar.ceil(handoff), calendar.floor(confirmed_at))


def _cutover_end(handoff: datetime, predecessor: _Regime | None, incoming: _Resolved) -> datetime:
    """E for an incoming offering at ``handoff``; the regime start with no predecessor.

    E is the end of the outgoing offering's last period that starts before T.
    When the outgoing regime itself owed nothing before T (its own handoff
    lies after T, as with two cutovers inside one period), that last period
    belongs to an earlier regime and ends at the outgoing regime's handoff.
    """
    if predecessor is None or not predecessor.resolved:
        return handoff

    def outgoing_end(item: _Resolved) -> datetime:
        return max(
            item.calendar.ceil(handoff),
            predecessor.handoffs.get(item.offering.offering_id, handoff),
        )

    for item in predecessor.resolved:
        if item.offering.offering_id == incoming.offering.offering_id:
            return outgoing_end(item)
    return max(outgoing_end(item) for item in predecessor.resolved)


def _subscription_id(subscription: Any, windows: Sequence[SubscriptionVersionWindow]) -> str:
    value = None
    if not isinstance(subscription, Sequence) or isinstance(subscription, Mapping):
        value = _value(subscription, "subscription_id")
    if value is None:
        configuration = windows[-1].configuration
        if hasattr(configuration, "model_dump"):
            configuration = configuration.model_dump(mode="json", exclude_none=True)
        value = _value(configuration, "subscription_id")
    if value is None:
        raise CoverageInputError("invalid_input", "subscription_id is required")
    return _text(value)


#: Deprecated alias of :class:`OwedSubscriptionPeriod`, kept for one beta.
ExpectedSubscriptionPeriod = OwedSubscriptionPeriod


def expected_subscription_periods(**kwargs: Any) -> list[OwedSubscriptionPeriod]:
    """Deprecated: renamed :func:`owed_subscription_periods` ("owed" on both sides).

    Kept for one beta; it warns and delegates.
    """
    import warnings

    warnings.warn(
        "expected_subscription_periods is renamed owed_subscription_periods",
        DeprecationWarning,
        stacklevel=2,
    )
    return owed_subscription_periods(**kwargs)


__all__ = [
    "AccountTimezoneChange",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_PERIODS",
    "ExpectedSubscriptionPeriod",
    "OwedSubscriptionPeriod",
    "SubscriptionAuthorization",
    "SubscriptionBuy",
    "SubscriptionBuyFlight",
    "SubscriptionBuyPackage",
    "SubscriptionException",
    "SubscriptionVersionWindow",
    "authorization_chain_start",
    "authorization_regranted_at",
    "expected_subscription_periods",
    "owed_subscription_periods",
    "subscription_covers_account",
    "subscription_exceptions",
    "subscription_version_windows",
]
