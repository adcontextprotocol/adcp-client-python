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
from adcp.reporting.subscriptions.resolution import SubscriptionOffering

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
    accounts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "definitive": self.definitive,
            "summary": self.summary.to_dict(),
            "periods": [line.to_dict() for line in self.periods],
            "issues": [vars(issue) for issue in self.issues],
            "accounts": list(self.accounts),
        }


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
    def __init__(self, task: str, detail: str) -> None:
        super().__init__(f"{task} failed: {detail}")
        self.task = task


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
    raise _UnreadableError("list_accounts", f"did not finish paging within {_MAX_PAGES} pages")


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
    raise _UnreadableError("get_media_buys", f"did not finish paging within {_MAX_PAGES} pages")


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
) -> tuple[dict[tuple[Any, ...], _Obligation], bool]:
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
    return index, ledger._read_fingerprint is not None


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
    now: datetime | None = None,
) -> ReconcileReport:
    """What every covered account is owed, and its status. See :meth:`ReportingClient.reconcile`."""
    instant = utc_instant(now, field="now") if now is not None else datetime.now(timezone.utc)
    if not isinstance(grace, timedelta) or grace < timedelta(0):
        raise ValueError("grace must be a non-negative timedelta")
    lower = _since(since)
    context = _Context(client, state or BuyerReportingState())
    records = await context.load(accounts=None if accounts == "covered" else list(accounts))
    lines: list[PeriodLine] = []
    complete = context.roster_complete
    for record in records:
        readable = await context.refresh(record)
        obligations: dict[tuple[Any, ...], _Obligation] = {}
        if readable:
            try:
                obligations, walked = await _obligations(client, record.account_id, instant)
                complete = complete and walked
            except Exception as error:  # noqa: BLE001
                complete = False
                context.issue(
                    "SUBSCRIPTION_LEDGER_UNAVAILABLE",
                    f"The obligations of {record.account_id!r} could not be walked completely "
                    f"({error}); periods are reported without delivery status. Retry later.",
                    account_id=record.account_id,
                )
        for state_item in context.subscriptions:
            sub_id = str(state_item.subscription_id)
            if subscription_id is not None and sub_id != subscription_id:
                continue
            if not _scope_covers(state_item, record) and not any(
                entry.account_id == record.account_id
                for entry in getattr(state_item, "accounts_unauthorized", None) or ()
            ):
                continue
            try:
                explainer = context.explainer(state_item, record, instant)
            except CoverageInputError as error:
                complete = False
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
    return ReconcileReport(
        as_of=iso_z(instant),
        definitive=complete and not context.issues,
        summary=ReconcileSummary(**counts),
        periods=tuple(lines),
        issues=tuple(context.issues),
        accounts=tuple(record.account_id for record in records),
    )


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
    "PeriodLine",
    "PeriodStatus",
    "ReconcileIssue",
    "ReconcileReport",
    "ReconcileSummary",
    "ResponsibleParty",
    "explain_buyer_coverage",
    "reconcile_subscriptions",
]
