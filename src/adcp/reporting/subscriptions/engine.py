"""Seller runtime for principal-level reporting subscriptions (experimental).

:class:`ReportingSubscriptionEngine` turns accepted subscriptions into what the
seller owes, using only facts the host alone knows:

* :meth:`ReportingSubscriptionHost.list_authorized_accounts`: the accounts a
  principal may read, paged. The SDK never infers authority.
* :meth:`ReportingSubscriptionEngine.authorization_changed` (or
  :meth:`~ReportingSubscriptionEngine.reconcile`, which diffs the host's
  answer) records grants and losses, so the SDK can stamp ``unauthorized_at``
  and re-grants.
* :meth:`ReportingSubscriptionHost.account_buys`: buy facts per account
  (:class:`~adcp.reporting.subscriptions.SubscriptionBuy`), including each
  package's ``reporting_delivery_offering_ids`` and earlier flights.
* Optionally :meth:`ReportingSubscriptionHost.account_delivery_configs`: the
  caller's own active ``sync_accounts`` configurations, for precedence.

From those the engine records exceptions as cutover intervals (precedence,
``max_accounts`` capacity, ``exclude_account_ids``, unsupported offerings),
tracks lost authority, serves ``subscription_coverage[]`` for
``get_reporting_status``, keeps ``accounts_with_exceptions[]`` and
``accounts_unauthorized[]`` current in the ``get_principal`` readback, and
computes owed periods with the same
:func:`~adcp.reporting.subscriptions.expected_subscription_periods` a buyer
runs, so both sides agree period by period.

``max_accounts`` order
    Among accounts eligible to be served (in scope, authorized, not excluded
    or overridden), accounts already served keep their slots; the remaining
    slots go to the others in ``account_id`` byte order. Accounts beyond the
    limit get a ``capacity`` exception. A newly granted account never
    displaces one being served. This matches the TypeScript SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from adcp.reporting.subscriptions._clock import PeriodCalendar, iso_z, utc_instant
from adcp.reporting.subscriptions.capabilities import ReportingSubscriptionSupport
from adcp.reporting.subscriptions.coverage import (
    AccountTimezoneChange,
    ExpectedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionException,
    SubscriptionVersionWindow,
    expected_subscription_periods,
    subscription_covers_account,
    subscription_version_windows,
)
from adcp.reporting.subscriptions.resolution import (
    SubscriptionDestination,
    SubscriptionOffering,
    SubscriptionSelector,
    resolve_subscription_offerings,
)
from adcp.types.domains.core.reporting_subscription_coverage import (
    ReportingSubscriptionCoverage,
)
from adcp.types.domains.core.reporting_subscription_state import ReportingSubscriptionState

if TYPE_CHECKING:
    from adcp.server.principal import (
        DeliveryConfigIdsHook,
        PrincipalIdentity,
        PrincipalService,
        ReportingSubscriptionOptions,
    )


# -- host facts ----------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedAccount:
    """One account the principal may read, as the host answers it.

    ``authorized_at`` is when the current grant began (the account's
    ``authorization.authorized_at``); ``None`` lets the engine stamp the
    instant it first observes the grant.
    """

    account_id: str
    timezone: str = "UTC"
    operator: str | None = None
    authorized_at: datetime | None = None


@dataclass(frozen=True)
class AuthorizedAccountPage:
    accounts: tuple[AuthorizedAccount, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class AccountDeliveryConfig:
    """The caller's own active ``sync_accounts`` ``reporting_delivery_configs`` entry."""

    delivery_config_id: str
    feed_purpose: str


class ReportingSubscriptionHost(Protocol):
    """Facts only the seller's host knows. The SDK never infers them."""

    async def list_authorized_accounts(
        self, principal: PrincipalIdentity, *, cursor: str | None
    ) -> AuthorizedAccountPage: ...

    async def account_buys(
        self, principal: PrincipalIdentity, account_id: str
    ) -> Sequence[SubscriptionBuy]: ...


class ReportingSubscriptionPrecedenceHost(Protocol):
    """Optional: the caller's own account-level configurations, for precedence."""

    async def account_delivery_configs(
        self, principal: PrincipalIdentity, account_id: str
    ) -> Sequence[AccountDeliveryConfig]: ...


# -- engine state --------------------------------------------------------------


@dataclass
class _Account:
    timezone: str
    operator: str | None
    grants: list[SubscriptionAuthorization] = field(default_factory=list)
    timezone_changes: list[AccountTimezoneChange] = field(default_factory=list)
    initial_timezone: str | None = None

    @property
    def authorized(self) -> bool:
        return bool(self.grants) and self.grants[-1].unauthorized_at is None


@dataclass
class _Exclusion:
    reason: str
    from_: datetime
    until: datetime | None = None
    media_buy_id: str | None = None
    package_ids: tuple[str, ...] = ()
    overridden_by: tuple[str, str] | None = None

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.reason, self.media_buy_id, self.package_ids, self.overridden_by)

    def wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason, "from": iso_z(self.from_)}
        if self.media_buy_id is not None:
            payload["media_buy_id"] = self.media_buy_id
        if self.until is not None:
            payload["until"] = iso_z(self.until)
        if self.package_ids:
            payload["package_ids"] = list(self.package_ids)
        if self.overridden_by is not None:
            payload["overridden_by"] = {self.overridden_by[0]: self.overridden_by[1]}
        return payload


@dataclass
class _PrincipalState:
    accounts: dict[str, _Account] = field(default_factory=dict)
    #: (subscription_id, version) -> configuration
    versions: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    #: (subscription_id, version) -> in-place inactive stretches [from, until)
    inactive: dict[tuple[str, int], list[tuple[datetime, datetime | None]]] = field(
        default_factory=dict
    )
    #: subscription_id -> accounts it ever covered
    covered: dict[str, set[str]] = field(default_factory=dict)
    #: subscription_id -> accounts holding a max_accounts slot
    served: dict[str, set[str]] = field(default_factory=dict)
    #: (subscription_id, account_id) -> exclusions, open and closed
    exclusions: dict[tuple[str, str], list[_Exclusion]] = field(default_factory=dict)


class InMemoryReportingSubscriptionStore:
    """Process-local engine state. Durable storage is a later slice."""

    def __init__(self) -> None:
        self.principals: dict[str, _PrincipalState] = {}
        self.lock = asyncio.Lock()

    def principal(self, subject: str) -> _PrincipalState:
        return self.principals.setdefault(subject, _PrincipalState())


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        assert isinstance(dumped, dict)
        return dumped
    return dict(value)


def _active(state: Mapping[str, Any]) -> bool:
    return state.get("state") != "inactive"


class ReportingSubscriptionEngine:
    """Owed periods, exceptions and lost authority for accepted subscriptions."""

    def __init__(
        self,
        *,
        principals: PrincipalService,
        host: ReportingSubscriptionHost,
        offerings: Sequence[SubscriptionOffering],
        support: ReportingSubscriptionSupport | None = None,
        status_retention_days: int = 400,
        store: InMemoryReportingSubscriptionStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ids = [item.offering_id for item in offerings]
        if len(set(ids)) != len(ids):
            raise ValueError("offering_id values must be unique")
        self._principals = principals
        self._host = host
        self._offerings = tuple(offerings)
        self._support = support or ReportingSubscriptionSupport()
        self._retention = timedelta(days=status_retention_days)
        self._retention_days = status_retention_days
        self._store = store or InMemoryReportingSubscriptionStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def support(self) -> ReportingSubscriptionSupport:
        return self._support

    def principal_options(
        self, *, delivery_config_ids: DeliveryConfigIdsHook | None = None
    ) -> ReportingSubscriptionOptions:
        """Options that link a ``PrincipalService`` to this engine."""
        from adcp.server.principal import ReportingSubscriptionOptions

        return ReportingSubscriptionOptions(
            delivery_config_ids=delivery_config_ids,
            record_versions=self.record_versions,
            clock=self._clock,
        )

    def _now(self, now: datetime | None) -> datetime:
        return utc_instant(now if now is not None else self._clock(), field="now")

    # -- recording -------------------------------------------------------------

    async def record_versions(
        self, identity: PrincipalIdentity, states: Sequence[ReportingSubscriptionState]
    ) -> None:
        """Keep every version's configuration and its in-place inactive stretches.

        The readback keeps only the current configuration, and drops
        ``deactivated_at`` when a version is reactivated in place; the engine
        remembers both so a reactivation is a fresh start, never owing the
        periods that started while the subscription was inactive.
        """
        now = self._now(None)
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            for state in states:
                payload = _dump(state)
                key = (str(payload["subscription_id"]), int(payload["subscription_version"]))
                principal.versions.setdefault(key, dict(payload["configuration"]))
                stretches = principal.inactive.setdefault(key, [])
                open_stretch = bool(stretches) and stretches[-1][1] is None
                if payload.get("state") == "inactive" and not open_stretch:
                    deactivated = payload.get("deactivated_at")
                    start = (
                        utc_instant(datetime.fromisoformat(deactivated.replace("Z", "+00:00")))
                        if isinstance(deactivated, str)
                        else now
                    )
                    stretches.append((start, None))
                elif payload.get("state") != "inactive" and open_stretch:
                    stretches[-1] = (stretches[-1][0], max(now, stretches[-1][0]))

    async def authorization_changed(
        self,
        identity: PrincipalIdentity,
        account_id: str,
        *,
        authorized: bool,
        at: datetime,
        timezone: str | None = None,
        operator: str | None = None,
    ) -> None:
        """Record a grant or a loss of authority at ``at``."""
        instant = utc_instant(at, field="at")
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            account = principal.accounts.get(account_id)
            if account is None:
                if not authorized:
                    return
                account = principal.accounts[account_id] = _Account(
                    timezone=timezone or "UTC", operator=operator, initial_timezone=timezone
                )
            self._apply_grant(account, authorized=authorized, at=instant)
            if timezone is not None and timezone != account.timezone:
                account.timezone_changes.append(AccountTimezoneChange(instant, timezone))
                account.timezone = timezone
            if operator is not None:
                account.operator = operator

    @staticmethod
    def _apply_grant(account: _Account, *, authorized: bool, at: datetime) -> None:
        if authorized and not account.authorized:
            if account.grants and account.grants[-1].unauthorized_at is not None:
                at = max(at, account.grants[-1].unauthorized_at)
            account.grants.append(SubscriptionAuthorization(at))
        elif not authorized and account.authorized:
            last = account.grants[-1]
            account.grants[-1] = SubscriptionAuthorization(
                last.authorized_at, max(at, last.authorized_at + timedelta(microseconds=1))
            )

    async def _list_accounts(self, identity: PrincipalIdentity) -> list[AuthorizedAccount]:
        accounts: list[AuthorizedAccount] = []
        cursor: str | None = None
        while True:
            page = await self._host.list_authorized_accounts(identity, cursor=cursor)
            accounts.extend(page.accounts)
            if not page.next_cursor:
                return accounts
            cursor = page.next_cursor

    # -- reconcile -------------------------------------------------------------

    async def reconcile(self, identity: PrincipalIdentity, *, now: datetime | None = None) -> None:
        """Diff the host's authorized accounts, then re-evaluate every exception.

        Call it on a schedule and whenever the host knows something changed
        (a grant, a ``sync_accounts`` write, a new buy or product offering).
        Exceptions open and close at the first boundary at or after ``now``,
        the cutover instant, and each change to an account's exceptions or
        authority fires ``principal.changed`` with reason
        ``reporting_subscription_changed``.
        """
        instant = self._now(now)
        listed = {item.account_id: item for item in await self._list_accounts(identity)}
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            for account_id, item in listed.items():
                account = principal.accounts.get(account_id)
                if account is None:
                    account = principal.accounts[account_id] = _Account(
                        timezone=item.timezone,
                        operator=item.operator,
                        initial_timezone=item.timezone,
                    )
                if not account.authorized:
                    granted = instant
                    lost = account.grants[-1].unauthorized_at if account.grants else None
                    if item.authorized_at is not None:
                        reported = utc_instant(item.authorized_at)
                        # A host that does not reset authorized_at on a
                        # re-grant reports the old grant; the re-grant is
                        # then the instant this reconcile observes it.
                        if lost is None or reported > lost:
                            granted = min(reported, instant)
                    self._apply_grant(account, authorized=True, at=granted)
                if item.timezone != account.timezone:
                    account.timezone_changes.append(AccountTimezoneChange(instant, item.timezone))
                    account.timezone = item.timezone
                account.operator = item.operator
            for account_id, account in principal.accounts.items():
                if account_id not in listed and account.authorized:
                    self._apply_grant(account, authorized=False, at=instant)
        await self._evaluate(identity, instant)

    async def _evaluate(self, identity: PrincipalIdentity, now: datetime) -> None:
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        destinations = await self._destinations(identity)
        precedence = getattr(self._host, "account_delivery_configs", None)
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            accounts = dict(principal.accounts)
            covered_ids = set().union(*principal.covered.values()) if principal.covered else set()
        buys: dict[str, Sequence[SubscriptionBuy]] = {}
        configs: dict[str, Sequence[AccountDeliveryConfig]] = {}
        for account_id, account in accounts.items():
            if account.authorized or account_id in covered_ids:
                buys[account_id] = await self._host.account_buys(identity, account_id)
            if account.authorized and precedence is not None:
                configs[account_id] = await precedence(identity, account_id)

        updates: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            for state in states:
                subscription_id = str(state["subscription_id"])
                desired = self._desired_exclusions(
                    state,
                    states,
                    accounts,
                    buys,
                    configs,
                    destinations,
                    now,
                    served=principal.served.setdefault(subscription_id, set()),
                )
                covered = principal.covered.setdefault(subscription_id, set())
                for account_id, wanted in desired.items():
                    if not any(item.reason == "excluded_by_buyer" for item in wanted):
                        covered.add(account_id)
                    self._sync_exclusions(
                        principal,
                        subscription_id,
                        account_id,
                        wanted,
                        state,
                        accounts,
                        buys.get(account_id, ()),
                        destinations,
                        now,
                    )
                # Accounts no longer authorized or no longer in scope close
                # their open exclusions.
                for (sub_id, account_id), entries in principal.exclusions.items():
                    if sub_id == subscription_id and account_id not in desired:
                        for entry in entries:
                            if entry.until is None:
                                entry.until = self._cutover(
                                    state, accounts.get(account_id), (), destinations, now
                                )
                updates.append(
                    (
                        subscription_id,
                        self._accounts_with_exceptions(principal, subscription_id, now),
                        self._accounts_unauthorized(
                            principal, state, accounts, buys, destinations, now
                        ),
                    )
                )
        for subscription_id, with_exceptions, unauthorized in updates:
            await self._principals.update_subscription_status(
                identity,
                subscription_id,
                accounts_with_exceptions=with_exceptions,
                accounts_unauthorized=unauthorized,
            )

    async def _destinations(self, identity: PrincipalIdentity) -> dict[str, Any]:
        return {
            item.destination_id: item.configuration
            for item in await self._principals.reporting_destinations(identity)
        }

    def _desired_exclusions(
        self,
        state: Mapping[str, Any],
        states: Sequence[Mapping[str, Any]],
        accounts: Mapping[str, _Account],
        buys: Mapping[str, Sequence[SubscriptionBuy]],
        configs: Mapping[str, Sequence[AccountDeliveryConfig]],
        destinations: Mapping[str, Any],
        now: datetime,
        *,
        served: set[str],
    ) -> dict[str, list[_Exclusion]]:
        """Per account the scope includes: the exclusions that should be open now.

        ``served`` holds the accounts with a ``max_accounts`` slot and is
        updated in place.
        """
        result: dict[str, list[_Exclusion]] = {}
        if not _active(state):
            return result
        configuration = state["configuration"]
        scope = configuration["scope"]
        feed_purpose = configuration["selector"]["feed_purpose"]
        excluded_ids = set(configuration.get("exclude_account_ids") or ())
        eligible: list[tuple[int, bytes, str]] = []
        for account_id, account in accounts.items():
            if not account.authorized:
                continue
            if account_id in excluded_ids:
                if _operator_selected(configuration, account):
                    result[account_id] = [_Exclusion("excluded_by_buyer", now)]
                continue
            if not _scope_selects(configuration, account_id, account):
                continue
            overriding = _override(
                configuration,
                scope,
                feed_purpose,
                account_id,
                account,
                states,
                configs.get(account_id, ()),
            )
            if overriding is not None:
                result[account_id] = [_Exclusion("overridden", now, overridden_by=overriding)]
                continue
            result[account_id] = []
            eligible.append(
                (0 if account_id in served else 1, account_id.encode("utf-8"), account_id)
            )
        limit = self._support.max_accounts
        ranked = [account_id for _held, _key, account_id in sorted(eligible)]
        kept = ranked if limit is None else ranked[:limit]
        served.clear()
        served.update(kept)
        for account_id in ranked[len(kept) :]:
            result[account_id] = [_Exclusion("capacity", now)]
        for account_id, entries in result.items():
            if entries:
                continue
            account = accounts[account_id]
            entries.extend(
                self._unsupported(state, account, buys.get(account_id, ()), destinations, now)
            )
        return result

    def _unsupported(
        self,
        state: Mapping[str, Any],
        account: _Account,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[_Exclusion]:
        selector = SubscriptionSelector.from_subscription(state["configuration"])
        destination = _destination_for(selector, destinations)
        entries: list[_Exclusion] = []
        for buy in sorted(buys, key=lambda item: item.media_buy_id.encode("utf-8")):
            if buy.confirmed_at is None:
                continue
            unresolved: list[str] = []
            products = buy.product_offering_ids()
            for package_id, offering_ids in products:
                resolution = resolve_subscription_offerings(
                    selector,
                    self._offerings,
                    product_offering_ids=offering_ids,
                    destination=destination,
                    at=now,
                )
                if not resolution.offerings:
                    unresolved.append(package_id)
            if len(unresolved) == len(products):
                entries.append(
                    _Exclusion("offering_unsupported", now, media_buy_id=buy.media_buy_id)
                )
            elif unresolved:
                entries.append(
                    _Exclusion(
                        "offering_unsupported",
                        now,
                        media_buy_id=buy.media_buy_id,
                        package_ids=tuple(sorted(unresolved)),
                    )
                )
        return entries

    def _sync_exclusions(
        self,
        principal: _PrincipalState,
        subscription_id: str,
        account_id: str,
        wanted: Sequence[_Exclusion],
        state: Mapping[str, Any],
        accounts: Mapping[str, _Account],
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> None:
        entries = principal.exclusions.setdefault((subscription_id, account_id), [])
        open_keys = {entry.key: entry for entry in entries if entry.until is None}
        wanted_keys = {entry.key for entry in wanted}
        account = accounts.get(account_id)
        cutover: datetime | None = None
        for key, entry in open_keys.items():
            if key not in wanted_keys:
                cutover = cutover or self._cutover(state, account, buys, destinations, now)
                entry.until = cutover
        for entry in wanted:
            if entry.key in open_keys:
                continue
            cutover = cutover or self._cutover(state, account, buys, destinations, now)
            entry.from_ = cutover
            entries.append(entry)

    def _schedules(
        self,
        state: Mapping[str, Any],
        account: _Account | None,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[PeriodCalendar]:
        if account is None:
            return []
        selector = SubscriptionSelector.from_subscription(state["configuration"])
        destination = _destination_for(selector, destinations)
        offering_ids: set[str] = set()
        for buy in buys:
            for _package, ids in buy.product_offering_ids():
                offering_ids.update(ids)
        if not offering_ids:
            offering_ids = {item.offering_id for item in self._offerings}
        resolution = resolve_subscription_offerings(
            selector,
            self._offerings,
            product_offering_ids=offering_ids,
            destination=destination,
            at=now,
        )
        return [
            item.calendar(account.timezone, selector.period_timezone)
            for item in resolution.offerings
        ]

    def _cutover(
        self,
        state: Mapping[str, Any],
        account: _Account | None,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> datetime:
        """E at ``now``: the latest first boundary at or after it among resolved schedules."""
        schedules = self._schedules(state, account, buys, destinations, now)
        if not schedules:
            return now
        return max(calendar.ceil(now) for calendar in schedules)

    def _accounts_with_exceptions(
        self, principal: _PrincipalState, subscription_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        result = []
        for (sub_id, account_id), entries in sorted(principal.exclusions.items()):
            if sub_id != subscription_id:
                continue
            account = principal.accounts.get(account_id)
            if account is None or not account.authorized:
                continue
            reasons = sorted({entry.reason for entry in entries if entry.until is None})
            if reasons:
                result.append({"account_id": account_id, "reasons": reasons})
        return result

    def _accounts_unauthorized(
        self,
        principal: _PrincipalState,
        state: Mapping[str, Any],
        accounts: Mapping[str, _Account],
        buys: Mapping[str, Sequence[SubscriptionBuy]],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        subscription_id = str(state["subscription_id"])
        result = []
        for account_id in sorted(principal.covered.get(subscription_id, ())):
            account = accounts.get(account_id)
            if account is None or account.authorized or not account.grants:
                continue
            lost = account.grants[-1].unauthorized_at
            assert lost is not None
            if now - lost >= self._retention:
                continue
            schedules = self._schedules(state, account, (), destinations, lost)
            owed = self._owed(
                principal,
                state,
                account_id,
                account,
                buys.get(account_id, ()),
                destinations,
                now,
            )
            entry: dict[str, Any] = {
                "account_id": account_id,
                "unauthorized_at": iso_z(lost),
                "withheld_period_count": sum(1 for item in owed if item.withheld),
            }
            if schedules:
                entry["last_owed_period_end"] = iso_z(
                    max(calendar.floor(lost) for calendar in schedules)
                )
            result.append(entry)
        return result

    # -- reads -----------------------------------------------------------------

    async def subscription_coverage(
        self, identity: PrincipalIdentity, account_id: str, *, now: datetime | None = None
    ) -> list[ReportingSubscriptionCoverage]:
        """``subscription_coverage[]`` for one account, while the caller is authorized.

        One entry per caller subscription whose scope includes the account,
        inactive ones included while retained. An unauthorized account is
        unknown here, as for any unauthorized caller.
        """
        instant = self._now(now)
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        destinations = await self._destinations(identity)
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            account = principal.accounts.get(account_id)
            if account is None or not account.authorized:
                return []
            result = []
            for state in states:
                configuration = state["configuration"]
                if not _scope_includes(configuration, account_id, account):
                    continue
                entries = principal.exclusions.get((str(state["subscription_id"]), account_id), [])
                account_level = [e for e in entries if e.until is None and e.media_buy_id is None]
                status = "active"
                if any(e.reason == "action_required" for e in account_level):
                    status = "action_required"
                elif account_level:
                    status = "excluded"
                payload: dict[str, Any] = {
                    "subscription_id": state["subscription_id"],
                    "subscription_version": state["subscription_version"],
                    "status": status,
                    "excluded": [
                        entry.wire()
                        for entry in entries
                        if entry.until is None or instant - entry.until <= self._retention
                    ],
                }
                gaps = self._gaps(state, account, destinations, instant)
                if gaps:
                    payload["gaps"] = gaps
                cutovers = self._timezone_cutovers(state, account, destinations, instant)
                if cutovers:
                    payload["timezone_cutovers"] = cutovers
                result.append(ReportingSubscriptionCoverage.model_validate(payload))
            return result

    def _gaps(
        self,
        state: Mapping[str, Any],
        account: _Account,
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        gaps = []
        for earlier, later in zip(account.grants, account.grants[1:]):
            lost = earlier.unauthorized_at
            assert lost is not None
            regranted = later.authorized_at
            expired = regranted - lost >= self._retention
            if self._support.backfill_on_regrant and not expired:
                continue
            if expired:
                continue  # a fresh coverage start claims no gap
            schedules = self._schedules(state, account, (), destinations, lost)
            if not schedules:
                continue
            until = max(calendar.ceil(regranted) for calendar in schedules)
            if now - until > self._retention:
                continue
            gaps.append(
                {
                    "from": iso_z(min(calendar.floor(lost) for calendar in schedules)),
                    "until": iso_z(until),
                    "reason": "unauthorized",
                }
            )
        return gaps

    def _timezone_cutovers(
        self,
        state: Mapping[str, Any],
        account: _Account,
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        cutovers = []
        zone = account.initial_timezone or account.timezone
        for change in account.timezone_changes:
            selector = SubscriptionSelector.from_subscription(state["configuration"])
            resolution = resolve_subscription_offerings(
                selector,
                self._offerings,
                product_offering_ids=[item.offering_id for item in self._offerings],
                destination=_destination_for(selector, destinations),
                at=change.changed_at,
            )
            schedules = [
                item.calendar(zone, selector.period_timezone)
                for item in resolution.offerings
                if item.alignment == "account_timezone"
            ]
            if schedules and now - change.changed_at <= self._retention:
                effective = max(calendar.ceil(change.changed_at) for calendar in schedules)
                cutovers.append({"effective_at": iso_z(effective), "timezone": change.timezone})
            zone = change.timezone
        return cutovers

    async def owed_periods(
        self,
        identity: PrincipalIdentity,
        subscription_id: str,
        account_id: str,
        *,
        now: datetime | None = None,
    ) -> list[ExpectedSubscriptionPeriod]:
        """Every period this subscription owes the account: the seller's obligations.

        The same :func:`expected_subscription_periods` a buyer runs, over the
        engine's records: every version's configuration, the grant history,
        timezone changes and every recorded exclusion.
        """
        instant = self._now(now)
        state = next(
            (
                _dump(item)
                for item in await self._principals.reporting_subscriptions(identity)
                if item.subscription_id == subscription_id
            ),
            None,
        )
        if state is None:
            raise KeyError(subscription_id)
        destinations = await self._destinations(identity)
        buys = await self._host.account_buys(identity, account_id)
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            account = principal.accounts.get(account_id)
            if account is None:
                return []
            return self._owed(principal, state, account_id, account, buys, destinations, instant)

    def _owed(
        self,
        principal: _PrincipalState,
        state: Mapping[str, Any],
        account_id: str,
        account: _Account,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[ExpectedSubscriptionPeriod]:
        subscription_id = str(state["subscription_id"])
        versions = {
            version: configuration
            for (sub_id, version), configuration in principal.versions.items()
            if sub_id == subscription_id
        }
        exclusions = [
            SubscriptionException(e.reason, e.from_, e.until, e.media_buy_id)
            for e in principal.exclusions.get((subscription_id, account_id), [])
            if not e.package_ids
        ]
        windows: tuple[SubscriptionVersionWindow, ...] = tuple(
            replace(
                window,
                inactive_intervals=tuple(
                    (start, until)
                    for start, until in principal.inactive.get(
                        (subscription_id, window.subscription_version), ()
                    )
                    if until is not None
                ),
            )
            for window in subscription_version_windows(state, version_configurations=versions)
        )
        return expected_subscription_periods(
            subscription=windows,
            account_id=account_id,
            buys=buys,
            offerings=self._offerings,
            account_timezone=account.initial_timezone or account.timezone,
            now=now,
            authorizations=list(account.grants),
            exceptions=exclusions,
            backfill_on_regrant=self._support.backfill_on_regrant,
            status_retention_days=self._retention_days,
            destinations=destinations,
            account_operator=account.operator,
            timezone_changes=list(account.timezone_changes),
        )

    async def receipt_roster(self, identity: PrincipalIdentity, account_id: str) -> list[str]:
        """Active ``consumer_receipt`` subscriptions covering the account.

        A consumer owes receipts for an account while one of its active
        ``consumer_receipt`` subscriptions covers it without an open
        account-level exception.
        """
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        async with self._store.lock:
            principal = self._store.principal(identity.subject)
            account = principal.accounts.get(account_id)
            if account is None or not account.authorized:
                return []
            result = []
            for state in states:
                configuration = state["configuration"]
                if (
                    not _active(state)
                    or configuration["reconciliation_mode"] != "consumer_receipt"
                    or not _scope_includes(configuration, account_id, account)
                ):
                    continue
                entries = principal.exclusions.get((str(state["subscription_id"]), account_id), [])
                if any(e.until is None and e.media_buy_id is None for e in entries):
                    continue
                result.append(str(state["subscription_id"]))
            return sorted(result)


# -- helpers -------------------------------------------------------------------


def _operator_selected(configuration: Mapping[str, Any], account: _Account) -> bool:
    domains = configuration.get("operator_domains")
    return not domains or account.operator in set(domains)


def _scope_selects(configuration: Mapping[str, Any], account_id: str, account: _Account) -> bool:
    try:
        return subscription_covers_account(
            configuration, account_id=account_id, account_operator=account.operator
        )
    except ValueError:
        return False  # operator_domains with an account the host gave no operator


def _scope_includes(configuration: Mapping[str, Any], account_id: str, account: _Account) -> bool:
    """Whether the scope names the account, counting buyer exclusions as included."""
    excluded = set(configuration.get("exclude_account_ids") or ())
    if account_id in excluded:
        return _operator_selected(configuration, account)
    return _scope_selects(configuration, account_id, account)


def _override(
    configuration: Mapping[str, Any],
    scope: Mapping[str, Any],
    feed_purpose: str,
    account_id: str,
    account: _Account,
    states: Sequence[Mapping[str, Any]],
    configs: Sequence[AccountDeliveryConfig],
) -> tuple[str, str] | None:
    """The caller's own registration that takes this account and purpose, if any."""
    for config in sorted(configs, key=lambda item: item.delivery_config_id.encode("utf-8")):
        if config.feed_purpose == feed_purpose:
            return ("delivery_config_id", config.delivery_config_id)
    if "all_authorized_accounts" in scope:
        for other in states:
            other_config = other["configuration"]
            if (
                _active(other)
                and "account_ids" in other_config["scope"]
                and other_config["selector"]["feed_purpose"] == feed_purpose
                and _scope_selects(other_config, account_id, account)
            ):
                return ("subscription_id", str(other["subscription_id"]))
    return None


def _destination_for(
    selector: SubscriptionSelector, destinations: Mapping[str, Any]
) -> SubscriptionDestination | None:
    if selector.destination_id is None:
        return None
    target = destinations.get(selector.destination_id)
    return None if target is None else SubscriptionDestination.coerce(target)


__all__ = [
    "AccountDeliveryConfig",
    "AuthorizedAccount",
    "AuthorizedAccountPage",
    "InMemoryReportingSubscriptionStore",
    "ReportingSubscriptionEngine",
    "ReportingSubscriptionHost",
    "ReportingSubscriptionPrecedenceHost",
]
