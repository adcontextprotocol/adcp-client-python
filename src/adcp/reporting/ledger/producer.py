"""The seller-side loop: close a period, acquire it, commit it, restate it.

:class:`ReportingProducer` is the scheduler.  A
:class:`~adcp.reporting.source.ReportingSourceExecutor` is not: it fulfills one
frozen slice and has no opinion about when.  Keeping that boundary is what lets
a seller swap ad servers without touching obligations, and swap schedules
without touching the fetch.

One turn of :meth:`ReportingProducer.run_worker` does, per leased configuration
generation:

1. **Freeze the scope and commit the obligation** for every elapsed eligible
   period, *before* acquiring source data.  A missing first report has to be
   detectable, and it only is if the obligation exists whether or not the
   source ever answered.
2. **Execute the source** for obligations without a satisfying revision, until
   the data is ready or the recovery window elapses.
3. **Commit an immutable revision** from the returned manifest -- including a
   zero-row one, which satisfies the obligation exactly like any other.
4. **Restate at declared finality times.**  A snapshot restatement supersedes
   the prior snapshot.  The official close is terminal; a later source
   correction becomes an adjustment, never an edit.

Nothing here needs Temporal, Celery, or a Redis lock.  The durability lives in
the ledger store; the worker is a stateless turn you can run from cron, from a
loop, or from whatever supervisor you already operate.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, TypeAlias

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.currency import (
    ReportingCurrencyError,
    require_frozen_currency,
    validate_currency,
)
from adcp.reporting.evidence import ReportingControlTotalRecord, freeze_control_totals
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingDeliveryEscalation,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingRevisionRecord,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.store import (
    LeasedConfiguration,
    LedgerConflictError,
    ReportingLedgerStore,
    RestatementCheckpoint,
    RestatementCheckpointStore,
)
from adcp.reporting.revision_selection import select_reporting_revision
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ProvisionalSnapshotOfferingV1,
    ReportingConstituent,
    ReportingContractIdentityV1,
    ReportingPublicationClass,
    ReportingSourceCoverageRequestV1,
    ReportingSourceExecutor,
    ReportingSourceExecutorResult,
    ReportingSourceIdentityV1,
    ReportingSourcePeriodV1,
    ReportingSourceSliceRequestV1,
    ReportingSourceStagedObjectReader,
    SourceBatchManifestV1,
    coverage_denominator_fingerprint_v1,
    iso_duration_milliseconds_v1,
    parse_verified_source_batch_manifest_v1,
)

if TYPE_CHECKING:
    from adcp.reporting.ledger.producer_progress import ReportingProducerProgress
    from adcp.reporting.materializer.verification import ReportingRevisionVerifier

__all__ = [
    "CurrencyResolver",
    "FixedCurrencyResolver",
    "ProducerOfferings",
    "ReportingProducer",
    "WorkerTurn",
    "revision_content_sha256",
]

logger = logging.getLogger(__name__)


CurrencyResolver: TypeAlias = Callable[
    [ReportingConfiguration, ReportingObligationRecord], Awaitable[str] | str
]
"""Resolve from trusted account/definition state at the obligation's period end.

The candidate obligation supplies the account-qualified generation, historical
scope and boundary. It has no currency yet. No buyer context or source response
is passed. Implementations must reject mixed-currency media-buy/package scope;
``require_single_currency`` can check their historical constituent values.
An async resolver may load the account context for a future reporting service.
"""


@dataclass(frozen=True)
class FixedCurrencyResolver:
    """The backward-compatible single-currency default, also usable explicitly."""

    currency: str = "USD"

    def __post_init__(self) -> None:
        validate_currency(self.currency)

    def __call__(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> str:
        return self.currency


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def revision_content_sha256(
    *,
    reporting_revision_id: str,
    row_count: int,
    control_totals: Sequence[tuple[str, str]],
    reporting_rows: Sequence[dict[str, Any]],
    control_total_evidence: Sequence[ReportingControlTotalRecord] | None = None,
) -> str:
    """The Core revision binding: JCS over the four bound fields, SHA-256.

    This is the digest ``get_media_buy_delivery`` echoes in
    ``reporting_revision_binding`` and the one a consumer independently
    recomputes from what it actually read.  It binds exactly
    ``{reporting_revision_id, row_count, control_totals, reporting_rows}`` --
    nothing about storage, materialization, or delivery, which is what keeps
    Core's digest distinct from the Managed Delivery canonicalization contract.

    New managed publishers supply ``control_total_evidence`` to bind the exact
    type/unit-bearing totals exposed by status and exact reads. Omitting it keeps
    the existing Core pair projection and all previously retained hashes intact.
    """
    totals = (
        [
            item.to_wire()
            for item in freeze_control_totals(tuple(control_total_evidence), tuple(control_totals))
        ]
        if control_total_evidence is not None
        else [{"name": name, "value": value} for name, value in control_totals]
    )
    return hashlib.sha256(
        canonical_json_utf8_v1(
            {
                "reporting_revision_id": reporting_revision_id,
                "row_count": row_count,
                "control_totals": totals,
                "reporting_rows": [dict(row) for row in reporting_rows],
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class ProducerOfferings:
    """Which source offering serves which finality for one configuration.

    Snapshot and official source publications use different offerings. A
    source-declared settling policy may use them sequentially for one ledger
    obligation, so the producer needs both named explicitly rather than
    inferring one from the other.
    """

    snapshot_offering_id: str | None = None
    official_offering_id: str | None = None
    publication_namespace: str = "reporting-source:default"
    requested_metrics: tuple[str, ...] = ("impressions", "spend")
    requested_dimensions: tuple[str, ...] = ()
    currency: str = "USD"
    source_scope: dict[str, Any] = field(default_factory=dict)
    slice_timeout: timedelta = timedelta(minutes=10)

    def offering_for(self, finality: str) -> str | None:
        return self.official_offering_id if finality == "official" else self.snapshot_offering_id


@dataclass
class WorkerTurn:
    """What one turn of the worker actually did.

    Returned rather than logged-and-forgotten so a supervisor can decide
    whether to run again immediately or back off, and so tests can assert on
    the loop without scraping logs.
    """

    leased: LeasedConfiguration | None = None
    obligations_committed: list[str] = field(default_factory=list)
    revisions_committed: list[str] = field(default_factory=list)
    slices_failed: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)

    @property
    def did_work(self) -> bool:
        return bool(
            self.obligations_committed
            or self.revisions_committed
            or self.slices_failed
            or self.escalated
        )


@dataclass(frozen=True)
class _SettlingPolicy:
    restatement_window: timedelta
    restatement_cadence: timedelta
    official_close_lag: timedelta | None


class ReportingProducer:
    """Closes periods, drives the source, and commits immutable revisions."""

    def __init__(
        self,
        *,
        source: ReportingSourceExecutor,
        offerings: ProducerOfferings,
        store: ReportingLedgerStore,
        object_reader: ReportingSourceStagedObjectReader | None = None,
        escalation: ReportingDeliveryEscalation | None = None,
        worker_id: str = "reporting-producer",
        lease_seconds: float = 60.0,
        max_periods_per_turn: int = 64,
        clock: Callable[[], datetime] | None = None,
        currency_resolver: CurrencyResolver | None = None,
        revision_verifier: ReportingRevisionVerifier | None = None,
    ) -> None:
        self._source = source
        self._offerings = offerings
        self._store = store
        self._object_reader = object_reader
        self._escalation = escalation or ReportingDeliveryEscalation()
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_periods_per_turn = max_periods_per_turn
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._currency_resolver = (
            currency_resolver
            if currency_resolver is not None
            else FixedCurrencyResolver(offerings.currency)
        )
        self._revision_verifier = revision_verifier

    @property
    def store(self) -> ReportingLedgerStore:
        return self._store

    @property
    def escalation(self) -> ReportingDeliveryEscalation:
        """The advertised escalation commitment, for the status handler.

        Pass the same object to :class:`~adcp.reporting.ledger.status.ReportingStatusHandler`
        so the projection honours exactly the window the seller published. A
        handler with a different window than the capability block would escalate
        on a clock no buyer can see.
        """
        return self._escalation

    def advertised_reporting_delivery(
        self,
        *,
        consumer_status_task: bool,
        offerings: Sequence[Mapping[str, Any]],
        automated_recovery_window: timedelta,
        status_retention_days: int,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The complete ``media_buy.reporting_delivery`` block for this producer.

        Returns a *whole* capability document, not a fragment, so the result can
        be validated against ``core/reporting-delivery-capabilities.json``
        before it is published. A fragment would push six required fields onto
        the caller to remember, and an under-filled capability block is exactly
        the kind of thing that passes review and fails a buyer's validator.

        The seller supplies what only it knows -- its ``offerings``, its
        seller-wide recovery window and retention. This method supplies the task
        names and the Reliable Reporting declarations, because those follow from
        the producer actually running rather than from configuration.

        ``consumer_status_task`` is an explicit argument rather than inferred:
        advertising it while the ingest is disabled is the half-implemented loop
        this module's docstring warns about, and a buyer that can file
        statements nobody reads believes it has told you.
        """
        if not consumer_status_task and self._escalation.consumer_mismatch_escalation is not None:
            raise ValueError(
                "consumer_mismatch_escalation_seconds requires consumer_status_task: an "
                "escalation commitment on a loop no buyer can post to is unpublishable"
            )
        payload: dict[str, Any] = {
            "supported": True,
            "reliable_reporting_version": "1.0",
            "configuration_task": "sync_accounts",
            "status_task": "get_reporting_status",
            # Required whenever reliable_reporting_version is 1.0, and again
            # whenever consumer_status_task is present. Both fields are `const`
            # *task names* in the schema, not booleans: a seller advertises
            # which task serves the capability, so emitting `true` produces a
            # block that fails its own capabilities schema.
            "revision_content_task": "get_media_buy_delivery",
            "offerings": [dict(offering) for offering in offerings],
            "automated_recovery_window_seconds": int(automated_recovery_window.total_seconds()),
            "status_retention_days": status_retention_days,
        }
        if consumer_status_task:
            payload["consumer_status_task"] = "sync_reporting_status"
        payload.update(self._escalation.to_wire())
        if extra:
            reserved = {
                *payload,
                "consumer_status_task",
                "managed_delivery",
                "reconciled_billing",
                "reporting.delivery_ready",
                "ledger_notification",
                "readiness_notification",
                "status_notification",
                "supports_webhook_activity",
                "receipt_task",
            }
            if any(key in reserved or key.endswith(("_task", "_notification")) for key in extra):
                raise ValueError("extra cannot override SDK-owned reporting capabilities")
            payload.update(extra)
        return payload

    # -- the worker turn -------------------------------------------------

    async def run_worker(self) -> WorkerTurn:
        """Run one leased turn. Safe to call from cron, a loop, or a supervisor.

        Returns immediately with an empty turn when nothing is leasable, so a
        caller can back off rather than spin.
        """
        now = self._clock()
        leased = await self._store.lease_period_close(
            worker_id=self._worker_id, now=now, lease_seconds=self._lease_seconds
        )
        turn = WorkerTurn(leased=leased)
        if leased is None:
            return turn
        try:
            configuration = next(
                (
                    candidate
                    for candidate in await self._store.list_configurations(
                        account_id=leased.account_id,
                        delivery_config_ids=[leased.delivery_config_id],
                    )
                    if candidate.generation_key == leased.generation_key
                ),
                None,
            )
            if configuration is None:
                return turn
            await self._close_elapsed_periods(configuration, turn, now=now)
            await self._acquire_pending(configuration, turn, now=now)
        finally:
            await self._store.release_period_close(leased, worker_id=self._worker_id)
        return turn

    async def run_configuration(
        self,
        configuration: ReportingConfiguration,
        *,
        now: datetime | None = None,
    ) -> WorkerTurn:
        """Run one turn for an already-routed configuration generation.

        High-level orchestrators use this entry point after freezing adapter,
        currency, and source scope for a specific generation. The orchestrator
        owns cross-process scheduling; obligation and revision writes remain
        convergent and immutable in the ledger store.

        Most adopters should continue using :meth:`run_worker`, whose store
        lease chooses a configuration automatically.
        """
        boundary = now or self._clock()
        turn = WorkerTurn()
        await self._close_elapsed_periods(configuration, turn, now=boundary)
        await self._acquire_pending(configuration, turn, now=boundary)
        return turn

    # -- step 1: obligations before reports ------------------------------

    async def close_elapsed_periods(
        self, configuration: ReportingConfiguration, *, now: datetime | None = None
    ) -> list[ReportingObligationRecord]:
        """Commit an obligation for every elapsed eligible period.

        Public because a seller often wants to run this on its own cadence --
        the obligation must land in the first ledger snapshot strictly after the
        period boundary, independent of whether the source is healthy.
        """
        turn = WorkerTurn()
        return await self._close_elapsed_periods(configuration, turn, now=now or self._clock())

    async def _close_elapsed_periods(
        self,
        configuration: ReportingConfiguration,
        turn: WorkerTurn,
        *,
        now: datetime,
    ) -> list[ReportingObligationRecord]:
        from adcp.reporting.ledger.producer_progress import ReportingProducerProgress

        progress = self._store if isinstance(self._store, ReportingProducerProgress) else None
        after = None if progress is None else await progress.producer_closed_through(configuration)
        committed: list[ReportingObligationRecord] = []
        for boundary in self._elapsed_periods(configuration, now=now, after=after):
            existing = await self._store.find_obligation(
                account_id=configuration.account_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                period_start=boundary.start,
                period_end=boundary.end,
            )
            if existing is not None:
                if progress is not None:
                    await progress.commit_producer_period(
                        configuration, existing, previous_end=after
                    )
                    after = boundary.end
                continue
            obligation = ReportingObligationRecord(
                reporting_obligation_id=self._obligation_id(configuration, boundary),
                account_id=configuration.account_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                report_definition_id=configuration.report_definition_id,
                reporting_profile=configuration.reporting_profile,
                feed_purpose=configuration.feed_purpose,
                period=boundary,
                # The denominator froze at the period boundary. Resolving it
                # "now" would silently include a media buy that started after
                # the period it is being reported against.
                scope_resolved_at=boundary.end,
                media_buy_ids=configuration.media_buy_ids,
                required_finality=configuration.required_finality,
                automated_recovery_deadline_at=(
                    boundary.expected_at + configuration.automated_recovery_window
                ),
                schedule=configuration.schedule,
                definition=configuration.definition,
                created_at=now,
            )
            resolved = self._currency_resolver(configuration, obligation)
            currency = await resolved if inspect.isawaitable(resolved) else resolved
            obligation = replace(obligation, currency=validate_currency(currency))
            stored = (
                await self._store.commit_obligation(obligation)
                if progress is None
                else await progress.commit_producer_period(
                    configuration, obligation, previous_end=after
                )
            )
            after = boundary.end
            committed.append(stored)
            turn.obligations_committed.append(stored.reporting_obligation_id)
        return committed

    def _elapsed_periods(
        self,
        configuration: ReportingConfiguration,
        *,
        now: datetime,
        after: datetime | None = None,
    ) -> list[ReportingPeriodBoundary]:
        """Every eligible period that has closed but is not yet obligated.

        A period is eligible once its *end* is at or before now. Activation
        owes the first full period; deactivation after a period has started
        retains that whole period and its original SLA. Polling forecasts use
        the same committed-generation iterator.
        """
        from itertools import islice

        from adcp.reporting.ledger.schedule import committed_periods

        boundaries: list[ReportingPeriodBoundary] = []
        near = (
            None
            if after is None
            else after + iso_duration_to_timedelta(configuration.schedule.delivery_sla)
        )
        periods = (
            period
            for period in committed_periods(configuration, near=near)
            if after is None or period.end > after
        )
        for boundary in islice(periods, self._max_periods_per_turn):
            if _utc(boundary.end) > _utc(now):
                break
            boundaries.append(boundary)
        return boundaries

    @staticmethod
    def _obligation_id(
        configuration: ReportingConfiguration, boundary: ReportingPeriodBoundary
    ) -> str:
        digest = hashlib.sha256(
            canonical_json_utf8_v1(
                [
                    configuration.account_id,
                    configuration.delivery_config_id,
                    configuration.delivery_config_version,
                    configuration.report_definition_id,
                    _utc(boundary.start).isoformat(),
                    _utc(boundary.end).isoformat(),
                ]
            )
        ).hexdigest()
        return f"rpo_{digest[:40]}"

    # -- steps 2-4: acquire, commit, restate -----------------------------

    async def _acquire_pending(
        self, configuration: ReportingConfiguration, turn: WorkerTurn, *, now: datetime
    ) -> None:
        from adcp.reporting.ledger.producer_progress import ReportingProducerProgress

        if isinstance(self._store, ReportingProducerProgress):
            await self._acquire_progress(self._store, configuration, turn, now=now)
            return
        for boundary in self._elapsed_periods(configuration, now=now):
            obligation = await self._store.find_obligation(
                account_id=configuration.account_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                period_start=boundary.start,
                period_end=boundary.end,
            )
            if obligation is None:
                continue
            try:
                policy = self._settling_policy(configuration, obligation)
                if policy is None:
                    await self.acquire_obligation(configuration, obligation, turn=turn, now=now)
                else:
                    await self._acquire_with_settling_policy(
                        configuration,
                        obligation,
                        policy=policy,
                        turn=turn,
                        now=now,
                    )
            except ReportingCurrencyError as error:
                # One obligation whose money cannot be interpreted -- a legacy
                # period with no retained currency, or a source contradicting
                # the frozen one -- is a stuck slice, not a broken worker.
                # Raising here would starve every later period under this
                # configuration on every turn, forever.
                logger.info(
                    "reporting slice failed obligation=%s code=%s",
                    obligation.reporting_obligation_id,
                    error.code,
                )
                turn.slices_failed.append(obligation.reporting_obligation_id)
                self._note_escalation(obligation, turn, now=now)

    async def _acquire_progress(
        self,
        progress: ReportingProducerProgress,
        configuration: ReportingConfiguration,
        turn: WorkerTurn,
        *,
        now: datetime,
    ) -> None:
        identifiers = await progress.next_producer_obligations(
            configuration, now=now, limit=self._max_periods_per_turn
        )
        for identifier in identifiers:
            obligation = await self._store.get_obligation(
                account_id=configuration.account_id, reporting_obligation_id=identifier
            )
            if obligation is None or obligation.generation_key != configuration.generation_key:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer history is unavailable")
            policy = self._settling_policy(configuration, obligation)
            finished = policy is None
            try:
                if policy is None:
                    await self.acquire_obligation(configuration, obligation, turn=turn, now=now)
                else:
                    finished = await self._acquire_with_settling_policy(
                        configuration, obligation, policy=policy, turn=turn, now=now
                    )
            except (ReportingCurrencyError, LedgerConflictError) as error:
                if isinstance(error, LedgerConflictError) and error.code not in {
                    "HISTORY_UNAVAILABLE",
                    "EMPTY_DENOMINATOR",
                }:
                    raise
                turn.slices_failed.append(identifier)
                self._note_escalation(obligation, turn, now=now)
                # Retain the existing corrupt-history parking check. A transient
                # currency failure during settling must not retire a readable leaf.
                finished = policy is None or isinstance(error, LedgerConflictError)
            if finished:
                await progress.finish_producer_acquisition(
                    configuration, reporting_obligation_id=identifier
                )

    def _settling_policy(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
    ) -> _SettlingPolicy | None:
        """Resolve the optional source-declared policy for a snapshot obligation."""
        offering_id = self._offerings.snapshot_offering_id
        if obligation.required_finality != "snapshot" or offering_id is None:
            return None
        offering = self._source.capabilities.offering(offering_id)
        if not isinstance(offering, ProvisionalSnapshotOfferingV1):
            return None
        if offering.restatement_window is None:
            return None

        if offering.restatement_cadence is not None:
            cadence = timedelta(
                milliseconds=iso_duration_milliseconds_v1(offering.restatement_cadence)
            )
        else:
            cadence = max(
                iso_duration_to_timedelta(configuration.schedule.period_duration),
                timedelta(milliseconds=iso_duration_milliseconds_v1(offering.fastest_safe_cadence)),
            )
        close_lag = (
            timedelta(milliseconds=iso_duration_milliseconds_v1(offering.official_close_lag))
            if offering.official_close_lag is not None
            else None
        )
        return _SettlingPolicy(
            restatement_window=timedelta(
                milliseconds=iso_duration_milliseconds_v1(offering.restatement_window)
            ),
            restatement_cadence=cadence,
            official_close_lag=close_lag,
        )

    async def _acquire_with_settling_policy(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        policy: _SettlingPolicy,
        turn: WorkerTurn,
        now: datetime,
    ) -> bool:
        """Return whether policy-controlled acquisition may leave the pending queue.

        A readable snapshot completes one acquisition, not the settling policy.
        The progress store retains its rotating work item until the policy ends.
        """
        checkpoint_store = self._restatement_store()
        revisions = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        if any(item.finality == "official" for item in revisions):
            return True
        if not revisions:
            await self.acquire_obligation(
                configuration,
                obligation,
                turn=turn,
                now=now,
                target_finality="snapshot",
                track_settling=True,
            )
            return False

        checkpoint = await checkpoint_store.get_restatement_checkpoint(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        declared_until = _utc(obligation.period.end) + policy.restatement_window
        settles_at = (
            _utc(checkpoint.provisional_until)
            if checkpoint is not None and checkpoint.provisional_until is not None
            else declared_until
        )
        if _utc(now) < settles_at:
            last_checked = (
                checkpoint.checked_at
                if checkpoint is not None
                else max(revisions, key=lambda item: _utc(item.created_at)).created_at
            )
            if _utc(now) >= _utc(last_checked) + policy.restatement_cadence:
                await self.acquire_obligation(
                    configuration,
                    obligation,
                    restate=True,
                    turn=turn,
                    now=now,
                    target_finality="snapshot",
                    track_settling=True,
                )
            return False

        if policy.official_close_lag is None or self._offerings.official_offering_id is None:
            return True
        closes_at = max(
            settles_at,
            _utc(obligation.period.end) + policy.official_close_lag,
        )
        if _utc(now) >= closes_at:
            await self.acquire_obligation(
                configuration,
                obligation,
                restate=True,
                turn=turn,
                now=now,
                target_finality="official",
                track_settling=True,
            )
            # An attempted close can still return not-ready. Retire only after
            # the authoritative revision was actually committed.
            revisions = await self._store.list_revisions(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            return any(item.finality == "official" for item in revisions)
        return False

    async def acquire_obligation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        restate: bool = False,
        turn: WorkerTurn | None = None,
        now: datetime | None = None,
        target_finality: str | None = None,
        track_settling: bool = False,
    ) -> ReportingRevisionRecord | None:
        """Drive one obligation from its source and commit what comes back.

        Returns the committed revision, or ``None`` when the source is not ready
        or the obligation is already satisfied.

        ``restate`` asks for a *new observation* of an already-satisfied
        snapshot obligation.  Without it a satisfied obligation is left alone,
        because re-reading a settled period on every worker turn would burn
        upstream quota to republish bytes nobody asked for.

        ``now`` freezes dispatch and the source read cutoff. Revision creation
        uses a fresh producer clock sample after the staged objects are read.
        """
        if configuration.generation_key != obligation.generation_key:
            raise LedgerConflictError(
                "CONFIGURATION_GENERATION_MISMATCH",
                "the source configuration must belong to the obligation's account and generation",
            )
        obligation = await self._stored_obligation(obligation)
        turn = turn or WorkerTurn()
        now = now or self._clock()
        revisions = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        selection = select_reporting_revision(
            revisions,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality=obligation.required_finality,
        )
        if selection.kind == "corrupt":
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "the revision history requires repair")
        current = selection.revision if selection.kind == "selected" else None
        if current is not None and current.finality == "official":
            # An official close is terminal. A later source correction is an
            # adjustment, never another acquisition.
            return None
        satisfied = current is not None and current.readable
        if satisfied and not restate:
            return None
        # Everything below needs the frozen code: the slice request carries it,
        # the manifest is checked against it, and the revision is written under
        # it. Gate here rather than earlier so a settled legacy obligation stays
        # the no-op it already was instead of becoming an error on every turn.
        require_frozen_currency(obligation.currency)

        finality = target_finality or obligation.required_finality
        offering_id = self._offerings.offering_for(finality)
        if offering_id is None:
            raise LedgerConflictError(
                "NO_OFFERING_FOR_FINALITY",
                f"this producer declares no source offering for {finality} reporting",
            )

        from adcp.reporting.ledger.producer_progress import ReportingProducerProgress

        constituents = (
            await self._store.producer_constituents(configuration, obligation)
            if isinstance(self._store, ReportingProducerProgress)
            else None
        )

        checkpoint_store = self._restatement_store() if track_settling else None
        checkpoint = (
            await checkpoint_store.get_restatement_checkpoint(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if checkpoint_store is not None
            else None
        )
        observation = checkpoint.next_observation if checkpoint is not None else len(revisions)
        request = self._build_slice(
            configuration,
            obligation,
            offering_id,
            finality=finality,
            now=now,
            observation=observation,
            constituents=constituents,
        )
        cancel = asyncio.Event()
        try:
            result = await asyncio.wait_for(
                self._source.execute(request, cancel=cancel),
                timeout=self._offerings.slice_timeout.total_seconds(),
            )
        except asyncio.TimeoutError:
            cancel.set()
            turn.slices_failed.append(obligation.reporting_obligation_id)
            self._note_escalation(obligation, turn, now=now)
            return None

        if not result.ok:
            error = result.error
            assert error is not None
            logger.info(
                "reporting slice failed obligation=%s code=%s retry=%s",
                obligation.reporting_obligation_id,
                error.code,
                error.retry,
            )
            turn.slices_failed.append(obligation.reporting_obligation_id)
            self._note_escalation(obligation, turn, now=now)
            return None

        manifest = self._verified_manifest(result)
        self._validate_manifest_currency(obligation, manifest)
        rows = await self._read_rows(request, manifest)
        fingerprint = manifest.content_fingerprint.split(":", 1)[-1]
        current_snapshot = self._current_snapshot(revisions)
        if (
            track_settling
            and finality == "snapshot"
            and current_snapshot is not None
            and current_snapshot.source_manifest_sha256 == fingerprint
        ):
            assert checkpoint_store is not None
            await self._record_restatement_checkpoint(
                checkpoint_store,
                obligation,
                manifest,
                checked_at=now,
                next_observation=observation + 1,
            )
            return None

        # ``now`` freezes dispatch/lease/cutoff decisions, not publication.
        # A conforming source can observe finality while acquisition is running.
        published_at = self._clock()
        if _utc(published_at) < _utc(now):
            raise LedgerConflictError(
                "PUBLICATION_TIME_INVALID",
                "producer clock regressed during acquisition; correct the clock before retrying",
            )
        committed = await self.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=rows,
            finality=finality,
            now=published_at,
            turn=turn,
        )
        if checkpoint_store is not None:
            await self._record_restatement_checkpoint(
                checkpoint_store,
                obligation,
                manifest,
                checked_at=now,
                next_observation=observation + 1,
            )
        return committed

    def _restatement_store(self) -> RestatementCheckpointStore:
        if not isinstance(self._store, RestatementCheckpointStore):
            raise LedgerConflictError(
                "RESTATEMENT_CHECKPOINTS_NOT_SUPPORTED",
                "a source settling window requires a ledger store with durable restatement "
                "checkpoints",
            )
        return self._store

    async def _record_restatement_checkpoint(
        self,
        store: RestatementCheckpointStore,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        *,
        checked_at: datetime,
        next_observation: int,
    ) -> None:
        await store.record_restatement_checkpoint(
            RestatementCheckpoint(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                checked_at=max(_utc(checked_at), _utc(manifest.acquired_at)),
                next_observation=next_observation,
                provisional_until=manifest.finality_evidence.provisional_until,
            )
        )

    def _note_escalation(
        self, obligation: ReportingObligationRecord, turn: WorkerTurn, *, now: datetime
    ) -> None:
        """Record that this obligation has run out of automated recovery.

        The health projection derives ``delayed`` versus ``action_required``
        from the clock, so the worker does not set a state -- it only surfaces
        that the boundary has passed so a supervisor can alert instead of
        letting a dead feed idle inside a retry loop forever.
        """
        if _utc(now) >= _utc(obligation.automated_recovery_deadline_at):
            turn.escalated.append(obligation.reporting_obligation_id)

    def _verified_manifest(self, result: ReportingSourceExecutorResult) -> SourceBatchManifestV1:
        response = result.response
        manifest_bytes = result.manifest_bytes
        assert response is not None and manifest_bytes is not None
        return parse_verified_source_batch_manifest_v1(response.manifest, manifest_bytes)

    async def _read_rows(
        self, request: ReportingSourceSliceRequestV1, manifest: SourceBatchManifestV1
    ) -> list[dict[str, Any]]:
        """Materialize the manifest's staged rows into the revision's frozen rows."""
        if self._object_reader is None:
            return []
        import json

        rows: list[dict[str, Any]] = []
        cancel = asyncio.Event()
        for staged in manifest.objects:
            payload = await self._object_reader.read(
                object_ref=staged.object_ref,
                object_generation=staged.object_generation,
                account_id=request.identity.account_id,
                source_scope=request.identity.source_scope,
                cancel=cancel,
            )
            if hashlib.sha256(payload).hexdigest() != staged.sha256:
                raise LedgerConflictError(
                    "STAGED_OBJECT_MISMATCH",
                    f"staged object {staged.ordinal} does not match its manifest digest",
                )
            for line in payload.decode("utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    async def commit_revision_from_manifest(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        *,
        rows: Sequence[dict[str, Any]],
        finality: str,
        now: datetime | None = None,
        turn: WorkerTurn | None = None,
    ) -> ReportingRevisionRecord:
        """Project a verified manifest into an immutable ledger revision.

        A snapshot restatement supersedes the current snapshot leaf; there is no
        edit path.  An official close is terminal, so a later source correction
        must arrive as an adjustment instead. ``now`` is a trusted publication
        instant, unlike the dispatch instant accepted by ``acquire_obligation``.
        Replaying a publication retains its original creation time and parent.
        """
        obligation = await self._stored_obligation(obligation)
        self._validate_manifest_currency(obligation, manifest)
        now = now or self._clock()
        turn = turn or WorkerTurn()
        existing = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        selection = select_reporting_revision(
            existing,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality="snapshot",
        )
        # A retained official close coexists with the snapshot chain and wins
        # whole-history selection outright, so it is never the snapshot leaf.
        # Reading it as one would root a restatement at ``None`` and split the
        # obligation into two snapshot roots -- a permanently corrupt history
        # over immutable rows, with no repair path.
        leaf = select_reporting_revision(
            tuple(item for item in existing if item.finality == "snapshot"),
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality="snapshot",
        )
        if selection.kind == "corrupt" or leaf.kind == "corrupt":
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "the revision history requires repair")
        supersedes = (
            leaf.revision.reporting_revision_id
            if finality == "snapshot" and leaf.kind == "selected"
            else None
        )

        control_totals = tuple((total.name, total.value) for total in manifest.control_totals)
        revision_id = f"rpr_{manifest.publication_id[4:44]}"
        prior = next((item for item in existing if item.reporting_revision_id == revision_id), None)
        created_at = prior.created_at if prior is not None else now
        if prior is not None:
            # Still reconstruct and verify the supplied content below. Merely
            # finding the ID must not bypass immutable-content validation.
            supersedes = prior.supersedes_reporting_revision_id
        if (
            _utc(manifest.acquired_at) > _utc(now)
            or _utc(manifest.observed_at) > _utc(created_at)
            or _utc(manifest.finality_evidence.observed_at) > _utc(created_at)
            or (
                finality == "official"
                and _utc(manifest.finality_evidence.observed_at) < _utc(obligation.period.end)
            )
        ):
            raise LedgerConflictError(
                "PUBLICATION_TIME_INVALID",
                "source observation or finality is outside the publication time bounds; "
                "check source evidence and the producer clock before retrying",
            )
        revision = ReportingRevisionRecord(
            reporting_revision_id=revision_id,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            finality="official" if finality == "official" else "snapshot",
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision_id,
                row_count=manifest.row_count,
                control_totals=control_totals,
                reporting_rows=rows,
            ),
            row_count=manifest.row_count,
            control_totals=control_totals,
            observed_at=manifest.observed_at,
            data_through=manifest.data_through,
            created_at=created_at,
            supersedes_reporting_revision_id=supersedes,
            finality_basis=(
                (
                    "stabilized"
                    if manifest.finality_evidence.basis == "elapsed_settlement_window"
                    else "source_final"
                )
                if finality == "official"
                else None
            ),
            finality_policy_id=(
                f"{obligation.report_definition_id}:{manifest.offering_id}"
                if finality == "official"
                else None
            ),
            finalized_at=manifest.finality_evidence.observed_at if finality == "official" else None,
            source_publication_id=manifest.publication_id,
            source_manifest_sha256=manifest.content_fingerprint.split(":", 1)[-1],
        )
        if self._revision_verifier is not None:
            from adcp.reporting.materializer.publication import verified_publication

            revision = verified_publication(self._revision_verifier, obligation, revision, rows)
        committed = await self._store.commit_revision(revision, rows)
        turn.revisions_committed.append(committed.reporting_revision_id)
        return committed

    async def _stored_obligation(
        self, obligation: ReportingObligationRecord
    ) -> ReportingObligationRecord:
        """Always use the durable winner, including for public low-level calls."""
        stored = await self._store.get_obligation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        if stored is None:
            raise LedgerConflictError(
                "OBLIGATION_NOT_FOUND", "commit the obligation before source work"
            )
        if stored.generation_key != obligation.generation_key:
            raise LedgerConflictError(
                "CONFIGURATION_GENERATION_MISMATCH", "the obligation's retained generation differs"
            )
        return stored

    @staticmethod
    def _validate_manifest_currency(
        obligation: ReportingObligationRecord, manifest: SourceBatchManifestV1
    ) -> None:
        currency = require_frozen_currency(obligation.currency)
        if manifest.currency != currency:
            raise ReportingCurrencyError(
                "CURRENCY_MISMATCH", "source manifest currency disagrees with the frozen obligation"
            )
        identity = manifest.identity
        if (
            identity.account_id != obligation.account_id
            or identity.reporting_obligation_id != obligation.reporting_obligation_id
            or identity.delivery_config_id != obligation.delivery_config_id
            or identity.delivery_config_version != obligation.delivery_config_version
            or identity.report_definition_id != obligation.report_definition_id
        ):
            raise LedgerConflictError("MANIFEST_MISMATCH", "manifest does not bind this obligation")
        definition = obligation.definition
        units = {"spend": currency}
        ReportingProducer._validate_definition_binding(obligation, manifest.contract)
        if definition is not None:
            units.update(definition.monetary_metric_units)
            units.update(definition.monetary_control_total_units)
        for total in manifest.control_totals:
            expected = units.get(total.name)
            if expected is not None and total.unit is not None and total.unit != expected:
                raise ReportingCurrencyError(
                    "CURRENCY_MISMATCH",
                    "source control total unit disagrees with the frozen currency",
                )

    @staticmethod
    def _validate_definition_binding(
        obligation: ReportingObligationRecord, contract: ReportingContractIdentityV1
    ) -> None:
        definition = obligation.definition
        if definition is not None and (
            contract.report_definition_id != obligation.report_definition_id
            or contract.reporting_profile != obligation.reporting_profile
            or any(getattr(contract, name) != value for name, value in definition.to_wire().items())
        ):
            raise LedgerConflictError(
                "REPORT_DEFINITION_MISMATCH",
                "the source definition differs from the obligation's pin",
            )

    @staticmethod
    def _current_snapshot_leaf(revisions: Sequence[ReportingRevisionRecord]) -> str | None:
        current = ReportingProducer._current_snapshot(revisions)
        return current.reporting_revision_id if current is not None else None

    @staticmethod
    def _current_snapshot(
        revisions: Sequence[ReportingRevisionRecord],
    ) -> ReportingRevisionRecord | None:
        snapshots = [item for item in revisions if item.finality == "snapshot"]
        if not snapshots:
            return None
        superseded = {
            item.supersedes_reporting_revision_id
            for item in snapshots
            if item.supersedes_reporting_revision_id
        }
        leaves = [item for item in snapshots if item.reporting_revision_id not in superseded]
        if not leaves:
            return None
        return max(leaves, key=lambda item: (_utc(item.created_at), item.reporting_revision_id))

    # -- slice construction ----------------------------------------------

    def _build_slice(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        offering_id: str,
        *,
        finality: str | None = None,
        now: datetime,
        observation: int = 0,
        constituents: tuple[ReportingConstituent, ...] | None = None,
    ) -> ReportingSourceSliceRequestV1:
        """Freeze one slice request from the obligation.

        ``source_execution_key`` is derived from the obligation, the offering,
        and the **observation ordinal**. Without a settling policy the ordinal
        is the number of revisions already committed. With one it comes from a
        durable checkpoint that also advances after an unchanged source read.

        That last term is what makes both behaviors correct at once. A *retry*
        of a failed acquisition commits nothing, so the ordinal is unchanged,
        the key is unchanged, and the source replays its sealed publication
        rather than minting a second one. A *restatement* follows a committed
        revision (or a successful unchanged check), so the ordinal advances
        and the source is genuinely re-read as a new immutable observation.
        The ordinal comes from durable state, not from the clock, so neither
        behavior depends on wall time.
        """
        offering = self._source.capabilities.offering(offering_id)
        resolved_constituents: list[ReportingConstituent] = (
            list(constituents)
            if constituents is not None
            else [
                MediaBuyConstituentV1(
                    constituent_id=media_buy_id,
                    product_id=obligation.report_definition_id,
                    media_buy_id=media_buy_id,
                )
                for media_buy_id in obligation.media_buy_ids
            ]
        )
        if not resolved_constituents:
            raise LedgerConflictError(
                "EMPTY_DENOMINATOR",
                "an obligation with no media buys has no source work; it is a platform-owned "
                "no-op, not a slice",
            )
        source_execution_key = (
            "rse-"
            + hashlib.sha256(
                canonical_json_utf8_v1(
                    [obligation.reporting_obligation_id, offering_id, observation]
                )
            ).hexdigest()[:40]
        )
        resolved_finality = finality or obligation.required_finality
        publication_class: ReportingPublicationClass = (
            "AUTHORITATIVE" if resolved_finality == "official" else "PROVISIONAL_SNAPSHOT"
        )
        return ReportingSourceSliceRequestV1(
            identity=ReportingSourceIdentityV1(
                account_id=obligation.account_id,
                delivery_config_id=obligation.delivery_config_id,
                delivery_config_version=obligation.delivery_config_version,
                report_definition_id=obligation.report_definition_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                period_key=obligation.period.period_key,
                source_execution_key=source_execution_key,
                run_id=f"run-{_utc(now).strftime('%Y%m%dT%H%M%S')}",
                logical_slice_fingerprint=_logical_slice_fingerprint(obligation, offering_id),
                source_scope=dict(self._offerings.source_scope),
            ),
            adapter_build=self._source.capabilities.adapter_build,
            offering_id=offering_id,
            publication_namespace=self._offerings.publication_namespace,
            publication_class=publication_class,
            contract=offering.contract,
            period=ReportingSourcePeriodV1(
                period_key=obligation.period.period_key,
                source_local_date=_source_local_date(
                    obligation.period.start, obligation.period.source_timezone
                ),
                start=obligation.period.start,
                end=obligation.period.end,
                source_timezone=obligation.period.source_timezone,
                # The read cutoff is "as late as the source could possibly know
                # about", floored at the period end: a closed period is always
                # readable through its own boundary, and reading past *now*
                # would ask the source about a future it cannot answer for.
                source_read_cutoff_at=max(_utc(now), _utc(obligation.period.end)),
                grain=self._source.capabilities.offering(offering_id).grain,
                windowing=self._source.capabilities.offering(offering_id).windowing,
            ),
            revision_kind="authoritative" if publication_class == "AUTHORITATIVE" else "snapshot",
            trigger="scheduled_poll",
            coverage=ReportingSourceCoverageRequestV1(
                expected="full",
                constituents=resolved_constituents,
                denominator_fingerprint=coverage_denominator_fingerprint_v1(resolved_constituents),
            ),
            requested_metrics=list(self._offerings.requested_metrics),
            requested_dimensions=list(self._offerings.requested_dimensions),
            currency=require_frozen_currency(obligation.currency),
            deadline_at=_utc(now) + self._offerings.slice_timeout,
        )


def _logical_slice_fingerprint(obligation: ReportingObligationRecord, offering_id: str) -> str:
    from adcp.reporting.canonical_json import reporting_fingerprint_v1

    return reporting_fingerprint_v1(
        {
            "account_id": obligation.account_id,
            "delivery_config_id": obligation.delivery_config_id,
            "delivery_config_version": obligation.delivery_config_version,
            "report_definition_id": obligation.report_definition_id,
            "offering_id": offering_id,
            "period_start": _utc(obligation.period.start).isoformat(),
            "period_end": _utc(obligation.period.end).isoformat(),
            "media_buy_ids": sorted(obligation.media_buy_ids),
            "currency": require_frozen_currency(obligation.currency),
        }
    )


def _source_local_date(instant: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    return instant.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")
