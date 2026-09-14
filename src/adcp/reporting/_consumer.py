"""Buyer side of the AdCP 3.2.0-rc.3 consumer-status loop.

The seller half lives in :mod:`adcp.reporting.ledger`.  This is the other end:
a buyer that read a revision decides what it owes the seller and by when.

Three jobs, and each exists because getting it wrong is silent:

**Classify a contract violation, not a measurement dispute.**
:func:`classify_content_mismatch` decides which of the six closed
``mismatch_code`` values a revision violated.  Every one is decidable from the
obligation, the pinned report definition, and the revision itself, with no
reference to either party's own ad server.  "Your impression count is 12% below
mine" is deliberately *not* in the set -- that is a measurement disagreement,
settled through ``measurement_terms`` and ``makegood_policy``.  A buyer that
routes count disputes through this loop turns an operational channel into a
billing argument nobody agreed to have here.

**Post by the deadline, not by scope close.**
:func:`plan_consumer_statuses` works out which periods are due now.  rc.3 moved
the buyer's duty from "have a status before you close the scope" to
"``expected_at + automated_recovery_window_seconds``".  A buyer that only
reports at scope close is silent for the whole flight, and the seller's
``obligation_counts.consumer_status_pending`` counts it the entire time.  A
still-retrying buyer posts ``revision_missing`` or ``unreadable`` by the
deadline and supersedes it later rather than staying quiet.

**Surface what the seller said back.**
:class:`ConsumerLoopView` carries the seller's ``consumer_status_pending``
count, the issue lifecycle fields (``opened_at``, ``issue_state``,
``external_ref``), and ``operations_contact``.  Without these an agent cannot
age a work item, correlate it with its own tracker, or find a human when the
protocol runs out of moves.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from adcp.types import (
    GetReportingStatusRequest,
    ReportingObligation,
    ReportingRevision,
    ReportingStatusIssue,
)

__all__ = [
    "ConsumerLoopView",
    "ConsumerStatusCheckpointStore",
    "ConsumerStatusPlanError",
    "ConsumerStatusPostError",
    "ConsumerStatusPostResult",
    "ConsumerStatusIntent",
    "ReportingContentReading",
    "ReportingFailureCode",
    "InMemoryConsumerStatusCheckpoints",
    "ReportingOperationsContactView",
    "classify_content_mismatch",
    "consumer_status_chain_key",
    "load_consumer_loop_view",
    "plan_consumer_statuses",
    "post_consumer_statuses",
]

#: The closed rc.3 reason set, in precedence order.  ``schema_nonconformant``
#: is checked *after* ``metric_missing`` on purpose: the spec says a metric that
#: is simply absent uses ``metric_missing`` even when the pinned schema declares
#: it required, so structural validation must not shadow it.
MismatchCode = Literal[
    "scope_media_buy_missing",
    "coverage_short",
    "metric_missing",
    "schema_nonconformant",
    "currency_mismatch",
    "period_mismatch",
]

ConsumerStatusValue = Literal[
    "received",
    "obligation_missing",
    "revision_missing",
    "unreadable",
    "content_mismatch",
]

#: Closed typed reason a named revision was unreadable. Agents dispatch on
#: this value, never on prose or a provider's response body.
ReportingFailureCode = Literal[
    "access_denied",
    "resource_not_found",
    "integrity_mismatch",
    "reader_incompatible",
    "transport_failed",
]


class ConsumerStatusPlanError(ValueError):
    """The caller's inputs cannot support an honest statement.

    Raised rather than guessing. Every guess this function could make is a
    claim attributed to the buyer about the seller's behaviour, and filing the
    wrong one is worse than refusing to file: ``revision_missing`` accuses the
    seller of publishing nothing, ``received`` asserts bytes were consumed.
    """


def _utc(value: datetime) -> datetime:
    """Normalize to UTC, refusing a naive datetime.

    ``astimezone`` on a naive value silently assumes the *local* machine zone,
    so a buyer running in a non-UTC container would stamp every statement hours
    off and the seller's clock-skew check would reject them -- or worse, accept
    them and record the wrong arrival evidence. Refusing is the only safe
    reading, because there is no correct guess.
    """
    if value.tzinfo is None:
        raise ValueError(
            f"{value!r} is naive; consumer-status timestamps must carry an explicit "
            "timezone (use datetime.now(timezone.utc), not datetime.now())"
        )
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _ids(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Normalize an id sequence whose items may be models, enums, or strings."""
    if not values:
        return ()
    out: list[str] = []
    for item in values:
        root = getattr(item, "root", item)
        out.append(str(getattr(root, "value", root)))
    return tuple(sorted(out))


@dataclass(frozen=True)
class ReportingContentReading:
    """What the buyer actually read out of one revision.

    Deliberately *not* the revision record the seller published: the whole
    point of ``content_mismatch`` is that the two disagree.  A buyer fills this
    in from its own parse of the rows, which is why every field is what was
    observed rather than what was promised.
    """

    reporting_revision_id: str
    #: Media buys with at least one row, **including** explicit zero rows. A
    #: buy present only as an explicit zero is covered; a buy with neither rows
    #: nor an explicit zero is ``scope_media_buy_missing``, because the revision
    #: cannot then distinguish zero delivery from an omitted buy.
    media_buy_ids: tuple[str, ...] = ()
    package_ids: tuple[str, ...] = ()
    #: Metric names present in the rows, matched against the pinned report
    #: definition's ``metrics[].name``.
    metric_names: tuple[str, ...] = ()
    #: Observed unit per metric, matched against the unit the definition fixed.
    metric_units: Mapping[str, str] = field(default_factory=dict)
    #: Observed unit per control-total name.
    control_total_units: Mapping[str, str] = field(default_factory=dict)
    #: Values of the time dimension the pinned grain declares, if any. Used for
    #: the half-open period check, so a value exactly equal to ``period.end``
    #: is out of range.
    time_dimension_values: tuple[datetime, ...] = ()
    #: ``False`` when the rows failed structural validation against the pinned
    #: ``schema_uri``/``schema_sha256``.
    schema_conformant: bool = True
    #: The Core revision binding the buyer recomputed. Required to file either
    #: ``received`` or ``content_mismatch``: it proves which exact bytes the
    #: buyer is describing.
    observed_revision_content_sha256: str | None = None
    #: Set when the revision was *advertised* but its content could not be
    #: consumed at all. Turns the reading into ``unreadable`` rather than
    #: ``received``/``content_mismatch``, which need bytes to talk about. The
    #: two are genuinely different claims: ``unreadable`` says "I could not
    #: read what you published", ``content_mismatch`` says "I read it and it
    #: contradicts what we agreed".
    failure_code: ReportingFailureCode | None = None
    #: When the named revision first became consumable *to this consumer*.
    #: ``status_as_of`` for a ``received`` statement, because the spec has
    #: sellers use it as buyer-attributed arrival evidence rather than
    #: silently substituting publication time. Planning time is not an
    #: acceptable stand-in: it would date every statement to whenever the loop
    #: happened to run.
    first_consumable_at: datetime | None = None


@dataclass(frozen=True)
class ReportingPinnedDefinition:
    """The promises the accepted configuration generation already fixed.

    A buyer resolves this once from the retained report definition and reuses
    it, rather than re-reading the seller's current definition -- which is the
    point of pinning. Comparing against a definition the seller can change
    would make every mismatch unfalsifiable.
    """

    metric_names: tuple[str, ...] = ()
    #: Unit fixed for each metric, e.g. ``{"spend": "USD"}``.
    metric_units: Mapping[str, str] = field(default_factory=dict)
    #: Unit the profile defines for each control-total name.
    control_total_units: Mapping[str, str] = field(default_factory=dict)


def classify_content_mismatch(
    *,
    obligation: ReportingObligation,
    revision: ReportingRevision,
    reading: ReportingContentReading,
    definition: ReportingPinnedDefinition | None = None,
) -> MismatchCode | None:
    """Which agreed fact this revision contradicts, or ``None`` if it honours them.

    Checked in the spec's precedence order.  Two orderings matter and are not
    arbitrary:

    * ``metric_missing`` is checked *before* ``schema_nonconformant``.  The spec
      is explicit: a metric that is simply absent uses ``metric_missing`` even
      when the pinned schema declares it required.  Checking structure first
      would collapse every missing metric into a generic schema failure and
      lose the one detail that tells the seller what to fix.
    * ``scope_media_buy_missing`` is checked before ``coverage_short`` because a
      missing buy is the coarser failure: reporting the package shortfall of a
      revision that is missing a whole buy describes a symptom, not the cause.

    Returns ``None`` rather than raising when nothing is wrong, so a caller can
    use it as the condition for filing ``received``.
    """
    frozen_buys = _ids(obligation.media_buy_ids)
    present_buys = set(reading.media_buy_ids)
    if any(buy not in present_buys for buy in frozen_buys):
        return "scope_media_buy_missing"

    covered = _ids(getattr(obligation.coverage, "covered_package_ids", None))
    if covered and not set(covered) <= set(reading.package_ids):
        return "coverage_short"

    if definition is not None:
        present_metrics = set(reading.metric_names)
        if any(name not in present_metrics for name in definition.metric_names):
            return "metric_missing"

    if not reading.schema_conformant:
        return "schema_nonconformant"

    if definition is not None:
        for name, unit in definition.metric_units.items():
            observed = reading.metric_units.get(name)
            if observed is not None and observed != unit:
                return "currency_mismatch"
        for name, unit in definition.control_total_units.items():
            observed = reading.control_total_units.get(name)
            if observed is not None and observed != unit:
                return "currency_mismatch"

    if reading.time_dimension_values:
        start = _utc(obligation.period.start)
        end = _utc(obligation.period.end)
        # Half-open: [start, end). A value exactly at the end belongs to the
        # next period, and a revision that carries it is reporting outside the
        # window the obligation froze.
        if any(not (start <= _utc(value) < end) for value in reading.time_dimension_values):
            return "period_mismatch"

    del revision  # Identity is asserted by the caller's recomputed digest.
    return None


@dataclass(frozen=True)
class ConsumerStatusIntent:
    """One statement the buyer owes, ready to batch into ``sync_reporting_status``.

    Carries ``due_at`` so a caller can log or alert on how late its own loop is
    without recomputing the deadline.
    """

    reporting_status_id: str
    consumer_status: ConsumerStatusValue
    delivery_config_id: str
    delivery_config_version: int
    report_definition_id: str
    period_start: datetime
    period_end: datetime
    period_source_timezone: str
    status_as_of: datetime
    due_at: datetime
    supersedes_reporting_status_id: str | None = None
    reporting_obligation_id: str | None = None
    reporting_revision_id: str | None = None
    observed_revision_content_sha256: str | None = None
    failure_code: str | None = None
    mismatch_code: MismatchCode | None = None
    consumer_commit_ref: str | None = None

    @property
    def overdue(self) -> bool:
        return _utc(self.status_as_of) >= _utc(self.due_at)

    def to_wire(self) -> dict[str, Any]:
        """The ``statuses[]`` entry for ``sync_reporting_status``.

        Absent keys are omitted rather than sent as ``null``: the schema's
        conditional blocks use ``not: {required: [...]}``, and an explicit null
        still satisfies JSON Schema's ``required``. Sending ``"failure_code":
        null`` on a ``received`` statement would therefore be rejected.
        """
        payload: dict[str, Any] = {
            "reporting_status_id": self.reporting_status_id,
            "delivery_config_id": self.delivery_config_id,
            "delivery_config_version": self.delivery_config_version,
            "report_definition_id": self.report_definition_id,
            "period": {
                "start": _iso(self.period_start),
                "end": _iso(self.period_end),
                "source_timezone": self.period_source_timezone,
            },
            "consumer_status": self.consumer_status,
            "status_as_of": _iso(self.status_as_of),
        }
        optional = {
            "supersedes_reporting_status_id": self.supersedes_reporting_status_id,
            "reporting_obligation_id": self.reporting_obligation_id,
            "reporting_revision_id": self.reporting_revision_id,
            "observed_revision_content_sha256": self.observed_revision_content_sha256,
            "failure_code": self.failure_code,
            "mismatch_code": self.mismatch_code,
            "consumer_commit_ref": self.consumer_commit_ref,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload


@dataclass(frozen=True)
class ReportingOperationsContactView:
    """The seller's advertised human escalation path.

    Inert display metadata. Agents surface it to an operator and MUST NOT fetch
    the URL, send protocol traffic to it, or treat either value as a credential
    or a callback. It is carried as a separate type rather than as a bare dict
    so that intent is impossible to miss at the call site.
    """

    url: str | None = None
    email: str | None = None

    @classmethod
    def from_capability(
        cls, payload: Mapping[str, Any] | None
    ) -> ReportingOperationsContactView | None:
        if not payload:
            return None
        contact = payload.get("operations_contact")
        if not isinstance(contact, Mapping):
            return None
        return cls(url=contact.get("url"), email=contact.get("email"))


@dataclass(frozen=True)
class ConsumerLoopView:
    """What the seller told this buyer about its own side of the loop.

    Read from the *summary* view, because ``consumer_status_pending`` lives in
    ``obligation_counts`` and the periods view does not carry it.
    """

    consumer_status_pending: int | None
    issues: tuple[ReportingStatusIssue, ...] = ()
    operations_contact: ReportingOperationsContactView | None = None

    @property
    def mismatch_issues(self) -> tuple[ReportingStatusIssue, ...]:
        """Only the issues attributed to this buyer's own statements."""
        return tuple(
            issue
            for issue in self.issues
            if str(getattr(issue.code, "value", issue.code)) == "CONSUMER_STATUS_MISMATCH"
        )

    def escalated(self) -> tuple[ReportingStatusIssue, ...]:
        """Mismatches the seller has escalated to a human.

        ``recommended_action`` in the ``contact_*`` family is the signal, not
        severity: a seller past its advertised
        ``consumer_mismatch_escalation_seconds`` MUST switch the action, and
        ``wait_for_retry`` cannot survive that boundary.
        """
        return tuple(
            issue
            for issue in self.mismatch_issues
            if str(getattr(issue.recommended_action, "value", issue.recommended_action)).startswith(
                "contact_"
            )
        )


async def load_consumer_loop_view(
    client: Any,
    request: GetReportingStatusRequest,
    *,
    capabilities: Mapping[str, Any] | None = None,
) -> ConsumerLoopView:
    """Read the summary view for the buyer-side fields of the rc.3 loop.

    ``consumer_status_pending`` is ``None`` when the seller did not emit it,
    which means it does not advertise ``consumer_status_task``. That is
    distinct from ``0``: zero is "you owe nothing", ``None`` is "this seller has
    no loop to owe anything to", and conflating them would make a buyer think
    it was caught up on a seller that never asked.
    """
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["view"] = "summary"
    payload.pop("pagination", None)
    result = await client.get_reporting_status(GetReportingStatusRequest.model_validate(payload))
    response = result.data
    if not result.success or response is None:
        from adcp.reporting._reconcile import ReportingReconciliationError

        raise ReportingReconciliationError(
            "STATUS_READ_FAILED", "get_reporting_status did not return a completed summary view"
        )
    counts = getattr(response, "obligation_counts", None)
    pending = getattr(counts, "consumer_status_pending", None) if counts is not None else None
    return ConsumerLoopView(
        consumer_status_pending=pending,
        issues=tuple(getattr(response, "issues", None) or ()),
        operations_contact=ReportingOperationsContactView.from_capability(capabilities),
    )


def plan_consumer_statuses(
    obligations: Sequence[ReportingObligation],
    *,
    now: datetime,
    automated_recovery_window: timedelta,
    missing_expected_periods: Sequence[Any] = (),
    readings: Mapping[str, ReportingContentReading] | None = None,
    definition: ReportingPinnedDefinition | None = None,
    revisions: Mapping[str, ReportingRevision] | None = None,
    obligation_revisions: Mapping[str, Sequence[ReportingRevision]] | None = None,
    current_statuses: Sequence[Any] = (),
    status_id_prefix: str = "rpcs",
) -> list[ConsumerStatusIntent]:
    """Decide what the buyer owes the seller right now.

    One intent per *expected* period whose deadline -- ``expected_at`` plus the
    seller's advertised ``automated_recovery_window_seconds`` -- has passed.
    Periods that are not due yet are skipped: filing early is not wrong, but
    filing ``revision_missing`` before the seller is late would be a false
    accusation this loop is supposed to prevent.

    "Expected" deliberately includes periods the seller's ledger omitted.
    ``missing_expected_periods`` (what ``evaluate_reporting_ledger`` computed
    by diffing the buyer's own denominator against the ledger) each become
    ``obligation_missing`` -- keyed without a seller obligation id, because
    requiring one would make the first missing report invisible again, which is
    the exact failure this loop exists to surface.

    Status selection, in the spec's terms:

    * no obligation at all -> ``obligation_missing``;
    * obligation with no required revision -> ``revision_missing``;
    * revision advertised but unreadable -> ``unreadable`` with the reading's
      closed ``failure_code``;
    * revision read -> ``received``, or ``content_mismatch`` when
      :func:`classify_content_mismatch` finds a contradicted contract fact.

    The third and fourth cases need the caller's own read result, so an
    obligation that *has* a required revision and *no* reading raises
    :class:`ConsumerStatusPlanError` rather than defaulting. Defaulting either
    way makes a false claim: ``revision_missing`` accuses the seller of
    publishing nothing it demonstrably published, and ``received`` asserts
    bytes the buyer never looked at.

    ``current_statuses`` is this caller's own status history from the same
    ledger read. An intent whose content matches the current leaf is skipped
    entirely -- re-filing an unchanged claim under a fresh id churns the chain
    for nothing, and a derived id that collided with the leaf would make the
    statement supersede *itself*.
    """
    readings = readings or {}
    revisions = revisions or {}
    obligation_revisions = obligation_revisions or {}
    leaves = _index_current_leaves(current_statuses)
    boundary = _utc(now)
    plan: list[ConsumerStatusIntent] = []

    def add(intent: ConsumerStatusIntent | None) -> None:
        """Append unless the current leaf already says exactly this."""
        if intent is not None:
            plan.append(intent)

    for period in missing_expected_periods:
        expected_at = _expected_at_of(period)
        if expected_at is None:
            # Without the buyer's own expected_at there is no defensible
            # deadline and no way to satisfy "obligation_missing is valid only
            # at or after expected_at". Skip rather than invent one.
            continue
        due_at = expected_at + automated_recovery_window
        if boundary < due_at:
            continue
        add(
            _intend(
                status="obligation_missing",
                delivery_config_id=period.delivery_config_id,
                delivery_config_version=period.delivery_config_version,
                report_definition_id=period.report_definition_id,
                period_start=_parse(period.period_start),
                period_end=_parse(period.period_end),
                source_timezone=getattr(period, "source_timezone", None) or "UTC",
                status_as_of=max(boundary, expected_at),
                due_at=due_at,
                leaves=leaves,
                prefix=status_id_prefix,
            )
        )

    for obligation in obligations:
        obligation_id = obligation.reporting_obligation_id
        due_at = _utc(obligation.expected_at) + automated_recovery_window
        if boundary < due_at:
            continue

        reading = readings.get(obligation_id)
        status: ConsumerStatusValue
        mismatch: MismatchCode | None = None
        failure: ReportingFailureCode | None = None
        revision_id: str | None = None
        digest: str | None = None
        status_as_of = boundary

        if reading is not None and reading.failure_code is not None:
            status = "unreadable"
            failure = reading.failure_code
            revision_id = reading.reporting_revision_id
        elif reading is not None:
            revision = revisions.get(reading.reporting_revision_id)
            if revision is None:
                # The buyer claims to have read a revision this ledger snapshot
                # does not contain. Filing `received` would bind the statement
                # to a revision the seller cannot resolve, and the seller must
                # reject it -- so say so here, where the mistake is fixable.
                raise ConsumerStatusPlanError(
                    f"reading names revision {reading.reporting_revision_id!r}, which is not "
                    f"in this ledger snapshot for obligation {obligation_id!r}; re-read the "
                    "snapshot rather than filing a status against a revision the seller "
                    "cannot resolve"
                )
            mismatch = classify_content_mismatch(
                obligation=obligation,
                revision=revision,
                reading=reading,
                definition=definition,
            )
            status = "content_mismatch" if mismatch else "received"
            revision_id = reading.reporting_revision_id
            digest = reading.observed_revision_content_sha256
            if status == "received":
                # Buyer-attributed arrival evidence, not planning time.
                status_as_of = _utc(reading.first_consumable_at or boundary)
        elif _has_required_revision(obligation, obligation_revisions.get(obligation_id)):
            raise ConsumerStatusPlanError(
                f"obligation {obligation_id!r} has a required revision but no reading was "
                "supplied; pass a ReportingContentReading (with failure_code when the read "
                "failed) rather than letting this default to revision_missing, which would "
                "accuse the seller of publishing nothing"
            )
        else:
            status = "revision_missing"

        add(
            _intend(
                status=status,
                delivery_config_id=obligation.delivery_config_id,
                delivery_config_version=obligation.delivery_config_version,
                report_definition_id=obligation.report_definition_id,
                period_start=_utc(obligation.period.start),
                period_end=_utc(obligation.period.end),
                source_timezone=_source_timezone(obligation),
                status_as_of=status_as_of,
                due_at=due_at,
                leaves=leaves,
                prefix=status_id_prefix,
                obligation_id=obligation_id,
                revision_id=revision_id,
                digest=digest,
                mismatch=mismatch,
                failure=failure,
            )
        )
    return plan


def _intend(
    *,
    status: ConsumerStatusValue,
    delivery_config_id: str,
    delivery_config_version: int,
    report_definition_id: str,
    period_start: datetime,
    period_end: datetime,
    source_timezone: str,
    status_as_of: datetime,
    due_at: datetime,
    leaves: Mapping[tuple[Any, ...], Any],
    prefix: str,
    obligation_id: str | None = None,
    revision_id: str | None = None,
    digest: str | None = None,
    mismatch: MismatchCode | None = None,
    failure: ReportingFailureCode | None = None,
) -> ConsumerStatusIntent | None:
    """Build one intent, or ``None`` when the current leaf already says this.

    Skipping an unchanged claim is not an optimization. A derived id includes
    the content, so an unchanged claim derives the *same* id as the leaf -- and
    an intent that both reuses the leaf's id and names it in
    ``supersedes_reporting_status_id`` supersedes itself, which no seller can
    apply.
    """
    chain = (
        delivery_config_id,
        delivery_config_version,
        report_definition_id,
        _iso(period_start),
        _iso(period_end),
    )
    leaf = leaves.get(chain)
    content = (
        status,
        obligation_id,
        revision_id,
        digest,
        mismatch,
        failure,
    )
    if leaf is not None and _leaf_content(leaf) == content:
        return None

    status_as_of = _utc(status_as_of)
    if leaf is not None:
        leaf_as_of = getattr(leaf, "status_as_of", None)
        if leaf_as_of is not None:
            # "status_as_of MUST be no earlier than the superseded statement's
            # status_as_of." A re-read that became consumable before the
            # previous statement was made would otherwise go backwards.
            status_as_of = max(status_as_of, _utc(leaf_as_of))

    return ConsumerStatusIntent(
        reporting_status_id=_status_id(prefix, chain, content, status_as_of),
        consumer_status=status,
        delivery_config_id=delivery_config_id,
        delivery_config_version=delivery_config_version,
        report_definition_id=report_definition_id,
        period_start=period_start,
        period_end=period_end,
        period_source_timezone=source_timezone,
        status_as_of=status_as_of,
        due_at=_utc(due_at),
        supersedes_reporting_status_id=(
            getattr(leaf, "reporting_status_id", None) if leaf is not None else None
        ),
        reporting_obligation_id=obligation_id,
        reporting_revision_id=revision_id,
        observed_revision_content_sha256=digest,
        failure_code=failure,
        mismatch_code=mismatch,
    )


def _index_current_leaves(statuses: Sequence[Any]) -> dict[tuple[Any, ...], Any]:
    """The newest statement per logical chain.

    The periods view returns append-only history, so several statements can
    share a chain. ``supersedes_reporting_status_id`` links them; whichever id
    nothing else supersedes is the leaf.
    """
    by_chain: dict[tuple[Any, ...], list[Any]] = {}
    superseded: set[str] = set()
    for status in statuses:
        period = status.period
        chain = (
            status.delivery_config_id,
            status.delivery_config_version,
            status.report_definition_id,
            _iso(_coerce(period.start)),
            _iso(_coerce(period.end)),
        )
        by_chain.setdefault(chain, []).append(status)
        if getattr(status, "supersedes_reporting_status_id", None):
            superseded.add(str(status.supersedes_reporting_status_id))
    leaves: dict[tuple[Any, ...], Any] = {}
    for chain, items in by_chain.items():
        unsuperseded = [item for item in items if item.reporting_status_id not in superseded]
        if len(unsuperseded) == 1:
            leaves[chain] = unsuperseded[0]
        elif unsuperseded:
            # A forked chain is a seller bug, not something to guess through.
            # Take the latest recorded so planning still supersedes *something*
            # real rather than filing a rootless statement.
            leaves[chain] = max(
                unsuperseded,
                key=lambda item: (
                    _coerce(getattr(item, "recorded_at", None) or item.status_as_of),
                    item.reporting_status_id,
                ),
            )
    return leaves


def _leaf_content(leaf: Any) -> tuple[Any, ...]:
    return (
        str(getattr(leaf.consumer_status, "value", leaf.consumer_status)),
        getattr(leaf, "reporting_obligation_id", None),
        getattr(leaf, "reporting_revision_id", None),
        getattr(leaf, "observed_revision_content_sha256", None),
        _enum_or_none(getattr(leaf, "mismatch_code", None)),
        _enum_or_none(getattr(leaf, "failure_code", None)),
    )


def _enum_or_none(value: Any) -> Any:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _coerce(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    return _parse(str(value))


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _expected_at_of(period: Any) -> datetime | None:
    raw = getattr(period, "expected_at", None)
    return _parse(raw) if raw else None


def _has_required_revision(
    obligation: ReportingObligation, revisions: Sequence[ReportingRevision] | None
) -> bool:
    """Whether the seller has published a revision meeting the required finality.

    Prefers the supplied revisions, because ``required_finality`` matters: an
    obligation needing ``official`` is not satisfied by snapshots. Falls back to
    ``revision_count``, which the spec defines as the number of distinct
    revision records for this obligation in the snapshot, so a caller that did
    not pass revisions still gets the coarse answer rather than a wrong one.
    """
    if revisions is not None:
        required = str(getattr(obligation.required_finality, "value", obligation.required_finality))
        return any(
            required == "snapshot"
            or str(getattr(item.finality, "value", item.finality)) == "official"
            for item in revisions
        )
    return bool(obligation.revision_count)


def _source_timezone(obligation: ReportingObligation) -> str:
    period = obligation.period
    return str(getattr(period, "source_timezone", None) or "UTC")


def _status_id(
    prefix: str,
    chain: tuple[Any, ...],
    content: tuple[Any, ...],
    status_as_of: datetime,
) -> str:
    """A deterministic, ``>= 16`` character status id over the whole statement.

    Derived rather than random so an interrupted buyer that re-plans the same
    claim produces the same id and gets an idempotent replay instead of a
    supersession conflict.

    The digest covers the *complete* content -- chain, status, obligation and
    revision ids, the observed digest, the mismatch/failure code, and
    ``status_as_of``. Hashing only the chain and status (as an earlier version
    did) collided across genuinely different statements: a second
    ``content_mismatch`` with a different ``mismatch_code``, or a second
    ``received`` after a restatement naming a different revision, reused the
    first statement's id with different content. That is an idempotency
    conflict at the seller, and when the colliding id *is* the current leaf the
    statement supersedes itself.
    """
    payload = "|".join(
        [
            prefix,
            *(str(part) for part in chain),
            *(str(part) for part in content),
            _iso(status_as_of),
        ]
    )
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:40]}"


# -- posting ----------------------------------------------------------------
#
# Planning alone is not the buyer side of the loop. A buyer that computes what
# it owes and never posts it is exactly the silence the seller counts in
# ``obligation_counts.consumer_status_pending``.


#: The request schema caps a batch at 100 statements.
_MAX_BATCH = 100


@dataclass(frozen=True)
class ConsumerStatusPostResult:
    """What one posting pass actually achieved.

    Failures are carried rather than raised: ``partial_results`` makes each
    statement independent, so one stale supersession pointer must not discard
    four good statements. A caller that wants to fail loudly checks
    :attr:`failed`.
    """

    recorded: tuple[ConsumerStatusIntent, ...] = ()
    unchanged: tuple[ConsumerStatusIntent, ...] = ()
    failed: tuple[tuple[ConsumerStatusIntent, str, str], ...] = ()

    @property
    def posted(self) -> int:
        """Statements the seller accepted, new or replayed."""
        return len(self.recorded) + len(self.unchanged)

    @property
    def ok(self) -> bool:
        return not self.failed

    def raise_for_failures(self) -> None:
        """Raise when any statement failed, after the successes are recorded.

        For a caller that would rather crash a scheduled job than let a
        reporting gap accumulate quietly.
        """
        if not self.failed:
            return
        detail = "; ".join(
            f"{intent.reporting_status_id}: {code} {message}"
            for intent, code, message in self.failed
        )
        raise ConsumerStatusPostError(f"{len(self.failed)} consumer status(es) failed: {detail}")


class ConsumerStatusPostError(RuntimeError):
    """One or more statements in a posting pass were rejected."""


class ConsumerStatusCheckpointStore(Protocol):
    """Where a buyer remembers the leaf it last posted per logical chain.

    Exists so a buyer does not have to re-read the seller's whole periods view
    to know what it already said. The seller remains authoritative -- this is a
    cache of the buyer's own last word, keyed by the same logical chain the
    seller uses: account, configuration generation, report definition, period.
    """

    async def get(self, chain: str) -> str | None:
        """The ``reporting_status_id`` this buyer last posted for ``chain``."""
        ...

    async def put(self, chain: str, reporting_status_id: str) -> None:
        """Record the leaf just accepted for ``chain``."""
        ...


class InMemoryConsumerStatusCheckpoints:
    """Process-local checkpoints. Correct, and not durable.

    A real buyer persists these: losing them means re-deriving intents from the
    seller's view, which works but re-reads more than it needs to.
    """

    def __init__(self) -> None:
        self._leaves: dict[str, str] = {}

    async def get(self, chain: str) -> str | None:
        return self._leaves.get(chain)

    async def put(self, chain: str, reporting_status_id: str) -> None:
        self._leaves[chain] = reporting_status_id


def consumer_status_chain_key(intent: ConsumerStatusIntent) -> str:
    """The logical chain an intent belongs to, as a checkpoint key.

    Keyed exactly as the seller keys it -- configuration generation, report
    definition, and the half-open period -- and deliberately *not* on the
    obligation id, because a chain that began as ``obligation_missing``
    attaches to the repaired obligation later and must not fork.
    """
    payload = "|".join(
        [
            intent.delivery_config_id,
            str(intent.delivery_config_version),
            intent.report_definition_id,
            _iso(intent.period_start),
            _iso(intent.period_end),
        ]
    )
    return "rpcc_" + hashlib.sha256(payload.encode()).hexdigest()[:40]


async def post_consumer_statuses(
    client: Any,
    plan: Sequence[ConsumerStatusIntent],
    *,
    account_id: str,
    checkpoints: ConsumerStatusCheckpointStore | None = None,
    idempotency_key_prefix: str = "rpcs_batch",
) -> ConsumerStatusPostResult:
    """Post a plan through ``sync_reporting_status``, batching as the schema allows.

    Each batch carries at most one statement per logical chain, because the
    spec rejects every duplicate-chain entry in a batch *without evaluating
    their supersession order* -- so two statements for one chain in one request
    lose both. Chains are therefore spread across batches rather than packed.

    ``recorded`` and ``unchanged`` are both successes: an exact retry replaying
    as ``unchanged`` is the idempotency contract working, not a problem to
    report. Only ``failed`` entries are surfaced as failures, and they are
    returned rather than raised so one bad statement does not discard the rest.

    The idempotency key is derived from the batch's contents, so a retry after
    a transport failure reuses it and the seller can replay rather than
    re-evaluate.
    """
    if not plan:
        return ConsumerStatusPostResult()

    recorded: list[ConsumerStatusIntent] = []
    unchanged: list[ConsumerStatusIntent] = []
    failed: list[tuple[ConsumerStatusIntent, str, str]] = []

    for batch in _batches(plan):
        by_id = {intent.reporting_status_id: intent for intent in batch}
        request = {
            "account": {"account_id": account_id},
            "idempotency_key": _batch_idempotency_key(idempotency_key_prefix, batch),
            "statuses": [intent.to_wire() for intent in batch],
        }
        results = await _submit(client, request)
        for entry in results:
            intent = _match_result(entry, by_id)
            if intent is None:
                continue
            outcome = str(entry.get("result"))
            if outcome == "recorded":
                recorded.append(intent)
            elif outcome == "unchanged":
                unchanged.append(intent)
            else:
                errors = entry.get("errors") or [{}]
                failed.append(
                    (
                        intent,
                        str(errors[0].get("code", "UNKNOWN")),
                        str(errors[0].get("message", "")),
                    )
                )
                continue
            if checkpoints is not None:
                await checkpoints.put(consumer_status_chain_key(intent), intent.reporting_status_id)

    return ConsumerStatusPostResult(
        recorded=tuple(recorded), unchanged=tuple(unchanged), failed=tuple(failed)
    )


def _batches(plan: Sequence[ConsumerStatusIntent]) -> list[list[ConsumerStatusIntent]]:
    """Split a plan so no batch repeats a chain and none exceeds the cap."""
    batches: list[list[ConsumerStatusIntent]] = []
    seen: list[set[str]] = []
    for intent in plan:
        chain = consumer_status_chain_key(intent)
        for index, used in enumerate(seen):
            if chain not in used and len(batches[index]) < _MAX_BATCH:
                batches[index].append(intent)
                used.add(chain)
                break
        else:
            batches.append([intent])
            seen.append({chain})
    return batches


def _batch_idempotency_key(prefix: str, batch: Sequence[ConsumerStatusIntent]) -> str:
    payload = "|".join(sorted(intent.reporting_status_id for intent in batch))
    return f"{prefix}_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


async def _submit(client: Any, request: dict[str, Any]) -> list[dict[str, Any]]:
    """Call the task and normalize the response to a list of result mappings."""
    from adcp.types import SyncReportingStatusRequest

    result = await client.sync_reporting_status(SyncReportingStatusRequest.model_validate(request))
    if not getattr(result, "success", False) or result.data is None:
        raise ConsumerStatusPostError(
            f"sync_reporting_status did not complete: {getattr(result, 'error', None)}"
        )
    data = result.data
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json", exclude_none=True)
    return list(data.get("results") or [])


def _match_result(
    entry: Mapping[str, Any], by_id: Mapping[str, ConsumerStatusIntent]
) -> ConsumerStatusIntent | None:
    """Find the intent a result refers to.

    A ``failed`` arm echoes ``reporting_status_id`` directly; the accepted arms
    carry the stored statement instead, so the id comes from inside it.
    """
    direct = entry.get("reporting_status_id")
    if isinstance(direct, str) and direct in by_id:
        return by_id[direct]
    stored = entry.get("consumer_status")
    if isinstance(stored, Mapping):
        nested = stored.get("reporting_status_id")
        if isinstance(nested, str):
            return by_id.get(nested)
    return None
