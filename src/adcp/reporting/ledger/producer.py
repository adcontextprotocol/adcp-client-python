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
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingRevisionRecord,
    derive_period,
)
from adcp.reporting.ledger.store import (
    LeasedConfiguration,
    LedgerConflictError,
    ReportingLedgerStore,
)
from adcp.reporting.source import (
    MediaBuyConstituentV1,
    ReportingConstituent,
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
    parse_verified_source_batch_manifest_v1,
)

__all__ = [
    "ProducerOfferings",
    "ReportingProducer",
    "WorkerTurn",
    "revision_content_sha256",
]

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def revision_content_sha256(
    *,
    reporting_revision_id: str,
    row_count: int,
    control_totals: Sequence[tuple[str, str]],
    reporting_rows: Sequence[dict[str, Any]],
) -> str:
    """The Core revision binding: JCS over the four bound fields, SHA-256.

    This is the digest ``get_media_buy_delivery`` echoes in
    ``reporting_revision_binding`` and the one a consumer independently
    recomputes from what it actually read.  It binds exactly
    ``{reporting_revision_id, row_count, control_totals, reporting_rows}`` --
    nothing about storage, materialization, or delivery, which is what keeps
    Core's digest distinct from the Managed Delivery canonicalization contract.
    """
    return hashlib.sha256(
        canonical_json_utf8_v1(
            {
                "reporting_revision_id": reporting_revision_id,
                "row_count": row_count,
                "control_totals": [
                    {"name": name, "value": value} for name, value in control_totals
                ],
                "reporting_rows": [dict(row) for row in reporting_rows],
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class ProducerOfferings:
    """Which source offering serves which finality for one configuration.

    A snapshot offering and an official offering are different obligations even
    over the same window, so the producer needs both named explicitly rather
    than inferring one from the other.
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


class ReportingProducer:
    """Closes periods, drives the source, and commits immutable revisions."""

    def __init__(
        self,
        *,
        source: ReportingSourceExecutor,
        offerings: ProducerOfferings,
        store: ReportingLedgerStore,
        object_reader: ReportingSourceStagedObjectReader | None = None,
        worker_id: str = "reporting-producer",
        lease_seconds: float = 60.0,
        max_periods_per_turn: int = 64,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._offerings = offerings
        self._store = store
        self._object_reader = object_reader
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_periods_per_turn = max_periods_per_turn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def store(self) -> ReportingLedgerStore:
        return self._store

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
                    if candidate.delivery_config_version == leased.delivery_config_version
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
        committed: list[ReportingObligationRecord] = []
        for boundary in self._elapsed_periods(configuration, now=now):
            existing = await self._store.find_obligation(
                account_id=configuration.account_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                period_start=boundary.start,
                period_end=boundary.end,
            )
            if existing is not None:
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
                created_at=now,
            )
            stored = await self._store.commit_obligation(obligation)
            committed.append(stored)
            turn.obligations_committed.append(stored.reporting_obligation_id)
        return committed

    def _elapsed_periods(
        self, configuration: ReportingConfiguration, *, now: datetime
    ) -> list[ReportingPeriodBoundary]:
        """Every eligible period that has closed but is not yet obligated.

        A period is eligible once its *end* is at or before now.  A snapshot
        taken exactly at the boundary does not expose it -- the obligation
        appears in the first snapshot strictly after it, which is the rule both
        sides derive independently.
        """
        from adcp.reporting.ledger.models import first_ordinal_after

        activated_at = configuration.activated_at
        if activated_at is None:
            return []
        ordinal = first_ordinal_after(
            configuration.schedule,
            account_timezone=configuration.account_timezone,
            activated_at=activated_at,
        )
        boundaries: list[ReportingPeriodBoundary] = []
        for _ in range(self._max_periods_per_turn):
            boundary = derive_period(
                configuration.schedule,
                account_timezone=configuration.account_timezone,
                ordinal=ordinal,
            )
            if _utc(boundary.end) > _utc(now):
                break
            if configuration.deactivated_at is not None and _utc(boundary.start) >= _utc(
                configuration.deactivated_at
            ):
                # Deactivation still owes a period that already started, but
                # not one that had not begun when the configuration stopped.
                break
            boundaries.append(boundary)
            ordinal += 1
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
            await self.acquire_obligation(configuration, obligation, turn=turn, now=now)

    async def acquire_obligation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        turn: WorkerTurn | None = None,
        now: datetime | None = None,
    ) -> ReportingRevisionRecord | None:
        """Drive one obligation from its source and commit what comes back.

        Returns the committed revision, or ``None`` when the source is not ready
        or the obligation is already satisfied.
        """
        turn = turn or WorkerTurn()
        now = now or self._clock()
        revisions = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        if any(
            item.finality == "official"
            or (obligation.required_finality == "snapshot" and item.readable)
            for item in revisions
        ):
            # Officials are terminal; a satisfied snapshot obligation restates
            # only when the caller asks for it explicitly.
            if obligation.required_finality == "snapshot" or any(
                item.finality == "official" for item in revisions
            ):
                return None

        finality = obligation.required_finality
        offering_id = self._offerings.offering_for(finality)
        if offering_id is None:
            raise LedgerConflictError(
                "NO_OFFERING_FOR_FINALITY",
                f"this producer declares no source offering for {finality} reporting",
            )

        request = self._build_slice(configuration, obligation, offering_id, now=now)
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
        return await self.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=await self._read_rows(request, manifest),
            finality=finality,
            now=now,
            turn=turn,
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
        must arrive as an adjustment instead.
        """
        now = now or self._clock()
        turn = turn or WorkerTurn()
        existing = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        supersedes = None
        if finality == "snapshot":
            supersedes = self._current_snapshot_leaf(existing)

        control_totals = tuple((total.name, total.value) for total in manifest.control_totals)
        revision_id = f"rpr_{manifest.publication_id[4:44]}"
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
            created_at=now,
            supersedes_reporting_revision_id=supersedes,
            finality_basis="source_final" if finality == "official" else None,
            finality_policy_id=(
                f"{obligation.report_definition_id}:{manifest.offering_id}"
                if finality == "official"
                else None
            ),
            finalized_at=manifest.finality_evidence.observed_at if finality == "official" else None,
            source_publication_id=manifest.publication_id,
            source_manifest_sha256=manifest.content_fingerprint.split(":", 1)[-1],
        )
        committed = await self._store.commit_revision(revision, rows)
        turn.revisions_committed.append(committed.reporting_revision_id)
        return committed

    @staticmethod
    def _current_snapshot_leaf(revisions: Sequence[ReportingRevisionRecord]) -> str | None:
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
        return max(
            leaves, key=lambda item: (_utc(item.created_at), item.reporting_revision_id)
        ).reporting_revision_id

    # -- slice construction ----------------------------------------------

    def _build_slice(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        offering_id: str,
        *,
        now: datetime,
    ) -> ReportingSourceSliceRequestV1:
        """Freeze one slice request from the obligation.

        ``source_execution_key`` is derived from the obligation plus the
        offering, so a retried acquisition of the same obligation replays the
        same sealed publication rather than minting a second one.
        """
        constituents: list[ReportingConstituent] = [
            MediaBuyConstituentV1(
                constituent_id=media_buy_id,
                product_id=configuration.report_definition_id,
                media_buy_id=media_buy_id,
            )
            for media_buy_id in obligation.media_buy_ids
        ]
        if not constituents:
            raise LedgerConflictError(
                "EMPTY_DENOMINATOR",
                "an obligation with no media buys has no source work; it is a platform-owned "
                "no-op, not a slice",
            )
        source_execution_key = (
            "rse-"
            + hashlib.sha256(
                canonical_json_utf8_v1([obligation.reporting_obligation_id, offering_id])
            ).hexdigest()[:40]
        )
        publication_class: ReportingPublicationClass = (
            "AUTHORITATIVE"
            if obligation.required_finality == "official"
            else "PROVISIONAL_SNAPSHOT"
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
            contract=self._source.capabilities.offering(offering_id).contract,
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
                constituents=constituents,
                denominator_fingerprint=coverage_denominator_fingerprint_v1(constituents),
            ),
            requested_metrics=list(self._offerings.requested_metrics),
            requested_dimensions=list(self._offerings.requested_dimensions),
            currency=self._offerings.currency,
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
        }
    )


def _source_local_date(instant: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    return instant.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d")
