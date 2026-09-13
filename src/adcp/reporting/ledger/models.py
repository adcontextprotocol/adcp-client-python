"""The records a seller's reporting ledger retains.

These are the seller's *own* durable records, deliberately narrower than the
AdCP wire types they project into.  A store keeps exactly what the seller must
be able to answer with years later; the ``get_reporting_status`` handler
projects them onto the wire.  Keeping the two separate is what lets the wire
shape move (rc.1 to rc.2 to 3.3) without a migration of retained evidence.

Four facts define the model:

1. **Obligations exist before reports.**  At each period close the seller
   freezes the scope and commits an obligation whether or not source data
   exists, so a missing first report is detectable rather than silent.
2. **A zero-row report differs from no report.**  A zero-row revision is a
   revision.  Absence means something is wrong.
3. **Revisions are immutable.**  A snapshot restatement is a *new* revision
   superseding the old one.  An official revision is terminal; a later
   correction is an explicit adjustment, never an edit.
4. **Health is derived, never stored.**  It is a function of the obligations,
   the revisions, and the clock at the snapshot boundary -- so it cannot go
   stale, and two readers of one snapshot cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

__all__ = [
    "ConsumerStatusRecord",
    "LedgerChange",
    "LedgerRecordKind",
    "LedgerSnapshot",
    "ReportingAdjustmentRecord",
    "ReportingConfiguration",
    "ReportingFinality",
    "ReportingHealth",
    "ReportingIssue",
    "ReportingObligationRecord",
    "ReportingPeriodBoundary",
    "ReportingProductionStatus",
    "ReportingRevisionRecord",
    "ReportingScheduleSpec",
    "derive_period",
    "iso_duration_to_timedelta",
]

ReportingFinality = Literal["snapshot", "official"]
ReportingHealth = Literal["healthy", "waiting", "delayed", "action_required", "complete"]
ReportingProductionStatus = Literal["not_due", "pending", "published", "failed"]
LedgerRecordKind = Literal["obligation", "revision", "adjustment", "consumer_status"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("reporting ledger timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def iso_duration_to_timedelta(value: str) -> timedelta:
    """Parse the restricted ISO-8601 durations a reporting schedule may use.

    Days, hours, minutes, seconds only.  Months and years are deliberately
    unsupported: their length depends on a calendar, and a schedule whose
    period length depends on which month it is cannot be derived identically by
    both sides -- which is the whole point of publishing the schedule.
    """
    import re

    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        value,
    )
    if match is None or value in {"P", "PT"}:
        raise ValueError(
            f"{value!r} is not a supported reporting duration; use day/hour/minute/second "
            "components only (calendar months and years are not derivable identically "
            "by both parties)"
        )
    days, hours, minutes, seconds = match.groups()
    return timedelta(
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=float(seconds or 0),
    )


@dataclass(frozen=True)
class ReportingScheduleSpec:
    """The clock a reporting configuration creates.

    ``expected_at`` is always ``period.end + delivery_sla``.  Accepting a media
    buy does not start a separate SLA; the configuration is the only clock.
    """

    period_duration: str
    delivery_sla: str
    alignment: Literal["utc", "account_timezone", "custom_timezone"] = "utc"
    period_timezone: str | None = None
    period_anchor: datetime | None = None

    def timezone_name(self, account_timezone: str) -> str:
        if self.alignment == "utc":
            return "UTC"
        if self.alignment == "account_timezone":
            return account_timezone
        if not self.period_timezone:
            raise ValueError("custom_timezone alignment requires period_timezone")
        return self.period_timezone


@dataclass(frozen=True)
class ReportingPeriodBoundary:
    """One derived half-open period plus the instant its report becomes late."""

    period_key: str
    start: datetime
    end: datetime
    source_timezone: str
    expected_at: datetime

    def __post_init__(self) -> None:
        if _utc(self.end) <= _utc(self.start):
            raise ValueError("a reporting period must be nonempty")
        if _utc(self.expected_at) < _utc(self.end):
            raise ValueError("expected_at cannot precede the period end")


def derive_period(
    schedule: ReportingScheduleSpec,
    *,
    account_timezone: str,
    ordinal: int,
    activated_at: datetime | None = None,
) -> ReportingPeriodBoundary:
    """Derive period ``ordinal`` from a schedule, the way both sides must.

    Both the buyer and the seller run this calculation independently, and a
    buyer treats a missing obligation as a protocol failure rather than
    evidence that nothing happened.  That only works if the derivation is
    identical, so it lives here rather than in each side's handler.

    Periods are anchored at the Unix epoch in the schedule's timezone (or at an
    explicit ``period_anchor`` for billing-cycle alignment), which is what makes
    "the next full boundary" unambiguous.  A configuration activated mid-period
    begins at the next boundary -- a partial first period would be reported as
    complete and understate delivery.
    """
    from zoneinfo import ZoneInfo

    zone_name = schedule.timezone_name(account_timezone)
    zone = ZoneInfo(zone_name)
    duration = iso_duration_to_timedelta(schedule.period_duration)
    if duration <= timedelta(0):
        raise ValueError("period_duration must be positive")
    sla = iso_duration_to_timedelta(schedule.delivery_sla)

    anchor = (
        _utc(schedule.period_anchor)
        if schedule.period_anchor is not None
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    # Walk whole periods from the anchor in local wall-clock terms so a DST
    # transition shifts the instant without changing which period it is.
    local_anchor = anchor.astimezone(zone).replace(tzinfo=None)
    start_local = local_anchor + duration * ordinal
    end_local = local_anchor + duration * (ordinal + 1)
    start = start_local.replace(tzinfo=zone).astimezone(timezone.utc)
    end = end_local.replace(tzinfo=zone).astimezone(timezone.utc)

    if activated_at is not None and _utc(activated_at) > start:
        raise ValueError(
            "a configuration activated mid-period owes its first obligation at the next "
            "full boundary; a partial first period would understate delivery"
        )
    return ReportingPeriodBoundary(
        period_key=f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{schedule.period_duration}",
        start=start,
        end=end,
        source_timezone=zone_name,
        expected_at=end + sla,
    )


def first_ordinal_after(
    schedule: ReportingScheduleSpec, *, account_timezone: str, activated_at: datetime
) -> int:
    """The first period ordinal that starts at or after ``activated_at``.

    A configuration activated at 00:20 with hourly aligned periods owes
    ``[01:00, 02:00)`` first, not a 40-minute stub.
    """
    ordinal = 0
    # Seek coarsely then step back, so a long-lived configuration does not walk
    # every period since the epoch one at a time.
    step = 1 << 20
    while step:
        candidate = derive_period(
            schedule, account_timezone=account_timezone, ordinal=ordinal + step
        )
        if _utc(candidate.start) <= _utc(activated_at):
            ordinal += step
        else:
            step //= 2
    while True:
        candidate = derive_period(schedule, account_timezone=account_timezone, ordinal=ordinal)
        if _utc(candidate.start) >= _utc(activated_at):
            return ordinal
        ordinal += 1


@dataclass(frozen=True)
class ReportingConfiguration:
    """One accepted reporting configuration generation.

    A generation is immutable.  Campaign starts, stops, and configuration
    changes alter *future* obligations; they never alter past ones, because a
    buyer that retained this generation must be able to re-derive the same
    expectations from it years later.
    """

    delivery_config_id: str
    delivery_config_version: int
    account_id: str
    report_definition_id: str
    reporting_profile: str
    feed_purpose: str
    schedule: ReportingScheduleSpec
    required_finality: ReportingFinality
    account_timezone: str = "UTC"
    activated_at: datetime | None = None
    deactivated_at: datetime | None = None
    media_buy_ids: tuple[str, ...] = ()
    automated_recovery_window: timedelta = timedelta(hours=6)
    status_retention_days: int = 400

    @property
    def generation_key(self) -> tuple[str, int]:
        return (self.delivery_config_id, self.delivery_config_version)


@dataclass(frozen=True)
class ReportingObligationRecord:
    """What *should* exist for one configuration generation and period.

    Committed at the period boundary, independently of whether source data is
    available and before any revision.  ``scope_resolved_at`` equals the period
    end: that is the instant the media-buy denominator froze, and a coverage
    evaluation at any other instant is describing a different scope.
    """

    reporting_obligation_id: str
    account_id: str
    delivery_config_id: str
    delivery_config_version: int
    report_definition_id: str
    reporting_profile: str
    feed_purpose: str
    period: ReportingPeriodBoundary
    scope_resolved_at: datetime
    media_buy_ids: tuple[str, ...]
    required_finality: ReportingFinality
    automated_recovery_deadline_at: datetime
    schedule: ReportingScheduleSpec
    coverage_status: Literal["full", "partial", "none", "unknown"] = "full"
    package_ids: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if _utc(self.scope_resolved_at) != _utc(self.period.end):
            raise ValueError(
                "scope_resolved_at must equal the period end; the denominator froze there "
                "and a coverage evaluation at another instant describes another scope"
            )
        if _utc(self.automated_recovery_deadline_at) < _utc(self.period.expected_at):
            raise ValueError("the automated recovery deadline cannot precede expected_at")


@dataclass(frozen=True)
class ReportingRevisionRecord:
    """One immutable publication of a period's content.

    ``revision_content_sha256`` is the Core binding: JCS over
    ``{reporting_revision_id, row_count, control_totals, reporting_rows}``,
    SHA-256 of those bytes.  A consumer recomputes it from what it actually
    read, which is what makes an exact read verifiable rather than trusted.

    ``readable`` is not cosmetic.  Core's promise is that a committed revision
    stays readable for ``status_retention_days``; an obligation whose only
    qualifying revision has become unreadable is ``action_required``, not
    ``complete``.
    """

    reporting_revision_id: str
    account_id: str
    reporting_obligation_id: str
    finality: ReportingFinality
    revision_content_sha256: str
    row_count: int
    control_totals: tuple[tuple[str, str], ...]
    observed_at: datetime
    data_through: datetime | None
    created_at: datetime
    supersedes_reporting_revision_id: str | None = None
    finality_basis: Literal["source_final", "contractual_cutoff", "stabilized"] | None = None
    finality_policy_id: str | None = None
    finalized_at: datetime | None = None
    readable: bool = True
    readable_at_commit: bool = True
    source_publication_id: str | None = None
    source_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.finality == "official":
            if not (self.finality_basis and self.finality_policy_id and self.finalized_at):
                raise ValueError(
                    "an official revision needs finality basis, policy, and finalized_at; "
                    "without them a consumer cannot tell contractual close from a guess"
                )
        elif self.finality_basis or self.finality_policy_id or self.finalized_at:
            raise ValueError("finality evidence belongs only to an official revision")
        if self.finality == "official" and self.supersedes_reporting_revision_id:
            raise ValueError(
                "an official revision is terminal and cannot supersede another revision; "
                "publish a later source correction as an adjustment"
            )


@dataclass(frozen=True)
class ReportingAdjustmentRecord:
    """An immutable post-official accounting correction.

    An official revision is terminal.  When the source corrects it, the
    correction lands here as a signed delta against an open accounting period,
    preserving the original invoice-to-revision binding rather than rewriting
    it.
    """

    reporting_adjustment_id: str
    account_id: str
    adjusts_reporting_revision_id: str
    reason_code: Literal[
        "invalid_traffic",
        "late_attribution",
        "source_correction",
        "mapping_correction",
        "commercial_adjustment",
        "other",
    ]
    accounting_period_start: datetime
    accounting_period_end: datetime
    control_total_deltas: tuple[tuple[str, str], ...]
    correction_observed_at: datetime
    created_at: datetime
    reason_detail: str | None = None


@dataclass(frozen=True)
class ConsumerStatusRecord:
    """PREVIEW: one authenticated consumer statement about what it could consume.

    Separately attributed, append-only, and never seller-authored evidence:
    ``received`` does not satisfy the seller's production health, and a
    conflicting statement degrades **only** the submitting caller's view.  See
    :mod:`adcp.reporting.ledger.consumer_status`.
    """

    reporting_status_id: str
    account_id: str
    consumer_id: str
    delivery_config_id: str
    delivery_config_version: int
    report_definition_id: str
    period_start: datetime
    period_end: datetime
    period_source_timezone: str
    consumer_status: Literal["received", "obligation_missing", "revision_missing", "unreadable"]
    status_as_of: datetime
    recorded_at: datetime
    supersedes_reporting_status_id: str | None = None
    reporting_obligation_id: str | None = None
    reporting_revision_id: str | None = None
    observed_revision_content_sha256: str | None = None
    failure_code: str | None = None
    consumer_commit_ref: str | None = None
    seller_ledger_snapshot_id: str | None = None
    seller_ledger_as_of: datetime | None = None
    superseded: bool = False

    @property
    def chain_key(self) -> tuple[str, str, str, int, str, str, str]:
        """The logical chain this statement belongs to.

        Deliberately keyed *without* the seller's obligation id.  Requiring it
        would make the first missing report invisible again, which is the exact
        failure this loop exists to surface.
        """
        return (
            self.account_id,
            self.consumer_id,
            self.delivery_config_id,
            self.delivery_config_version,
            self.report_definition_id,
            _utc(self.period_start).isoformat(),
            _utc(self.period_end).isoformat(),
        )


@dataclass(frozen=True)
class ReportingIssue:
    """A typed, actionable statement about why a scope is not healthy.

    Issue identity is stable and derived, not stored: the same unresolved
    condition yields the same ``issue_id`` on every read, so a consumer can
    deduplicate across polls and a notification can reference it.
    """

    issue_id: str
    code: str
    severity: Literal["info", "delayed", "action_required"]
    responsible_party: Literal["buyer", "seller", "provider"]
    recommended_action: str
    reporting_obligation_id: str | None = None
    delivery_config_id: str | None = None
    delivery_config_version: int | None = None
    feed_purpose: str | None = None
    media_buy_ids: tuple[str, ...] = ()
    period_start: datetime | None = None
    period_end: datetime | None = None
    expected_at: datetime | None = None
    reporting_status_id: str | None = None
    message: str | None = None

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issue_id": self.issue_id,
            "code": self.code,
            "severity": self.severity,
            "responsible_party": self.responsible_party,
            "recommended_action": self.recommended_action,
        }
        optional: dict[str, Any] = {
            "reporting_obligation_id": self.reporting_obligation_id,
            "delivery_config_id": self.delivery_config_id,
            "delivery_config_version": self.delivery_config_version,
            "feed_purpose": self.feed_purpose,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "expected_at": self.expected_at,
            "reporting_status_id": self.reporting_status_id,
            "message": self.message,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value.isoformat() if isinstance(value, datetime) else value
        if self.media_buy_ids:
            payload["media_buy_ids"] = list(self.media_buy_ids)
        return payload


@dataclass(frozen=True)
class LedgerChange:
    """One append to the per-account change feed.

    The feed is what makes ``changes_after`` exact.  Every immutable record --
    obligation, revision, adjustment, consumer status -- appends here in the
    same transaction that writes it, so a consumer that persists a checkpoint
    and replays from it cannot miss a record or see one twice under a different
    identity.
    """

    sequence: int
    account_id: str
    record_kind: LedgerRecordKind
    record_id: str
    committed_at: datetime


@dataclass(frozen=True)
class LedgerSnapshot:
    """A consistent read boundary over one account's ledger.

    Every page reached from one cursor returns the same ``snapshot_id`` and
    ``ledger_as_of``.  Records committed *during* pagination appear after the
    returned ``changes_checkpoint`` on the next read, never inside the current
    snapshot -- otherwise a consumer's record count would not match what it
    received, and it could not tell a lost record from a late one.
    """

    snapshot_id: str
    account_id: str
    ledger_as_of: datetime
    max_sequence: int
