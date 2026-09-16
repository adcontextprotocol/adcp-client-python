"""``sync_reporting_status`` ingest and the consumer-mismatch projection.

This closes the operational loop between what a seller says it published and
what an authenticated buyer could actually consume.  Without it, a seller's
ledger is a monologue: every obligation can look ``complete`` while the buyer
has never successfully read a single revision.

**Opt-in, and off by default.** ``sync_reporting_status`` is an additive
extension in AdCP 3.2.0-rc.2; it becomes required Core only in the next
eligible minor after 2026-10-24.  Turn the ingest on with::

    import adcp.reporting.ledger.consumer_status as consumer_status
    consumer_status.CONSUMER_STATUS_ENABLED = True

or per-instance via ``ConsumerStatusIngest(..., enabled=True)``.  A seller that
turns it on must advertise ``consumer_status_task``; a seller that does not
should leave it off, because a half-implemented status loop is worse than none
-- a buyer that can file statements nobody reads believes it has told you.

Wire types come from :mod:`adcp.types`; the conditional rules that
``datamodel-code-generator`` flattens away (``received`` requires a revision id
and digest, ``obligation_missing`` forbids them, and so on) are enforced
directly against the bundled ``core/reporting-consumer-status.json`` through
:func:`adcp.validation.schema_loader.get_named_validator`, so they stay in the
schema rather than being restated in Python and drifting.

What a statement is, and is not
-------------------------------

It is **operational status**: could the required reporting for this expected
period actually be consumed?  It is *not* measurement data, delivery totals, a
billing receipt, or authority over the seller's ledger.  ``received`` proves the
buyer consumed the exact Core revision binding -- nothing about materialization
evidence or billing control totals.

Four statuses, four different problems:

============================ ====================================================
``received``                 The exact revision content was consumed.
``obligation_missing``       The period is required by the accepted configuration
                             generation but the seller ledger omitted it.
``revision_missing``         The obligation exists but no required revision was
                             available after ``expected_at``.
``unreadable``               A revision was advertised but its content could not
                             be consumed; ``failure_code`` says why.
============================ ====================================================

``obligation_missing`` is deliberately keyed *without* a seller-issued
obligation id.  Requiring one would make the first missing report invisible
again, which is the exact failure this loop exists to surface.

Attribution
-----------

Consumer status is separately attributed and never satisfies seller production
health.  A conflicting current statement makes **only the submitting caller's**
view ``action_required`` with a stable ``CONSUMER_STATUS_MISMATCH`` issue.  One
buyer's assertion never changes another caller's view, and never touches
seller-advertised reliability statistics without corroboration.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.health import issue_id_for
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ConsumerStatusValue,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDeliveryEscalation,
    ReportingHealth,
    ReportingIssue,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import LedgerConflictError, ReportingLedgerStore
from adcp.reporting.revision_selection import select_reporting_revision

__all__ = [
    "CONSUMER_STATUS_ENABLED",
    "ConsumerMismatch",
    "ConsumerStatusDisabledError",
    "ConsumerStatusIngest",
    "consumer_mismatch_issue_key",
    "consumer_statement_conflicts",
    "current_consumer_statement",
    "project_consumer_mismatch",
    "stale_received_grace_deadline",
]

ResponsibleParty = Literal["buyer", "seller", "provider"]

#: Module-level opt-in for the whole consumer-status surface. Off by default:
#: the task is an additive extension a seller must deliberately advertise.
CONSUMER_STATUS_ENABLED = False


class ConsumerStatusDisabledError(RuntimeError):
    """The consumer-status ingest was called without being enabled."""


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConsumerStatusIngest:
    """Records authenticated consumer statements into the ledger.

    Framework-agnostic like :class:`~adcp.reporting.ledger.status.ReportingStatusHandler`:
    a request mapping plus an authenticated caller in, a response mapping out.
    """

    store: ReportingLedgerStore
    enabled: bool | None = None
    #: Supplies ``recorded_at`` -- when the seller durably recorded the
    #: statement -- and therefore the issue's ``opened_at``. Defaults to wall
    #: clock. A deployment whose ledger boundary comes from the database should
    #: pass the same source here, or an issue's escalation clock and the
    #: snapshot that reads it will disagree about what time it is.
    clock: Callable[[], datetime] | None = None

    def _require_enabled(self) -> None:
        active = CONSUMER_STATUS_ENABLED if self.enabled is None else self.enabled
        if not active:
            raise ConsumerStatusDisabledError(
                "sync_reporting_status is an additive opt-in extension. Set "
                "adcp.reporting.ledger.consumer_status.CONSUMER_STATUS_ENABLED = True, or "
                "pass enabled=True, and advertise consumer_status_task before accepting "
                "live statements."
            )

    async def handle(
        self, request: dict[str, Any], *, account_id: str, consumer_id: str
    ) -> dict[str, Any]:
        """Serve one ``sync_reporting_status`` batch.

        Returns one ``recorded`` / ``unchanged`` / ``failed`` result per
        submitted statement.  A failure in one statement does not fail the
        batch: a buyer reporting five periods should not lose four good
        statements to one stale supersession pointer.
        """
        self._require_enabled()

        statements = request.get("statuses") or []
        if not statements:
            raise LedgerConflictError(
                "EMPTY_BATCH", "a sync_reporting_status batch must carry at least one statement"
            )
        seen_chains: set[tuple[Any, ...]] = set()
        results: list[dict[str, Any]] = []
        for statement in statements:
            status_id = statement.get("reporting_status_id", "")
            problems = _validate_consumer_status_wire(statement)
            if problems:
                results.append(_failed(status_id, "INVALID_CONSUMER_STATUS", problems[0]))
                continue
            try:
                record = self._to_record(
                    statement,
                    account_id=account_id,
                    consumer_id=consumer_id,
                    recorded_at=self._now(),
                )
            except ValueError as error:
                results.append(_failed(status_id, "INVALID_CONSUMER_STATUS", str(error)))
                continue
            if record.chain_key in seen_chains:
                # "A batch contains at most one update for each logical status
                # chain" -- two updates in one batch have no defined order, so
                # the second would silently win or silently lose.
                results.append(
                    _failed(
                        status_id,
                        "DUPLICATE_CHAIN_IN_BATCH",
                        "a batch may carry at most one statement per logical status chain",
                    )
                )
                continue
            seen_chains.add(record.chain_key)
            try:
                # Answer "exact retry?" before validating anything, because
                # some of that validation is time-dependent. "content_mismatch
                # must name the revision the seller currently requires" has an
                # answer that changes when the seller restates, so a retry of a
                # statement this ledger already accepted would otherwise be
                # rejected for a reason that did not apply when it was first
                # recorded. The spec's result_mapping requires `unchanged`, and
                # a buyer retrying after a transport failure has no way to tell
                # a genuine rejection from that.
                replay = await self.store.resolve_consumer_status_replay(record)
                if replay is not None:
                    results.append({"result": "unchanged", "consumer_status": _to_wire(replay)})
                    continue
                await self._validate_against_configuration(record)
                from adcp.reporting.ledger.status_snapshot import ReportingStatusParticipant

                atomic = isinstance(self.store, ReportingStatusParticipant)
                if atomic and isinstance(self.store, ReportingStatusParticipant):
                    stored, recorded = await self.store.record_consumer_status_with_lifecycle(
                        record
                    )
                else:
                    stored, recorded = await self.store.record_consumer_status(record)
            except LedgerConflictError as error:
                results.append(_failed(status_id, error.code, str(error)))
                continue
            if recorded and not atomic:
                await self._open_issue_on_first_observation(stored)
            results.append(
                {
                    "result": "recorded" if recorded else "unchanged",
                    "consumer_status": _to_wire(stored),
                }
            )
        return {"status": "completed", "results": results}

    async def _open_issue_on_first_observation(self, stored: ConsumerStatusRecord) -> None:
        """Start the escalation clock when the statement arrives, not when polled.

        ``opened_at`` is "when the seller first observed this logical
        condition", and the seller observes it here -- a statement landing on
        this task *is* the observation. Deferring to the first
        ``get_reporting_status`` would tie the escalation clock to whether
        anyone happened to poll: a mismatch filed and never read would sit at
        ``opened_at = None`` indefinitely and could never cross
        ``consumer_mismatch_escalation_seconds``, which is precisely the
        unattended case the boundary exists for.

        Only opens; never retires and never changes an existing occurrence.
        ``ensure_issue_opened`` is idempotent, so a supersession that keeps the
        same condition open keeps the same ``opened_at``.
        """
        obligation: ReportingObligationRecord | None = None
        if stored.reporting_obligation_id is not None:
            obligation = await self.store.get_obligation(
                account_id=stored.account_id,
                reporting_obligation_id=stored.reporting_obligation_id,
            )
        if obligation is None:
            obligation = await self.store.find_obligation(
                account_id=stored.account_id,
                delivery_config_id=stored.delivery_config_id,
                delivery_config_version=stored.delivery_config_version,
                period_start=stored.period_start,
                period_end=stored.period_end,
            )
        revisions = (
            await self.store.list_revisions(
                account_id=stored.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if obligation is not None
            else ()
        )
        current = None
        if obligation is not None:
            selection = select_reporting_revision(
                revisions,
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                required_finality=obligation.required_finality,
            )
            if selection.kind == "corrupt":
                return  # Corruption cannot establish or resolve consumer disagreement.
            if selection.kind == "selected":
                current = selection.revision
        if not consumer_statement_conflicts(
            current=stored, current_revision=current, revisions=revisions
        ):
            return
        await self.store.ensure_issue_opened(
            issue_key=consumer_mismatch_issue_key(
                account_id=stored.account_id,
                consumer_id=stored.consumer_id,
                delivery_config_id=stored.delivery_config_id,
                delivery_config_version=stored.delivery_config_version,
                report_definition_id=stored.report_definition_id,
                period_start=stored.period_start,
                period_end=stored.period_end,
            ),
            account_id=stored.account_id,
            consumer_id=stored.consumer_id,
            observed_at=stored.recorded_at,
        )

    async def _validate_against_configuration(self, record: ConsumerStatusRecord) -> None:
        """Resolve every identifier the statement names, within caller + account.

        Deliberately does *not* require an obligation to exist *when none is
        named*: the whole point of ``obligation_missing`` is that it is filed
        when the seller's ledger omitted the period, and the configuration
        generation is what makes that period legitimate.

        But an obligation or revision id the caller *does* supply must resolve.
        The spec's ``batch_identity`` rule is explicit that every optional
        obligation, revision, and superseded status MUST resolve within the
        authenticated caller and account or fail with the same unavailable
        result. Skipping that lets a statement name a foreign, nonexistent, or
        long-superseded revision and still be recorded -- and then projected as
        ``action_required`` against a seller who never published the thing
        being disputed.
        """
        configurations = await self.store.list_configurations(
            account_id=record.account_id, delivery_config_ids=[record.delivery_config_id]
        )
        generation = next(
            (item for item in configurations if item.generation_key == record.generation_key),
            None,
        )
        if generation is None:
            raise LedgerConflictError(
                "UNKNOWN_CONFIGURATION_GENERATION",
                f"{record.delivery_config_id}@{record.delivery_config_version} is not an "
                "accepted configuration generation for this account",
            )
        if generation.report_definition_id != record.report_definition_id:
            raise LedgerConflictError(
                "REPORT_DEFINITION_MISMATCH",
                "the statement names a different report definition than the configuration "
                "generation accepted; unlike reporting promises must not share a status chain",
            )
        if (record.seller_ledger_snapshot_id is None) != (record.seller_ledger_as_of is None):
            raise LedgerConflictError(
                "SELLER_SNAPSHOT_EVIDENCE_INCOMPLETE",
                "seller_ledger_snapshot_id and seller_ledger_as_of are present together or "
                "not at all",
            )
        await self._resolve_named_records(record)
        validate_consumer_status_timing(record, generation, as_of=self._now())

    async def _resolve_named_records(self, record: ConsumerStatusRecord) -> None:
        """Resolve the optional obligation and revision ids the caller supplied.

        Unknown, unauthorized, cross-account, and cross-caller identifiers all
        produce the same result, so this surface cannot be used as an oracle to
        discover which ids exist on another tenant.
        """
        obligation: ReportingObligationRecord | None = None
        if record.reporting_obligation_id is not None:
            obligation = await self.store.get_obligation(
                account_id=record.account_id,
                reporting_obligation_id=record.reporting_obligation_id,
            )
            if obligation is None:
                raise LedgerConflictError(
                    "LOOKUP_UNAVAILABLE",
                    f"reporting_obligation_id {record.reporting_obligation_id} does not "
                    "resolve for this caller and account",
                )
            if (
                obligation.generation_key != record.generation_key
                or obligation.report_definition_id != record.report_definition_id
                or _utc(obligation.period.start) != _utc(record.period_start)
                or _utc(obligation.period.end) != _utc(record.period_end)
            ):
                # The id resolves but describes a different period or
                # generation. Recording it would attach this chain to the wrong
                # obligation, which the immutability rule forbids.
                raise LedgerConflictError(
                    "OBLIGATION_IDENTITY_MISMATCH",
                    f"reporting_obligation_id {record.reporting_obligation_id} names a "
                    "different configuration generation, report definition, or period than "
                    "this statement",
                )

        if obligation is None:
            obligation = await self.store.find_obligation(
                account_id=record.account_id,
                delivery_config_id=record.delivery_config_id,
                delivery_config_version=record.delivery_config_version,
                period_start=record.period_start,
                period_end=record.period_end,
            )
        required = None
        if obligation is not None:
            revisions = await self.store.list_revisions(
                account_id=record.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            selection = select_reporting_revision(
                revisions,
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                required_finality=obligation.required_finality,
            )
            if selection.kind == "corrupt":
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "the revision history requires repair"
                )
            if selection.kind == "selected":
                required = selection.revision
        if record.reporting_revision_id is None:
            return

        revision = await self.store.get_revision(
            account_id=record.account_id, reporting_revision_id=record.reporting_revision_id
        )
        if revision is None or (
            obligation is not None
            and revision.reporting_obligation_id != obligation.reporting_obligation_id
        ):
            raise LedgerConflictError(
                "LOOKUP_UNAVAILABLE",
                f"reporting_revision_id {record.reporting_revision_id} does not resolve for "
                "this caller, account, and period",
            )

        if record.consumer_status != "content_mismatch" or obligation is None:
            return

        # ``content_mismatch`` is valid only against a revision the seller
        # *currently requires* for the period. Against a superseded revision the
        # dispute is already moot -- the seller has replaced the content -- and
        # recording it would degrade the caller's own view over bytes neither
        # party stands behind any more. The buyer's move there is to re-read and
        # either accept or dispute the current revision.
        if required is None or required.reporting_revision_id != record.reporting_revision_id:
            raise LedgerConflictError(
                "REVISION_NOT_CURRENTLY_REQUIRED",
                f"content_mismatch names revision {record.reporting_revision_id}, which is "
                "not the revision this seller currently requires for the period; re-read the "
                "current revision and file against that",
            )

    def _now(self) -> datetime:
        return _utc(self.clock() if self.clock is not None else datetime.now(timezone.utc))

    @staticmethod
    def _to_record(
        statement: dict[str, Any],
        *,
        account_id: str,
        consumer_id: str,
        recorded_at: datetime,
    ) -> ConsumerStatusRecord:
        from adcp.types import ReportingConsumerStatus

        parsed = ReportingConsumerStatus.model_validate(statement)
        period = parsed.period
        return ConsumerStatusRecord(
            reporting_status_id=parsed.reporting_status_id,
            # Identity comes from authenticated transport. A body that asserts
            # a buyer or consumer principal is ignored, not trusted.
            account_id=account_id,
            consumer_id=consumer_id,
            delivery_config_id=parsed.delivery_config_id,
            delivery_config_version=parsed.delivery_config_version,
            report_definition_id=parsed.report_definition_id,
            period_start=_utc(period.start),
            period_end=_utc(period.end),
            period_source_timezone=period.source_timezone,
            consumer_status=_narrow_recorded_status(parsed.consumer_status.value),
            status_as_of=_utc(parsed.status_as_of),
            recorded_at=recorded_at,
            supersedes_reporting_status_id=parsed.supersedes_reporting_status_id,
            reporting_obligation_id=parsed.reporting_obligation_id,
            reporting_revision_id=parsed.reporting_revision_id,
            observed_revision_content_sha256=parsed.observed_revision_content_sha256,
            failure_code=parsed.failure_code.value if parsed.failure_code else None,
            mismatch_code=parsed.mismatch_code.value if parsed.mismatch_code else None,
            consumer_commit_ref=parsed.consumer_commit_ref,
            seller_ledger_snapshot_id=parsed.seller_ledger_snapshot_id,
            seller_ledger_as_of=(
                _utc(parsed.seller_ledger_as_of) if parsed.seller_ledger_as_of else None
            ),
        )


def validate_consumer_status_timing(
    record: ConsumerStatusRecord, generation: ReportingConfiguration, *, as_of: datetime
) -> None:
    """Validate a statement's derived calendar and observation at the locked boundary."""
    from zoneinfo import ZoneInfo

    from adcp.reporting.ledger.models import derive_period, iso_duration_to_timedelta

    stamps = (
        record.period_start,
        record.period_end,
        record.status_as_of,
        record.recorded_at,
        as_of,
    )
    if any(t.tzinfo is None or t.utcoffset() is None for t in stamps):
        raise LedgerConflictError(
            "INVALID_STATUS_TIME", "status timestamps must include a timezone"
        )
    if record.status_as_of > as_of or record.status_as_of < record.period_start:
        raise LedgerConflictError(
            "INVALID_STATUS_TIME", "status observation is outside its valid time range"
        )
    zone_name = generation.schedule.timezone_name(generation.account_timezone)
    if record.period_source_timezone != zone_name:
        raise LedgerConflictError(
            "INVALID_STATUS_PERIOD", "status timezone differs from the accepted schedule"
        )
    zone = ZoneInfo(zone_name)
    anchor = generation.schedule.period_anchor or datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = record.period_start.astimezone(zone).replace(tzinfo=None) - anchor.astimezone(
        zone
    ).replace(tzinfo=None)
    duration = iso_duration_to_timedelta(generation.schedule.period_duration)
    if duration <= timedelta(0) or delta % duration:
        raise LedgerConflictError(
            "INVALID_STATUS_PERIOD", "status period is not a scheduled boundary"
        )
    try:
        period = derive_period(
            generation.schedule,
            account_timezone=generation.account_timezone,
            ordinal=delta // duration,
            activated_at=generation.activated_at,
        )
    except ValueError:
        raise LedgerConflictError(
            "INVALID_STATUS_PERIOD", "status period is outside the accepted generation"
        ) from None
    if (
        period.start != record.period_start
        or period.end != record.period_end
        or (
            generation.deactivated_at is not None
            and record.period_start >= generation.deactivated_at
        )
    ):
        raise LedgerConflictError(
            "INVALID_STATUS_PERIOD", "status period is outside the accepted generation"
        )
    if (
        record.consumer_status in {"obligation_missing", "revision_missing"}
        and record.status_as_of < period.expected_at
    ):
        raise LedgerConflictError(
            "STATUS_NOT_DUE", "a missing report cannot be asserted before its expected time"
        )


def _narrow_recorded_status(value: str) -> ConsumerStatusValue:
    """Narrow a schema ``consumer_status`` to the values this ledger records.

    All five rc.3 values are recordable. Raising on anything else rather than
    coercing means a *future* schema value cannot reach storage disguised as a
    plausible-looking wrong status -- the failure mode that would make a
    buyer's statement say something it never said.
    """
    if value in {
        "received",
        "obligation_missing",
        "revision_missing",
        "unreadable",
        "content_mismatch",
    }:
        return cast(ConsumerStatusValue, value)
    raise ValueError(f"consumer_status {value!r} is not recordable by this ledger")


def _validate_consumer_status_wire(payload: dict[str, Any]) -> list[str]:
    """Check a statement against the bundled schema's conditional rules.

    ``datamodel-code-generator`` flattens the ``allOf``/``if``/``then`` block
    that makes ``received`` require a revision id and digest,
    ``revision_missing`` require an obligation id and forbid a revision id, and
    so on.  Restating those rules in Python would mean maintaining a second
    copy that drifts, so the schema shipped with the SDK enforces them.

    Returns human-readable messages, empty when the statement is valid.  A
    bundle without the schema (an older pin) yields no messages rather than
    failing closed: the Pydantic model has already checked the field types, and
    refusing every statement because the SDK is pinned a version back would
    take a seller's whole status loop offline.
    """
    from adcp.validation.schema_loader import get_named_validator

    validator = get_named_validator("core/reporting-consumer-status.json")
    if validator is None:
        return []
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or 'statement'}: {error.message}"
        for error in sorted(validator.iter_errors(payload), key=str)
    ]


def _failed(status_id: str, code: str, message: str) -> dict[str, Any]:
    return {
        "result": "failed",
        "reporting_status_id": status_id,
        "errors": [{"code": code, "message": message}],
    }


def _to_wire(record: ConsumerStatusRecord) -> dict[str, Any]:
    from adcp.reporting.ledger.status import _consumer_status_to_wire

    return _consumer_status_to_wire(record)


@dataclass(frozen=True)
class ConsumerMismatch:
    """A consumer mismatch, with the severity it degrades the caller's view to.

    Severity is carried alongside the issue rather than read back off it so the
    caller cannot accidentally project ``delayed`` health from an
    ``action_required`` issue, or the reverse.
    """

    issue: ReportingIssue
    severity: Literal["delayed", "action_required"]
    #: Only published occurrences contribute health. A waived occurrence is
    #: omitted entirely by ``project_consumer_mismatch`` until agreement
    #: retires it and rearms the chain for a subsequent occurrence.
    published: bool = True


def consumer_mismatch_issue_key(
    *,
    account_id: str,
    consumer_id: str,
    delivery_config_id: str,
    delivery_config_version: int,
    report_definition_id: str,
    period_start: datetime,
    period_end: datetime,
) -> str:
    """Identity of the *condition*, not of the statement that evidences it.

    Keyed on the logical status chain, deliberately not on
    ``reporting_status_id``. A buyer that supersedes one conflicting statement
    with another conflicting statement has not resolved anything, and rc.3
    anchors the escalation clock to the issue's ``opened_at``. Keying on the
    statement id would mint a fresh issue -- and a fresh clock -- on every
    re-file, letting an unresolved disagreement dodge escalation forever.

    Also deliberately not keyed on the obligation id: a chain that began as
    ``obligation_missing`` attaches to the repaired obligation later, and the
    spec requires that chain never be lost, forked, or reset.
    """
    generation = ReportingConfigurationGenerationKey(
        account_id=account_id,
        delivery_config_id=delivery_config_id,
        delivery_config_version=delivery_config_version,
    )
    payload = canonical_json_utf8_v1(
        [
            "core-consumer-status-mismatch-v1",
            generation.account_id,
            consumer_id,
            generation.delivery_config_id,
            generation.delivery_config_version,
            report_definition_id,
            _utc(period_start).isoformat(),
            _utc(period_end).isoformat(),
        ]
    )
    return "rpik_" + hashlib.sha256(payload).hexdigest()[:40]


def stale_received_grace_deadline(
    *,
    received_revision_id: str,
    revisions: Sequence[ReportingRevisionRecord],
    delivery_sla: timedelta,
    automated_recovery_window: timedelta,
) -> datetime | None:
    """When a stale ``received`` stops being excusable.

    The anchor is the ``created_at`` of the **first** revision that superseded
    the one the consumer named, plus the configuration generation's
    ``delivery_sla`` as exact elapsed UTC time. Anchoring to the first
    supersession rather than the newest one is what stops the window from
    becoming a hiding place: otherwise a seller could hold a genuinely
    unresolved mismatch below ``action_required`` by restating on a timer.

    When ``delivery_sla`` resolves to zero in any of its legal spellings, the
    seller uses ``automated_recovery_window`` instead, so a zero-SLA feed still
    yields a bounded re-read window rather than an instant escalation.

    Returns ``None`` when nothing superseded the named revision -- there is no
    staleness to forgive.
    """
    superseder = _superseder(received_revision_id, revisions)
    if superseder is None:
        return None
    window = delivery_sla if delivery_sla > timedelta(0) else automated_recovery_window
    return _utc(superseder.created_at) + window


def current_consumer_statement(
    statuses: Sequence[ConsumerStatusRecord],
) -> ConsumerStatusRecord | None:
    """This caller's one unsuperseded leaf, or ``None`` for an empty chain."""
    return next((item for item in statuses if not item.superseded), None)


def consumer_statement_conflicts(
    *,
    current: ConsumerStatusRecord,
    current_revision: ReportingRevisionRecord | None,
    revisions: Sequence[ReportingRevisionRecord] = (),
) -> bool:
    """Whether this statement contradicts the seller's *record* of the period.

    Deliberately independent of the seller's health *label*. The two questions
    are different and conflating them is how an escalation clock gets reset:
    health answers "should this caller's view be degraded right now", while
    this answers "does the disagreement still stand" -- which is what decides
    whether the issue may be retired.

    The spec allows resolution two ways: the consumer supersedes with a
    statement that agrees, or the seller's own projection changes so the two no
    longer conflict. Both are record comparisons:

    * ``obligation_missing`` -- the obligation is in front of us, so the buyer's
      claim that the seller omitted the period is contradicted. Always conflicts.
    * ``revision_missing`` -- conflicts only while the seller actually has a
      current required revision. If the seller has none either, they agree.
    * ``unreadable`` -- conflicts while the named revision is still readable in
      the seller's record. A seller that has since marked it unreadable agrees.
    * ``content_mismatch`` -- conflicts while the named revision is still the
      one the seller requires, because that is the seller standing behind the
      content the buyer disputes.
    * ``received`` -- conflicts only once something superseded the revision the
      buyer named.
    """
    status = current.consumer_status
    if status == "obligation_missing":
        return True
    if status == "revision_missing":
        return current_revision is not None
    if status == "unreadable":
        named = _named_revision(current, revisions)
        return named is None or named.readable
    if status == "content_mismatch":
        return (
            current_revision is not None
            and current.reporting_revision_id == current_revision.reporting_revision_id
        )
    if status == "received":
        if current.reporting_revision_id is None:
            return False
        if (
            current_revision is not None
            and current.reporting_revision_id == current_revision.reporting_revision_id
        ):
            return False
        # Stale only if a restatement actually superseded it. With no
        # supersession and no current required revision there is nothing to
        # re-read, so nothing to forgive and nothing to page about.
        return _superseder(current.reporting_revision_id, revisions) is not None or (
            current_revision is not None
        )
    # No fallback return: every ``ConsumerStatusValue`` is handled above, so a
    # sixth status added to the schema fails mypy with "missing return
    # statement" rather than silently classifying itself as "no conflict".


def _named_revision(
    current: ConsumerStatusRecord, revisions: Sequence[ReportingRevisionRecord]
) -> ReportingRevisionRecord | None:
    return next(
        (
            revision
            for revision in revisions
            if revision.reporting_revision_id == current.reporting_revision_id
        ),
        None,
    )


def _superseder(
    revision_id: str, revisions: Sequence[ReportingRevisionRecord]
) -> ReportingRevisionRecord | None:
    """The one revision that superseded ``revision_id``, if any.

    Unique by construction: the ledger's one-successor index permits at most
    one, which is what makes "the *first* supersession" well defined.
    """
    return next(
        (
            revision
            for revision in revisions
            if revision.supersedes_reporting_revision_id == revision_id
        ),
        None,
    )


def project_consumer_mismatch(
    *,
    obligation: ReportingObligationRecord,
    current_revision: ReportingRevisionRecord | None,
    statuses: Sequence[ConsumerStatusRecord],
    seller_health: ReportingHealth,
    revisions: Sequence[ReportingRevisionRecord] = (),
    ledger_as_of: datetime | None = None,
    delivery_sla: timedelta = timedelta(0),
    automated_recovery_window: timedelta = timedelta(hours=6),
    escalation: ReportingDeliveryEscalation | None = None,
    lifecycle: ReportingIssueLifecycle | None = None,
) -> ConsumerMismatch | None:
    """Compare the seller's projection with this caller's current statement.

    Returns a mismatch only when they genuinely conflict. Five conflict kinds,
    four of them immediate:

    * ``obligation_missing``, ``revision_missing``, ``unreadable``, and
      ``content_mismatch`` against an otherwise ``healthy``/``complete`` seller
      projection are ``action_required`` at once; and
    * ``received`` naming a revision the seller has since superseded is
      ``delayed`` until its grace deadline, then ``action_required``.

    The carve-out exists because that buyer consumed exactly what the seller
    required at the time and has not yet had a bounded chance to re-read.
    Paging a human the instant a seller restates a provisional revision would
    train buyers to ignore the signal.

    An advertised ``consumer_mismatch_escalation`` boundary takes precedence
    when the two windows overlap: past ``opened_at`` plus that window the issue
    is ``action_required`` with a ``contact_*`` action regardless of the grace
    deadline. ``wait_for_retry`` must not survive that boundary -- an
    unattended mismatch is an escalation, not a retry.

    Missing consumer status stays *unknown*. It never excuses seller reporting
    and never by itself degrades seller health; silence is surfaced only
    through ``obligation_counts.consumer_status_pending``.
    """
    current = current_consumer_statement(statuses)
    if current is None:
        return None
    if lifecycle is not None and lifecycle.issue_state == "waived":
        return None
    if not consumer_statement_conflicts(
        current=current, current_revision=current_revision, revisions=revisions
    ):
        return None
    if seller_health not in {"healthy", "complete"}:
        # The seller already knows something is wrong and has said so. Adding a
        # second issue for the same condition would double-count it.
        #
        # Note this is *only* a suppression of the emission. The condition is
        # still open, so a caller must not read ``None`` here as "resolved" --
        # see :func:`consumer_statement_conflicts`.
        return None

    boundary = _utc(ledger_as_of) if ledger_as_of is not None else None
    severity: Literal["delayed", "action_required"] = "action_required"
    grace_deadline: datetime | None = None

    if current.consumer_status == "received":
        assert current.reporting_revision_id is not None  # the conflict test proved it
        grace_deadline = stale_received_grace_deadline(
            received_revision_id=current.reporting_revision_id,
            revisions=revisions,
            delivery_sla=delivery_sla,
            automated_recovery_window=automated_recovery_window,
        )
        if grace_deadline is not None and boundary is not None and boundary < grace_deadline:
            severity = "delayed"
        responsible: ResponsibleParty = "buyer"
        message = (
            "the authenticated consumer received revision "
            f"{current.reporting_revision_id} but the current required revision is "
            f"{current_revision.reporting_revision_id if current_revision else 'unknown'}; "
            "the consumer is working from a superseded restatement"
        )
    else:
        responsible = _responsible_for(current)
        message = _negative_message(current)

    opened_at = _utc(lifecycle.opened_at) if lifecycle is not None else boundary
    escalated = False
    window = escalation.consumer_mismatch_escalation if escalation is not None else None
    if window is not None and opened_at is not None and boundary is not None:
        # Precedence: the escalation boundary wins when it overlaps the grace
        # window. Measured from the issue's opened_at, never from this poll, so
        # re-emission cannot reset it.
        if boundary >= opened_at + window:
            severity = "action_required"
            escalated = True

    issue = _mismatch_issue(
        obligation,
        current,
        responsible_party=responsible,
        message=message,
        severity=severity,
        escalated=escalated,
        lifecycle=lifecycle,
        opened_at=opened_at,
    )
    published = lifecycle is None or lifecycle.published
    return ConsumerMismatch(issue=issue, severity=severity, published=published)


def _negative_message(status: ConsumerStatusRecord) -> str:
    if status.consumer_status == "content_mismatch":
        return (
            "the authenticated consumer read revision "
            f"{status.reporting_revision_id} and reports {status.mismatch_code}: the content "
            "contradicts a fact this configuration generation already fixed. This is a "
            "contract-fact disagreement, not a dispute about counts"
        )
    return (
        f"the authenticated consumer reports {status.consumer_status} for this period "
        "while the seller projects healthy reporting"
    )


def _responsible_for(status: ConsumerStatusRecord) -> ResponsibleParty:
    """Diagnose who must act, from the typed failure rather than from prose.

    ``access_denied`` and ``reader_incompatible`` describe the consumer's own
    access or reader; everything else points at the seller's production or
    publication.  A seller with better diagnostics should override this.
    """
    if status.consumer_status == "unreadable" and status.failure_code in {
        "access_denied",
        "reader_incompatible",
    }:
        return "buyer"
    if status.consumer_status == "unreadable" and status.failure_code == "transport_failed":
        return "provider"
    if status.consumer_status == "content_mismatch":
        # Every mismatch_code names a promise the seller's own revision failed
        # to keep against the generation both sides accepted -- a missing
        # frozen media buy, short coverage, an absent promised metric, a
        # non-conformant row schema, the wrong unit, rows outside the period.
        # None of them is something the buyer can repair on its side.
        return "seller"
    return "seller"


_CONTACT_FOR: dict[ResponsibleParty, str] = {
    "buyer": "contact_buyer",
    "seller": "contact_seller",
    "provider": "contact_provider",
}


def _mismatch_issue(
    obligation: ReportingObligationRecord,
    status: ConsumerStatusRecord,
    *,
    responsible_party: ResponsibleParty,
    message: str,
    severity: Literal["delayed", "action_required"],
    escalated: bool,
    lifecycle: ReportingIssueLifecycle | None,
    opened_at: datetime | None,
) -> ReportingIssue:
    if escalated:
        recommended = _CONTACT_FOR[responsible_party]
    elif severity == "delayed":
        recommended = "wait_for_retry"
    elif responsible_party == "buyer":
        recommended = "repair_access"
    else:
        recommended = _CONTACT_FOR[responsible_party]
    return ReportingIssue(
        issue_id=(
            lifecycle.issue_id
            if lifecycle is not None
            else issue_id_for(
                "core-consumer-status-mismatch-v1",
                obligation.reporting_obligation_id,
                status.reporting_status_id,
            )
        ),
        code="CONSUMER_STATUS_MISMATCH",
        severity=severity,
        responsible_party=responsible_party,
        recommended_action=recommended,
        reporting_obligation_id=obligation.reporting_obligation_id,
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        feed_purpose=obligation.feed_purpose,
        media_buy_ids=obligation.media_buy_ids,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
        expected_at=obligation.period.expected_at,
        reporting_status_id=status.reporting_status_id,
        message=message,
        opened_at=opened_at,
        issue_state=lifecycle.issue_state if lifecycle is not None else None,
        external_ref=lifecycle.external_ref if lifecycle is not None else None,
    )


def consumer_status_chain_id(
    *,
    account_id: str,
    consumer_id: str,
    delivery_config_id: str,
    version: int,
    period_end: datetime,
) -> str:
    """A stable id for one logical status chain, for logs and metrics."""
    payload = f"{account_id}|{consumer_id}|{delivery_config_id}|{version}|{_utc(period_end)}"
    return "rpsc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
