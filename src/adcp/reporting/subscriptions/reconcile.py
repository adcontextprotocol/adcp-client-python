"""The buyer's ``reconcile`` and ``explain``: what am I owed, and why (AdCP 3.3, experimental).

``client.reporting.reconcile()`` answers, for every account the caller's
subscriptions cover, which periods the seller owes, which it delivered, and
whose move each one is. Everything is read from the wire on each run:

* the roster is ``list_accounts`` ∩ the subscriptions' scopes, plus each
  subscription's ``accounts_unauthorized[]``, so there is no static account
  list to keep;
* the subscriptions, their versions (``prior_versions[].configuration``) and
  ``inactive_intervals`` come from ``get_principal``;
* each account's ``authorization.authorized_at`` and ``regranted_at``, its
  ``timezone`` and ``operator`` come from ``list_accounts``; a loss comes from
  ``accounts_unauthorized[].unauthorized_at``. Grant history is never the
  caller's to record;
* buys come from ``get_media_buys``, offerings, ``backfill_on_regrant`` and
  ``status_retention_days`` from capabilities, ``subscription_coverage[]`` and
  the obligations from a complete ``get_reporting_status`` periods walk.

The owed set is :func:`~adcp.reporting.subscriptions.owed_subscription_periods`
over those facts, the same rule the seller runs; each period becomes a
:class:`PeriodLine` with a ``status``, the spec ``reason`` code and the
``responsible_party``. ``explain`` returns the same
:class:`~adcp.reporting.subscriptions.CoverageTrace` the seller's
``explain_coverage`` builds, with ``side="buyer"``.

The client keeps one cache, per account: the last facts read while the caller
was authorized (``authorized_at``, timezone, operator, buys and coverage). It
is what lets ``reconcile`` show an account whose authority was lost, which the
seller then reports as unknown, with its owed periods ``withheld``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from adcp.reporting.subscriptions._clock import CoverageInputError, iso_z, utc_instant
from adcp.reporting.subscriptions.coverage import (
    OwedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    subscription_covers_account,
    subscription_version_windows,
)
from adcp.reporting.subscriptions.explain import (
    CoverageExplainer,
    CoverageTrace,
    _Candidate,
    sort_traces,
)
from adcp.reporting.subscriptions.receipts import (
    ReceiptPolicy,
    ReceiptReport,
    apply_receipt_policy,
)
from adcp.reporting.subscriptions.resolution import SubscriptionOffering

logger = logging.getLogger(__name__)

PeriodStatus = Literal["delivered", "pending", "late", "missing_obligation", "withheld", "not_owed"]
ResponsibleParty = Literal["seller", "buyer", "none"]

#: How long after a period's end, or its ``expected_at``, before a missing
#: obligation or revision counts against the seller.
DEFAULT_GRACE = timedelta(hours=1)
#: Pages walked for one roster or buy listing; a walk that does not end is
#: refused, never truncated.
_MAX_PAGES = 1_000
_BUY_STATUSES = (
    "pending_creatives",
    "pending_start",
    "active",
    "paused",
    "completed",
    "rejected",
    "canceled",
)
_FINALITY_RANK = {"provisional": 0, "snapshot": 1, "official": 2}


@dataclass(frozen=True)
class PeriodLine:
    """One period of one (subscription version, offering) on one account.

    Keys are wire-style in both SDKs, so exports match. ``reason`` is a spec
    reason code and ``message`` its explanation sentence.
    """

    account_id: str
    subscription_id: str
    subscription_version: int
    offering_id: str | None
    report_definition_id: str | None
    media_buy_ids: tuple[str, ...]
    period: Mapping[str, str]
    status: PeriodStatus
    reason: str
    responsible_party: ResponsibleParty
    message: str
    expected_at: str | None = None
    reporting_obligation_id: str | None = None
    reporting_revision_id: str | None = None
    finality: str | None = None
    receipt: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "account_id": self.account_id,
            "subscription_id": self.subscription_id,
            "subscription_version": self.subscription_version,
            "offering_id": self.offering_id,
            "report_definition_id": self.report_definition_id,
            "media_buy_ids": list(self.media_buy_ids),
            "period": dict(self.period),
            "status": self.status,
            "reason": self.reason,
            "responsible_party": self.responsible_party,
            "message": self.message,
        }
        for name in (
            "expected_at",
            "reporting_obligation_id",
            "reporting_revision_id",
            "finality",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.receipt is not None:
            result["receipt"] = dict(self.receipt)
        return result


@dataclass(frozen=True)
class ReconcileSummary:
    """Counts by status; ``owed`` is every line except ``not_owed``."""

    owed: int = 0
    delivered: int = 0
    pending: int = 0
    late: int = 0
    missing_obligation: int = 0
    withheld: int = 0
    not_owed: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(vars(self))


@dataclass(frozen=True)
class ReconcileIssue:
    """Why part of a reconcile is not definitive; the message ends with the fix."""

    code: str
    message: str
    account_id: str | None = None
    subscription_id: str | None = None


@dataclass(frozen=True)
class ReconcileAccountError:
    """What could not be read for one account: the task, a code and the detail."""

    task: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"task": self.task, "code": self.code, "message": self.message}


AccountStatus = Literal["reconciled", "unauthorized", "failed"]


@dataclass(frozen=True)
class ReconcileAccount:
    """One account the run reconciled, or could not; the same shape as the TS SDK's.

    * ``reconciled``: its facts were read and its owed set computed.
    * ``unauthorized``: the caller's authority ended at ``unauthorized_at``;
      ``withheld_period_count`` is the seller's count of withheld periods.
    * ``failed``: ``error`` says which read failed. Its lines, if any, come
      from the last facts read for it. ``account_id`` is ``"*"`` when the
      principal or the roster could not be read at all.
    """

    account_id: str
    subscription_ids: tuple[str, ...] = ()
    status: AccountStatus = "reconciled"
    unauthorized_at: str | None = None
    withheld_period_count: int | None = None
    error: ReconcileAccountError | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "account_id": self.account_id,
            "subscription_ids": list(self.subscription_ids),
            "status": self.status,
        }
        if self.unauthorized_at is not None:
            result["unauthorized_at"] = self.unauthorized_at
        if self.withheld_period_count is not None:
            result["withheld_period_count"] = self.withheld_period_count
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result


@dataclass(frozen=True)
class ReconcileReport:
    """The result of ``client.reporting.reconcile()``.

    ``definitive`` is true only when the roster and every account's
    obligations walk completed and every owed set was computed; otherwise
    ``issues`` say what is missing, and the lines are what could be read,
    never a smaller success.
    """

    as_of: str
    definitive: bool
    summary: ReconcileSummary
    periods: tuple[PeriodLine, ...]
    issues: tuple[ReconcileIssue, ...] = ()
    #: One entry per account, with its status (``reconciled``, ``unauthorized``
    #: or ``failed``). Before 3.3 this was a tuple of account ids; that shape is
    #: :attr:`account_ids`.
    accounts: tuple[ReconcileAccount, ...] = ()
    #: What the receipts policy did; ``None`` with ``mode="off"``.
    receipts: ReceiptReport | None = None
    #: The seller's agent id, when the client has one.
    agent_id: str | None = None
    #: The window start the run used; ``None`` when no ``since`` was given.
    since: str | None = None

    @property
    def account_ids(self) -> tuple[str, ...]:
        """The ids of :attr:`accounts`, the shape ``accounts`` had before."""
        return tuple(item.account_id for item in self.accounts if item.account_id != "*")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "as_of": self.as_of,
            "definitive": self.definitive,
            "summary": self.summary.to_dict(),
            "periods": [line.to_dict() for line in self.periods],
            "issues": [vars(issue) for issue in self.issues],
            "accounts": [item.to_dict() for item in self.accounts],
        }
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.since is not None:
            result["since"] = self.since
        return result


# -- wire reads ----------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True, by_alias=True)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _data(result: Any, task: str) -> Any:
    data = getattr(result, "data", None)
    if not getattr(result, "success", False) or data is None:
        raise _UnreadableError(task, str(getattr(result, "error", None) or "no response"))
    return data


class _UnreadableError(Exception):
    def __init__(self, task: str, detail: str, code: str = "TASK_FAILED") -> None:
        super().__init__(f"{task} failed: {detail}")
        self.task = task
        self.code = code


def _failure(error: BaseException, task: str) -> ReconcileAccountError:
    """The ``error`` of a ``failed`` account: the task that failed, its code and detail."""
    if isinstance(error, _UnreadableError):
        return ReconcileAccountError(error.task, error.code, str(error))
    if isinstance(error, CoverageInputError):
        return ReconcileAccountError("coverage", error.code, str(error))
    code = getattr(error, "code", None)
    return ReconcileAccountError(
        task, str(getattr(code, "value", code) or "TASK_FAILED"), str(error) or type(error).__name__
    )


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_instant(value)
    from adcp.reporting._timestamp import aware_timestamp

    return utc_instant(aware_timestamp(str(value)))


@dataclass
class _Account:
    """What the caller last read for one account while authorized."""

    account_id: str
    timezone: str | None = None
    operator: str | None = None
    authorized_at: datetime | None = None
    regranted_at: datetime | None = None
    #: ``unauthorized_at`` values seen in ``accounts_unauthorized[]``.
    losses: list[datetime] = field(default_factory=list)
    buys: list[dict[str, Any]] = field(default_factory=list)
    #: subscription_id -> the ``subscription_coverage[]`` entry
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    listed: bool = False


@dataclass
class _Seller:
    offerings: tuple[SubscriptionOffering, ...]
    backfill_on_regrant: bool
    status_retention_days: int | None


class BuyerReportingState:
    """The buyer's cache of the last facts read per account (in memory).

    Only what the wire stops showing after a loss of authority is kept:
    the chain start, zone, operator, buys and coverage last read.
    """

    def __init__(self) -> None:
        self.accounts: dict[str, _Account] = {}
        self.warned_receipts = False

    def account(self, account_id: str) -> _Account:
        record = self.accounts.get(account_id)
        if record is None:
            record = self.accounts[account_id] = _Account(account_id)
        return record


async def _seller_facts(client: Any) -> _Seller:
    fetch = getattr(client, "fetch_capabilities", None)
    if fetch is None:
        raise _UnreadableError("get_adcp_capabilities", "the client cannot read capabilities")
    capabilities = await fetch()
    payload = _plain(capabilities)
    block = (payload.get("media_buy") or {}).get("reporting_delivery") or {}
    offerings = []
    for item in block.get("offerings") or ():
        try:
            offerings.append(SubscriptionOffering.from_offering(item))
        except (TypeError, ValueError):
            continue  # an offering without standard_dimensions cannot be selected
    subscriptions = block.get("subscriptions") or {}
    return _Seller(
        offerings=tuple(offerings),
        backfill_on_regrant=bool(subscriptions.get("backfill_on_regrant", False)),
        status_retention_days=block.get("status_retention_days"),
    )


async def _roster(client: Any) -> list[dict[str, Any]]:
    from adcp.types import ListAccountsRequest

    list_accounts = getattr(client, "list_accounts", None)
    if list_accounts is None:
        raise _UnreadableError("list_accounts", "the client cannot list accounts")
    accounts: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(_MAX_PAGES):
        payload: dict[str, Any] = {}
        if cursor:
            payload["pagination"] = {"cursor": cursor}
        data = _plain(
            _data(await list_accounts(ListAccountsRequest.model_validate(payload)), "list_accounts")
        )
        accounts.extend(data.get("accounts") or ())
        pagination = data.get("pagination") or {}
        cursor = pagination.get("cursor") if pagination.get("has_more") else None
        if not cursor:
            return accounts
    raise _UnreadableError(
        "list_accounts", f"did not finish paging within {_MAX_PAGES} pages", "LIMIT_EXCEEDED"
    )


async def _buys(client: Any, account_id: str) -> list[dict[str, Any]]:
    from adcp.types import GetMediaBuysRequest

    buys: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(_MAX_PAGES):
        payload: dict[str, Any] = {
            "account": {"account_id": account_id},
            "status_filter": list(_BUY_STATUSES),
        }
        if cursor:
            payload["pagination"] = {"cursor": cursor}
        data = _plain(
            _data(
                await client.get_media_buys(GetMediaBuysRequest.model_validate(payload)),
                "get_media_buys",
            )
        )
        buys.extend(data.get("media_buys") or ())
        pagination = data.get("pagination") or {}
        cursor = pagination.get("cursor") if pagination.get("has_more") else None
        if not cursor:
            return buys
    raise _UnreadableError(
        "get_media_buys", f"did not finish paging within {_MAX_PAGES} pages", "LIMIT_EXCEEDED"
    )


async def _coverage(client: Any, account_id: str) -> dict[str, dict[str, Any]]:
    from adcp.types import GetReportingStatusRequest

    request = GetReportingStatusRequest.model_validate(
        {"account": {"account_id": account_id}, "view": "summary"}
    )
    data = _plain(_data(await client.get_reporting_status(request), "get_reporting_status"))
    return {
        str(entry["subscription_id"]): entry for entry in data.get("subscription_coverage") or ()
    }


def _subscription_buys(
    raw: Iterable[Mapping[str, Any]], every: Sequence[str]
) -> list[SubscriptionBuy]:
    """Buys as coverage reads them.

    Each package's ``reporting_delivery_offering_ids`` snapshot is used as
    served (``[]`` means no offering). A package served without one is read
    as every advertised offering, the same fallback the SDK seller applies,
    so both sides compute the same owed set.
    """
    result = []
    for item in raw:
        if item.get("start_time") is None or item.get("end_time") is None:
            continue  # not yet flighted: nothing can be owed for it
        media_buy = dict(item)
        media_buy["packages"] = [
            {
                **package,
                "reporting_delivery_offering_ids": list(
                    package["reporting_delivery_offering_ids"]
                    if isinstance(package.get("reporting_delivery_offering_ids"), (list, tuple))
                    else every
                ),
            }
            for package in item.get("packages") or ()
            if package.get("package_id")
        ]
        buy = SubscriptionBuy.from_media_buy(media_buy)
        if not buy.packages:
            from dataclasses import replace

            buy = replace(buy, reporting_delivery_offering_ids=tuple(every))
        result.append(buy)
    return result


def _grants(
    record: _Account, coverage: Mapping[str, Any] | None
) -> list[SubscriptionAuthorization]:
    """The grant history the wire states for one account.

    ``authorized_at`` starts the current chain and ``regranted_at`` is its
    latest re-grant; the loss before that re-grant is the
    ``unauthorized_at`` last seen in ``accounts_unauthorized[]``, else the
    start of the seller's ``gaps[]`` entry for it (the last boundary at or
    before the loss, which removes exactly the same periods). A current loss
    ends the chain.
    """
    start = record.authorized_at
    if start is None:
        return []
    regranted = record.regranted_at
    current_loss = None if record.listed else max(record.losses, default=None)
    if regranted is None or regranted <= start:
        if current_loss is not None and current_loss > start:
            return [SubscriptionAuthorization(start, current_loss)]
        return [SubscriptionAuthorization(start)]
    seen = [loss for loss in record.losses if start < loss < regranted]
    if seen:
        lost = max(seen)
    else:
        gap_starts = [
            _instant(gap.get("from"))
            for gap in (coverage or {}).get("gaps") or ()
            if (_instant(gap.get("until")) or regranted) >= regranted
        ]
        candidates = [item for item in gap_starts if item is not None and start < item < regranted]
        lost = max(candidates) if candidates else regranted - timedelta(microseconds=1)
    lost = max(lost, start + timedelta(microseconds=1))
    later = SubscriptionAuthorization(
        regranted, current_loss if current_loss and current_loss > regranted else None
    )
    return [SubscriptionAuthorization(start, lost), later]


def _configuration(state: Any) -> dict[str, Any]:
    return dict(_plain(getattr(state, "configuration", None) or {}))


def _scope_covers(state: Any, record: _Account) -> bool:
    configuration = _configuration(state)
    return subscription_covers_account(
        configuration, account_id=record.account_id, account_operator=record.operator
    )


class _Context:
    """Every wire fact one reconcile or explain run reads."""

    def __init__(self, client: Any, state: BuyerReportingState) -> None:
        self.client = client
        self.state = state
        self.issues: list[ReconcileIssue] = []
        self.subscriptions: tuple[Any, ...] = ()
        self.seller: _Seller | None = None
        self.roster_complete = False
        #: account_id -> the latest ``unauthorized_at`` in ``accounts_unauthorized[]``
        self.unauthorized: dict[str, datetime] = {}
        #: account_id -> the seller's withheld-period count, summed over subscriptions
        self.withheld: dict[str, int] = {}
        #: account_id -> the first read that failed for it
        self.failures: dict[str, ReconcileAccountError] = {}
        #: the principal or roster read that failed, which no account can name
        self.roster_failure: ReconcileAccountError | None = None

    def fail(self, account_id: str, error: BaseException, task: str) -> None:
        self.failures.setdefault(account_id, _failure(error, task))

    def issue(
        self,
        code: str,
        message: str,
        *,
        account_id: str | None = None,
        subscription_id: str | None = None,
    ) -> None:
        self.issues.append(ReconcileIssue(code, message, account_id, subscription_id))

    async def load(self, *, accounts: Sequence[str] | None, strict: bool = False) -> list[_Account]:
        """Read the principal, capabilities and roster.

        Every read degrades to an issue, including ``get_principal``
        (``SUBSCRIPTION_PRINCIPAL_UNAVAILABLE``, after which nothing can be
        computed and the report is empty and not definitive). ``strict``
        (``explain``, which has no issue channel) raises it instead.
        """
        from adcp.reporting.subscriptions.consumer import ReportingSubscriptionError
        from adcp.reporting.subscriptions.facade import read_principal

        try:
            principal = await read_principal(self.client)
        except ReportingSubscriptionError as error:
            if strict:
                raise
            self.roster_failure = _failure(error, "get_principal")
            self.issue(
                "SUBSCRIPTION_PRINCIPAL_UNAVAILABLE",
                f"The subscriptions could not be read ({error}). Nothing was computed; retry "
                "once get_principal answers.",
            )
            return []
        self.subscriptions = principal.subscriptions
        try:
            self.seller = await _seller_facts(self.client)
        except Exception as error:  # noqa: BLE001 - reported, never a smaller success
            self.issue(
                "SUBSCRIPTION_OFFERINGS_UNAVAILABLE",
                f"Capabilities could not be read ({error}). Retry once the seller is reachable.",
            )
            self.seller = _Seller((), False, None)
        unauthorized: dict[str, datetime] = {}
        for state in self.subscriptions:
            for entry in getattr(state, "accounts_unauthorized", None) or ():
                at = _instant(getattr(entry, "unauthorized_at", None))
                if at is not None:
                    previous = unauthorized.get(entry.account_id)
                    unauthorized[entry.account_id] = max(at, previous) if previous else at
                count = getattr(entry, "withheld_period_count", None) or 0
                self.withheld[entry.account_id] = self.withheld.get(entry.account_id, 0) + count
            listed = getattr(state, "accounts_unauthorized_count", None)
            inline = len(getattr(state, "accounts_unauthorized", None) or ())
            if listed is not None and listed > inline:
                self.issue(
                    "SUBSCRIPTION_ROSTER_UNAVAILABLE",
                    f"{state.subscription_id} lists {inline} of {listed} unauthorized accounts "
                    "inline. Page the rest through get_principal reporting_subscription_page.",
                    subscription_id=state.subscription_id,
                )
        try:
            roster = await _roster(self.client)
            self.roster_complete = True
        except Exception as error:  # noqa: BLE001
            roster = []
            self.roster_failure = _failure(error, "list_accounts")
            self.issue(
                "SUBSCRIPTION_ROSTER_UNAVAILABLE",
                f"The account roster could not be read completely ({error}); a partial roster "
                "is never used. Retry once list_accounts answers.",
            )
        seen: set[str] = set()
        for raw in roster:
            account_id = str(raw.get("account_id") or "")
            if not account_id:
                continue
            record = self.state.account(account_id)
            record.listed = True
            seen.add(account_id)
            record.timezone = raw.get("timezone") or record.timezone
            record.operator = raw.get("operator") or record.operator
            authorization = raw.get("authorization") or {}
            record.authorized_at = _instant(authorization.get("authorized_at"))
            record.regranted_at = _instant(authorization.get("regranted_at"))
        self.unauthorized = unauthorized
        for account_id, lost in unauthorized.items():
            record = self.state.account(account_id)
            if account_id not in seen:
                record.listed = False
            if lost not in record.losses:
                record.losses.append(lost)
        if self.roster_complete:
            for record in self.state.accounts.values():
                if record.account_id not in seen and record.account_id not in unauthorized:
                    record.listed = False
        wanted = set(accounts) if accounts is not None else None
        result: list[_Account] = []
        for record in sorted(self.state.accounts.values(), key=lambda r: r.account_id):
            if wanted is not None and record.account_id not in wanted:
                continue
            covered = any(_scope_covers(state, record) for state in self.subscriptions)
            if not covered and record.account_id not in unauthorized:
                continue
            if not record.listed and record.account_id not in unauthorized:
                continue
            result.append(record)
        if wanted is not None:
            for missing in sorted(wanted - {r.account_id for r in result}):
                self.issue(
                    "SUBSCRIPTION_ACCOUNT_NOT_COVERED",
                    f"Account {missing!r} is not readable or no subscription covers it. "
                    "Check list_accounts and the subscription scopes.",
                    account_id=missing,
                )
        return result

    async def refresh(self, record: _Account) -> bool:
        """Read buys and coverage for a readable account; ``False`` when it is not."""
        if not record.listed:
            return False
        try:
            record.buys = await _buys(self.client, record.account_id)
            record.coverage = await _coverage(self.client, record.account_id)
        except Exception as error:  # noqa: BLE001
            self.fail(record.account_id, error, "get_media_buys")
            self.issue(
                "SUBSCRIPTION_ACCOUNT_UNREADABLE",
                f"Account {record.account_id!r} could not be read ({error}). The last facts "
                "read for it are used; retry once the seller answers.",
                account_id=record.account_id,
            )
            return False
        return True

    def explainer(self, state: Any, record: _Account, now: datetime) -> CoverageExplainer:
        assert self.seller is not None
        coverage = record.coverage.get(str(state.subscription_id))
        windows = subscription_version_windows(state, require_prior_configurations=False)
        return CoverageExplainer(
            subscription=windows,
            account_id=record.account_id,
            buys=_subscription_buys(
                record.buys, [item.offering_id for item in self.seller.offerings]
            ),
            offerings=self.seller.offerings,
            account_timezone=record.timezone,
            now=now,
            authorizations=_grants(record, coverage),
            coverage=coverage,
            backfill_on_regrant=self.seller.backfill_on_regrant,
            status_retention_days=self.seller.status_retention_days,
            account_operator=record.operator,
        )


# -- reconcile -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Obligation:
    obligation_id: str
    revision_id: str | None
    finality: str | None
    receipt_owed: bool


async def _obligations(
    client: Any, account_id: str, now: datetime
) -> tuple[dict[tuple[Any, ...], _Obligation], bool, Any]:
    """Obligations keyed by (subscription, version, offering, start, end), and walk completeness."""
    from adcp.reporting._reconcile import evaluate_reporting_ledger, load_reporting_ledger
    from adcp.types import GetReportingStatusRequest

    request = GetReportingStatusRequest.model_validate(
        {"account": {"account_id": account_id}, "view": "periods"}
    )
    ledger = await load_reporting_ledger(client, request)
    result = evaluate_reporting_ledger(ledger, now=now)
    revisions = {item.reporting_revision_id: item for item in ledger.revisions}
    outcomes = {item.reporting_obligation_id: item for item in result.obligations}
    index: dict[tuple[Any, ...], _Obligation] = {}
    for obligation in ledger.obligations:
        outcome = outcomes.get(obligation.reporting_obligation_id)
        revision = (
            revisions.get(outcome.reporting_revision_id)
            if outcome and outcome.reporting_revision_id
            else None
        )
        finality = (
            None
            if revision is None
            else str(getattr(revision.finality, "value", revision.finality))
        )
        key = (
            obligation.delivery_config_id,
            obligation.delivery_config_version,
            obligation.offering_id,
            utc_instant(obligation.period.start),
            utc_instant(obligation.period.end),
        )
        index[key] = _Obligation(
            obligation.reporting_obligation_id,
            None if revision is None else revision.reporting_revision_id,
            finality,
            bool(outcome and "MISSING_MATCHING_CONSUMER_RECEIPT" in outcome.reasons),
        )
    return index, ledger._read_fingerprint is not None, ledger


def _line(
    period: OwedSubscriptionPeriod,
    trace_reason: str,
    message: str,
    obligation: _Obligation | None,
    required_finality: str,
    *,
    now: datetime,
    grace: timedelta,
) -> PeriodLine:
    end = utc_instant(datetime.fromisoformat(period.period_end.replace("Z", "+00:00")))
    expected = (
        None
        if period.expected_at is None
        else utc_instant(datetime.fromisoformat(period.expected_at.replace("Z", "+00:00")))
    )
    status: PeriodStatus
    responsible: ResponsibleParty = "none"
    receipt = None
    if (
        obligation is not None
        and obligation.revision_id is not None
        and (
            _FINALITY_RANK.get(obligation.finality or "", -1)
            >= _FINALITY_RANK.get(required_finality, 2)
        )
    ):
        status = "delivered"
        if obligation.receipt_owed:
            responsible = "buyer"
            receipt = {"status": "owed_by_buyer"}
    elif period.withheld or trace_reason == "owed:withheld":
        # Withheld while authority is lost: never missing or late.
        status = "withheld"
    elif obligation is None:
        if now >= end + grace:
            status, responsible = "missing_obligation", "seller"
        else:
            status = "pending"
    elif expected is not None and now >= expected + grace:
        status, responsible = "late", "seller"
    else:
        status = "pending"
    return PeriodLine(
        account_id=period.account_id,
        subscription_id=period.subscription_id,
        subscription_version=period.delivery_config_version,
        offering_id=period.offering_id,
        report_definition_id=period.report_definition_id,
        media_buy_ids=tuple(period.media_buy_ids),
        period={
            "start": period.period_start,
            "end": period.period_end,
            "timezone": period.source_timezone,
        },
        status=status,
        reason=trace_reason,
        responsible_party=responsible,
        message=message,
        expected_at=period.expected_at,
        reporting_obligation_id=None if obligation is None else obligation.obligation_id,
        reporting_revision_id=None if obligation is None else obligation.revision_id,
        finality=None if obligation is None else obligation.finality,
        receipt=receipt,
    )


def _owed_reason(explainer: CoverageExplainer, period: OwedSubscriptionPeriod) -> tuple[str, str]:
    """The trace reason and sentence of an owed, ended period."""
    start = utc_instant(datetime.fromisoformat(period.period_start.replace("Z", "+00:00")))
    end = utc_instant(datetime.fromisoformat(period.period_end.replace("Z", "+00:00")))
    buy_id = period.media_buy_ids[0]
    buy = next(b for b in explainer.buys if b.media_buy_id == buy_id)
    offering = explainer.offerings[period.offering_id]
    candidate = _Candidate(
        buy,
        offering,
        explainer._calendar(offering, period.source_timezone),
        start,
        end,
        period.source_timezone,
    )
    trace = explainer.trace(candidate)
    return trace.reason, trace.explanation


def _since(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_instant(value, field="since")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value)
    if len(text) == 10:
        day = date.fromisoformat(text)
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    instant = _instant(text)
    assert instant is not None
    return instant


async def reconcile_subscriptions(
    client: Any,
    *,
    state: BuyerReportingState | None = None,
    since: Any = None,
    accounts: Sequence[str] | Literal["covered"] = "covered",
    subscription_id: str | None = None,
    grace: timedelta = DEFAULT_GRACE,
    include_not_owed: bool = False,
    receipts: ReceiptPolicy | None = None,
    now: datetime | None = None,
) -> ReconcileReport:
    """What every covered account is owed, and its status. See :meth:`ReportingClient.reconcile`."""
    report, _context = await _reconcile(
        client,
        receipts=receipts,
        state=state,
        since=since,
        accounts=accounts,
        subscription_id=subscription_id,
        grace=grace,
        include_not_owed=include_not_owed,
        now=now,
    )
    return report


async def _reconcile(
    client: Any,
    *,
    state: BuyerReportingState | None,
    since: Any,
    accounts: Sequence[str] | Literal["covered"],
    subscription_id: str | None,
    grace: timedelta,
    include_not_owed: bool,
    now: datetime | None,
    receipts: ReceiptPolicy | None = None,
) -> tuple[ReconcileReport, _Context]:
    instant = utc_instant(now, field="now") if now is not None else datetime.now(timezone.utc)
    if not isinstance(grace, timedelta) or grace < timedelta(0):
        raise ValueError("grace must be a non-negative timedelta")
    lower = _since(since)
    policy = receipts
    receipt_report = ReceiptReport()
    context = _Context(client, state or BuyerReportingState())
    records = await context.load(accounts=None if accounts == "covered" else list(accounts))
    lines: list[PeriodLine] = []
    entries: list[ReconcileAccount] = []
    if context.roster_failure is not None:
        entries.append(ReconcileAccount("*", (), "failed", error=context.roster_failure))
    complete = context.roster_complete
    for record in records:
        readable = await context.refresh(record)
        obligations: dict[tuple[Any, ...], _Obligation] = {}
        if readable:
            try:
                obligations, walked, ledger = await _obligations(client, record.account_id, instant)
                if policy is not None and policy.mode == "verified" and walked:
                    if await apply_receipt_policy(
                        client, record.account_id, ledger, policy, receipt_report, now=instant
                    ):
                        obligations, walked, ledger = await _obligations(
                            client, record.account_id, instant
                        )
                complete = complete and walked
            except Exception as error:  # noqa: BLE001
                complete = False
                context.fail(record.account_id, error, "get_reporting_status")
                context.issue(
                    "SUBSCRIPTION_LEDGER_UNAVAILABLE",
                    f"The obligations of {record.account_id!r} could not be walked completely "
                    f"({error}); periods are reported without delivery status. Retry later.",
                    account_id=record.account_id,
                )
        subscription_ids: list[str] = []
        for state_item in context.subscriptions:
            sub_id = str(state_item.subscription_id)
            if subscription_id is not None and sub_id != subscription_id:
                continue
            if not _scope_covers(state_item, record) and not any(
                entry.account_id == record.account_id
                for entry in getattr(state_item, "accounts_unauthorized", None) or ()
            ):
                continue
            subscription_ids.append(sub_id)
            try:
                explainer = context.explainer(state_item, record, instant)
            except CoverageInputError as error:
                complete = False
                context.fail(record.account_id, error, "coverage")
                context.issue(
                    error.code,
                    f"{sub_id} on {record.account_id!r}: {error}",
                    account_id=record.account_id,
                    subscription_id=sub_id,
                )
                continue
            selector = _configuration(state_item).get("selector") or {}
            required = str(selector.get("required_finality") or "official")
            for period in explainer.owed:
                start = utc_instant(
                    datetime.fromisoformat(period.period_start.replace("Z", "+00:00"))
                )
                end = utc_instant(datetime.fromisoformat(period.period_end.replace("Z", "+00:00")))
                if end > instant or (lower is not None and start < lower):
                    continue
                reason, message = _owed_reason(explainer, period)
                obligation = obligations.get(
                    (sub_id, period.delivery_config_version, period.offering_id, start, end)
                )
                lines.append(
                    _line(period, reason, message, obligation, required, now=instant, grace=grace)
                )
            if include_not_owed:
                for candidate in explainer.flight_candidates(since=lower, until=instant):
                    if candidate.end > instant:
                        continue
                    trace = explainer.trace(candidate)
                    if trace.verdict == "owed":
                        continue
                    lines.append(_not_owed_line(trace))
        if subscription_ids or subscription_id is None or record.account_id in context.failures:
            # With subscription_id=, an account only other subscriptions cover is not listed.
            entries.append(_account_entry(context, record, subscription_ids))
    lines.sort(
        key=lambda line: (
            line.account_id.encode(),
            line.subscription_id.encode(),
            line.period["start"],
            str(line.offering_id or "").encode(),
            ",".join(line.media_buy_ids).encode(),
        )
    )
    counts = {name: 0 for name in ReconcileSummary.__dataclass_fields__}
    for line in lines:
        counts[line.status] += 1
        if line.status != "not_owed":
            counts["owed"] += 1
    receipt_report.owed_by_buyer = sum(
        1 for line in lines if line.receipt and line.receipt.get("status") == "owed_by_buyer"
    )
    if receipt_report.owed_by_buyer and (policy is None or policy.mode == "off"):
        if not context.state.warned_receipts:
            context.state.warned_receipts = True
            logger.warning(
                "%d billing period(s) owe a consumer receipt and the receipts policy is off. "
                "Pass receipts=ReceiptPolicy(mode='verified', commit=...) or submit them with "
                "client.reporting.submit_receipts.",
                receipt_report.owed_by_buyer,
            )
    report = ReconcileReport(
        as_of=iso_z(instant),
        definitive=complete and not context.issues,
        summary=ReconcileSummary(**counts),
        periods=tuple(lines),
        issues=tuple(context.issues),
        accounts=tuple(sorted(entries, key=lambda entry: entry.account_id.encode())),
        receipts=receipt_report if policy is not None and policy.mode != "off" else None,
        agent_id=_agent_id(client),
        since=None if lower is None else iso_z(lower),
    )
    return report, context


def _agent_id(client: Any) -> str | None:
    config = getattr(client, "agent_config", None)
    identifier = getattr(config, "id", None)
    return None if identifier is None else str(identifier)


def _account_entry(
    context: _Context, record: _Account, subscription_ids: Sequence[str]
) -> ReconcileAccount:
    ids = tuple(sorted(set(subscription_ids), key=str.encode))
    failure = context.failures.get(record.account_id)
    if failure is not None:
        return ReconcileAccount(record.account_id, ids, "failed", error=failure)
    lost = context.unauthorized.get(record.account_id)
    if not record.listed and lost is not None:
        return ReconcileAccount(
            record.account_id,
            ids,
            "unauthorized",
            unauthorized_at=iso_z(lost),
            withheld_period_count=context.withheld.get(record.account_id, 0),
        )
    return ReconcileAccount(record.account_id, ids, "reconciled")


def _not_owed_line(trace: CoverageTrace) -> PeriodLine:
    subject = trace.subject
    return PeriodLine(
        account_id=subject["account_id"],
        subscription_id=subject["subscription_id"],
        subscription_version=int(subject["subscription_version"]),
        offering_id=subject.get("offering_id"),
        report_definition_id=None,
        media_buy_ids=(subject["media_buy_id"],),
        period=dict(subject["period"]),
        status="not_owed",
        reason=trace.reason,
        responsible_party="none",
        message=trace.explanation,
    )


# -- fleet ------------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetReconcileIssue:
    """A seller the fleet run could not reconcile at all, with its ``agent_id``."""

    agent_id: str
    code: str
    message: str
    account_id: str | None = None
    subscription_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass(frozen=True)
class FleetReconcileReport:
    """``multi_client.reporting.reconcile()``: one report per seller and the summed summary.

    ``by_seller`` maps an agent id to its :class:`ReconcileReport`, whose own
    ``issues`` and ``accounts`` say what that run could not read. ``issues``
    names the sellers that could not be reconciled at all (their principal
    could not be read, or the run raised); they are not in ``by_seller`` and
    their periods are not in ``summary``. ``definitive`` is true only when
    every seller's report is definitive and no seller failed.
    """

    by_seller: Mapping[str, ReconcileReport]
    issues: tuple[FleetReconcileIssue, ...]
    definitive: bool
    summary: ReconcileSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "by_seller": {key: report.to_dict() for key, report in self.by_seller.items()},
            "issues": [issue.to_dict() for issue in self.issues],
            "definitive": self.definitive,
            "summary": self.summary.to_dict(),
        }


async def reconcile_fleet(
    clients: Mapping[str, Any],
    *,
    agent_ids: Sequence[str] | None = None,
    concurrency: int = 4,
    since: Any = None,
    accounts: Sequence[str] | Literal["covered"] = "covered",
    subscription_id: str | None = None,
    grace: timedelta = DEFAULT_GRACE,
    include_not_owed: bool = False,
    receipts: ReceiptPolicy | None = None,
    now: datetime | None = None,
) -> FleetReconcileReport:
    """Reconcile each seller with bounded concurrency. See :meth:`FleetReportingClient.reconcile`.

    ``clients`` maps an agent id to an object with a ``reconcile`` coroutine
    taking :func:`reconcile_subscriptions`' keyword options (a
    :class:`~adcp.reporting.subscriptions.facade.ReportingClient`).
    """
    from adcp.reporting.subscriptions.facade import _invalid

    if type(concurrency) is not int or concurrency < 1:
        raise _invalid("concurrency must be a positive integer. Pass concurrency=4 or more.")
    if agent_ids is not None:
        unknown = sorted(set(agent_ids) - set(clients))
        if unknown:
            raise _invalid(f"Unknown agent ids {unknown}. Pass ids from multi_client.agent_ids.")
    selected = [key for key in clients if agent_ids is None or key in agent_ids]
    options: dict[str, Any] = {
        "since": since,
        "accounts": accounts,
        "subscription_id": subscription_id,
        "grace": grace,
        "include_not_owed": include_not_owed,
        "receipts": receipts,
        "now": now,
    }
    reports: dict[str, ReconcileReport] = {}
    issues: list[FleetReconcileIssue] = []
    gate = asyncio.Semaphore(concurrency)

    async def one(agent_id: str) -> None:
        async with gate:
            try:
                report = await clients[agent_id].reconcile(**options)
            except Exception as error:  # noqa: BLE001 - one seller never fails the fleet
                code = getattr(error, "code", None) or "TASK_FAILED"
                issues.append(
                    FleetReconcileIssue(
                        agent_id,
                        "SUBSCRIPTION_PRINCIPAL_UNAVAILABLE",
                        f"{error} ({getattr(code, 'value', code)})",
                    )
                )
                return
        unread = next(
            (item for item in report.issues if item.code == "SUBSCRIPTION_PRINCIPAL_UNAVAILABLE"),
            None,
        )
        if unread is not None:
            # Nothing could be computed for this seller: a fleet issue, as in the TS SDK.
            issues.append(FleetReconcileIssue(agent_id, unread.code, unread.message))
            return
        reports[agent_id] = report

    await asyncio.gather(*(one(agent_id) for agent_id in selected))
    counts = {name: 0 for name in ReconcileSummary.__dataclass_fields__}
    for report in reports.values():
        for name in counts:
            counts[name] += getattr(report.summary, name)
    by_seller = {key: reports[key] for key in selected if key in reports}
    ordered = sorted(issues, key=lambda issue: selected.index(issue.agent_id))
    return FleetReconcileReport(
        by_seller=by_seller,
        issues=tuple(ordered),
        definitive=not ordered and all(report.definitive for report in by_seller.values()),
        summary=ReconcileSummary(**counts),
    )


async def explain_buyer_coverage(
    client: Any,
    *,
    account_id: str,
    period: Any,
    state: BuyerReportingState | None = None,
    subscription_id: str | None = None,
    media_buy_id: str | None = None,
    offering_id: str | None = None,
    now: datetime | None = None,
) -> list[CoverageTrace]:
    """The buyer's :class:`CoverageTrace` list. See :meth:`ReportingClient.explain`."""
    instant = utc_instant(now, field="now") if now is not None else datetime.now(timezone.utc)
    context = _Context(client, state or BuyerReportingState())
    records = await context.load(accounts=[account_id], strict=True)
    record = next((item for item in records if item.account_id == account_id), None)
    if record is None:
        record = context.state.accounts.get(account_id)
    if record is None:
        raise CoverageInputError(
            "invalid_input",
            f"Account {account_id!r} is not readable and no subscription covers it. "
            "Check list_accounts and the subscription scopes.",
        )
    await context.refresh(record)
    traces: list[CoverageTrace] = []
    found = False
    for state_item in context.subscriptions:
        if subscription_id is not None and str(state_item.subscription_id) != subscription_id:
            continue
        found = True
        explainer = context.explainer(state_item, record, instant)
        if media_buy_id is not None and media_buy_id not in {
            b.media_buy_id for b in explainer.buys
        }:
            continue
        traces.extend(explainer.explain(period, media_buy_id=media_buy_id, offering_id=offering_id))
    if subscription_id is not None and not found:
        raise KeyError(subscription_id)
    return sort_traces(traces)


__all__ = [
    "DEFAULT_GRACE",
    "BuyerReportingState",
    "FleetReconcileIssue",
    "FleetReconcileReport",
    "PeriodLine",
    "PeriodStatus",
    "ReconcileAccount",
    "ReconcileAccountError",
    "ReconcileIssue",
    "ReconcileReport",
    "ReconcileSummary",
    "ResponsibleParty",
    "explain_buyer_coverage",
    "reconcile_fleet",
    "reconcile_subscriptions",
]
