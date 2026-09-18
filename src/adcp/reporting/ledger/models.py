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
from zoneinfo import ZoneInfo

from adcp.reporting.currency import validate_currency, validate_currency_units
from adcp.reporting.evidence import (
    ReportingCanonicalDigest,
    ReportingControlTotalRecord,
    consumer_reference,
    freeze_control_totals,
)

__all__ = [
    "ConsumerStatusRecord",
    "ConsumerStatusValue",
    "ReportingDefinitionBinding",
    "LedgerChange",
    "LedgerRecordKind",
    "LedgerSnapshot",
    "ReportingAdjustmentRecord",
    "ReportingConfiguration",
    "ReportingConfigurationGenerationKey",
    "ReportingFinality",
    "ReportingHealth",
    "ReportingDeliveryEscalation",
    "ReportingIssue",
    "ReportingIssueLifecycle",
    "ReportingIssueStateValue",
    "ReportingMismatchCode",
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

#: The five values a consumer may state about one expected period. AdCP
#: 3.2.0-rc.3 adds ``content_mismatch``: reporting that arrived and parsed but
#: contradicts a fact the accepted configuration generation already fixed.
ConsumerStatusValue = Literal[
    "received",
    "obligation_missing",
    "revision_missing",
    "unreadable",
    "content_mismatch",
]

#: Closed reason a consumed revision contradicts the accepted generation. Each
#: value is decidable from the obligation, the pinned report definition, and
#: the revision alone -- never from either party's own measurement. A
#: disagreement about *counts* is not in this set; that is a measurement
#: dispute settled through measurement_terms and makegood_policy.
ReportingMismatchCode = Literal[
    "scope_media_buy_missing",
    "coverage_short",
    "metric_missing",
    "schema_nonconformant",
    "currency_mismatch",
    "period_mismatch",
]

#: Seller-maintained issue lifecycle. Moves forward only. ``open`` is the
#: default when omitted. Only ``open`` and ``acknowledged`` appear in
#: ``issues[]``; retiring an issue removes it from the projection rather than
#: publishing it in a terminal state, so a reader that treats a nonempty
#: ``issues[]`` as degradation stays correct.
ReportingIssueStateValue = Literal["open", "acknowledged", "resolved", "waived"]


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
class ReportingDefinitionBinding:
    """The content-addressed report definition an obligation reports against.

    Core's wire records are self-describing: a retained revision names the
    exact definition and row schema it was produced under, by URI *and* digest,
    so a consumer reading it years later can verify it is reading what it
    thinks it is.  Without this, ``report_definition_id`` is just a string two
    parties hope still means the same thing.

    Optional on :class:`ReportingConfiguration` only so an in-process pilot can
    get moving; a seller advertising ``reporting.core`` must supply it, because
    :class:`~adcp.reporting.ledger.status.ReportingStatusHandler` cannot emit a
    schema-valid ``reporting-revision`` record without it.
    """

    report_definition_uri: str
    report_definition_sha256: str
    schema_version: str
    schema_uri: str
    schema_sha256: str
    schema_dialect: str = "https://json-schema.org/draft/2020-12/schema"
    schema_ref_policy: str = "local_fragment_only"
    # Trusted projections of the content-addressed definition, not adapter or
    # buyer context. Tuples keep these declarations immutable after acceptance.
    monetary_metric_units: tuple[tuple[str, str], ...] = ()
    monetary_control_total_units: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("monetary_metric_units", "monetary_control_total_units"):
            units = tuple(
                (name, validate_currency(unit)) for name, unit in getattr(self, field_name)
            )
            if len(dict(units)) != len(units):
                raise ValueError(f"duplicate names in {field_name}")
            object.__setattr__(self, field_name, units)

    def to_storage(self) -> dict[str, Any]:
        """Retain monetary semantics without changing AdCP wire records or old hashes."""
        payload = self.to_wire()
        if self.monetary_metric_units:
            payload["monetary_metric_units"] = list(self.monetary_metric_units)
        if self.monetary_control_total_units:
            payload["monetary_control_total_units"] = list(self.monetary_control_total_units)
        return payload

    def to_wire(self) -> dict[str, Any]:
        return {
            "report_definition_uri": self.report_definition_uri,
            "report_definition_sha256": self.report_definition_sha256,
            "schema_version": self.schema_version,
            "schema_uri": self.schema_uri,
            "schema_sha256": self.schema_sha256,
            "schema_dialect": self.schema_dialect,
            "schema_ref_policy": self.schema_ref_policy,
        }


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


def _schedule_clock(
    schedule: ReportingScheduleSpec, account_timezone: str
) -> tuple[ZoneInfo, timedelta, datetime]:
    zone = ZoneInfo(schedule.timezone_name(account_timezone))
    duration = iso_duration_to_timedelta(schedule.period_duration)
    if duration <= timedelta(0):
        raise ValueError("period_duration must be positive")
    anchor = (
        _utc(schedule.period_anchor)
        if schedule.period_anchor is not None
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    return zone, duration, anchor.astimezone(zone).replace(tzinfo=None)


def _period_instants(
    schedule: ReportingScheduleSpec, account_timezone: str, ordinal: int
) -> tuple[datetime, datetime]:
    zone, duration, anchor = _schedule_clock(schedule, account_timezone)
    # Civil-time boundaries use the first occurrence of an ambiguous local
    # time. A spring-forward gap can collapse a slot to zero elapsed time;
    # the shared schedule iterator skips that slot, never inventing a report.
    start = (anchor + duration * ordinal).replace(tzinfo=zone).astimezone(timezone.utc)
    end = (anchor + duration * (ordinal + 1)).replace(tzinfo=zone).astimezone(timezone.utc)
    return start, end


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
    zone_name = schedule.timezone_name(account_timezone)
    sla = iso_duration_to_timedelta(schedule.delivery_sla)
    # Walk whole periods from the anchor in local wall-clock terms so a DST
    # transition shifts the instant without changing which period it is.
    start, end = _period_instants(schedule, account_timezone, ordinal)

    if activated_at is not None and _utc(activated_at) > start:
        raise ValueError(
            "a configuration activated mid-period owes its first obligation at the next "
            "full boundary; a partial first period would understate delivery"
        )
    return ReportingPeriodBoundary(
        # No "/": a period key travels into the source slice request, whose
        # identifier pattern is [A-Za-z0-9_.:-].
        period_key=f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}_{schedule.period_duration}",
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
    zone, duration, anchor = _schedule_clock(schedule, account_timezone)
    at = _utc(activated_at)
    local = at.astimezone(zone).replace(tzinfo=None)
    ordinal = (local - anchor) // duration
    # Seek directly in civil time, then resolve timezone folds/gaps against
    # instants. This also supports an explicit anchor after activation without
    # scanning from the Unix epoch or constructing overflowing probe dates.
    while _period_instants(schedule, account_timezone, ordinal)[0] < at:
        ordinal += 1
    while _period_instants(schedule, account_timezone, ordinal - 1)[0] >= at:
        ordinal -= 1
    return ordinal


@dataclass(frozen=True)
class ReportingConfigurationGenerationKey:
    """The account-qualified identity of one accepted configuration generation.

    ``delivery_config_id`` is caller-selected and may be reused by another
    account. Use this value for lookups, joins, and leases rather than a tuple
    that could omit the account. It is immutable and hashable for use in maps.
    """

    account_id: str
    delivery_config_id: str
    delivery_config_version: int


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
    definition: ReportingDefinitionBinding | None = None
    # AdCP 3.2.0-rc.3 reserves this for the buyer-deposited billing revision
    # task scoped to a later minor. ``seller`` is the default and the only
    # value any 3.2 seller accepts; ``consumer`` is carried rather than
    # dropped so :meth:`ReportingLedgerStore.put_configuration` can reject it
    # with UNSUPPORTED_FEATURE. The spec forbids silently coercing it.
    authoritative_party: Literal["seller", "consumer"] = "seller"

    @property
    def generation_key(self) -> ReportingConfigurationGenerationKey:
        return ReportingConfigurationGenerationKey(
            account_id=self.account_id,
            delivery_config_id=self.delivery_config_id,
            delivery_config_version=self.delivery_config_version,
        )


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
    definition: ReportingDefinitionBinding | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # None describes legacy evidence only. New writes must freeze a currency;
    # neither a restart nor a source response may fill an unknown historical one.
    currency: str | None = None

    @property
    def generation_key(self) -> ReportingConfigurationGenerationKey:
        return ReportingConfigurationGenerationKey(
            account_id=self.account_id,
            delivery_config_id=self.delivery_config_id,
            delivery_config_version=self.delivery_config_version,
        )

    def __post_init__(self) -> None:
        if self.currency is not None:
            validate_currency(self.currency)
            if self.definition is not None:
                validate_currency_units(
                    self.currency,
                    (
                        *self.definition.monetary_metric_units,
                        *self.definition.monetary_control_total_units,
                    ),
                )
        object.__setattr__(self, "media_buy_ids", tuple(self.media_buy_ids))
        object.__setattr__(self, "package_ids", tuple(self.package_ids))
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
    # Optional managed evidence supplied by a trusted publisher before any
    # destination work. Core neither computes nor requires this contract.
    canonical_content_digest: ReportingCanonicalDigest | None = None
    managed_control_totals: tuple[ReportingControlTotalRecord, ...] | None = None

    def __post_init__(self) -> None:
        if (
            self.canonical_content_digest is not None
            and type(self.canonical_content_digest) is not ReportingCanonicalDigest
        ):
            raise ValueError("managed revision evidence requires an immutable canonical digest")
        object.__setattr__(
            self, "control_totals", tuple(tuple(item) for item in self.control_totals)
        )
        if self.managed_control_totals is not None:
            object.__setattr__(
                self,
                "managed_control_totals",
                freeze_control_totals(self.managed_control_totals, self.control_totals),
            )
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
    managed_control_total_deltas: tuple[ReportingControlTotalRecord, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "control_total_deltas", tuple(tuple(item) for item in self.control_total_deltas)
        )
        if self.managed_control_total_deltas is not None:
            object.__setattr__(
                self,
                "managed_control_total_deltas",
                freeze_control_totals(self.managed_control_total_deltas, self.control_total_deltas),
            )


@dataclass(frozen=True)
class ConsumerStatusRecord:
    """One authenticated consumer statement about what it could consume.

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
    consumer_status: ConsumerStatusValue
    status_as_of: datetime
    recorded_at: datetime
    supersedes_reporting_status_id: str | None = None
    reporting_obligation_id: str | None = None
    reporting_revision_id: str | None = None
    observed_revision_content_sha256: str | None = None
    failure_code: str | None = None
    # AdCP 3.2.0-rc.3. Present exactly when consumer_status is
    # ``content_mismatch``: the closed reason the consumed revision contradicts
    # a fact the accepted configuration generation already fixed. Agents
    # dispatch on this value, never on ``message`` prose.
    mismatch_code: ReportingMismatchCode | None = None
    consumer_commit_ref: str | None = None
    seller_ledger_snapshot_id: str | None = None
    seller_ledger_as_of: datetime | None = None
    superseded: bool = False

    def __post_init__(self) -> None:
        consumer_reference(self.consumer_id)

    @property
    def generation_key(self) -> ReportingConfigurationGenerationKey:
        return ReportingConfigurationGenerationKey(
            account_id=self.account_id,
            delivery_config_id=self.delivery_config_id,
            delivery_config_version=self.delivery_config_version,
        )

    @property
    def chain_key(self) -> tuple[str, str, str, int, str, str, str]:
        """The logical chain this statement belongs to.

        Deliberately keyed *without* the seller's obligation id.  Requiring it
        would make the first missing report invisible again, which is the exact
        failure this loop exists to surface.

        The flat shape is retained for compatibility with persisted statement
        digests. Use ``generation_key`` when joining configuration generations.
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
    # AdCP 3.2.0-rc.3 issue lifecycle. ``opened_at`` is when the seller first
    # observed this logical condition and MUST NOT advance while the same
    # ``issue_id`` is re-emitted -- it anchors the escalation clock, so a
    # re-emission that reset it would let a seller hold an unresolved mismatch
    # below action_required forever. Required on CONSUMER_STATUS_MISMATCH.
    opened_at: datetime | None = None
    issue_state: ReportingIssueStateValue | None = None
    # Inert correlation text for the party's own tracker. Never dereferenced,
    # resolved, or executed, and never reused across callers on a
    # caller-scoped issue -- that would leak one tenant's blast radius to
    # another.
    external_ref: str | None = None

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
            "opened_at": self.opened_at,
            "issue_state": self.issue_state,
            "external_ref": self.external_ref,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value.isoformat() if isinstance(value, datetime) else value
        if self.media_buy_ids:
            payload["media_buy_ids"] = list(self.media_buy_ids)
        return payload


@dataclass(frozen=True)
class ReportingIssueLifecycle:
    """Durable state for one logical issue, so ``opened_at`` can stay fixed.

    Issue *identity* elsewhere in this module is derived (see
    :func:`~adcp.reporting.ledger.health.issue_id_for`) because the conditions
    it names are monotone for one immutable obligation.  A consumer mismatch is
    not monotone: the buyer can supersede its statement, the seller can restate,
    and the same logical disagreement can go quiet and come back.  AdCP
    3.2.0-rc.3 also requires ``opened_at`` to survive every re-emission,
    because it anchors the escalation clock -- a derived timestamp would reset
    on each poll and an unattended mismatch would never escalate.

    ``issue_key`` identifies the *condition*; ``issue_id`` identifies one
    *occurrence* of it.  Retirement bumps ``generation``, so a recurrence after
    ``resolved`` or ``waived`` gets a new ``issue_id`` and a new ``opened_at``
    exactly as the spec requires, while an unresolved condition keeps both
    across a severity change from ``delayed`` to ``action_required``.
    """

    issue_key: str
    issue_id: str
    account_id: str
    opened_at: datetime
    issue_state: ReportingIssueStateValue = "open"
    generation: int = 1
    #: Caller-scoped issues (every consumer mismatch) carry the consumer whose
    #: statement caused them. ``None`` is a seller-wide condition.
    consumer_id: str | None = None
    external_ref: str | None = None
    retired_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.consumer_id is not None:
            consumer_reference(self.consumer_id)

    @property
    def live(self) -> bool:
        """Whether this occurrence still stands, publishable or not.

        ``waived`` blocks a new occurrence while the same disagreement remains,
        but contributes neither health degradation nor a public issue. An
        agreeing statement lets the pure projection retire it; a subsequent
        disagreement receives a new occurrence and notification.

        ``resolved`` frees the condition to recur under a new
        ``issue_id``, and only the projection can set it (see
        :meth:`~adcp.reporting.ledger.store.ReportingLedgerStore.retire_issue`).
        """
        return self.issue_state in {"open", "acknowledged", "waived"}

    @property
    def published(self) -> bool:
        """Whether this occurrence belongs in ``issues[]``.

        Only ``open`` and ``acknowledged``.  Retiring an issue removes it from
        the projection rather than publishing it in a terminal state, so a
        reader that treats a nonempty ``issues[]`` as degradation stays
        correct. Waived occurrences contribute neither an issue nor degradation.
        """
        return self.issue_state in {"open", "acknowledged"}


@dataclass(frozen=True)
class ReportingDeliveryEscalation:
    """The seller's advertised escalation commitment for consumer mismatches.

    Both halves of AdCP 3.2.0-rc.3's
    ``reporting_delivery_capabilities.consumer_mismatch_escalation_seconds`` /
    ``operations_contact`` pair.  The schema makes the contact mandatory when
    the window is advertised, so this class refuses the window without one:
    committing to escalate with nowhere to escalate *to* is the failure the
    requirement exists to prevent.

    ``operations_contact`` is inert human-facing metadata.  Agents surface it
    to an operator and MUST NOT fetch the URL, send protocol traffic to it, or
    treat either value as a credential or a callback.
    """

    consumer_mismatch_escalation: timedelta | None = None
    operations_contact_url: str | None = None
    operations_contact_email: str | None = None

    def __post_init__(self) -> None:
        has_contact = bool(self.operations_contact_url or self.operations_contact_email)
        if self.consumer_mismatch_escalation is not None and not has_contact:
            raise ValueError(
                "consumer_mismatch_escalation requires operations_contact_url or "
                "operations_contact_email; the schema makes the contact mandatory so the "
                "escalation has a destination"
            )
        if (
            self.consumer_mismatch_escalation is not None
            and self.consumer_mismatch_escalation.total_seconds() < 0
        ):
            raise ValueError("consumer_mismatch_escalation cannot be negative")
        if (
            self.consumer_mismatch_escalation is not None
            and self.consumer_mismatch_escalation.total_seconds() % 1
        ):
            raise ValueError("consumer_mismatch_escalation requires integral seconds")
        if self.operations_contact_url is not None and not self.operations_contact_url.startswith(
            "https://"
        ):
            # Same hardened public-origin shape as the offering document URIs.
            # Enforcing the scheme here keeps "never dereference" true by
            # construction for the obvious loopback/credentialed cases.
            raise ValueError("operations_contact_url must be an https:// URL")

    def to_wire(self) -> dict[str, Any]:
        """The fragment a seller merges into its advertised capability block."""
        payload: dict[str, Any] = {}
        if self.consumer_mismatch_escalation is not None:
            payload["consumer_mismatch_escalation_seconds"] = int(
                self.consumer_mismatch_escalation.total_seconds()
            )
        contact = {
            key: value
            for key, value in (
                ("url", self.operations_contact_url),
                ("email", self.operations_contact_email),
            )
            if value is not None
        }
        if contact:
            payload["operations_contact"] = contact
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
