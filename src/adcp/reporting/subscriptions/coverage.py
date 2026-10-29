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
   of ``end_time``, ``canceled_at`` and ``completed_at``, with the flight taken
   as of the period's ``scope_resolved_at`` (its end).

Periods are never clipped: the period containing the stop time is owed in
full. Every change that hands periods from an outgoing schedule to an incoming
one at an instant T is a cutover: the outgoing schedule owes its periods that
start before T in full, E is the end of the last of them, and the incoming
schedule owes its periods that start at or after E. Version changes,
deactivation, resolution changes and account-timezone changes all use it.

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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from adcp.reporting._reconcile import ExpectedReportingPeriod
from adcp.reporting.subscriptions._clock import PeriodCalendar, iso_z, require_zone, utc_instant
from adcp.reporting.subscriptions.resolution import (
    SubscriptionOffering,
    SubscriptionSelector,
    _text,
    _value,
    resolve_subscription_offerings,
)

#: Exclusion reasons that remove periods. ``package_ids`` entries never do.
_REASONS = frozenset(
    {"offering_unsupported", "capacity", "overridden", "excluded_by_buyer", "action_required"}
)


def _instant(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime or an RFC 3339 string")
    return utc_instant(value, field=field)


def _require_instant(value: Any, *, field: str) -> datetime:
    instant = _instant(value, field=field)
    if instant is None:
        raise ValueError(f"{field} is required")
    return instant


# -- plain-data inputs ------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionBuyFlight:
    """A flight a buy had before a later change superseded it at ``superseded_at``.

    Periods whose ``scope_resolved_at`` (end) is before ``superseded_at`` were
    resolved under this flight, so an extension or reinstatement never makes
    an elapsed period owed after the fact.
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
    records earlier flights and when a change replaced each. Without
    ``packages``, ``reporting_delivery_offering_ids`` is the buy's products'
    offering ids, resolved as one product.
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

    def __post_init__(self) -> None:
        if not self.media_buy_id:
            raise ValueError("media_buy_id is required")
        for name in ("confirmed_at", "start_time", "end_time", "canceled_at", "completed_at"):
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
        return _stop(self.end_time, self.canceled_at, self.completed_at)

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
                raise ValueError("unauthorized_at must be after authorized_at")


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
    #: A ``gaps[]`` entry for one offering's boundaries; ``None`` is every offering.
    offering_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_", utc_instant(self.from_, field="from"))
        if self.until is not None:
            object.__setattr__(self, "until", utc_instant(self.until, field="until"))

    def removes(self, start: datetime, media_buy_id: str, offering_id: str | None = None) -> bool:
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

    An ``offering_unsupported`` entry naming ``package_ids`` removes those
    packages from the buy's coverage, not periods, so it is dropped here.
    """
    result: list[SubscriptionException] = []
    for entry in excluded:
        if isinstance(entry, SubscriptionException):
            result.append(entry)
            continue
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump(mode="json", exclude_none=True, by_alias=True)
        if _value(entry, "package_ids"):
            continue
        reason = _text(_value(entry, "reason"))
        if reason not in _REASONS:
            raise ValueError(f"unknown subscription exclusion reason {reason!r}")
        media_buy_id = _value(entry, "media_buy_id")
        result.append(
            SubscriptionException(
                reason=reason,
                from_=_require_instant(_value(entry, "from", _value(entry, "from_")), field="from"),
                until=_instant(_value(entry, "until"), field="until"),
                media_buy_id=None if media_buy_id is None else _text(media_buy_id),
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
                raise ValueError("a version cannot end before it was accepted")
        intervals = []
        for start, until in self.inactive_intervals:
            start = utc_instant(start, field="inactive_intervals.from")
            if until is not None:
                until = utc_instant(until, field="inactive_intervals.until")
                if until <= start:
                    raise ValueError("an inactive interval must end after it starts")
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
    (adcp#8588). A seller on 3.3.0-beta.1 does not send it; then the current
    configuration is assumed, unless ``require_prior_configurations`` makes
    the gap an error. A version accepted while the subscription was already
    inactive has an empty window.
    """
    if hasattr(subscription, "model_dump"):
        subscription = subscription.model_dump(mode="json", exclude_none=True)
    configurations = dict(version_configurations or {})
    current_version = int(_value(subscription, "subscription_version"))
    current_configuration = _value(subscription, "configuration")
    if current_configuration is None:
        raise ValueError("subscription state has no configuration")
    state = _text(_value(subscription, "state", "active"))
    deactivated_at = _instant(_value(subscription, "deactivated_at"), field="deactivated_at")
    if state == "inactive" and deactivated_at is None:
        raise ValueError("an inactive subscription must carry deactivated_at")
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
                raise ValueError(
                    f"no configuration is known for prior subscription_version {version}"
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
            raise ValueError("subscription versions must increase with accepted_at")
        if earlier.ends_at != later.accepted_at:
            raise ValueError("superseded_at must equal the next version's accepted_at")
    return tuple(windows)


def subscription_covers_account(
    configuration: Any, *, account_id: str, account_operator: str | None = None
) -> bool:
    """Whether a subscription version's scope selects the account.

    ``exclude_account_ids`` and ``operator_domains`` narrow the scope; the
    seller also reports an excluded account as ``excluded_by_buyer``. With
    ``operator_domains``, an account whose operator is unknown is not selected.
    """
    if hasattr(configuration, "model_dump"):
        configuration = configuration.model_dump(mode="json", exclude_none=True)
    scope = _value(configuration, "scope") or {}
    account_ids = _value(scope, "account_ids")
    if account_ids is not None:
        if account_id not in {_text(item) for item in account_ids}:
            return False
    elif _value(scope, "all_authorized_accounts") is not True:
        return False
    excluded = _value(configuration, "exclude_account_ids") or ()
    if account_id in {_text(item) for item in excluded}:
        return False
    domains = _value(configuration, "operator_domains")
    if domains:
        return account_operator is not None and account_operator in {
            _text(item) for item in domains
        }
    return True


# -- output -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ExpectedSubscriptionPeriod(ExpectedReportingPeriod):
    """One owed period of one (subscription version, resolved offering).

    An :class:`~adcp.reporting.ExpectedReportingPeriod`, so it feeds
    :func:`~adcp.reporting.evaluate_reporting_ledger` and the consumer-status
    planner unchanged. ``delivery_config_id`` is the ``subscription_id`` and
    ``delivery_config_version`` the ``subscription_version``. ``media_buy_ids``
    are the buys owed this period under this offering, the denominator the
    seller freezes at ``scope_resolved_at``.

    ``withheld`` marks a period owed before a loss of authority that is not
    due while authority is absent; its ``expected_at`` is ``None``. After a
    re-grant it is due at the re-grant time plus the offering's
    ``delivery_sla``, never earlier than its original ``expected_at``.
    """

    account_id: str
    subscription_id: str
    offering_id: str
    withheld: bool = False


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
    media_buy_ids: set[str] = field(default_factory=set)


def _chains(
    authorizations: Sequence[SubscriptionAuthorization], retention: timedelta | None
) -> list[_Chain]:
    chains: list[_Chain] = []
    previous_end: datetime | None = None
    for index, grant in enumerate(authorizations):
        if index > 0 and previous_end is None:
            raise ValueError("only the last grant may be unrevoked")
        if chains and previous_end is not None and grant.authorized_at <= previous_end:
            # adcp#8585 keeps authorized_at at the chain start after a re-grant
            # inside retention, so a grant listed with it continues the chain
            # at an unknown instant; the seller's gaps[] says what it removed.
            chains[-1].interruptions.append((previous_end, None))
            chains[-1].end = None
        elif chains and previous_end is not None:
            if retention is None:
                raise ValueError("status_retention_days is required to evaluate a re-grant")
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


def _authorized(
    chains: Sequence[_Chain], calendar: PeriodCalendar, start: datetime, backfill: bool
) -> bool:
    if not chains:
        return True  # authorized_at is not advertised and authority never lapsed
    for chain in chains:
        if start < calendar.ceil(chain.start):
            continue
        if chain.end is not None and start >= calendar.floor(chain.end):
            continue
        if not backfill and any(
            calendar.floor(lost) <= start < calendar.ceil(regranted)
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
    """When a period is due, and whether it is withheld for lost authority."""
    sla = calendar.delivery_sla
    expected: datetime = end + sla
    for chain in chains:
        for lost, regranted in chain.interruptions:
            if regranted is None:
                continue  # an undated re-grant: the original deadline stands
            pending = end <= calendar.floor(lost) and expected > lost
            backfilled = backfill and calendar.floor(lost) <= start < calendar.ceil(regranted)
            if pending or backfilled:
                expected = max(expected, regranted + sla)
        if chain.end is not None and end <= calendar.floor(chain.end) and expected > chain.end:
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


def _windows(
    subscription: Any, version_configurations: Mapping[int, Any] | None
) -> tuple[SubscriptionVersionWindow, ...]:
    if isinstance(subscription, Sequence) and not isinstance(subscription, (str, bytes, Mapping)):
        windows = tuple(subscription)
        for earlier, later in zip(windows, windows[1:]):
            if later.subscription_version <= earlier.subscription_version or (
                earlier.ends_at != later.accepted_at
            ):
                raise ValueError("versions must be ordered and each superseded at the next")
        return windows
    return subscription_version_windows(subscription, version_configurations=version_configurations)


def expected_subscription_periods(
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
) -> list[ExpectedSubscriptionPeriod]:
    """Every period one subscription owes one account, from plain data.

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
    windows = _windows(subscription, version_configurations)
    if not windows:
        return []
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
    changes.sort(key=lambda item: item.changed_at)
    if account_timezone is not None:
        require_zone(account_timezone, field="account_timezone")
    for change in changes:
        require_zone(change.timezone, field="timezone_cutovers.timezone")
    for change in changes:
        if change.previous_timezone is None:
            raise ValueError(
                "a timezone change needs previous_timezone: the account's current timezone "
                "already shows the new zone"
            )
        require_zone(change.previous_timezone, field="timezone_cutovers.previous_timezone")
    changes.sort(key=lambda item: item.changed_at)
    removals = subscription_exceptions(excluded, gap_entries)
    ids = [item.offering_id for item in offerings]
    if len(set(ids)) != len(ids):
        raise ValueError("offering_id values must be unique")
    buy_ids = [item.media_buy_id for item in buys]
    if len(set(buy_ids)) != len(buy_ids):
        raise ValueError("media_buy_id values must be unique")

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
    ordered = sorted(points)
    selectors = {
        window.subscription_version: SubscriptionSelector.from_subscription(window.configuration)
        for window in windows
    }
    in_scope = {
        window.subscription_version: subscription_covers_account(
            window.configuration, account_id=account_id, account_operator=account_operator
        )
        for window in windows
    }

    owed: dict[tuple[int, str, str, datetime], _Owed] = {}
    for buy in buys:
        if buy.confirmed_at is None:
            continue
        regimes = _regimes(
            buy,
            ordered,
            windows,
            selectors,
            in_scope,
            offerings,
            destinations,
            account_timezone,
            changes,
        )
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
                if lower >= upper:
                    continue
                for start, end in calendar.periods(starts_from=lower, starts_before=upper):
                    if not include_unresolved and end > now:
                        break
                    flight_start, flight_stop = buy.flight_at(end)
                    # An empty flight (stopped at or before it started)
                    # overlaps no period.
                    if flight_stop <= flight_start or not (
                        start < flight_stop and end > flight_start
                    ):
                        continue
                    if not _authorized(chains, calendar, start, backfill_on_regrant):
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
                        )
                    entry.media_buy_ids.add(buy.media_buy_id)

    periods = [
        ExpectedSubscriptionPeriod(
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
        destination = window.destination
        if destination is None and selector.destination_id is not None:
            if destinations is None or selector.destination_id not in destinations:
                raise ValueError(
                    f"version {window.subscription_version} names destination "
                    f"{selector.destination_id!r}; pass its pattern and transport"
                )
            destination = destinations[selector.destination_id]
        found: dict[str, SubscriptionOffering] = {}
        for _package_id, offering_ids in buy.product_offering_ids():
            resolution = resolve_subscription_offerings(
                selector,
                offerings,
                product_offering_ids=offering_ids,
                destination=destination,
                at=start,
            )
            for item in resolution.offerings:
                found[item.offering_id] = item
        resolved = tuple(
            _Resolved(
                item,
                item.calendar(
                    _timezone_at(start, account_timezone, changes, item.offering_id),
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
        regime.handoffs = {
            item.offering.offering_id: _cutover_end(start, predecessor, item) for item in resolved
        }
        regimes.append(regime)
    return regimes


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
        raise ValueError("subscription_id is required")
    return _text(value)


__all__ = [
    "AccountTimezoneChange",
    "ExpectedSubscriptionPeriod",
    "SubscriptionAuthorization",
    "SubscriptionBuy",
    "SubscriptionBuyFlight",
    "SubscriptionBuyPackage",
    "SubscriptionException",
    "SubscriptionVersionWindow",
    "expected_subscription_periods",
    "subscription_covers_account",
    "subscription_exceptions",
    "subscription_version_windows",
]
