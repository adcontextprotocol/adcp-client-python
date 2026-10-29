"""Buyer side of reporting subscriptions: register, read back, and stay current.

* :func:`sync_reporting_subscriptions` writes the ``sync_principal``
  ``reporting_subscriptions`` section and returns the seller's readback.
* :func:`read_reporting_subscriptions` reads it back through ``get_principal``.
* :func:`load_subscription_coverage` reads ``subscription_coverage[]`` from the
  ``get_reporting_status`` summary view for one account.
* :class:`ReportingSubscriptionTracker` keeps the readback current, refreshes
  when a ``principal.changed`` notification arrives, remembers every version
  the buyer registered (the readback keeps only the current configuration),
  tracks lost authority from ``accounts_unauthorized[]``, and computes the
  buyer's expected periods with
  :func:`~adcp.reporting.subscriptions.expected_subscription_periods`.

The expected periods are :class:`~adcp.reporting.ExpectedReportingPeriod`
values, so they go straight into
:func:`~adcp.reporting.evaluate_reporting_ledger` (``expected_periods=``) next
to configuration-derived expectations, and from there into
:func:`~adcp.reporting.plan_consumer_statuses`. A withheld period has no
``expected_at`` and is never claimed missing while authority is absent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from adcp.reporting.subscriptions._clock import utc_instant
from adcp.reporting.subscriptions.coverage import (
    AccountTimezoneChange,
    ExpectedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    expected_subscription_periods,
)
from adcp.reporting.subscriptions.resolution import SubscriptionOffering
from adcp.types import (
    GetPrincipalRequest,
    GetPrincipalResponse,
    GetReportingStatusRequest,
    GetReportingStatusResponse,
    SyncPrincipalRequest,
    SyncPrincipalResponse,
)
from adcp.types.core import TaskResult
from adcp.types.domains.core.reporting_subscription_coverage import (
    ReportingSubscriptionCoverage,
)
from adcp.types.domains.core.reporting_subscription_state import ReportingSubscriptionState

#: ``principal.changed`` reason that means subscription state changed.
REPORTING_SUBSCRIPTION_CHANGED = "reporting_subscription_changed"


class ReportingSubscriptionClientError(RuntimeError):
    """The seller refused or failed a subscription read or write."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class PrincipalClient(Protocol):
    async def get_principal(
        self, request: GetPrincipalRequest
    ) -> TaskResult[GetPrincipalResponse]: ...

    async def sync_principal(
        self, request: SyncPrincipalRequest
    ) -> TaskResult[SyncPrincipalResponse]: ...


class SubscriptionStatusClient(Protocol):
    async def get_reporting_status(
        self, request: GetReportingStatusRequest
    ) -> TaskResult[GetReportingStatusResponse]: ...


@dataclass(frozen=True)
class ReportingSubscriptionReadback:
    """The caller's subscriptions as the seller reports them."""

    configuration_version: str | None
    subscriptions: tuple[ReportingSubscriptionState, ...] = ()

    def get(self, subscription_id: str) -> ReportingSubscriptionState | None:
        return next(
            (item for item in self.subscriptions if item.subscription_id == subscription_id),
            None,
        )


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True)
        assert isinstance(dumped, dict)
        return dumped
    return dict(value)


def _readback(result: Any, *, task: str) -> ReportingSubscriptionReadback:
    response = getattr(result, "data", None)
    if not getattr(result, "success", False) or response is None:
        raise ReportingSubscriptionClientError(
            f"{task} failed: {getattr(result, 'error', None) or 'no response'}"
        )
    outcome = response.result
    kind = str(getattr(outcome, "kind", ""))
    if kind == "failed":
        errors = list(getattr(outcome, "errors", None) or [])
        first = errors[0] if errors else None
        raise ReportingSubscriptionClientError(
            f"{task} was rejected: {getattr(first, 'message', 'unknown error')}",
            code=None if first is None else str(getattr(first, "code", "")),
        )
    configuration = getattr(outcome, "configuration", None)
    states = tuple(getattr(configuration, "reporting_subscriptions", None) or ())
    return ReportingSubscriptionReadback(
        configuration_version=getattr(outcome, "configuration_version", None),
        subscriptions=states,
    )


async def sync_reporting_subscriptions(
    client: PrincipalClient,
    subscriptions: Sequence[Any],
    *,
    reporting_destinations: Sequence[Any] | None = None,
    idempotency_key: str | None = None,
    expected_configuration_version: str | None = None,
    dry_run: bool = False,
) -> ReportingSubscriptionReadback:
    """Write the complete desired subscription set and return the readback.

    The section is complete desired state: a subscription missing from
    ``subscriptions`` is deactivated. Pass ``reporting_destinations`` to
    replace that section in the same request, so a new subscription can name
    a destination registered alongside it. A ``dry_run`` validates without
    applying and returns an empty readback.
    """
    configuration: dict[str, Any] = {
        "reporting_subscriptions": [_dump(item) for item in subscriptions]
    }
    if reporting_destinations is not None:
        configuration["reporting_destinations"] = [_dump(item) for item in reporting_destinations]
    payload: dict[str, Any] = {
        "idempotency_key": idempotency_key or f"rsub-{uuid4()}",
        "configuration": configuration,
    }
    if expected_configuration_version is not None:
        payload["expected_configuration_version"] = expected_configuration_version
    if dry_run:
        payload["dry_run"] = True
    result = await client.sync_principal(SyncPrincipalRequest.model_validate(payload))
    return _readback(result, task="sync_principal")


async def read_reporting_subscriptions(client: PrincipalClient) -> ReportingSubscriptionReadback:
    """Read the caller's subscriptions back through ``get_principal``."""
    result = await client.get_principal(GetPrincipalRequest.model_validate({}))
    response = getattr(result, "data", None)
    if response is not None and str(getattr(response.result, "kind", "")) in {
        "unconfigured",
        "recognized",
    }:
        return ReportingSubscriptionReadback(configuration_version=None)
    return _readback(result, task="get_principal")


async def load_subscription_coverage(
    client: SubscriptionStatusClient, *, account: Mapping[str, Any] | Any
) -> tuple[ReportingSubscriptionCoverage, ...]:
    """``subscription_coverage[]`` for one account from the summary view.

    An account the caller has lost authority for is unknown to
    ``get_reporting_status``; read ``accounts_unauthorized[]`` from
    :func:`read_reporting_subscriptions` instead.
    """
    request = GetReportingStatusRequest.model_validate(
        {"account": _dump(account), "view": "summary"}
    )
    result = await client.get_reporting_status(request)
    response = getattr(result, "data", None)
    if not getattr(result, "success", False) or response is None:
        raise ReportingSubscriptionClientError(
            f"get_reporting_status failed: {getattr(result, 'error', None) or 'no response'}"
        )
    return tuple(getattr(response, "subscription_coverage", None) or ())


def is_subscription_change(notification: Mapping[str, Any] | Any) -> bool:
    """Whether a ``principal.changed`` payload calls for re-reading subscriptions.

    Receivers re-read rather than infer state from the reason; ``other`` and
    destination changes can move a subscription to ``action_required`` too.
    """
    payload = _dump(notification)
    if payload.get("notification_type") not in {None, "principal.changed"}:
        return False
    return str(payload.get("reason")) in {
        REPORTING_SUBSCRIPTION_CHANGED,
        "destination_state_changed",
        "other",
    }


@dataclass
class _AccountRecord:
    timezone: str
    operator: str | None = None
    initial_timezone: str = ""
    grants: list[SubscriptionAuthorization] = field(default_factory=list)
    timezone_changes: list[AccountTimezoneChange] = field(default_factory=list)


class ReportingSubscriptionTracker:
    """A buyer's own records for its subscriptions with one seller.

    Register through :meth:`sync` so every version's configuration is kept.
    Record what :func:`list_accounts` shows with :meth:`observe_account`.
    Call :meth:`handle_notification` with each ``principal.changed`` payload
    (or :meth:`refresh` on a schedule). :meth:`expected_periods` then computes
    what the seller owes from these records alone.
    """

    def __init__(
        self,
        client: PrincipalClient,
        *,
        offerings: Sequence[SubscriptionOffering],
        backfill_on_regrant: bool = False,
        status_retention_days: int | None = None,
        destinations: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._offerings = tuple(offerings)
        self._backfill = backfill_on_regrant
        self._retention = status_retention_days
        self._destinations = dict(destinations or {})
        self._sent: dict[str, dict[int, dict[str, Any]]] = {}
        self._accounts: dict[str, _AccountRecord] = {}
        self._readback = ReportingSubscriptionReadback(configuration_version=None)

    @classmethod
    def from_capabilities(
        cls,
        client: PrincipalClient,
        capabilities: Mapping[str, Any] | Any,
        *,
        offerings: Sequence[SubscriptionOffering],
        destinations: Mapping[str, Any] | None = None,
    ) -> ReportingSubscriptionTracker:
        """Read ``backfill_on_regrant`` and ``status_retention_days`` from capabilities."""
        payload = _dump(capabilities)
        block = (payload.get("media_buy") or {}).get("reporting_delivery") or payload
        subscriptions = block.get("subscriptions") or {}
        return cls(
            client,
            offerings=offerings,
            backfill_on_regrant=bool(subscriptions.get("backfill_on_regrant", False)),
            status_retention_days=block.get("status_retention_days"),
            destinations=destinations,
        )

    @property
    def readback(self) -> ReportingSubscriptionReadback:
        return self._readback

    async def sync(
        self,
        subscriptions: Sequence[Any],
        *,
        reporting_destinations: Sequence[Any] | None = None,
        idempotency_key: str | None = None,
        expected_configuration_version: str | None = None,
    ) -> ReportingSubscriptionReadback:
        """Register the desired set, remembering each version's configuration."""
        payloads = [_dump(item) for item in subscriptions]
        readback = await sync_reporting_subscriptions(
            self._client,
            payloads,
            reporting_destinations=reporting_destinations,
            idempotency_key=idempotency_key,
            expected_configuration_version=expected_configuration_version,
        )
        for item in payloads:
            versions = self._sent.setdefault(str(item["subscription_id"]), {})
            versions.setdefault(int(item["subscription_version"]), item)
        if reporting_destinations is not None:
            for destination in reporting_destinations:
                payload = _dump(destination)
                self._destinations[str(payload["destination_id"])] = payload
        self._readback = readback
        return readback

    async def refresh(self) -> ReportingSubscriptionReadback:
        """Re-read the readback and fold ``accounts_unauthorized[]`` into grant history."""
        self._readback = await read_reporting_subscriptions(self._client)
        for state in self._readback.subscriptions:
            for entry in state.accounts_unauthorized or ():
                self._record_loss(entry.account_id, utc_instant(entry.unauthorized_at))
        return self._readback

    async def handle_notification(self, notification: Mapping[str, Any] | Any) -> bool:
        """Refresh on a relevant ``principal.changed``; returns whether it refreshed."""
        if not is_subscription_change(notification):
            return False
        await self.refresh()
        return True

    def observe_account(
        self,
        account_id: str,
        *,
        authorized_at: datetime | None,
        timezone: str = "UTC",
        operator: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Record what ``list_accounts`` shows for an account the caller can read.

        A new ``authorized_at`` after a recorded loss is a re-grant. A changed
        ``timezone`` is recorded as a timezone change at ``observed_at``.
        """
        record = self._accounts.get(account_id)
        if record is None:
            record = self._accounts[account_id] = _AccountRecord(
                timezone, operator, initial_timezone=timezone
            )
        elif timezone != record.timezone:
            if observed_at is None:
                raise ValueError("a timezone change needs observed_at")
            record.timezone_changes.append(
                AccountTimezoneChange(utc_instant(observed_at), timezone)
            )
            record.timezone = timezone
        if operator is not None:
            record.operator = operator
        if authorized_at is None:
            return
        granted = utc_instant(authorized_at, field="authorized_at")
        if not record.grants:
            record.grants.append(SubscriptionAuthorization(granted))
            return
        last = record.grants[-1]
        if last.unauthorized_at is not None and granted > last.unauthorized_at:
            record.grants.append(SubscriptionAuthorization(granted))

    def _record_loss(self, account_id: str, unauthorized_at: datetime) -> None:
        record = self._accounts.get(account_id)
        if record is None or not record.grants:
            return
        last = record.grants[-1]
        if last.unauthorized_at is None and unauthorized_at > last.authorized_at:
            record.grants[-1] = SubscriptionAuthorization(last.authorized_at, unauthorized_at)

    def expected_periods(
        self,
        subscription_id: str,
        account_id: str,
        *,
        buys: Sequence[SubscriptionBuy],
        now: datetime,
        coverage: ReportingSubscriptionCoverage | None = None,
        include_unresolved: bool = False,
    ) -> list[ExpectedSubscriptionPeriod]:
        """What the seller owes this account under the subscription, from buyer records.

        ``coverage`` is the subscription's ``subscription_coverage[]`` entry
        for the account, read while the caller is authorized.
        """
        state = self._readback.get(subscription_id)
        if state is None:
            raise KeyError(subscription_id)
        record = self._accounts.get(account_id)
        if record is None:
            raise KeyError(account_id)
        return expected_subscription_periods(
            subscription=state,
            version_configurations=self._sent.get(subscription_id),
            account_id=account_id,
            buys=buys,
            offerings=self._offerings,
            account_timezone=record.initial_timezone,
            now=now,
            authorizations=list(record.grants),
            exceptions=() if coverage is None else coverage.excluded,
            gaps=() if coverage is None else (coverage.gaps or ()),
            backfill_on_regrant=self._backfill,
            status_retention_days=self._retention,
            destinations=self._destinations,
            account_operator=record.operator,
            timezone_changes=list(record.timezone_changes),
            include_unresolved=include_unresolved,
        )


__all__ = [
    "REPORTING_SUBSCRIPTION_CHANGED",
    "PrincipalClient",
    "ReportingSubscriptionClientError",
    "ReportingSubscriptionReadback",
    "ReportingSubscriptionTracker",
    "SubscriptionStatusClient",
    "is_subscription_change",
    "load_subscription_coverage",
    "read_reporting_subscriptions",
    "sync_reporting_subscriptions",
]
