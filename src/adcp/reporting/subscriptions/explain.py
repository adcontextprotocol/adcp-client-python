"""Why one period is, or is not, owed: the shared ``CoverageTrace`` (AdCP 3.3, experimental).

:func:`explain_subscription_coverage` classifies periods of one subscription on
one account against the same facts :func:`owed_subscription_periods` reads. The
verdict is the owed rule's own: a period is ``owed`` exactly when the rule owes
it. The reason is the first rule, in the spec's fixed order, that rejects the
period, with the facts that rule read. Seller and buyer build the identical
trace from the identical wire-visible facts, so a disagreement between two
traces of the same period is a comparison of two documents:

* equal ``inputs_digest`` and different verdicts is a logic bug in one SDK;
* different ``inputs_digest`` means one side read stale or different state,
  and :func:`diff_coverage_traces` names the first rule whose facts differ.

The reason codes, their verdicts and their rules are read from the spec's
``reason-codes.json``, packaged with the SDK for the AdCP release it serves.
Explanations are rendered from :data:`EXPLAIN_TEMPLATES`, one sentence per
reason, so both sides emit identical text for identical facts. ``now`` is not
part of the digest, but whether the period has ended is, so
``period_not_ended`` is never a same-inputs difference.

``seller_only`` facts (when the seller froze a period, whether a buy is
visible to the caller, the obligation id) never enter the digest, and a
trace with them is never sent to a buyer: :meth:`CoverageTrace.for_buyer`
strips them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.subscriptions._clock import (
    CoverageInputError,
    PeriodCalendar,
    iso_z,
    require_zone,
    utc_instant,
)
from adcp.reporting.subscriptions.coverage import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_PERIODS,
    AccountTimezoneChange,
    OwedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionException,
    SubscriptionVersionWindow,
    _Chain,
    _coverage_context,
    _CoverageContext,
    _instant,
    _Regime,
    _timezone_at,
    authorization_chain_start,
    authorization_regranted_at,
    owed_subscription_periods,
)
from adcp.reporting.subscriptions.resolution import SubscriptionOffering
from adcp.reporting.subscriptions.vocabulary import COVERAGE_RULE_ORDER

#: The ``CoverageTrace`` document version.
TRACE_VERSION = 1
#: Version of :data:`EXPLAIN_TEMPLATES`.
EXPLAIN_TEMPLATES_VERSION = 1

CoverageVerdict = Literal["owed", "not_owed"]
CoverageSide = Literal["buyer", "seller"]
StepResult = Literal["pass", "fail", "not_applicable"]

#: One sentence per reason code. Placeholders are filled from the trace's
#: subject and the failing step's facts; instants render as RFC 3339 UTC with
#: minute precision when the seconds are zero. Both SDKs ship this table
#: verbatim (proposed for the spec's ``explain-templates.json``).
EXPLAIN_TEMPLATES: Mapping[str, str] = {
    "owed": (
        "Owed: {media_buy_id} under {offering_id} for the period {period_start} to "
        "{period_end}, due at {expected_at}."
    ),
    "owed:regrant_backfill": (
        "Owed as a re-grant backfill: the period {period_start} to {period_end} falls in the "
        "interruption from {unauthorized_at} to {regranted_at}, which the seller backfills; "
        "due at {expected_at}."
    ),
    "owed:withheld": (
        "Owed but withheld: the caller lost authority for {account_id} at {unauthorized_at}, "
        "so the period {period_start} to {period_end} is due again only after a re-grant."
    ),
    "period_not_ended": (
        "Not owed yet: the period {period_start} to {period_end} has not ended. It becomes "
        "owed when it ends unless a later change removes it."
    ),
    "not_in_scope": (
        "Not owed: version {subscription_version} of {subscription_id} does not select "
        "account {account_id}."
    ),
    "excluded:excluded_by_buyer": (
        "Not owed: the buyer excluded account {account_id} from {subscription_id} from {from}."
    ),
    "not_authorized": (
        "Not owed: the caller held no authority for {account_id} over the period "
        "{period_start} to {period_end}."
    ),
    "before_coverage_start": (
        "Not owed: the period starts {period_start}, before coverage starts at {coverage_start}."
    ),
    "subscription_inactive": (
        "Not owed: {subscription_id} was inactive from {deactivated_at} when the period "
        "started at {period_start}."
    ),
    "excluded:offering_unsupported": (
        "Not owed: {offering_id} does not report {media_buy_id} under the selector of "
        "version {subscription_version}."
    ),
    "outside_flight": (
        "Not owed: the period ends {period_end}, at or before {media_buy_id} starts at "
        "{start_time}."
    ),
    "after_stop": (
        "Not owed: the period starts {period_start}, at or after {media_buy_id} stopped at "
        "{stop_time}."
    ),
    "before_confirmation": (
        "Not owed: the period ends {period_end}, before {media_buy_id} was confirmed"
        "{confirmed_clause}."
    ),
    "excluded:capacity": (
        "Not owed: account {account_id} is beyond the seller's subscription capacity from {from}."
    ),
    "excluded:overridden": (
        "Not owed: another of the caller's delivery configurations takes {account_id} for "
        "this feed from {from}."
    ),
    "excluded:action_required": (
        "Not owed: the seller serves account {account_id} only after a pending action "
        "completes; excluded from {from}."
    ),
    "gap:unauthorized": (
        "Not owed: the period falls in the authorization gap from {from} to {until}."
    ),
    "cutover_stretch": (
        "Not owed: the period {period_start} to {period_end} in {timezone} lies outside the "
        "span its side owes around the handoff at {handoff}."
    ),
    "owed_under_other_version": (
        "Not owed under version {subscription_version}: the side before the handoff at "
        "{handoff} owes this time."
    ),
    "buy_not_visible": ("Not owed: the caller cannot see {media_buy_id} through get_media_buys."),
}


# -- the spec vocabulary --------------------------------------------------------


@dataclass(frozen=True)
class CoverageReason:
    """One code of the spec's frozen ``reason-codes.json``."""

    code: str
    verdict: CoverageVerdict
    #: The rule that assigns it; ``None`` for the outcome codes.
    rule: str | None
    #: ``both``, or ``seller`` for a seller-only code.
    side: str
    definition: str


@lru_cache(maxsize=1)
def coverage_reasons() -> Mapping[str, CoverageReason]:
    """The reason codes from the spec's ``reason-codes.json`` for the release served.

    Read from the compliance tree packaged with the SDK
    (``adcp/_compliance/<ADCP_VERSION>/test-vectors/reporting-subscriptions/``).
    """
    release = files("adcp").joinpath("ADCP_VERSION").read_text(encoding="utf-8").strip()
    path = files("adcp").joinpath(
        f"_compliance/{release}/test-vectors/reporting-subscriptions/reason-codes.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    if tuple(document["rule_order"]) != COVERAGE_RULE_ORDER:
        raise RuntimeError("reason-codes.json rule_order differs from COVERAGE_RULE_ORDER")
    return {
        item["code"]: CoverageReason(
            code=item["code"],
            verdict=item["verdict"],
            rule=item.get("rule"),
            side=item["side"],
            definition=item["definition"],
        )
        for item in document["reasons"]
    }


def _reason(code: str) -> CoverageReason:
    reason = coverage_reasons().get(code)
    if reason is None:  # pragma: no cover - a vocabulary bug, never input
        raise RuntimeError(f"{code!r} is not a spec reason code")
    return reason


# -- the trace -------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageTraceStep:
    """One rule as applied to the period, with the facts it read."""

    rule: str
    result: StepResult
    facts: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "result": self.result, "facts": dict(self.facts)}


@dataclass(frozen=True)
class CoverageTrace:
    """Why one period of one (buy, offering) under one subscription is or is not owed.

    The same JSON in both SDKs and on both sides of the wire
    (:meth:`to_dict`). ``seller_only`` is never sent to a buyer.
    """

    side: CoverageSide
    subject: Mapping[str, Any]
    verdict: CoverageVerdict
    reason: str
    explanation: str
    steps: tuple[CoverageTraceStep, ...]
    inputs_digest: str
    seller_only: Mapping[str, Any] | None = None
    trace_version: int = TRACE_VERSION

    @property
    def rule(self) -> str | None:
        """The rule that assigned :attr:`reason`; ``None`` for an outcome code."""
        failed = next((step.rule for step in self.steps if step.result == "fail"), None)
        return failed if failed is not None else _reason(self.reason).rule

    def for_buyer(self) -> CoverageTrace:
        """This trace without ``seller_only``, the form that may leave the seller."""
        return CoverageTrace(
            side=self.side,
            subject=self.subject,
            verdict=self.verdict,
            reason=self.reason,
            explanation=self.explanation,
            steps=self.steps,
            inputs_digest=self.inputs_digest,
            trace_version=self.trace_version,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "trace_version": self.trace_version,
            "side": self.side,
            "subject": _plain(self.subject),
            "verdict": self.verdict,
            "reason": self.reason,
            "explanation": self.explanation,
            "steps": [step.to_dict() for step in self.steps],
            "inputs_digest": self.inputs_digest,
        }
        if self.seller_only is not None:
            result["seller_only"] = _plain(self.seller_only)
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageTrace:
        if value.get("trace_version") != TRACE_VERSION:
            raise ValueError(f"unsupported trace_version {value.get('trace_version')!r}")
        return cls(
            side=value["side"],
            subject=dict(value["subject"]),
            verdict=value["verdict"],
            reason=value["reason"],
            explanation=value["explanation"],
            steps=tuple(
                CoverageTraceStep(item["rule"], item["result"], dict(item.get("facts") or {}))
                for item in value["steps"]
            ),
            inputs_digest=value["inputs_digest"],
            seller_only=value.get("seller_only"),
        )


@dataclass(frozen=True)
class CoverageTraceDifference:
    """Where two traces of the same period first disagree."""

    field: str
    #: The first rule whose result or facts differ, when the difference is in a step.
    rule: str | None
    left: Any
    right: Any
    #: Whether the digests are equal: then the difference is a logic bug in one SDK,
    #: otherwise one side read different state.
    same_inputs: bool

    def describe(self) -> str:
        where = f"rule {self.rule!r}" if self.rule else self.field
        cause = (
            "Equal inputs_digest: one SDK applied the rule differently."
            if self.same_inputs
            else "Different inputs_digest: the two sides read different state."
        )
        return f"First difference at {where}: {self.left!r} != {self.right!r}. {cause}"


def diff_coverage_traces(
    left: CoverageTrace, right: CoverageTrace
) -> CoverageTraceDifference | None:
    """The first difference between two traces of one period, or ``None``.

    Compares the subject, then each rule's result and facts in rule order,
    then the verdict and reason. ``side``, ``explanation`` wording beyond the
    reason, and ``seller_only`` are not compared.
    """
    same = left.inputs_digest == right.inputs_digest
    # principal_id is the seller's name for the caller; a buyer trace omits it.
    subjects = [
        {key: value for key, value in _plain(trace.subject).items() if key != "principal_id"}
        for trace in (left, right)
    ]
    if subjects[0] != subjects[1]:
        return CoverageTraceDifference("subject", None, subjects[0], subjects[1], same)
    lefts = {step.rule: step for step in left.steps}
    rights = {step.rule: step for step in right.steps}
    for rule in COVERAGE_RULE_ORDER:
        a, b = lefts.get(rule), rights.get(rule)
        if a is None and b is None:
            continue
        a_doc = None if a is None else a.to_dict()
        b_doc = None if b is None else b.to_dict()
        if _plain(a_doc) != _plain(b_doc):
            return CoverageTraceDifference("steps", rule, a_doc, b_doc, same)
    for name in ("verdict", "reason"):
        if getattr(left, name) != getattr(right, name):
            return CoverageTraceDifference(
                name, None, getattr(left, name), getattr(right, name), same
            )
    if not same:
        return CoverageTraceDifference(
            "inputs_digest", None, left.inputs_digest, right.inputs_digest, same
        )
    return None


# -- helpers ---------------------------------------------------------------------


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default, sort_keys=True))


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_z(value)
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value) if isinstance(value, (set, frozenset)) else list(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _z(value: datetime | None) -> str | None:
    return None if value is None else iso_z(value)


def _text_instant(value: Any) -> str:
    """An instant for an explanation: RFC 3339 UTC, minutes when the seconds are zero."""
    if value is None:
        return "an unknown time"
    if isinstance(value, str):
        instant = _instant(value, field="explanation")
        if instant is None:  # pragma: no cover
            return value
        value = instant
    instant = utc_instant(value)
    if instant.second == 0 and instant.microsecond == 0:
        return instant.strftime("%Y-%m-%dT%H:%MZ")
    return iso_z(instant)


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", exclude_none=True, by_alias=True)
        assert isinstance(dumped, dict)
        return dumped
    return {key: item for key, item in dict(value).items() if item is not None}


_SET_FIELDS = ("account_ids", "operator_domains")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _normalized_configuration(configuration: Any) -> dict[str, Any]:
    """A version's configuration as both sides read it: sets sorted, defaults spelled out."""
    payload = {
        key: value
        for key, value in _dump(configuration).items()
        if key not in {"active", "replace_account_configs", "context", "ext"}
    }
    scope = dict(payload.get("scope") or {})
    if "account_ids" in scope:
        scope["account_ids"] = sorted(scope["account_ids"])
    payload["scope"] = scope
    for name in ("exclude_account_ids", "operator_domains"):
        if name in payload:
            payload[name] = sorted(payload[name])
    if "destination_overrides" in payload:
        payload["destination_overrides"] = sorted(
            (
                {**item, **{name: sorted(item[name]) for name in _SET_FIELDS if name in item}}
                for item in payload["destination_overrides"]
            ),
            key=lambda item: str(item.get("destination_id")),
        )
    selector = dict(payload.get("selector") or {})
    if "dimensions" in selector:
        selector["dimensions"] = sorted(selector["dimensions"])
    selector.setdefault("match", "exact" if "dimensions" in selector else "finest")
    payload["selector"] = selector
    return payload


def _authorization_projection(
    authorizations: Sequence[SubscriptionAuthorization], retention_days: int | None
) -> dict[str, Any]:
    """The wire-visible authorization: chain start, latest re-grant, and a current loss."""
    if not authorizations:
        return {}
    retention = 10**6 if retention_days is None else retention_days
    last = authorizations[-1]
    if last.unauthorized_at is None:
        return {
            "authorized_at": _z(
                authorization_chain_start(authorizations, status_retention_days=retention)
            ),
            "regranted_at": _z(
                authorization_regranted_at(authorizations, status_retention_days=retention)
            ),
        }
    open_history = [*authorizations[:-1], SubscriptionAuthorization(last.authorized_at)]
    return {
        "authorized_at": _z(
            authorization_chain_start(open_history, status_retention_days=retention)
        ),
        "regranted_at": _z(
            authorization_regranted_at(open_history, status_retention_days=retention)
        ),
        "unauthorized_at": _z(last.unauthorized_at),
    }


def _offering_facts(offering: SubscriptionOffering | None) -> dict[str, Any] | None:
    if offering is None:
        return None
    return {
        "offering_id": offering.offering_id,
        "report_definition_id": offering.report_definition_id,
        "feed_purpose": offering.feed_purpose,
        "period_duration": offering.period_duration,
        "alignment": offering.alignment,
        "delivery_sla": offering.delivery_sla,
        # The zone the offering publishes; an account-resolved source zone is
        # not on the wire, so it is not part of the facts.
        "period_timezone": (
            None
            if offering.alignment == "source_timezone"
            and offering.period_timezone_policy != "fixed"
            else offering.period_timezone
        ),
        "period_anchor": _z(offering.period_anchor),
        "available_from": _z(offering.available_from),
        "withdrawn_at": _z(offering.withdrawn_at),
    }


def _buy_facts(buy: SubscriptionBuy) -> dict[str, Any]:
    return {
        "media_buy_id": buy.media_buy_id,
        "confirmed_at": _z(buy.confirmed_at),
        "start_time": _z(buy.start_time),
        "end_time": _z(buy.end_time),
        "canceled_at": _z(buy.canceled_at),
        "cancellation_effective_at": _z(buy.cancellation_effective_at),
        "completed_at": _z(buy.completed_at),
        "packages": [
            {"package_id": package_id, "reporting_delivery_offering_ids": sorted(ids)}
            for package_id, ids in buy.product_offering_ids()
        ],
        "flight_history": [
            {
                "superseded_at": _z(flight.superseded_at),
                "start_time": _z(flight.start_time),
                "stop_time": _z(flight.stop_time),
            }
            for flight in buy.superseded_flights
        ],
    }


def _exception_facts(item: SubscriptionException) -> dict[str, Any]:
    reason = "gap" if item.reason == "unauthorized" else item.reason
    facts: dict[str, Any] = {"reason": reason, "from": _z(item.from_), "until": _z(item.until)}
    if item.media_buy_id is not None:
        facts["media_buy_id"] = item.media_buy_id
    if item.offering_id is not None:
        facts["offering_id"] = item.offering_id
    if item.package_ids:
        facts["package_ids"] = sorted(item.package_ids)
    return facts


def _window_at(
    windows: Sequence[SubscriptionVersionWindow], instant: datetime
) -> SubscriptionVersionWindow:
    """The version whose ``[accepted_at, ends_at)`` holds ``instant``, else the nearest one."""
    for window in windows:
        if window.accepted_at <= instant and (window.ends_at is None or instant < window.ends_at):
            return window
    if instant < windows[0].accepted_at:
        return windows[0]
    return windows[-1]


# -- period selection ------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    buy: SubscriptionBuy
    offering: SubscriptionOffering | None
    calendar: PeriodCalendar | None
    start: datetime
    end: datetime
    zone: str


def _period_spec(period: Any) -> tuple[str, Any]:
    """``("date", date)``, ``("instant", datetime)`` or ``("range", (start, end))``."""
    if isinstance(period, date) and not isinstance(period, datetime):
        return "date", period
    if isinstance(period, datetime):
        return "instant", utc_instant(period, field="period")
    if isinstance(period, str):
        if len(period) == 10:
            try:
                return "date", date.fromisoformat(period)
            except ValueError as error:
                raise CoverageInputError(
                    "invalid_input", f"period {period!r} is not a YYYY-MM-DD date"
                ) from error
        instant = _instant(period, field="period")
        assert instant is not None
        return "instant", instant
    if isinstance(period, Mapping):
        start = _instant(period.get("start"), field="period.start")
        end = _instant(period.get("end"), field="period.end")
        if start is None or end is None or end <= start:
            raise CoverageInputError("invalid_input", "period needs a start before its end")
        return "range", (start, end)
    if isinstance(period, Sequence) and len(period) == 2:
        return _period_spec({"start": period[0], "end": period[1]})
    raise CoverageInputError(
        "invalid_input",
        "period is a YYYY-MM-DD date read in the offering's zone, an instant, or {start, end}",
    )


def _local_day(day: date, zone: str) -> tuple[datetime, datetime]:
    tz = require_zone(zone)
    start = datetime.combine(day, time(0), tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tz)
    return utc_instant(start), utc_instant(end)


def _zones(
    context: _CoverageContext, offering: SubscriptionOffering, around: Iterable[datetime]
) -> list[str]:
    """Every zone the offering's periods can take: both sides of a timezone cutover, and
    each version's ``period_timezone`` override."""
    overrides = list(dict.fromkeys(s.period_timezone for s in context.selectors.values()))
    accounts = [context.account_timezone]
    for change in context.changes:
        if change.offering_id in {None, offering.offering_id}:
            accounts.extend((change.previous_timezone, change.timezone))
    for instant in around:
        accounts.append(
            _timezone_at(instant, context.account_timezone, context.changes, offering.offering_id)
        )
    zones: list[str] = []
    for override in overrides:
        for zone in accounts:
            try:
                name = offering.zone(zone, override)
            except CoverageInputError:
                continue
            if name not in zones:
                zones.append(name)
    if not zones:
        offering.zone(context.account_timezone, None)  # raises the input error
    return zones


def _periods_for(
    calendar: PeriodCalendar, kind: str, value: Any, zone: str
) -> list[tuple[datetime, datetime]]:
    if kind == "range":
        start, end = value
        if (
            calendar.floor(start) == start
            and calendar.ceil(start + timedelta(microseconds=1)) == end
        ):
            return [(start, end)]
        return []
    if kind == "instant":
        start = calendar.floor(value)
        return list(
            calendar.periods(starts_from=start, starts_before=start + timedelta(microseconds=1))
        )
    day_start, day_end = _local_day(value, zone)
    return list(calendar.periods(starts_from=calendar.ceil(day_start), starts_before=day_end))


# -- the classifier --------------------------------------------------------------


@dataclass
class _Walk:
    steps: list[CoverageTraceStep] = field(default_factory=list)
    reason: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def passed(self, rule: str, facts: Mapping[str, Any] | None = None) -> None:
        self.steps.append(CoverageTraceStep(rule, "pass", dict(facts or {})))

    def skipped(self, rule: str, facts: Mapping[str, Any] | None = None) -> None:
        self.steps.append(CoverageTraceStep(rule, "not_applicable", dict(facts or {})))

    def failed(self, rule: str, reason: str, facts: Mapping[str, Any], **params: Any) -> None:
        self.steps.append(CoverageTraceStep(rule, "fail", dict(facts)))
        self.reason = reason
        self.params.update(params)


def _regime_at(regimes: Sequence[_Regime | None], instant: datetime) -> _Regime | None:
    for regime in regimes:
        if regime is None:
            continue
        if regime.start <= instant and (regime.until is None or instant < regime.until):
            return regime
    return None


def _chain_facts(chains: Sequence[_Chain]) -> dict[str, Any]:
    if not chains:
        return {"authorized_at": None, "chain": "not_advertised"}
    chain = chains[-1]
    regrants = [regranted for _lost, regranted in chain.interruptions if regranted is not None]
    return {
        "authorized_at": _z(chain.start),
        "unauthorized_at": _z(chain.end),
        "regranted_at": _z(max(regrants)) if regrants else None,
        "chain": "continued" if chain.interruptions else ("new" if len(chains) > 1 else "single"),
    }


def _interruption(
    chains: Sequence[_Chain], start: datetime, end: datetime
) -> tuple[datetime, datetime] | None:
    for chain in chains:
        for lost, regranted in chain.interruptions:
            if regranted is not None and end > lost and start < regranted:
                return lost, regranted
    return None


def _classify(
    context: _CoverageContext,
    candidate: _Candidate,
    owed: OwedSubscriptionPeriod | None,
    owed_elsewhere: OwedSubscriptionPeriod | None,
    *,
    now: datetime,
    backfill: bool,
    invisible: bool,
) -> tuple[_Walk, int]:
    """The rule walk for one candidate period. Returns the walk and the subject version."""
    walk = _Walk()
    buy, offering, start, end = candidate.buy, candidate.offering, candidate.start, candidate.end
    window = _window_at(context.windows, start)
    version = owed.delivery_config_version if owed is not None else window.subscription_version
    configuration = _dump(window.configuration)
    scope = configuration.get("scope") or {}

    # scope -----------------------------------------------------------------
    scope_facts: dict[str, Any] = {
        "scope": "account_ids" if "account_ids" in scope else "all_authorized_accounts",
        "operator_domains": sorted(configuration.get("operator_domains") or []) or None,
        "operator": context.account_operator,
    }
    buyer_exclusion = next(
        (
            item
            for item in context.removals
            if item.reason == "excluded_by_buyer" and item.removes(start, buy.media_buy_id)
        ),
        None,
    )
    excluded_ids = {str(item) for item in configuration.get("exclude_account_ids") or ()}
    if invisible:
        walk.failed("scope", "buy_not_visible", {**scope_facts, "buy_visible": False})
        return walk, version
    if buyer_exclusion is not None or (
        not context.membership_intervals and context.account_id in excluded_ids
    ):
        facts = {**scope_facts, "excluded_by_buyer": True}
        if buyer_exclusion is not None:
            facts.update(_exception_facts(buyer_exclusion))
        walk.failed(
            "scope",
            "excluded:excluded_by_buyer",
            facts,
            **{"from": buyer_exclusion.from_ if buyer_exclusion else window.accepted_at},
        )
        return walk, version
    if not context.in_scope[window.subscription_version]:
        walk.failed("scope", "not_in_scope", scope_facts)
        return walk, version
    walk.passed("scope", scope_facts)

    # authorization ---------------------------------------------------------
    chains = context.chains
    authorization_facts = _chain_facts(chains)
    if chains and start >= chains[0].start:
        held = any(
            start >= chain.start and (chain.end is None or end <= chain.end) for chain in chains
        )
        if not held:
            lost = next((c for c in chains if c.end is not None and end > c.end), None)
            walk.failed(
                "authorization",
                "not_authorized",
                authorization_facts,
                unauthorized_at=None if lost is None else lost.end,
            )
            return walk, version
    walk.passed("authorization", authorization_facts)

    # version ---------------------------------------------------------------
    selecting = [w.accepted_at for w in context.windows if context.in_scope[w.subscription_version]]
    coverage_start = max([min(selecting), *([chains[0].start] if chains else [])])
    if candidate.calendar is not None:
        coverage_start = candidate.calendar.ceil(coverage_start)
    version_facts = {
        "subscription_version": window.subscription_version,
        "accepted_at": _z(window.accepted_at),
        "coverage_start": _z(coverage_start),
    }
    if start < coverage_start:
        walk.failed(
            "version", "before_coverage_start", version_facts, coverage_start=coverage_start
        )
        return walk, version
    walk.passed("version", version_facts)

    # activity --------------------------------------------------------------
    inactive = [
        {"from": _z(begin), "until": _z(until)} for begin, until in window.inactive_intervals
    ]
    activity_facts: dict[str, Any] = {"inactive_intervals": inactive}
    interval = next(
        (
            (begin, until)
            for begin, until in window.inactive_intervals
            if begin <= start and (until is None or start < until)
        ),
        None,
    )
    deactivated = (
        window is context.windows[-1] and window.ends_at is not None and start >= window.ends_at
    )
    if interval is not None or deactivated:
        deactivated_at = interval[0] if interval is not None else window.ends_at
        walk.failed(
            "activity",
            "subscription_inactive",
            {**activity_facts, "deactivated_at": _z(deactivated_at)},
            deactivated_at=deactivated_at,
        )
        return walk, version
    walk.passed("activity", activity_facts)

    # offering --------------------------------------------------------------
    regimes = context.regimes(buy) if buy.confirmed_at is not None else []
    regime = _regime_at(regimes, start)
    candidates = sorted({oid for _pkg, ids in buy.product_offering_ids() for oid in ids})
    resolved = [] if regime is None else [item.offering.offering_id for item in regime.resolved]
    selector = context.selectors[window.subscription_version]
    offering_facts = {
        "match": selector.match,
        "candidates": candidates,
        "resolved": resolved,
        "selected": None if offering is None else offering.offering_id,
    }
    unsupported = next(
        (
            item
            for item in context.removals
            if item.reason == "offering_unsupported"
            and item.removes(
                start, buy.media_buy_id, None if offering is None else offering.offering_id
            )
        ),
        None,
    )
    if unsupported is not None:
        walk.failed(
            "offering",
            "excluded:offering_unsupported",
            {**offering_facts, **_exception_facts(unsupported)},
        )
        return walk, version
    resolves = offering is not None and (
        offering.offering_id in resolved
        # An unconfirmed buy resolves nothing yet; judge it by confirmation.
        or (buy.confirmed_at is None and offering.offering_id in candidates)
    )
    if not resolves:
        walk.failed("offering", "excluded:offering_unsupported", offering_facts)
        return walk, version
    walk.passed("offering", offering_facts)

    # flight ----------------------------------------------------------------
    flight_start, flight_stop = buy.flight_at(end)
    flight_facts = {
        "start_time": _z(flight_start),
        "stop_time": _z(flight_stop),
        "as_of": _z(end),
    }
    if flight_stop <= flight_start or end <= flight_start:
        walk.failed("flight", "outside_flight", flight_facts, start_time=flight_start)
        return walk, version
    if start >= flight_stop:
        walk.failed("flight", "after_stop", flight_facts, stop_time=flight_stop)
        return walk, version
    walk.passed("flight", flight_facts)

    # confirmation ----------------------------------------------------------
    confirmation_facts = {"confirmed_at": _z(buy.confirmed_at)}
    if buy.confirmed_at is None or end <= buy.confirmed_at:
        walk.failed(
            "confirmation",
            "before_confirmation",
            confirmation_facts,
            confirmed_clause=(
                f" at {_text_instant(buy.confirmed_at)}"
                if buy.confirmed_at
                else " (it is not confirmed yet)"
            ),
        )
        return walk, version
    walk.passed("confirmation", confirmation_facts)

    # exception -------------------------------------------------------------
    exception = next(
        (
            item
            for item in context.removals
            if item.reason in {"capacity", "overridden", "action_required"}
            and item.removes(start, buy.media_buy_id, offering.offering_id if offering else None)
        ),
        None,
    )
    if exception is not None:
        walk.failed(
            "exception",
            f"excluded:{exception.reason}",
            _exception_facts(exception),
            **{"from": exception.from_, "until": exception.until},
        )
        return walk, version
    walk.passed("exception", {"open": []})

    # gap -------------------------------------------------------------------
    gap = next(
        (
            item
            for item in context.removals
            if item.reason == "unauthorized"
            and item.removes(start, buy.media_buy_id, offering.offering_id if offering else None)
        ),
        None,
    )
    interruption = _interruption(chains, start, end)
    if gap is not None:
        walk.failed(
            "gap",
            "gap:unauthorized",
            _exception_facts(gap),
            **{"from": gap.from_, "until": gap.until},
        )
        return walk, version
    gap_facts = {
        "backfill_on_regrant": backfill,
        "interruption": None
        if interruption is None
        else {"unauthorized_at": _z(interruption[0]), "regranted_at": _z(interruption[1])},
    }
    if interruption is not None and not backfill:
        walk.failed(
            "gap",
            "gap:unauthorized",
            gap_facts,
            **{"from": interruption[0], "until": interruption[1]},
        )
        return walk, version
    walk.passed("gap", gap_facts)

    # cutover and timezone --------------------------------------------------
    handoff = None
    if regime is not None and offering is not None:
        handoff = regime.handoffs.get(offering.offering_id)
    cutover_facts: dict[str, Any] = {"handoff": _z(handoff)}
    timezone_rule = offering is not None and offering.alignment == "account_timezone"
    zone_now = (
        _timezone_at(start, context.account_timezone, context.changes, offering.offering_id)
        if offering is not None
        else None
    )
    zone_facts = {"timezone": candidate.zone, "zone_in_effect": zone_now}
    if owed is None:
        if timezone_rule and selector.period_timezone is None and zone_now != candidate.zone:
            walk.passed("cutover", cutover_facts)
            walk.failed(
                "timezone",
                "cutover_stretch",
                zone_facts,
                handoff=next(
                    (
                        change.changed_at
                        for change in context.changes
                        if change.offering_id in {None, offering.offering_id if offering else None}
                    ),
                    None,
                ),
                timezone=candidate.zone,
            )
            return walk, version
        straddles = handoff is not None and start < handoff < end
        reason = (
            "cutover_stretch" if straddles or owed_elsewhere is None else "owed_under_other_version"
        )
        if owed_elsewhere is not None:
            cutover_facts["owed_under_version"] = owed_elsewhere.delivery_config_version
        walk.failed("cutover", reason, cutover_facts, handoff=handoff, timezone=candidate.zone)
        return walk, version
    walk.passed("cutover", cutover_facts)
    if timezone_rule:
        walk.passed("timezone", zone_facts)
    else:
        walk.skipped("timezone", zone_facts)

    # outcome ---------------------------------------------------------------
    walk.params.update(expected_at=owed.expected_at)
    if end > now:
        walk.reason = "period_not_ended"
    elif owed.withheld or (chains and chains[-1].end is not None and end <= chains[-1].end):
        # reason-codes.json: the period ended at or before the latest
        # unauthorized_at and no re-grant has followed yet.
        last = chains[-1] if chains else None
        walk.reason = "owed:withheld"
        walk.params.update(unauthorized_at=None if last is None else last.end)
    elif backfill and interruption is not None:
        walk.reason = "owed:regrant_backfill"
        walk.params.update(unauthorized_at=interruption[0], regranted_at=interruption[1])
    else:
        walk.reason = "owed"
    return walk, version


class _Defaults(dict[str, Any]):
    """Template values; a fact the walk did not read renders as unknown."""

    def __missing__(self, key: str) -> str:
        return "an unknown time"


def _explanation(reason: str, subject: Mapping[str, Any], params: Mapping[str, Any]) -> str:
    period = subject["period"]
    values: dict[str, Any] = {
        "account_id": subject["account_id"],
        "subscription_id": subject["subscription_id"],
        "subscription_version": subject["subscription_version"],
        "media_buy_id": subject.get("media_buy_id") or "the buy",
        "offering_id": subject.get("offering_id") or "no offering",
        "period_start": _text_instant(period["start"]),
        "period_end": _text_instant(period["end"]),
        "timezone": period.get("timezone") or "UTC",
    }
    for key, value in params.items():
        if isinstance(value, datetime) or (isinstance(value, str) and _RFC3339.match(value)):
            values[key] = _text_instant(value)
        elif value is None:
            values[key] = "an unknown time"
        else:
            values[key] = value
    if params.get("timezone"):
        values["timezone"] = params["timezone"]

    return EXPLAIN_TEMPLATES[reason].format_map(_Defaults(values))


def _inputs_document(
    context: _CoverageContext,
    candidate: _Candidate,
    subject: Mapping[str, Any],
    *,
    authorization: Mapping[str, Any],
    backfill: bool,
    status_retention_days: int | None,
    ended: bool,
) -> dict[str, Any]:
    """The wire-visible facts one period's classification depends on.

    Local to the subject, and in the form both sides can build from what the
    wire carries: the version in effect at the period start, the account's
    authorization as ``list_accounts`` and ``accounts_unauthorized`` show it,
    the zone in effect, the buy as ``get_media_buys`` returns it, the
    offering, and the ``excluded[]`` entries covering the period start. An
    authorization gap enters as whether it covers the period, which the
    seller's ``gaps[]`` and its grant journal state identically. ``now``
    itself is not part of it, but whether the period has ended is: two traces
    taken on either side of the period end differ in their inputs, never only
    in their verdict.
    """
    start, end = candidate.start, candidate.end
    window = _window_at(context.windows, start)
    buy_id = candidate.buy.media_buy_id
    offering_id = candidate.offering.offering_id if candidate.offering else None
    covering = sorted(
        (
            _exception_facts(item)
            for item in context.removals
            if item.reason != "unauthorized" and item.removes(start, buy_id, offering_id)
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    packages = sorted(
        (
            _exception_facts(item)
            for item in context.package_removals
            if item.media_buy_id in {None, buy_id}
            and item.from_ <= start
            and (item.until is None or start < item.until)
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    gap = any(
        item.reason == "unauthorized" and item.removes(start, buy_id, offering_id)
        for item in context.removals
    ) or (not backfill and _interruption(context.chains, start, end) is not None)
    zone = None
    if candidate.offering is not None:
        zone = _timezone_at(
            start, context.account_timezone, context.changes, candidate.offering.offering_id
        )
    return {
        "trace_version": TRACE_VERSION,
        "subject": {k: v for k, v in subject.items() if k != "principal_id"},
        "version": {
            "subscription_version": window.subscription_version,
            "accepted_at": _z(window.accepted_at),
            "ends_at": _z(window.ends_at),
            "inactive_intervals": [[_z(a), _z(b)] for a, b in window.inactive_intervals],
            "configuration": _normalized_configuration(window.configuration),
        },
        "first_selecting_accepted_at": _z(
            min(
                (
                    w.accepted_at
                    for w in context.windows
                    if context.in_scope[w.subscription_version]
                ),
                default=None,
            )
        ),
        "account": {"operator": context.account_operator, "zone_in_effect": zone},
        "authorization": dict(authorization),
        "buy": _buy_facts(candidate.buy),
        "offering": _offering_facts(candidate.offering),
        "exceptions": covering,
        "package_exceptions": packages,
        "authorization_gap": gap,
        "backfill_on_regrant": backfill,
        "status_retention_days": status_retention_days,
        "period_ended": ended,
    }


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_utf8_v1(_plain(document))).hexdigest()


def _parse_z(value: str) -> datetime:
    return utc_instant(datetime.fromisoformat(value.replace("Z", "+00:00")))


class CoverageExplainer:
    """One coverage input, prepared once, explaining as many periods as asked.

    :func:`explain_subscription_coverage` is the one-call form; the buyer's
    ``reconcile`` uses this to classify every period of an account against
    one owed set. The constructor takes the input of
    :func:`explain_subscription_coverage` without the selection.
    """

    def __init__(
        self,
        *,
        subscription: Any,
        account_id: str,
        buys: Sequence[SubscriptionBuy],
        offerings: Sequence[SubscriptionOffering],
        account_timezone: str | None,
        now: datetime,
        side: CoverageSide = "buyer",
        principal_id: str | None = None,
        invisible_media_buy_ids: Iterable[str] = (),
        seller_only: Mapping[str, Any] | None = None,
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
        max_periods: int = DEFAULT_MAX_PERIODS,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.now = utc_instant(now, field="now")
        if side not in ("buyer", "seller"):
            raise ValueError("side is 'buyer' or 'seller'")
        self.side: CoverageSide = side
        self.hidden = frozenset(invisible_media_buy_ids)
        if self.hidden and side != "seller":
            raise ValueError("invisible_media_buy_ids is seller-only")
        self.principal_id = principal_id
        self.seller_only = dict(seller_only or {})
        self.account_id = account_id
        self.account_timezone = account_timezone
        self.backfill = backfill_on_regrant
        self.retention_days = status_retention_days
        self.buys = list(buys)
        self.offerings = {item.offering_id: item for item in offerings}
        context = _coverage_context(
            subscription=subscription,
            account_id=account_id,
            buys=self.buys,
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
        self.context = context
        self.owed: list[OwedSubscriptionPeriod] = []
        if context is not None:
            self.owed = owed_subscription_periods(
                subscription=context.windows,
                account_id=account_id,
                buys=[buy for buy in self.buys if buy.media_buy_id not in self.hidden],
                offerings=offerings,
                account_timezone=account_timezone,
                now=self.now,
                authorizations=authorizations,
                exceptions=exceptions,
                gaps=gaps,
                coverage=coverage,
                backfill_on_regrant=backfill_on_regrant,
                status_retention_days=status_retention_days,
                destinations=destinations,
                account_operator=account_operator,
                timezone_changes=timezone_changes,
                include_unresolved=True,
                max_periods=max_periods,
                max_iterations=max_iterations,
            )
        self._index: dict[tuple[str, str, datetime, datetime, str], OwedSubscriptionPeriod] = {}
        self._stubs: dict[tuple[str, str], tuple[datetime, datetime]] = {}
        for item in self.owed:
            start, end = _parse_z(item.period_start), _parse_z(item.period_end)
            for buy_id in item.media_buy_ids:
                self._index[(buy_id, item.offering_id, start, end, item.source_timezone)] = item
                if item.stub:
                    self._stubs[(buy_id, item.offering_id)] = (start, end)
        self._authorization = _authorization_projection(authorizations, status_retention_days)

    @property
    def subscription_id(self) -> str | None:
        return None if self.context is None else self.context.subscription_id

    def owed_period(
        self, media_buy_id: str, offering_id: str, start: datetime, end: datetime, zone: str
    ) -> OwedSubscriptionPeriod | None:
        """The owed period (including not yet ended) for one buy, if any."""
        return self._index.get((media_buy_id, offering_id, start, end, zone))

    def _calendar(self, offering: SubscriptionOffering, zone: str) -> PeriodCalendar:
        return PeriodCalendar.for_schedule(
            period_duration=offering.period_duration,
            delivery_sla=offering.delivery_sla,
            zone_name=zone,
            anchor=offering.period_anchor if offering.alignment == "billing_cycle" else None,
            local=offering.alignment != "utc",
        )

    def select(
        self, period: Any, *, media_buy_id: str | None = None, offering_id: str | None = None
    ) -> list[_Candidate]:
        """The (buy, offering, period) candidates a ``period`` selection names."""
        context = self.context
        if context is None:
            return []
        kind, value = _period_spec(period)
        selected = [b for b in self.buys if media_buy_id is None or b.media_buy_id == media_buy_id]
        if media_buy_id is not None and not selected:
            raise CoverageInputError(
                "invalid_input", f"media_buy_id {media_buy_id!r} is not among the buys passed"
            )
        if offering_id is not None and offering_id not in self.offerings:
            raise CoverageInputError(
                "invalid_input", f"offering_id {offering_id!r} is not among the offerings passed"
            )
        zone_default = self.account_timezone or "UTC"
        candidates: list[_Candidate] = []
        for buy in selected:
            listed = sorted({oid for _pkg, ids in buy.product_offering_ids() for oid in ids})
            wanted = (
                [offering_id]
                if offering_id is not None
                else [o for o in listed if o in self.offerings]
            )
            if not wanted:
                if kind == "range":
                    periods = [value]
                elif kind == "instant":
                    day = value.astimezone(require_zone(zone_default)).date()
                    periods = [_local_day(day, zone_default)]
                else:
                    periods = [_local_day(value, zone_default)]
                candidates.extend(
                    _Candidate(buy, None, None, s, e, zone_default) for s, e in periods
                )
                continue
            for oid in wanted:
                offering = self.offerings[oid]
                if kind == "range":
                    reference = value[0]
                elif kind == "instant":
                    reference = value
                else:
                    reference = _local_day(value, zone_default)[0]
                seen: set[tuple[datetime, datetime]] = set()
                for zone in _zones(context, offering, [reference, reference + timedelta(days=1)]):
                    calendar = self._calendar(offering, zone)
                    periods = _periods_for(calendar, kind, value, zone)
                    if (
                        kind == "range"
                        and not periods
                        and self._stubs.get((buy.media_buy_id, oid)) == tuple(value)
                    ):
                        periods = [value]
                    for s, e in periods:
                        if (s, e) not in seen:
                            seen.add((s, e))
                            candidates.append(_Candidate(buy, offering, calendar, s, e, zone))
                if kind == "range" and not seen:
                    raise CoverageInputError(
                        "invalid_input",
                        f"{iso_z(value[0])}/{iso_z(value[1])} is not a period of offering {oid!r}",
                    )
        return candidates

    def flight_candidates(
        self, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[_Candidate]:
        """Every period of each listed offering that overlaps each buy's flight.

        Bounded by ``[since, until)`` (``until`` defaults to now), so the
        buyer's ``reconcile`` can list what is not owed, and why.
        """
        context = self.context
        if context is None:
            return []
        upper_bound = self.now if until is None else utc_instant(until, field="until")
        result: list[_Candidate] = []
        for buy in self.buys:
            low = buy._earliest_start()
            high = min(buy._latest_stop(), upper_bound)
            if since is not None:
                low = max(low, utc_instant(since, field="since"))
            if high <= low:
                continue
            listed = sorted({oid for _pkg, ids in buy.product_offering_ids() for oid in ids})
            for oid in [o for o in listed if o in self.offerings]:
                offering = self.offerings[oid]
                # One candidate per period, as select() does, however many
                # zones the offering's periods can take.
                seen: set[tuple[datetime, datetime]] = set()
                for zone in _zones(context, offering, [low, high]):
                    calendar = self._calendar(offering, zone)
                    for s, e in calendar.periods(
                        starts_from=calendar.floor(low), starts_before=high
                    ):
                        if (s, e) not in seen:
                            seen.add((s, e))
                            result.append(_Candidate(buy, offering, calendar, s, e, zone))
        return result

    def trace(self, candidate: _Candidate) -> CoverageTrace:
        """The trace of one candidate."""
        context = self.context
        assert context is not None
        key_offering = candidate.offering.offering_id if candidate.offering else ""
        buy_id = candidate.buy.media_buy_id
        owed_item = self._index.get(
            (buy_id, key_offering, candidate.start, candidate.end, candidate.zone)
        )
        elsewhere = None
        if owed_item is None and candidate.offering is not None:
            elsewhere = next(
                (
                    item
                    for (owed_buy, oid, s, e, _zone), item in self._index.items()
                    if owed_buy == buy_id
                    and oid == key_offering
                    and s < candidate.end
                    and e > candidate.start
                ),
                None,
            )
        walk, version = _classify(
            context,
            candidate,
            owed_item,
            elsewhere,
            now=self.now,
            backfill=self.backfill,
            invisible=buy_id in self.hidden,
        )
        assert walk.reason is not None
        reason = _reason(walk.reason)
        subject: dict[str, Any] = {
            "account_id": self.account_id,
            "subscription_id": context.subscription_id,
            "subscription_version": version,
            "media_buy_id": buy_id,
            "offering_id": candidate.offering.offering_id if candidate.offering else None,
            "period": {
                "start": iso_z(candidate.start),
                "end": iso_z(candidate.end),
                "timezone": candidate.zone,
            },
        }
        if self.principal_id is not None:
            subject = {"principal_id": self.principal_id, **subject}
        inputs = _inputs_document(
            context,
            candidate,
            subject,
            authorization=self._authorization,
            backfill=self.backfill,
            status_retention_days=self.retention_days,
            ended=candidate.end <= self.now,
        )
        only = None
        if self.side == "seller":
            only = {"buy_visible": buy_id not in self.hidden, **self.seller_only}
        return CoverageTrace(
            side=self.side,
            subject=subject,
            verdict=reason.verdict,
            reason=reason.code,
            explanation=_explanation(reason.code, subject, walk.params),
            steps=tuple(walk.steps),
            inputs_digest=_digest(inputs),
            seller_only=only,
        )

    def explain(
        self, period: Any, *, media_buy_id: str | None = None, offering_id: str | None = None
    ) -> list[CoverageTrace]:
        traces = [
            self.trace(candidate)
            for candidate in self.select(period, media_buy_id=media_buy_id, offering_id=offering_id)
        ]
        return sort_traces(traces)


def sort_traces(traces: Iterable[CoverageTrace]) -> list[CoverageTrace]:
    """Traces by period start, buy and offering (byte order), then zone."""
    return sorted(
        traces,
        key=lambda t: (
            t.subject["period"]["start"],
            str(t.subject.get("media_buy_id")).encode(),
            str(t.subject.get("offering_id") or "").encode(),
            t.subject["period"]["timezone"],
        ),
    )


def explain_subscription_coverage(
    *,
    period: Any,
    media_buy_id: str | None = None,
    offering_id: str | None = None,
    **inputs: Any,
) -> list[CoverageTrace]:
    """One :class:`CoverageTrace` per (buy, offering, period) the request selects.

    Takes the input of :func:`owed_subscription_periods` (``subscription``,
    ``account_id``, ``buys``, ``offerings``, ``account_timezone``, ``now``
    and the optional facts), plus:

    :param period: a ``YYYY-MM-DD`` local date, read in each offering's zone
        (every period of the offering that starts on that date); an instant
        (the period containing it); or ``{"start", "end"}``, which must be a
        period of the offering's schedule.
    :param media_buy_id: one buy; every buy passed when omitted.
    :param offering_id: one offering; every offering the buy's packages list
        when omitted. A buy that lists no offering yields one trace without
        ``offering_id``.
    :param side: ``buyer`` (default) or ``seller``; only the seller passes
        ``invisible_media_buy_ids`` (buys in the account the caller cannot see,
        reason ``buy_not_visible``) and ``seller_only``.
    :param principal_id: the seller's name for the caller, echoed in the subject.

    Traces are sorted by period start, buy and offering (byte order). A buy
    without a match for the selection yields no trace.
    """
    explainer = CoverageExplainer(**inputs)
    return explainer.explain(period, media_buy_id=media_buy_id, offering_id=offering_id)


__all__ = [
    "CoverageExplainer",
    "sort_traces",
    "EXPLAIN_TEMPLATES",
    "EXPLAIN_TEMPLATES_VERSION",
    "TRACE_VERSION",
    "CoverageReason",
    "CoverageSide",
    "CoverageTrace",
    "CoverageTraceDifference",
    "CoverageTraceStep",
    "CoverageVerdict",
    "StepResult",
    "coverage_reasons",
    "diff_coverage_traces",
    "explain_subscription_coverage",
]
