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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from adcp.types import (
    GetReportingStatusRequest,
    ReportingObligation,
    ReportingRevision,
    ReportingStatusIssue,
)

__all__ = [
    "ConsumerLoopView",
    "ConsumerStatusIntent",
    "ReportingContentReading",
    "ReportingOperationsContactView",
    "classify_content_mismatch",
    "load_consumer_loop_view",
    "plan_consumer_statuses",
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


def _utc(value: datetime) -> datetime:
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
    readings: Mapping[str, ReportingContentReading] | None = None,
    definition: ReportingPinnedDefinition | None = None,
    revisions: Mapping[str, ReportingRevision] | None = None,
    current_status_ids: Mapping[str, str] | None = None,
    status_id_prefix: str = "rpcs",
) -> list[ConsumerStatusIntent]:
    """Decide what the buyer owes the seller right now.

    One intent per obligation whose deadline -- ``expected_at`` plus the
    seller's advertised ``automated_recovery_window_seconds`` -- has passed.
    Periods that are not due yet are skipped: filing early is not wrong, but
    filing ``revision_missing`` before the seller is late would be a false
    accusation this loop is supposed to prevent.

    ``readings`` is keyed by ``reporting_obligation_id``.  An obligation with a
    reading gets ``received`` or ``content_mismatch`` depending on
    :func:`classify_content_mismatch`; one without gets ``revision_missing``,
    because the buyer is past its deadline and has nothing to show.

    ``current_status_ids`` maps obligation id to the buyer's existing leaf, so
    the returned intents supersede it.  Omitting a known leaf would fail
    atomically at the seller rather than fork the chain -- the conflict is
    correct, but this is how a buyer avoids provoking it.
    """
    readings = readings or {}
    revisions = revisions or {}
    current_status_ids = current_status_ids or {}
    boundary = _utc(now)
    plan: list[ConsumerStatusIntent] = []
    for obligation in obligations:
        obligation_id = obligation.reporting_obligation_id
        due_at = _utc(obligation.expected_at) + automated_recovery_window
        if boundary < due_at:
            continue

        reading = readings.get(obligation_id)
        status: ConsumerStatusValue
        mismatch: MismatchCode | None = None
        revision_id: str | None = None
        digest: str | None = None
        if reading is None:
            status = "revision_missing"
        else:
            revision = revisions.get(reading.reporting_revision_id)
            mismatch = (
                classify_content_mismatch(
                    obligation=obligation,
                    revision=revision,
                    reading=reading,
                    definition=definition,
                )
                if revision is not None
                else None
            )
            status = "content_mismatch" if mismatch else "received"
            revision_id = reading.reporting_revision_id
            digest = reading.observed_revision_content_sha256

        plan.append(
            ConsumerStatusIntent(
                reporting_status_id=_status_id(status_id_prefix, obligation_id, status),
                consumer_status=status,
                delivery_config_id=obligation.delivery_config_id,
                delivery_config_version=obligation.delivery_config_version,
                report_definition_id=obligation.report_definition_id,
                period_start=_utc(obligation.period.start),
                period_end=_utc(obligation.period.end),
                period_source_timezone=_source_timezone(obligation),
                status_as_of=boundary,
                due_at=due_at,
                supersedes_reporting_status_id=current_status_ids.get(obligation_id)
                or obligation.current_consumer_status_id,
                reporting_obligation_id=obligation_id,
                reporting_revision_id=revision_id,
                observed_revision_content_sha256=digest,
                mismatch_code=mismatch,
            )
        )
    return plan


def _source_timezone(obligation: ReportingObligation) -> str:
    period = obligation.period
    return str(getattr(period, "source_timezone", None) or "UTC")


def _status_id(prefix: str, obligation_id: str, status: str) -> str:
    """A deterministic, ``>= 16`` character status id.

    Derived rather than random so an interrupted buyer that re-plans produces
    the same id for the same claim and gets an idempotent replay instead of a
    supersession conflict. Changing the claim changes the id, which is exactly
    when a new immutable statement is required.
    """
    import hashlib

    digest = hashlib.sha256(f"{prefix}|{obligation_id}|{status}".encode()).hexdigest()
    return f"{prefix}_{digest[:40]}"
