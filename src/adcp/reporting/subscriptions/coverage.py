"""Which reporting periods a subscription owes one account: the buyer's rule.

This is the pure, plain-data form of the coverage rule in
``core/reporting-subscription.json`` (``x-adcp-validation.coverage`` and
``cutover``). A buyer runs it from its own records; the seller runtime runs the
same function over the same facts to decide which obligations it owes, so the
two sides agree period by period by construction.

For each covered buy and resolved offering, the owed periods are the complete
periods of the offering's published schedule that

1. start on or after the first boundary at or after the latest of the
   subscription version's ``accepted_at``, the buy's ``confirmed_at`` and the
   account's ``authorized_at``; and
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

Pairing rule for cutovers (where the spec is silent)
    Cutovers are paired per product. An offering present on both sides of T
    continues from E of its own outgoing schedule. When offerings leave and
    others arrive at the same T (a resolution or selector change), each
    arriving offering starts at the latest E among the leaving ones, so no
    period is owed twice. An offering that arrives with nothing leaving starts
    at T, under the ordinary start rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.reporting._reconcile import ExpectedReportingPeriod
from adcp.reporting.ledger.models import ReportingScheduleSpec
from adcp.reporting.subscriptions._clock import (
    delivery_sla,
    first_boundary_at_or_after,
    iso_z,
    iter_periods,
    last_boundary_at_or_before,
    utc_instant,
)
from adcp.reporting.subscriptions.resolution import (
    SubscriptionOffering,
    SubscriptionSelector,
    _text,
    _value,
    resolve_subscription_offerings,
)

_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)
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
    """An account timezone change at ``changed_at`` (the instant T of the cutover)."""

    changed_at: datetime
    timezone: str

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_", utc_instant(self.from_, field="from"))
        if self.until is not None:
            object.__setattr__(self, "until", utc_instant(self.until, field="until"))

    def removes(self, start: datetime, media_buy_id: str) -> bool:
        if self.media_buy_id is not None and self.media_buy_id != media_buy_id:
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
        result.append(
            SubscriptionException(
                reason="unauthorized",
                from_=_require_instant(_value(gap, "from", _value(gap, "from_")), field="from"),
                until=_require_instant(_value(gap, "until"), field="until"),
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_at", utc_instant(self.accepted_at, field="accepted_at"))
        if self.ends_at is not None:
            object.__setattr__(self, "ends_at", utc_instant(self.ends_at, field="ends_at"))
            if self.ends_at < self.accepted_at:
                raise ValueError("a version cannot end before it was accepted")

    def contains(self, instant: datetime) -> bool:
        return self.accepted_at <= instant and (self.ends_at is None or instant < self.ends_at)


def subscription_version_windows(
    subscription: Any, *, version_configurations: Mapping[int, Any] | None = None
) -> tuple[SubscriptionVersionWindow, ...]:
    """Derive version windows from a ``core/reporting-subscription-state.json`` readback.

    The readback keeps ``prior_versions`` (version, ``accepted_at``,
    ``superseded_at``) but not their configurations. Supply each prior
    version's configuration in ``version_configurations`` when it differs
    from the current one; otherwise the current configuration is assumed.
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
    windows = [
        SubscriptionVersionWindow(
            subscription_version=current_version,
            accepted_at=_require_instant(_value(subscription, "accepted_at"), field="accepted_at"),
            ends_at=deactivated_at if state == "inactive" else None,
            configuration=configurations.get(current_version, current_configuration),
        )
    ]
    for prior in _value(subscription, "prior_versions", ()) or ():
        version = int(_value(prior, "subscription_version"))
        windows.append(
            SubscriptionVersionWindow(
                subscription_version=version,
                accepted_at=_require_instant(_value(prior, "accepted_at"), field="accepted_at"),
                ends_at=_require_instant(_value(prior, "superseded_at"), field="superseded_at"),
                configuration=configurations.get(version, current_configuration),
            )
        )
    windows.sort(key=lambda item: (item.accepted_at, item.subscription_version))
    for earlier, later in zip(windows, windows[1:]):
        if earlier.subscription_version >= later.subscription_version:
            raise ValueError("subscription versions must increase with accepted_at")
        if earlier.ends_at is None or earlier.ends_at > later.accepted_at:
            raise ValueError("a prior version must be superseded no later than its successor")
    return tuple(windows)


def subscription_covers_account(
    configuration: Any, *, account_id: str, account_operator: str | None = None
) -> bool:
    """Whether a subscription version's scope selects the account.

    ``exclude_account_ids`` and ``operator_domains`` narrow the scope; the
    seller also reports an excluded account as ``excluded_by_buyer``.
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
        if account_operator is None:
            raise ValueError(
                "the subscription filters by operator_domains; pass the account's operator"
            )
        return account_operator in {_text(item) for item in domains}
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
class _Slot:
    """One (version, offering, zone) feed a product resolves to in one segment."""

    version: int
    offering: SubscriptionOffering
    zone: str
    schedule: ReportingScheduleSpec

    def same_feed(self, other: _Slot) -> bool:
        return (
            self.version == other.version
            and self.offering.offering_id == other.offering.offering_id
            and self.zone == other.zone
        )


@dataclass
class _SlotState:
    slot: _Slot
    lower: datetime


@dataclass(frozen=True)
class _Loss:
    unauthorized_at: datetime
    regranted_at: datetime | None
    expired: bool


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


def _losses(
    authorizations: Sequence[SubscriptionAuthorization], retention: timedelta | None
) -> tuple[datetime | None, tuple[_Loss, ...]]:
    ordered = sorted(authorizations, key=lambda item: item.authorized_at)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.unauthorized_at is None or earlier.unauthorized_at > later.authorized_at:
            raise ValueError("authorization grants must not overlap")
    losses: list[_Loss] = []
    for index, grant in enumerate(ordered):
        if grant.unauthorized_at is None:
            continue
        regranted = ordered[index + 1].authorized_at if index + 1 < len(ordered) else None
        expired = (
            regranted is not None
            and retention is not None
            and regranted - grant.unauthorized_at > retention
        )
        losses.append(_Loss(grant.unauthorized_at, regranted, expired))
    return (ordered[0].authorized_at if ordered else None), tuple(losses)


def _zone_at(
    instant: datetime, account_timezone: str, changes: Sequence[AccountTimezoneChange]
) -> str:
    zone = account_timezone
    for change in changes:
        if change.changed_at <= instant:
            zone = change.timezone
    return zone


def expected_subscription_periods(
    *,
    subscription: Any,
    account_id: str,
    buys: Sequence[SubscriptionBuy],
    offerings: Sequence[SubscriptionOffering],
    account_timezone: str,
    now: datetime,
    authorizations: Sequence[SubscriptionAuthorization] = (),
    exceptions: Sequence[Any] = (),
    gaps: Sequence[Any] = (),
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
    :param account_timezone: the account's timezone before the first entry of
        ``timezone_changes`` (its current timezone when there are none).
    :param now: periods are owed once resolved, at their end; a period ending
        after ``now`` is returned only with ``include_unresolved``.
    :param authorizations: the account's grant history. Empty means the seller
        does not advertise ``authorized_at`` and authority never lapsed.
    :param exceptions: ``subscription_coverage[].excluded`` entries, or
        :class:`SubscriptionException` values.
    :param gaps: ``subscription_coverage[].gaps`` entries.
    :param backfill_on_regrant: the seller's ``subscriptions.backfill_on_regrant``.
    :param status_retention_days: the seller's status retention; a re-grant
        later than this after a loss is a fresh coverage start.
    :param destinations: the caller's ``reporting_destinations`` by
        ``destination_id``, needed when a version names a destination.
    :param account_operator: the account's operator, needed with
        ``operator_domains``.
    """
    now = utc_instant(now, field="now")
    windows = (
        tuple(subscription)
        if isinstance(subscription, Sequence)
        and not isinstance(subscription, (str, bytes, Mapping))
        else subscription_version_windows(
            subscription, version_configurations=version_configurations
        )
    )
    if not windows:
        return []
    subscription_id = _subscription_id(subscription, windows)
    retention = None if status_retention_days is None else timedelta(days=status_retention_days)
    chain_start, losses = _losses(authorizations, retention)
    removals = subscription_exceptions(exceptions, gaps)
    tz_changes = tuple(sorted(timezone_changes, key=lambda item: item.changed_at))
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
    instants = {window.accepted_at for window in windows}
    instants.update(window.ends_at for window in windows if window.ends_at is not None)
    instants.update(item.available_from for item in offerings if item.available_from is not None)
    instants.update(item.withdrawn_at for item in offerings if item.withdrawn_at is not None)
    instants.update(change.changed_at for change in tz_changes)
    first = windows[0].accepted_at
    boundaries = sorted(instant for instant in instants if instant >= first)

    owed: dict[tuple[int, str, str, datetime], _Owed] = {}
    for buy in buys:
        if buy.confirmed_at is None:
            continue
        floor = max(item for item in (buy.confirmed_at, chain_start) if item is not None)
        ceiling = buy._latest_stop()
        if not include_unresolved:
            ceiling = min(ceiling, now)
        for _package_id, offering_ids in buy.product_offering_ids():
            previous: dict[str, _SlotState] = {}
            for index, segment_start in enumerate(boundaries):
                segment_end = boundaries[index + 1] if index + 1 < len(boundaries) else None
                window = next((item for item in windows if item.contains(segment_start)), None)
                slots: dict[str, _Slot] = {}
                if window is not None and in_scope[window.subscription_version]:
                    zone_name = _zone_at(segment_start, account_timezone, tz_changes)
                    selector = selectors[window.subscription_version]
                    destination = None
                    if selector.destination_id is not None:
                        if destinations is None or selector.destination_id not in destinations:
                            raise ValueError(
                                f"destination {selector.destination_id!r} is not in destinations"
                            )
                        destination = destinations[selector.destination_id]
                    resolution = resolve_subscription_offerings(
                        selector,
                        offerings,
                        product_offering_ids=offering_ids,
                        account_timezone=zone_name,
                        destination=destination,
                        at=segment_start,
                    )
                    for offering in resolution.offerings:
                        zone = offering.zone(zone_name)
                        slots[offering.offering_id] = _Slot(
                            window.subscription_version,
                            offering,
                            zone,
                            offering.schedule(zone_name),
                        )
                current = _pair(previous, slots, segment_start)
                for state in current.values():
                    _collect(
                        owed,
                        state,
                        buy=buy,
                        floor=floor,
                        starts_before=min(segment_end or _FAR_FUTURE, ceiling),
                        now=now,
                        include_unresolved=include_unresolved,
                        removals=removals,
                        losses=losses,
                        backfill=backfill_on_regrant,
                    )
                previous = current

    periods = [
        ExpectedSubscriptionPeriod(
            delivery_config_id=subscription_id,
            delivery_config_version=item.version,
            report_definition_id=item.offering.report_definition_id,
            feed_purpose=item.offering.feed_purpose,
            reporting_profile=item.offering.reporting_profile,
            media_buy_ids=tuple(sorted(item.media_buy_ids)),
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
            item.period_start,
            item.period_end,
            item.offering_id.encode("utf-8"),
            item.delivery_config_version,
        )
    )
    return periods


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


def _pair(
    previous: Mapping[str, _SlotState], slots: Mapping[str, _Slot], at: datetime
) -> dict[str, _SlotState]:
    """Apply the cutover rule between consecutive segments of one product."""
    leaving = [state for key, state in previous.items() if key not in slots]
    current: dict[str, _SlotState] = {}
    for key, slot in slots.items():
        before = previous.get(key)
        if before is not None:
            if before.slot.same_feed(slot):
                lower = max(at, before.lower)
            else:
                lower = max(first_boundary_at_or_after(before.slot.schedule, at), before.lower)
        elif leaving:
            lower = max(
                max(first_boundary_at_or_after(state.slot.schedule, at), state.lower)
                for state in leaving
            )
        else:
            lower = at
        current[key] = _SlotState(slot, lower)
    return current


def _collect(
    owed: dict[tuple[int, str, str, datetime], _Owed],
    state: _SlotState,
    *,
    buy: SubscriptionBuy,
    floor: datetime,
    starts_before: datetime,
    now: datetime,
    include_unresolved: bool,
    removals: Sequence[SubscriptionException],
    losses: Sequence[_Loss],
    backfill: bool,
) -> None:
    slot = state.slot
    schedule = slot.schedule
    lower = max(state.lower, floor)
    # Skip straight to the period containing the earliest flight start; a
    # period that ends at or before every flight start can never overlap.
    earliest = buy._earliest_start()
    if earliest > lower:
        lower = max(lower, last_boundary_at_or_before(schedule, earliest))
    if lower >= starts_before:
        return
    sla = delivery_sla(schedule)
    gates = [
        (
            loss,
            last_boundary_at_or_before(schedule, loss.unauthorized_at),
            (
                None
                if loss.regranted_at is None
                else first_boundary_at_or_after(schedule, loss.regranted_at)
            ),
        )
        for loss in losses
    ]
    for start, end in iter_periods(schedule, starts_from=lower, starts_before=starts_before):
        if not include_unresolved and end > now:
            break
        flight_start, flight_stop = buy.flight_at(end)
        if not (start < flight_stop and end > flight_start):
            continue
        if any(item.removes(start, buy.media_buy_id) for item in removals):
            continue
        expected_at: datetime | None = end + sla
        withheld = False
        removed = False
        for loss, gap_from, gap_until in gates:
            if start >= gap_from:
                if gap_until is None:
                    removed = True
                    break
                if start < gap_until:
                    if backfill and not loss.expired:
                        assert loss.regranted_at is not None
                        expected_at = _later(expected_at, loss.regranted_at + sla)
                    else:
                        removed = True
                        break
            elif expected_at is not None and expected_at > loss.unauthorized_at:
                # Owed before the loss but not yet due when authority ended:
                # withheld, and due again only on a retained re-grant.
                if loss.regranted_at is None or loss.expired:
                    expected_at, withheld = None, True
                else:
                    expected_at = _later(expected_at, loss.regranted_at + sla)
        if removed:
            continue
        key = (slot.version, slot.offering.offering_id, slot.zone, start)
        entry = owed.get(key)
        if entry is None:
            entry = owed[key] = _Owed(
                slot.version, slot.offering, slot.zone, start, end, expected_at, withheld
            )
        entry.media_buy_ids.add(buy.media_buy_id)


def _later(current: datetime | None, candidate: datetime) -> datetime | None:
    return None if current is None else max(current, candidate)


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
