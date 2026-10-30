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
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from adcp.reporting.subscriptions._clock import PeriodCalendar, iso_z, require_zone, utc_instant
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
from adcp.reporting.subscriptions.wire import project_subscription_coverage
from adcp.types.domains.core.reporting_subscription_coverage import (
    ReportingSubscriptionCoverage,
)
from adcp.types.domains.core.reporting_subscription_state import ReportingSubscriptionState

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Attempts at a compare-and-swap write before giving up under contention.
_MAX_STATE_WRITE_ATTEMPTS = 8

if TYPE_CHECKING:
    from adcp.reporting.subscriptions.ledger import FrozenOwedSet
    from adcp.server.principal import (
        DeliveryConfigIdsHook,
        PrincipalIdentity,
        PrincipalService,
        ReportingSubscriptionOptions,
    )


# -- host facts ----------------------------------------------------------------


#: The task whose grant carries subscription coverage.
REPORTING_STATUS_TASK = "get_reporting_status"


@dataclass(frozen=True)
class AuthorizedAccount:
    """One account the principal may read, as the host answers it.

    ``timezone`` is the account's IANA zone; the engine never substitutes one.
    ``authorized_at`` is the start of the caller's current authorization chain,
    exactly what ``list_accounts`` serves as ``authorization.authorized_at``
    (sellers advertising subscriptions MUST populate it), so the buyer and the
    engine start coverage at the same instant.
    """

    account_id: str
    timezone: str
    authorized_at: datetime
    operator: str | None = None
    #: The grant's ``authorization.allowed_tasks``; ``None`` when the host does
    #: not scope tasks. Access means access: coverage continues only while the
    #: grant lists ``get_reporting_status``, and every reporting task is checked
    #: against the grant. A seller that wants a buyer to finish reconciliation
    #: keeps a reduced grant (for example ``get_reporting_status``,
    #: ``sync_reporting_receipts`` and ``sync_reporting_status``) rather than
    #: revoking; after revocation the account is unknown to every task.
    allowed_tasks: frozenset[str] | None = None

    def __post_init__(self) -> None:
        require_zone(self.timezone, field="AuthorizedAccount.timezone")
        if self.allowed_tasks is not None:
            object.__setattr__(self, "allowed_tasks", frozenset(self.allowed_tasks))
        object.__setattr__(
            self, "authorized_at", utc_instant(self.authorized_at, field="authorized_at")
        )


@dataclass(frozen=True)
class AuthorizedAccountPage:
    accounts: tuple[AuthorizedAccount, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class AccountDeliveryConfig:
    """The caller's own active ``sync_accounts`` ``reporting_delivery_configs`` entry.

    ``active_from`` is when it took effect. The ``overridden`` exception opens
    at the cutover at that instant rather than at the reconcile that first sees
    it, so the two registrations never both owe a period. Without it, run
    :meth:`ReportingSubscriptionEngine.reconcile` synchronously with every
    ``sync_accounts`` write.
    """

    delivery_config_id: str
    feed_purpose: str
    active_from: datetime | None = None


class ReportingSubscriptionHost(Protocol):
    """Facts only the seller's host knows. The SDK never infers them."""

    async def list_authorized_accounts(
        self, principal: PrincipalIdentity, *, cursor: str | None
    ) -> AuthorizedAccountPage: ...

    async def account_buys(
        self, principal: PrincipalIdentity, account_id: str
    ) -> Sequence[SubscriptionBuy]: ...


class ReportingSubscriptionDeliveryHost(Protocol):
    """Optional: delivery facts, for an exact ``withheld_period_count``.

    Without it the engine counts the owed periods not yet due
    (``period_end + delivery_sla``) when authority ended, as the TypeScript SDK
    does. With it, a period is withheld when it ended at or before
    ``unauthorized_at`` and was not delivered by then.
    """

    async def is_period_delivered(
        self,
        principal: PrincipalIdentity,
        account_id: str,
        period: ExpectedSubscriptionPeriod,
        *,
        by: datetime,
    ) -> bool: ...


class ReportingSubscriptionPrecedenceHost(Protocol):
    """Optional: the caller's own account-level configurations, for precedence."""

    async def account_delivery_configs(
        self, principal: PrincipalIdentity, account_id: str
    ) -> Sequence[AccountDeliveryConfig]: ...


# -- engine state --------------------------------------------------------------


@dataclass
class _Account:
    #: ``None`` while the host has not said; account-timezone offerings then
    #: fail for this account (isolated per subscription) rather than use UTC.
    timezone: str | None
    operator: str | None
    grants: list[SubscriptionAuthorization] = field(default_factory=list)
    timezone_changes: list[AccountTimezoneChange] = field(default_factory=list)
    #: The current grant's ``allowed_tasks``; ``None`` when the host does not scope tasks.
    allowed_tasks: frozenset[str] | None = None

    def grants_task(self, task: str) -> bool:
        return self.authorized and (self.allowed_tasks is None or task in self.allowed_tasks)

    def set_timezone(self, timezone: str | None, at: datetime) -> None:
        """Record a zone; a change away from an unknown zone is not a cutover."""
        if timezone is None or timezone == self.timezone:
            return
        if self.timezone is not None:
            self.timezone_changes.append(
                AccountTimezoneChange(at, timezone, previous_timezone=self.timezone)
            )
        self.timezone = timezone

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
    #: When the cause took effect, if known; the exception opens at its cutover.
    effective_at: datetime | None = None

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
    #: subscription_id -> accounts it ever covered
    covered: dict[str, set[str]] = field(default_factory=dict)
    #: subscription_id -> accounts holding a max_accounts slot
    served: dict[str, set[str]] = field(default_factory=dict)
    #: (subscription_id, account_id) -> exclusions, open and closed
    exclusions: dict[tuple[str, str], list[_Exclusion]] = field(default_factory=dict)
    #: (subscription_id, version) -> the named destination's configuration,
    #: snapshotted at acceptance so a later removal never breaks old versions
    destinations: dict[tuple[str, int], Any] = field(default_factory=dict)
    #: (subscription_id, account_id, unauthorized_at) -> withheld count from the
    #: host's delivery facts; delivery by a past instant never changes
    withheld: dict[tuple[str, str, datetime], int] = field(default_factory=dict)


#: Version of the persisted engine-state document; bump on a breaking change.
STATE_DOCUMENT_VERSION = 1


@dataclass(frozen=True)
class StoredReportingSubscriptionState:
    """One principal's persisted engine state and its store revision."""

    document: Mapping[str, Any]
    revision: int


class ReportingSubscriptionStateStore(Protocol):
    """Durable engine state: one JSON document per principal, written by CAS.

    The engine loads a principal's document, applies a change, and writes it
    back with :meth:`compare_and_swap`; on a conflict it reloads and reapplies
    the change, so every worker sharing the backing store sees one history.
    The document holds grants and losses, timezone changes, version
    configurations and destination snapshots, ``max_accounts`` slots,
    exceptions and withheld-period counts. It is plain JSON; stores persist it
    verbatim and never interpret it.
    """

    async def load(self, subject: str) -> StoredReportingSubscriptionState | None:
        """Return the principal's document and revision, or ``None`` if none is stored."""
        ...

    async def compare_and_swap(
        self,
        subject: str,
        expected_revision: int | None,
        document: Mapping[str, Any],
    ) -> bool:
        """Store ``document`` if the current revision is ``expected_revision``.

        ``expected_revision=None`` inserts only when nothing is stored. A
        successful write sets the revision to ``expected_revision + 1`` (``1``
        for an insert) and returns ``True``; any other current state returns
        ``False`` and changes nothing.
        """
        ...


class InMemoryReportingSubscriptionStore:
    """Process-local :class:`ReportingSubscriptionStateStore` for tests and single workers.

    State is lost on restart. Production sellers use a durable store, such as
    ``adcp_reporting_storage``'s PostgreSQL implementation.
    """

    def __init__(self) -> None:
        self._documents: dict[str, StoredReportingSubscriptionState] = {}
        self._lock = asyncio.Lock()

    async def load(self, subject: str) -> StoredReportingSubscriptionState | None:
        async with self._lock:
            stored = self._documents.get(subject)
            if stored is None:
                return None
            return StoredReportingSubscriptionState(_copy(stored.document), stored.revision)

    async def compare_and_swap(
        self,
        subject: str,
        expected_revision: int | None,
        document: Mapping[str, Any],
    ) -> bool:
        async with self._lock:
            current = self._documents.get(subject)
            if (current.revision if current is not None else None) != expected_revision:
                return False
            self._documents[subject] = StoredReportingSubscriptionState(
                _copy(document), 1 if expected_revision is None else expected_revision + 1
            )
            return True


class ReportingSubscriptionStateConflictError(RuntimeError):
    """The engine could not write its state after repeated concurrent changes."""


def _copy(document: Mapping[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(document))
    assert isinstance(copied, dict)
    return copied


# -- state document codec ------------------------------------------------------


def _instant(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


def _required_instant(value: str) -> datetime:
    instant = _instant(value)
    assert instant is not None
    return instant


def _iso(value: datetime | None) -> str | None:
    return None if value is None else iso_z(value)


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return json.loads(json.dumps(value))


def encode_principal_state(
    principal: _PrincipalState, *, prune_closed_before: datetime | None = None
) -> dict[str, Any]:
    """Serialize engine state to a JSON document.

    Exceptions closed before ``prune_closed_before`` are dropped: the spec
    lets a seller forget them ``status_retention_days`` after they close.
    Version configurations and destination snapshots are never pruned, so
    ``prior_versions`` stay answerable for as long as obligations cite them.
    """
    exclusions: list[dict[str, Any]] = []
    for (subscription_id, account_id), entries in sorted(principal.exclusions.items()):
        kept = [
            entry
            for entry in entries
            if prune_closed_before is None
            or entry.until is None
            or entry.until >= prune_closed_before
        ]
        exclusions.append(
            {
                "subscription_id": subscription_id,
                "account_id": account_id,
                "entries": [
                    {
                        "reason": entry.reason,
                        "from": iso_z(entry.from_),
                        "until": _iso(entry.until),
                        "media_buy_id": entry.media_buy_id,
                        "package_ids": list(entry.package_ids),
                        "overridden_by": (
                            None if entry.overridden_by is None else list(entry.overridden_by)
                        ),
                        "effective_at": _iso(entry.effective_at),
                    }
                    for entry in kept
                ],
            }
        )
    return {
        "document_version": STATE_DOCUMENT_VERSION,
        "accounts": {
            account_id: {
                "timezone": account.timezone,
                "operator": account.operator,
                # Present only when the host scopes tasks, so documents written
                # before allowed_tasks existed stay byte-identical.
                **(
                    {"allowed_tasks": sorted(account.allowed_tasks)}
                    if account.allowed_tasks is not None
                    else {}
                ),
                "grants": [
                    {
                        "authorized_at": iso_z(grant.authorized_at),
                        "unauthorized_at": _iso(grant.unauthorized_at),
                    }
                    for grant in account.grants
                ],
                "timezone_changes": [
                    {
                        "changed_at": iso_z(change.changed_at),
                        "timezone": change.timezone,
                        "offering_id": change.offering_id,
                        "previous_timezone": change.previous_timezone,
                    }
                    for change in account.timezone_changes
                ],
            }
            for account_id, account in sorted(principal.accounts.items())
        },
        "versions": [
            {"subscription_id": sid, "version": version, "configuration": _json_value(config)}
            for (sid, version), config in sorted(principal.versions.items())
        ],
        "covered": {sid: sorted(ids) for sid, ids in sorted(principal.covered.items())},
        "served": {sid: sorted(ids) for sid, ids in sorted(principal.served.items())},
        "exclusions": exclusions,
        "destinations": [
            {"subscription_id": sid, "version": version, "destination": _json_value(target)}
            for (sid, version), target in sorted(principal.destinations.items())
        ],
        "withheld": [
            {
                "subscription_id": sid,
                "account_id": account_id,
                "unauthorized_at": iso_z(lost),
                "count": count,
            }
            for (sid, account_id, lost), count in sorted(principal.withheld.items())
        ],
    }


def decode_principal_state(document: Mapping[str, Any]) -> _PrincipalState:
    """Rebuild engine state from :func:`encode_principal_state` output."""
    version = document.get("document_version")
    if version != STATE_DOCUMENT_VERSION:
        raise ValueError(f"unsupported reporting-subscription state document version {version!r}")
    principal = _PrincipalState()
    for account_id, raw in document["accounts"].items():
        principal.accounts[account_id] = _Account(
            timezone=raw["timezone"],
            operator=raw["operator"],
            allowed_tasks=(
                frozenset(raw["allowed_tasks"]) if raw.get("allowed_tasks") is not None else None
            ),
            grants=[
                SubscriptionAuthorization(
                    _required_instant(grant["authorized_at"]), _instant(grant["unauthorized_at"])
                )
                for grant in raw["grants"]
            ],
            timezone_changes=[
                AccountTimezoneChange(
                    _required_instant(change["changed_at"]),
                    change["timezone"],
                    offering_id=change["offering_id"],
                    previous_timezone=change["previous_timezone"],
                )
                for change in raw["timezone_changes"]
            ],
        )
    for item in document["versions"]:
        principal.versions[(item["subscription_id"], int(item["version"]))] = dict(
            item["configuration"]
        )
    principal.covered = {sid: set(ids) for sid, ids in document["covered"].items()}
    principal.served = {sid: set(ids) for sid, ids in document["served"].items()}
    for item in document["exclusions"]:
        principal.exclusions[(item["subscription_id"], item["account_id"])] = [
            _Exclusion(
                reason=entry["reason"],
                from_=_required_instant(entry["from"]),
                until=_instant(entry["until"]),
                media_buy_id=entry["media_buy_id"],
                package_ids=tuple(entry["package_ids"]),
                overridden_by=(
                    None
                    if entry["overridden_by"] is None
                    else (entry["overridden_by"][0], entry["overridden_by"][1])
                ),
                effective_at=_instant(entry["effective_at"]),
            )
            for entry in item["entries"]
        ]
    for item in document["destinations"]:
        principal.destinations[(item["subscription_id"], int(item["version"]))] = item[
            "destination"
        ]
    for item in document["withheld"]:
        principal.withheld[
            (
                item["subscription_id"],
                item["account_id"],
                _required_instant(item["unauthorized_at"]),
            )
        ] = int(item["count"])
    return principal


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
        store: ReportingSubscriptionStateStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        ids = [item.offering_id for item in offerings]
        if len(set(ids)) != len(ids):
            raise ValueError("offering_id values must be unique")
        for offering in offerings:
            # Refuse what would otherwise fail mid-evaluation for every
            # subscription that resolves the offering.
            if offering.alignment == "source_timezone" and offering.period_timezone is None:
                raise ValueError(
                    f"offering {offering.offering_id!r} resolves its source timezone per "
                    "account; register it with the resolved period_timezone"
                )
            if offering.alignment != "account_timezone":
                offering.calendar(None)
            else:
                offering.calendar("UTC")  # validates the duration for a local alignment
        self._principals = principals
        self._host = host
        self._offerings = tuple(offerings)
        self._support = support or ReportingSubscriptionSupport()
        self._retention = timedelta(days=status_retention_days)
        self._retention_days = status_retention_days
        self._store = store or InMemoryReportingSubscriptionStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._errors: dict[tuple[str, str], Exception] = {}

    @property
    def evaluation_errors(self) -> Mapping[tuple[str, str], Exception]:
        """``(principal subject, subscription_id)`` -> the last evaluation failure.

        One subscription that cannot be evaluated never stops the others; its
        failure stays here until a later reconcile succeeds for it.
        """
        return dict(self._errors)

    @property
    def support(self) -> ReportingSubscriptionSupport:
        return self._support

    @property
    def offerings(self) -> tuple[SubscriptionOffering, ...]:
        """The offerings subscriptions resolve against, with their dimension mappings."""
        return self._offerings

    @property
    def status_retention_days(self) -> int:
        return self._retention_days

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

    async def _read(self, subject: str) -> _PrincipalState:
        stored = await self._store.load(subject)
        return _PrincipalState() if stored is None else decode_principal_state(stored.document)

    async def _mutate(
        self,
        subject: str,
        change: Callable[[_PrincipalState], _T],
        *,
        now: datetime | None = None,
    ) -> _T:
        """Apply ``change`` to the stored state and write it back by CAS.

        ``change`` may run more than once: on a conflicting concurrent write
        the state is reloaded and ``change`` reapplied. Nothing is written
        when the document is unchanged.
        """
        prune = None if now is None else now - self._retention
        for _attempt in range(_MAX_STATE_WRITE_ATTEMPTS):
            stored = await self._store.load(subject)
            principal = (
                _PrincipalState() if stored is None else decode_principal_state(stored.document)
            )
            result = change(principal)
            document = encode_principal_state(principal, prune_closed_before=prune)
            if stored is not None and document == stored.document:
                return result
            if await self._store.compare_and_swap(
                subject, None if stored is None else stored.revision, document
            ):
                return result
        raise ReportingSubscriptionStateConflictError(
            f"reporting-subscription state for {subject!r} kept changing concurrently"
        )

    # -- recording -------------------------------------------------------------

    async def record_versions(
        self, identity: PrincipalIdentity, states: Sequence[ReportingSubscriptionState]
    ) -> None:
        """Keep every version's configuration; the readback keeps only the current one.

        In-place reactivations need no engine state: the readback's
        ``inactive_intervals`` (adcp#8585) carry them. Each version's named
        destination is snapshotted, so removing it later never breaks that
        version's coverage.
        """
        destinations = await self._destinations(identity)
        payloads = [_dump(state) for state in states]

        def change(principal: _PrincipalState) -> None:
            for payload in payloads:
                key = (str(payload["subscription_id"]), int(payload["subscription_version"]))
                configuration = dict(payload["configuration"])
                principal.versions.setdefault(key, configuration)
                destination = configuration.get("destination")
                if destination is not None and key not in principal.destinations:
                    target = destinations.get(destination["destination_id"])
                    if target is not None:
                        principal.destinations[key] = _json_value(target)

        await self._mutate(identity.subject, change)

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
        if timezone is not None:
            require_zone(timezone, field="timezone")

        def change(principal: _PrincipalState) -> None:
            account = principal.accounts.get(account_id)
            if account is None:
                if not authorized:
                    return
                account = principal.accounts[account_id] = _Account(
                    timezone=timezone, operator=operator
                )
            self._apply_grant(account, authorized=authorized, at=instant)
            account.set_timezone(timezone, instant)
            if operator is not None:
                account.operator = operator

        await self._mutate(identity.subject, change, now=instant)

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
        # A grant that no longer lists get_reporting_status ends coverage, as a
        # revocation does: access means access.
        listed = {
            item.account_id: item
            for item in await self._list_accounts(identity)
            if item.allowed_tasks is None or REPORTING_STATUS_TASK in item.allowed_tasks
        }

        def change(principal: _PrincipalState) -> None:
            for account_id, item in listed.items():
                account = principal.accounts.get(account_id)
                if account is None:
                    account = principal.accounts[account_id] = _Account(
                        timezone=item.timezone, operator=item.operator
                    )
                if not account.authorized:
                    lost = account.grants[-1].unauthorized_at if account.grants else None
                    reported = item.authorized_at
                    # adcp#8585: authorized_at is the chain start. A later one
                    # is a new chain starting then; an unchanged one is a
                    # re-grant within retention, which the wire does not
                    # date, so it is stamped when this reconcile observes it.
                    if lost is None or reported > lost:
                        granted = min(reported, instant)
                    else:
                        granted = instant
                    self._apply_grant(account, authorized=True, at=granted)
                account.set_timezone(item.timezone, instant)
                account.operator = item.operator
                account.allowed_tasks = item.allowed_tasks
            for account_id, account in principal.accounts.items():
                if account_id not in listed and account.authorized:
                    self._apply_grant(account, authorized=False, at=instant)
                if account_id not in listed:
                    account.allowed_tasks = None

        await self._mutate(identity.subject, change, now=instant)
        await self._evaluate(identity, instant)

    async def _evaluate(self, identity: PrincipalIdentity, now: datetime) -> None:
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        destinations = await self._destinations(identity)
        precedence = getattr(self._host, "account_delivery_configs", None)
        snapshot = await self._read(identity.subject)
        accounts = dict(snapshot.accounts)
        covered_ids = set().union(*snapshot.covered.values()) if snapshot.covered else set()
        buys: dict[str, Sequence[SubscriptionBuy]] = {}
        configs: dict[str, Sequence[AccountDeliveryConfig]] = {}
        for account_id, account in accounts.items():
            if account.authorized or account_id in covered_ids:
                buys[account_id] = await self._host.account_buys(identity, account_id)
            if account.authorized and precedence is not None:
                configs[account_id] = await precedence(identity, account_id)

        def change(
            principal: _PrincipalState,
        ) -> list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]]:
            evaluated: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
            for state in states:
                subscription_id = str(state["subscription_id"])
                try:
                    evaluated.append(
                        self._evaluate_subscription(
                            principal, state, states, accounts, buys, configs, destinations, now
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolate one subscription
                    self._errors[(identity.subject, subscription_id)] = error
                    logger.warning(
                        "reporting subscription %s could not be evaluated: %s",
                        subscription_id,
                        error,
                    )
                else:
                    self._errors.pop((identity.subject, subscription_id), None)
            return evaluated

        updates = await self._mutate(identity.subject, change, now=now)
        delivered = getattr(self._host, "is_period_delivered", None)
        for subscription_id, _with_exceptions, unauthorized in updates:
            for lost_entry in unauthorized:
                ended = lost_entry.pop("_ended")
                if delivered is None:
                    continue
                lost = datetime.fromisoformat(lost_entry["unauthorized_at"].replace("Z", "+00:00"))
                cache_key = (subscription_id, lost_entry["account_id"], lost)
                cached = (await self._read(identity.subject)).withheld.get(cache_key)
                if cached is None:
                    # Delivery by a past instant never changes: ask once per loss.
                    count = 0
                    for period in ended:
                        if not await delivered(identity, lost_entry["account_id"], period, by=lost):
                            count += 1

                    def record(
                        principal: _PrincipalState,
                        key: tuple[str, str, datetime] = cache_key,
                        count: int = count,
                    ) -> int:
                        return principal.withheld.setdefault(key, count)

                    cached = await self._mutate(identity.subject, record)
                lost_entry["withheld_period_count"] = cached
        for subscription_id, with_exceptions, unauthorized in updates:
            await self._principals.update_subscription_status(
                identity,
                subscription_id,
                accounts_with_exceptions=with_exceptions,
                accounts_unauthorized=unauthorized,
                changed_at=now,
            )

    def _evaluate_subscription(
        self,
        principal: _PrincipalState,
        state: Mapping[str, Any],
        states: Sequence[Mapping[str, Any]],
        accounts: Mapping[str, _Account],
        buys: Mapping[str, Sequence[SubscriptionBuy]],
        configs: Mapping[str, Sequence[AccountDeliveryConfig]],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
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
            # Covered: served, with no account-level exception.
            if not any(item.media_buy_id is None for item in wanted):
                covered.add(account_id)
                # Surface an account the schedules cannot be derived for (no
                # timezone yet) now, rather than at the first owed_periods.
                self._schedules(
                    state, accounts[account_id], buys.get(account_id, ()), destinations, now
                )
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
        # Exceptions close only when their cause ends. While the caller has
        # lost authority for an account they stay as they were: closing them
        # would owe the revoked interval again on a backfilled re-grant.
        for (sub_id, account_id), entries in principal.exclusions.items():
            if sub_id != subscription_id or account_id in desired:
                continue
            account = accounts.get(account_id)
            if account is not None and not account.authorized:
                continue
            for entry in entries:
                if entry.until is None:
                    entry.until = self._cutover(
                        state, account, buys.get(account_id, ()), destinations, now
                    )
        return (
            subscription_id,
            self._accounts_with_exceptions(principal, subscription_id, now),
            self._accounts_unauthorized(principal, state, accounts, buys, destinations, now),
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
                kind, identifier, effective_at = overriding
                result[account_id] = [
                    _Exclusion(
                        "overridden",
                        now,
                        overridden_by=(kind, identifier),
                        effective_at=effective_at,
                    )
                ]
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
            if entry.effective_at is not None and entry.effective_at <= now:
                entry.from_ = self._cutover(state, account, buys, destinations, entry.effective_at)
            else:
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
    ) -> list[tuple[str, PeriodCalendar]]:
        """``(offering_id, calendar)`` for each offering the account resolves to."""
        if account is None:
            return []
        selector = SubscriptionSelector.from_subscription(state["configuration"])
        destination = _destination_for(selector, destinations)
        products = [ids for buy in buys for _package, ids in buy.product_offering_ids()]
        if not products:
            products = [tuple(item.offering_id for item in self._offerings)]
        found: dict[str, SubscriptionOffering] = {}
        for offering_ids in products:
            # Per product, as the coverage rule resolves it.
            for item in resolve_subscription_offerings(
                selector,
                self._offerings,
                product_offering_ids=offering_ids,
                destination=destination,
                at=now,
            ).offerings:
                found[item.offering_id] = item
        return [
            (item.offering_id, item.calendar(account.timezone, selector.period_timezone))
            for item in sorted(found.values(), key=lambda value: value.offering_id.encode())
        ]

    def _cutover(
        self,
        state: Mapping[str, Any],
        account: _Account | None,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> datetime:
        """The cutover instant for an account-level exception changing at ``now``.

        The earliest E among the resolved schedules: no schedule has a boundary
        in ``[now, E)``, so nothing is owed twice and nothing is lost, whereas
        the latest E would leave a finer schedule owing periods after the
        change.
        """
        schedules = self._schedules(state, account, buys, destinations, now)
        if not schedules:
            return now
        return min(calendar.ceil(now) for _offering_id, calendar in schedules)

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
                # Owed periods that ended by the loss, for a delivery hook.
                "_ended": [
                    item
                    for item in owed
                    if datetime.fromisoformat(item.period_end.replace("Z", "+00:00")) <= lost
                ],
            }
            if schedules:
                entry["last_owed_period_end"] = iso_z(
                    max(calendar.floor(lost) for _offering_id, calendar in schedules)
                )
            result.append(entry)
        return result

    # -- reads -----------------------------------------------------------------

    async def subscription_coverage(
        self,
        identity: PrincipalIdentity,
        account_id: str,
        *,
        now: datetime | None = None,
        adcp_version: str | None = None,
    ) -> list[ReportingSubscriptionCoverage]:
        """``subscription_coverage[]`` for one account, while the caller is authorized.

        One entry per caller subscription whose scope includes the account,
        inactive ones included while retained. An unauthorized account is
        unknown here, as for any unauthorized caller. ``adcp_version`` is the
        release the response is served at: below 3.3.0-beta.2, the adcp#8585
        fields are dropped and per-offering gaps and cutovers become
        account-wide (see :mod:`adcp.reporting.subscriptions.wire`).
        """
        instant = self._now(now)
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        destinations = await self._destinations(identity)
        buys = await self._host.account_buys(identity, account_id)
        principal = await self._read(identity.subject)
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
            gaps = self._gaps(state, account, buys, destinations, instant)
            if gaps:
                payload["gaps"] = gaps
            cutovers = self._timezone_cutovers(state, account, buys, destinations, instant)
            if cutovers:
                payload["timezone_cutovers"] = cutovers
            result.append(
                ReportingSubscriptionCoverage.model_validate(
                    project_subscription_coverage(payload, adcp_version)
                )
            )
        return result

    def _gaps(
        self,
        state: Mapping[str, Any],
        account: _Account,
        buys: Sequence[SubscriptionBuy],
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
            schedules = self._schedules(state, account, buys, destinations, lost)
            entries = [
                (offering_id, calendar.floor(lost), calendar.ceil(regranted))
                for offering_id, calendar in schedules
            ]
            if not entries or now - max(until for _o, _f, until in entries) > self._retention:
                continue
            gaps.extend(
                _per_offering(
                    entries,
                    lambda start, until: {
                        "from": iso_z(start),
                        "until": iso_z(until),
                        "reason": "unauthorized",
                    },
                )
            )
        return gaps

    def _timezone_cutovers(
        self,
        state: Mapping[str, Any],
        account: _Account,
        buys: Sequence[SubscriptionBuy],
        destinations: Mapping[str, Any],
        now: datetime,
    ) -> list[dict[str, Any]]:
        cutovers = []
        for change in account.timezone_changes:
            # Each change carries the zone it replaced, which the wire reports.
            zone = change.previous_timezone
            selector = SubscriptionSelector.from_subscription(state["configuration"])
            resolution = resolve_subscription_offerings(
                selector,
                self._offerings,
                product_offering_ids=_buy_offering_ids(buys)
                or [item.offering_id for item in self._offerings],
                destination=_destination_for(selector, destinations),
                at=change.changed_at,
            )
            effective = [
                (
                    item.offering_id,
                    item.calendar(zone, selector.period_timezone).ceil(change.changed_at),
                )
                for item in resolution.offerings
                if item.alignment == "account_timezone"
            ]
            if effective and now - change.changed_at <= self._retention:
                previous, timezone = zone, change.timezone

                def cutover(at: datetime, _same: datetime) -> dict[str, Any]:
                    return {
                        "effective_at": iso_z(at),
                        "timezone": timezone,
                        "previous_timezone": previous,
                    }

                cutovers.extend(
                    _per_offering([(offering_id, at, at) for offering_id, at in effective], cutover)
                )
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
        principal = await self._read(identity.subject)
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
        # The seller never guesses a prior version's configuration, and each
        # version resolves against the destination it named when accepted.
        windows: tuple[SubscriptionVersionWindow, ...] = tuple(
            replace(
                window,
                destination=principal.destinations.get(
                    (subscription_id, window.subscription_version)
                ),
            )
            for window in subscription_version_windows(
                state, version_configurations=versions, require_prior_configurations=True
            )
        )
        return expected_subscription_periods(
            subscription=windows,
            account_id=account_id,
            buys=buys,
            offerings=self._offerings,
            account_timezone=account.timezone,
            now=now,
            authorizations=list(account.grants),
            exceptions=exclusions,
            backfill_on_regrant=self._support.backfill_on_regrant,
            status_retention_days=self._retention_days,
            destinations=destinations,
            account_operator=account.operator,
            timezone_changes=list(account.timezone_changes),
        )

    async def frozen_owed_sets(
        self,
        identity: PrincipalIdentity,
        *,
        account_id: str | None = None,
        now: datetime | None = None,
    ) -> list[FrozenOwedSet]:
        """Every (account, subscription) owed set frozen through ``now``, for the ledger.

        Only periods that have ended are returned: a period's buys are frozen
        at its end, and ledger obligations are immutable. A subscription that
        cannot be computed for an account is skipped and recorded in
        :attr:`evaluation_errors`; the others are returned.
        """
        from adcp.reporting.subscriptions.ledger import FrozenOwedSet

        instant = self._now(now)
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        destinations = await self._destinations(identity)
        principal = await self._read(identity.subject)
        accounts = {
            key: value
            for key, value in principal.accounts.items()
            if account_id is None or key == account_id
        }
        buys = {key: await self._host.account_buys(identity, key) for key in accounts}
        result: list[FrozenOwedSet] = []
        for state in states:
            subscription_id = str(state["subscription_id"])
            versions = {
                version: configuration
                for (sub_id, version), configuration in principal.versions.items()
                if sub_id == subscription_id
            }
            for key, account in sorted(accounts.items()):
                try:
                    periods = self._owed(
                        principal, state, key, account, buys[key], destinations, instant
                    )
                except Exception as error:  # noqa: BLE001 - isolate one owed set
                    self._errors[(identity.subject, subscription_id)] = error
                    continue
                if periods:
                    result.append(
                        FrozenOwedSet(
                            principal_id=identity.subject,
                            account_id=key,
                            subscription_id=subscription_id,
                            through=instant,
                            periods=tuple(periods),
                            versions=versions,
                        )
                    )
        return result

    async def grants_task(self, identity: PrincipalIdentity, account_id: str, task: str) -> bool:
        """Whether the principal's current grant on the account allows ``task``.

        From the engine's last reconcile of the host's grants. There is no
        window after a loss of authority: a revoked account allows nothing.
        """
        account = (await self._read(identity.subject)).accounts.get(account_id)
        return account is not None and account.grants_task(task)

    async def receipt_roster(self, identity: PrincipalIdentity, account_id: str) -> list[str]:
        """Active ``consumer_receipt`` subscriptions covering the account.

        A consumer owes receipts for an account while one of its active
        ``consumer_receipt`` subscriptions covers it without an open
        account-level exception.
        """
        states = [_dump(item) for item in await self._principals.reporting_subscriptions(identity)]
        principal = await self._read(identity.subject)
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


def _buy_offering_ids(buys: Sequence[SubscriptionBuy]) -> list[str]:
    ids: set[str] = set()
    for buy in buys:
        for _package, offering_ids in buy.product_offering_ids():
            ids.update(offering_ids)
    return sorted(ids)


def _per_offering(
    entries: Sequence[tuple[str, datetime, datetime]],
    build: Callable[[datetime, datetime], dict[str, Any]],
) -> list[dict[str, Any]]:
    """One entry for every offering when their boundaries agree, else one each.

    ``gaps[]`` and ``timezone_cutovers[]`` entries without ``offering_id``
    apply to every offering on the account (adcp#8585).
    """
    instants = {(first, second) for _offering_id, first, second in entries}
    if len(instants) == 1:
        first, second = next(iter(instants))
        return [build(first, second)]
    result = []
    for offering_id, first, second in sorted(entries, key=lambda item: item[0].encode()):
        entry = build(first, second)
        entry["offering_id"] = offering_id
        result.append(entry)
    return result


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
) -> tuple[str, str, datetime | None] | None:
    """The caller's own registration that takes this account and purpose, if any."""
    for config in sorted(configs, key=lambda item: item.delivery_config_id.encode("utf-8")):
        if config.feed_purpose == feed_purpose:
            return ("delivery_config_id", config.delivery_config_id, config.active_from)
    if "all_authorized_accounts" in scope:
        for other in states:
            other_config = other["configuration"]
            if (
                _active(other)
                and "account_ids" in other_config["scope"]
                and other_config["selector"]["feed_purpose"] == feed_purpose
                and _scope_selects(other_config, account_id, account)
            ):
                return ("subscription_id", str(other["subscription_id"]), None)
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
    "ReportingSubscriptionDeliveryHost",
    "ReportingSubscriptionEngine",
    "ReportingSubscriptionHost",
    "ReportingSubscriptionPrecedenceHost",
    "ReportingSubscriptionStateConflictError",
    "ReportingSubscriptionStateStore",
    "STATE_DOCUMENT_VERSION",
    "StoredReportingSubscriptionState",
    "decode_principal_state",
    "encode_principal_state",
]
