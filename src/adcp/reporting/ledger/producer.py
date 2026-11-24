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
import gzip
import hashlib
import inspect
import json
import logging
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, TypeAlias
from uuid import uuid4
from zoneinfo import ZoneInfo

import rfc8785

from adcp.reporting._settlement import cancel_and_settle
from adcp.reporting._source_authorization import (
    bind_inline_publication,
    require_account_work,
    source_revoked,
    source_turn,
)
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.currency import (
    ReportingCurrencyError,
    require_frozen_currency,
    validate_currency,
)
from adcp.reporting.evidence import ReportingControlTotalRecord, freeze_control_totals
from adcp.reporting.ledger.coverage import _covered_subset_publishable, build_reporting_coverage
from adcp.reporting.ledger.coverage_guard import (
    ReportingRowCoverageError,
    _package_scope_conflict,
    _require_rows_within_frozen_coverage,
)
from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
from adcp.reporting.ledger.finality import (
    MAX_STABILIZATION_HISTORY_OBSERVATIONS,
    FinalityProof,
    ReportingContractualCutoffPolicy,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
    consecutive_stabilization_slots,
    contractual_cutoff_at,
    finality_policy_source_class,
    manifest_finality_proof,
    prove_stabilized_finality,
    stabilization_history_limit,
    stabilization_interval_elapsed,
)
from adcp.reporting.ledger.media_buy_scope import (
    CompleteMediaBuyScope,
    MediaBuyScopeResolver,
    MissingHistoricalMediaBuyScope,
)
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDeliveryEscalation,
    ReportingObligationRecord,
    ReportingPeriodBoundary,
    ReportingRevisionRecord,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.package_roster import (
    CompletePackageRoster,
    MissingHistoricalPackageRoster,
    PackageRosterResolver,
    PackageRosterResult,
    PerMediaBuyPackageRoster,
    UnsupportedMediaBuyRoster,
    _resolve_roster_until,
)
from adcp.reporting.ledger.provisional import (
    PendingProvisionalAcquisitionStore,
    ProvisionalAcquisition,
    ProvisionalObservation,
    ProvisionalObservationPublicationStore,
    ProvisionalObservationStore,
    ProvisionalPolicy,
    SnapshotOriginStore,
    StabilizationScheduleStore,
    StabilizationStore,
)
from adcp.reporting.ledger.store import (
    LeasedConfiguration,
    LedgerConflictError,
    ReportingLedgerStore,
    ReportingRowStagingStore,
    ReportingSourcePublicationStore,
    ReportingWorkerRetryPolicy,
    RestatementCheckpoint,
    RestatementCheckpointStore,
    RetryScheduleEntry,
    RetryScheduleStore,
    managed_revision_metadata,
    validate_retained_provisional_observation,
    validate_snapshot_observation_binding,
    validate_snapshot_origin,
    validate_snapshot_rows,
)
from adcp.reporting.revision_selection import select_reporting_revision
from adcp.reporting.source import (
    AuthoritativeOfferingV1,
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
    ReportingTriggerKind,
    SourceBatchManifestV1,
    SourceBatchObjectV1,
    _validate_metric_applicability,
    coverage_denominator_fingerprint_v1,
    iso_duration_milliseconds_v1,
    parse_verified_source_batch_manifest_v1,
)

if TYPE_CHECKING:
    from adcp.reporting._source_authorization import InlineSealPublisher
    from adcp.reporting.inline_source import ReportingSealStore
    from adcp.reporting.inline_storage import _Code as InlineStorageErrorCode
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


@dataclass(frozen=True)
class _InlineStorageFailure:
    code: InlineStorageErrorCode


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


def _source_run_id(cutoff: datetime) -> str:
    return f"run-{_utc(cutoff).strftime('%Y%m%dT%H%M%S')}"


def _supports_official_source_formats(
    offering: ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1,
) -> bool:
    """An official source may emit any encoding it declares for the selected offering."""
    return all(
        item.media_type in {"application/json", "application/x-ndjson"}
        and item.compression in {"none", "gzip"}
        for item in offering.formats
    )


def _staged_object_rows(staged: SourceBatchObjectV1, payload: bytes) -> list[dict[str, Any]]:
    try:
        if staged.compression == "gzip":
            payload = gzip.decompress(payload)
        if staged.media_type == "application/json":
            document = json.loads(payload)
            rows = document if isinstance(document, list) else [document]
        else:
            rows = [
                json.loads(line) for line in payload.decode("utf-8").splitlines() if line.strip()
            ]
    except (ValueError, OSError, EOFError, zlib.error) as error:
        raise LedgerConflictError(
            "STAGED_OBJECT_MISMATCH", "staged object does not decode with its declared format"
        ) from error
    if len(rows) != staged.row_count or not all(isinstance(row, dict) for row in rows):
        raise LedgerConflictError(
            "STAGED_OBJECT_MISMATCH", "staged object does not contain the declared reporting rows"
        )
    return rows


def _source_finality(obligation: ReportingObligationRecord, output_finality: str) -> str:
    policy = obligation.definition.finality_policy if obligation.definition else None
    if output_finality == "official" and policy is not None:
        return finality_policy_source_class(policy)
    return output_finality


def snapshot_restatement_cadence(
    offering: ProvisionalSnapshotOfferingV1, period_duration: str
) -> timedelta:
    if offering.restatement_cadence is not None:
        return timedelta(milliseconds=iso_duration_milliseconds_v1(offering.restatement_cadence))
    return max(
        iso_duration_to_timedelta(period_duration),
        timedelta(milliseconds=iso_duration_milliseconds_v1(offering.fastest_safe_cadence)),
    )


@dataclass(frozen=True)
class _StabilizedPublication:
    proof: FinalityProof
    manifest: SourceBatchManifestV1
    rows: Sequence[dict[str, Any]]
    history: tuple[ProvisionalObservation, ...]
    snapshots: Mapping[str, ReportingRevisionRecord]


@dataclass
class _FirstCoreDelivery:
    """The first snapshot remains one claim through dispatch and retry bookkeeping."""

    state: Literal["dispatching", "retry", "consumed"]
    retry: RetryScheduleEntry | None = None
    request: ReportingSourceSliceRequestV1 | None = None

    def interrupted(self) -> None:
        if self.state == "dispatching":
            self.state = "retry"


def _implements_store_method(store: object, stub: Callable[..., Any]) -> bool:
    name = stub.__name__
    if inspect.getattr_static(store, name, None) is None:
        return False
    method = getattr(store, name, None)
    return callable(method) and getattr(method, "__func__", method) is not stub


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
    # RFC 8785 exactly, not the restricted evidence profile: a reader must
    # accept any JCS number in a stored row, so a row a peer SDK wrote with a
    # fractional or exponent number still verifies. The two encodings are
    # byte-identical for every value the profile accepts.
    return hashlib.sha256(
        rfc8785.dumps(
            {
                "reporting_revision_id": reporting_revision_id,
                "row_count": row_count,
                "control_totals": totals,
                "reporting_rows": [dict(row) for row in reporting_rows],
            }
        )
    ).hexdigest()


def _constituent_keys(
    constituents: Sequence[ReportingConstituent],
) -> list[tuple[str, str | None, str | None]]:
    return [
        (
            item.constituent_id,
            getattr(item, "media_buy_id", None),
            getattr(item, "product_id", None),
        )
        for item in constituents
    ]


def subscription_rows(
    obligation: ReportingObligationRecord, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every row of a reporting-subscription revision carries its ``account_id``.

    AdCP 3.3: "Every delivered row carries account_id", so a principal reading
    one subscription's feed across accounts can attribute each row. The
    producer stamps it before the revision is bound, so the Core revision
    digest (``revision_content_sha256``) and, for managed delivery, the
    canonical content digest both cover it. A row that already names an
    account must name this one. The report definition's row schema must admit
    an ``account_id`` string; otherwise managed publication fails closed with
    ``SOURCE_INVALID``. Account-level obligations are returned unchanged, so
    their rows and digests are byte-for-byte what they were.
    """
    if obligation.offering_id is None:
        return list(rows)
    stamped = []
    for row in rows:
        named = row.get("account_id", obligation.account_id)
        if named != obligation.account_id:
            raise LedgerConflictError(
                "ROW_ACCOUNT_MISMATCH",
                "a reporting-subscription row names another account",
            )
        stamped.append({**row, "account_id": obligation.account_id})
    return stamped


RevisionBindingAlgorithm = Literal["rfc8785_jcs_v1", "legacy_py_core_pairs_v0"]


def revision_binding_algorithm(revision: ReportingRevisionRecord) -> RevisionBindingAlgorithm:
    """Which encoding of ``control_totals`` the revision's content digest binds.

    ``rfc8785_jcs_v1`` is the protocol shape: ``{name, value, value_type[, unit]}``
    members, which a buyer can recompute from the wire. Core releases before
    protocol-shape totals hashed ``{name, value}`` pairs while serving typed
    totals, so a buyer could not recompute the digest whenever totals were
    non-empty. Those retained revisions are never rehashed; they report
    ``legacy_py_core_pairs_v0``, an import-only algorithm that is never written.
    Revisions without totals are byte-identical under both encodings.
    """
    if revision.managed_control_totals is not None or not revision.control_totals:
        return "rfc8785_jcs_v1"
    return "legacy_py_core_pairs_v0"


def recompute_revision_content_sha256(
    revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
) -> str:
    """Recompute a retained revision's binding with the algorithm it was written under.

    Use this to verify what was read against ``revision.revision_content_sha256``:
    protocol-shape revisions bind their typed totals, retained pair-hashed
    revisions bind ``{name, value}`` pairs, and neither is rehashed.
    """
    return revision_content_sha256(
        reporting_revision_id=revision.reporting_revision_id,
        row_count=revision.row_count,
        control_totals=revision.control_totals,
        reporting_rows=rows,
        control_total_evidence=revision.managed_control_totals,
    )


def _typed_control_totals(
    obligation: ReportingObligationRecord, manifest: SourceBatchManifestV1
) -> tuple[ReportingControlTotalRecord, ...]:
    """Protocol-shape totals for a new revision.

    The declared ``value_type`` and ``unit`` come from the source manifest.
    A monetary total with no declared unit takes the obligation's frozen unit,
    the same projection the legacy read path applied, so the served and the
    hashed totals agree.
    """
    units: dict[str, str] = {}
    if obligation.currency is not None:
        units["spend"] = obligation.currency
    if obligation.definition is not None:
        units.update(dict(obligation.definition.monetary_control_total_units))
    try:
        totals = tuple(
            ReportingControlTotalRecord(
                total.name, total.value, total.value_type, total.unit or units.get(total.name)
            )
            for total in manifest.control_totals
        )
        # Closed evidence: names are unique and agree with the retained pairs. A
        # manifest that repeats a name is a source defect, not a worker crash.
        return freeze_control_totals(
            totals, tuple((total.name, total.value) for total in manifest.control_totals)
        )
    except ValueError:
        raise LedgerConflictError(
            "MANIFEST_MISMATCH", "source control totals are not valid protocol totals"
        ) from None


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
    snapshot_requests_official_close: bool = False

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
    earliest_retry_at: datetime | None = None
    _retry_keys_by_obligation: dict[str, tuple[str, str, str]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _retry_entries: dict[str, RetryScheduleEntry] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def did_work(self) -> bool:
        return bool(
            self.obligations_committed
            or self.revisions_committed
            or self.slices_failed
            or self.escalated
        )


def _withheld(configuration: ReportingConfiguration, obligation: ReportingObligationRecord) -> bool:
    """An account obligation with a retained incomplete partition publishes nothing.

    Unless its generation is ``allow_partial`` and the period is publishable
    over its fully covered buys (:func:`_covered_subset_publishable`). The
    partition is retained for per-buy roster evidence, for all_media_buys
    generations and for ``allow_partial`` generations; a status-only obligation
    (a whole-configuration roster on an exact ``full`` scope) and a
    subscription's owed set publish as they always have.
    """
    return (
        not configuration.owed_set
        and obligation.coverage is not None
        and obligation.coverage_status != "full"
        and not _covered_subset_publishable(configuration.coverage_requirement, obligation.coverage)
    )


def _require_covered_rows(
    obligation: ReportingObligationRecord, rows: Sequence[dict[str, Any]]
) -> None:
    """An ``allow_partial`` revision's rows cover only its covered denominator.

    ``reporting-revision.json``: rows, ``row_count``, control totals and the
    content digest cover only ``coverage.covered_package_ids``. Every row must
    name a fully covered ``media_buy_id`` or a covered ``package_id``, and every
    buy or package it names must be covered; a row that names neither cannot be
    attributed to the covered subset. Called only for a period
    :func:`~adcp.reporting.ledger.coverage._covered_subset_publishable` admits.
    """
    coverage = obligation.coverage
    assert coverage is not None
    buys = frozenset(coverage.fully_covered_media_buy_ids)
    packages = frozenset(coverage.covered_package_ids)
    outside_buys: set[str] = set()
    outside_packages: set[str] = set()
    unattributed = 0
    for row in rows:
        buy = row.get("media_buy_id")
        package = row.get("package_id")
        if buy is None and package is None:
            unattributed += 1
        if buy is not None and buy not in buys:
            outside_buys.add(str(buy))
        if package is not None and package not in packages:
            outside_packages.add(str(package))
    if outside_buys or outside_packages or unattributed:
        raise ReportingRowCoverageError(
            obligation,
            media_buy_ids=outside_buys,
            package_ids=outside_packages,
            unattributed_rows=unattributed,
        )


def _zero_buy_snapshot(obligation: ReportingObligationRecord) -> bool:
    """A proven zero-buy period whose generation requires only snapshot reporting."""
    return (
        not obligation.media_buy_ids
        and obligation.coverage_status == "full"
        and obligation.required_finality == "snapshot"
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
        package_roster_resolver: PackageRosterResolver | None = None,
        media_buy_scope_resolver: MediaBuyScopeResolver | None = None,
        revision_verifier: ReportingRevisionVerifier | None = None,
        retry_initial_delay: timedelta = timedelta(minutes=1),
        retry_max_delay: timedelta = timedelta(minutes=30),
        post_deadline_retry_interval: timedelta = timedelta(hours=1),
        retry_store: RetryScheduleStore | None = None,
        read_jitter_window: timedelta = timedelta(minutes=5),
    ) -> None:
        if read_jitter_window < timedelta(0):
            raise ValueError("read jitter window must be nonnegative")
        if (
            retry_initial_delay <= timedelta(0)
            or retry_max_delay < retry_initial_delay
            or post_deadline_retry_interval <= timedelta(0)
        ):
            raise ValueError("retry delays must be positive and maximum must exceed initial")
        if retry_store is None:
            if not isinstance(store, RetryScheduleStore):
                raise TypeError(
                    "ReportingProducer requires a RetryScheduleStore to persist retry state"
                )
            retry_store = store
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
        self._package_roster_resolver = package_roster_resolver
        self._media_buy_scope_resolver = media_buy_scope_resolver
        self._retry_initial_delay = retry_initial_delay
        self._retry_max_delay = retry_max_delay
        self._post_deadline_retry_interval = post_deadline_retry_interval
        self._retry_store = retry_store
        self._read_jitter_window = read_jitter_window
        self._recovered_stabilization: dict[
            ReportingConfigurationGenerationKey, ProvisionalPolicy
        ] = {}
        self._first_core_deliveries: dict[
            tuple[ReportingConfigurationGenerationKey, str],
            Literal["ready", "attempted"] | _FirstCoreDelivery,
        ] = {}

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
        return _advertised_reporting_delivery(
            escalation=self._escalation,
            consumer_status_task=consumer_status_task,
            offerings=offerings,
            automated_recovery_window=automated_recovery_window,
            status_retention_days=status_retention_days,
            extra=extra,
        )

    # -- the worker turn -------------------------------------------------

    async def run_worker(self) -> WorkerTurn:
        """Run one leased turn. Safe to call from cron, a loop, or a supervisor.

        Returns immediately with an empty turn when nothing is leasable, so a
        caller can back off rather than spin. Standalone PostgreSQL turns retry
        deadlocks and lock timeouts at most twice, after rollback and fenced
        release. Each retry must acquire a new lease; other failures propagate.
        """
        retryable = (
            self._store.worker_lock_errors()
            if isinstance(self._store, ReportingWorkerRetryPolicy)
            else ()
        )
        for attempt in range(3):
            leased = None
            with source_turn():
                try:
                    lease_started_at = asyncio.get_running_loop().time()
                    now = self._clock()
                    leased = await self._store.lease_period_close(
                        worker_id=self._worker_id, now=now, lease_seconds=self._lease_seconds
                    )
                    turn = WorkerTurn(leased=leased)
                    if leased is None:
                        return turn
                    roster_deadline = self._roster_deadline(started_at=lease_started_at)
                    await self._work_leased_configuration(
                        leased, turn, now=now, roster_deadline=roster_deadline
                    )
                except retryable:
                    # Store transactions have already exited/rolled back. A
                    # committed lease is released only under its original fence;
                    # a competitor may win before the next acquisition.
                    if leased is not None:
                        await self._release_worker_lease(leased, retryable)
                    if attempt == 2:
                        raise
                except BaseException as original:
                    if leased is not None:
                        try:
                            await self._release_worker_lease(leased, retryable)
                        except retryable:
                            # Never turn an immutable-content conflict or
                            # cancellation into a retryable cleanup failure.
                            raise original from None
                    raise
                else:
                    # Exhausted release retries escape from here, rather than
                    # starting another turn or reporting a false empty success.
                    await self._release_worker_lease(leased, retryable)
                    return turn
            await asyncio.sleep(0.05 * (2**attempt))
        raise AssertionError("unreachable worker retry")  # pragma: no cover

    async def _release_worker_lease(
        self, leased: LeasedConfiguration, retryable: tuple[type[Exception], ...]
    ) -> None:
        for attempt in range(3):
            try:
                await self._store.release_period_close(leased, worker_id=self._worker_id)
                return
            except retryable:
                if attempt == 2:
                    raise
            await asyncio.sleep(0.05 * (2**attempt))

    def _roster_deadline(self, *, started_at: float | None = None) -> float:
        budget = max(0.0, self._lease_seconds * 0.9)
        return (asyncio.get_running_loop().time() if started_at is None else started_at) + budget

    async def _work_leased_configuration(
        self,
        leased: LeasedConfiguration,
        turn: WorkerTurn,
        *,
        now: datetime,
        roster_deadline: float,
    ) -> None:
        from adcp.reporting.production.contracts import _SourceAuthorizationRevokedError

        try:
            configuration = next(
                (
                    candidate
                    for candidate in await self._store.list_configurations(
                        caller=ReportingDeliveryPrincipal(leased.account_id, leased.consumer_id),
                        delivery_config_ids=[leased.delivery_config_id],
                    )
                    if candidate.generation_key == leased.generation_key
                ),
                None,
            )
            if configuration is None or configuration.quarantined:
                return
            require_account_work(configuration.account_id)
            await self._close_elapsed_periods(
                configuration, turn, now=now, roster_deadline=roster_deadline
            )
            await self._acquire_pending(configuration, turn, now=now)
        except _SourceAuthorizationRevokedError:
            # Keep pending acquisitions retryable. Authorization may be
            # restored on the next turn; it is not a history failure.
            return

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
        from adcp.reporting.production.contracts import _SourceAuthorizationRevokedError

        boundary = now or self._clock()
        turn = WorkerTurn()
        with source_turn():
            try:
                require_account_work(configuration.account_id)
                await self._close_elapsed_periods(
                    configuration, turn, now=boundary, roster_deadline=self._roster_deadline()
                )
                await self._acquire_pending(configuration, turn, now=boundary)
            except _SourceAuthorizationRevokedError:
                return turn
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
        if configuration.quarantined:
            raise LedgerConflictError(
                "REPORTING_GENERATION_QUARANTINED", "legacy generations cannot run"
            )
        turn = WorkerTurn()
        if self._stabilization_requested(configuration):
            self._stabilization_store()
        return await self._close_elapsed_periods(
            configuration,
            turn,
            now=now or self._clock(),
            roster_deadline=self._roster_deadline(),
            admit_first_core_delivery=True,
        )

    async def _close_elapsed_periods(
        self,
        configuration: ReportingConfiguration,
        turn: WorkerTurn,
        *,
        now: datetime,
        roster_deadline: float,
        admit_first_core_delivery: bool = False,
    ) -> list[ReportingObligationRecord]:
        from adcp.reporting.ledger.producer_progress import ReportingProducerProgress

        if configuration.owed_set:
            # A subscription's owed set is written by its planner, never filled
            # from a period grid.
            return []
        all_media_buys = configuration.media_buy_scope == "all_media_buys"
        if all_media_buys and self._media_buy_scope_resolver is None:
            raise LedgerConflictError(
                "UNSUPPORTED_FEATURE",
                "all_media_buys reporting requires a media buy scope resolver; "
                "ReliableReportingService wires it only with admit_all_media_buys=True",
            )
        if (
            "package" in self._offerings.requested_dimensions
            and not all_media_buys
            and not configuration.media_buy_ids
        ):
            raise LedgerConflictError(
                "HISTORY_UNAVAILABLE", "account package reporting requires an exact buy scope"
            )
        selected = configuration.definition.finality_policy if configuration.definition else None
        frozen = None
        if isinstance(selected, ReportingStabilizedPolicy):
            frozen = await self._recover_stabilization_schedule(configuration, required=False)
        progress = self._store if isinstance(self._store, ReportingProducerProgress) else None
        after = None if progress is None else await progress.producer_closed_through(configuration)
        committed: list[ReportingObligationRecord] = []
        for boundary in self._elapsed_periods(configuration, now=now, after=after):
            existing = await self._store.find_obligation(
                account_id=configuration.account_id,
                consumer_id=configuration.consumer_id,
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
            scoped = configuration
            if all_media_buys:
                assert self._media_buy_scope_resolver is not None
                scope = await _resolve_roster_until(
                    self._media_buy_scope_resolver,
                    configuration,
                    boundary,
                    deadline=roster_deadline,
                    what="media buy scope",
                )
                if isinstance(scope, MissingHistoricalMediaBuyScope):
                    # Membership at the cutoff is unproven: commit nothing and
                    # leave this and every later period open for a later turn.
                    self._note_unclosed_period(
                        configuration, boundary, turn, now=now, why="media buy scope unresolved"
                    )
                    break
                if not isinstance(scope, CompleteMediaBuyScope):
                    raise TypeError("media buy scope resolver returned an unsupported result")
                scoped = replace(configuration, media_buy_ids=scope.media_buy_ids)
            roster: PackageRosterResult | None = None
            # A proven zero-buy period owns no packages; there is nothing to roster.
            if "package" in self._offerings.requested_dimensions and (
                scoped.media_buy_ids or not all_media_buys
            ):
                if self._package_roster_resolver is not None:
                    roster = await _resolve_roster_until(
                        self._package_roster_resolver,
                        scoped,
                        boundary,
                        deadline=roster_deadline,
                    )
                else:
                    roster = MissingHistoricalPackageRoster()
                if not isinstance(
                    roster,
                    (
                        CompletePackageRoster,
                        MissingHistoricalPackageRoster,
                        UnsupportedMediaBuyRoster,
                        PerMediaBuyPackageRoster,
                    ),
                ):
                    raise TypeError("package roster resolver returned an unsupported result")
            try:
                coverage = build_reporting_coverage(scoped.media_buy_ids, roster, boundary.end)
            except ValueError:
                # The roster contradicts the frozen buys (other keys, or a
                # package both covered and unsupported). It is the resolver's
                # contract to fix; the period stays open like an unresolved one.
                self._note_unclosed_period(
                    configuration,
                    boundary,
                    turn,
                    now=now,
                    why="package roster resolver broke its contract",
                )
                break
            conflict = _package_scope_conflict(
                scoped.media_buy_ids,
                roster,
                package_grain="package" in self._offerings.requested_dimensions,
            )
            if conflict is not None:
                self._note_unclosed_period(configuration, boundary, turn, now=now, why=conflict)
                break
            if selected is None and self._official_close_requested(configuration):
                raise LedgerConflictError(
                    "FINALITY_POLICY_UNPROVEN", "official finality requires a selected policy"
                )
            if isinstance(selected, ReportingStabilizedPolicy) and self._stabilization_requested(
                configuration
            ):
                self._stabilization_store()
                offering_id = self._offerings.snapshot_offering_id
                offering = self._source.capabilities.offering(offering_id) if offering_id else None
                if not isinstance(offering, ProvisionalSnapshotOfferingV1):
                    raise LedgerConflictError(
                        "SNAPSHOT_OFFERING_REQUIRED",
                        "stabilized finality requires a snapshot source offering",
                    )
                stabilization_history_limit(
                    selected,
                    (
                        frozen.cadence
                        if frozen is not None
                        else snapshot_restatement_cadence(
                            offering, configuration.schedule.period_duration
                        )
                    ),
                )
            else:
                await self._check_official_publication_lock(configuration)
            obligation = ReportingObligationRecord(
                reporting_obligation_id=self._obligation_id(configuration, boundary),
                account_id=configuration.account_id,
                consumer_id=configuration.consumer_id,
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
                media_buy_ids=scoped.media_buy_ids,
                package_ids=coverage.package_ids,
                coverage_status=coverage.status,
                # A whole-configuration roster on an exact full scope keeps the
                # status-only coverage it always had. A per-buy roster, an
                # all_media_buys generation, or an allow_partial generation
                # (which publishes over it) retains its incomplete partition.
                coverage=(
                    coverage
                    if coverage.status != "full"
                    and (
                        all_media_buys
                        or configuration.coverage_requirement == "allow_partial"
                        or isinstance(roster, (PerMediaBuyPackageRoster, UnsupportedMediaBuyRoster))
                    )
                    else None
                ),
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
            if selected is not None and self._optional_publication_store() is None:
                if (
                    admit_first_core_delivery
                    and stored == obligation
                    and self._observation_checkpoint_store() is None
                ):
                    self._first_core_deliveries.setdefault(
                        (stored.generation_key, stored.reporting_obligation_id), "ready"
                    )
                if not _implements_store_method(
                    self._store, PendingProvisionalAcquisitionStore.get_provisional_acquisition
                ):
                    break
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
                    configuration.consumer_id,
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

        if configuration.owed_set:
            await self._acquire_owed_set(configuration, turn, now=now)
            return
        if isinstance(self._store, ReportingProducerProgress):
            await self._acquire_progress(self._store, configuration, turn, now=now)
            return
        refused: ReportingRowCoverageError | None = None
        for boundary in self._elapsed_periods(configuration, now=now):
            obligation = await self._store.find_obligation(
                account_id=configuration.account_id,
                consumer_id=configuration.consumer_id,
                delivery_config_id=configuration.delivery_config_id,
                delivery_config_version=configuration.delivery_config_version,
                period_start=boundary.start,
                period_end=boundary.end,
            )
            if obligation is None:
                continue
            if await self._close_without_source(configuration, obligation, turn, now=now):
                continue
            if not await self._automatic_read_ready(configuration, obligation, now=now):
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
            except ReportingRowCoverageError as error:
                refused = refused or error
                await self._park_rows_outside_coverage(error, obligation, turn, now=now)
            except (ReportingCurrencyError, LedgerConflictError) as error:
                if isinstance(error, LedgerConflictError) and error.code not in {
                    "OBSERVATION_CONFLICT",
                    "STABILIZATION_ORIGIN_UNAVAILABLE",
                    "STABILIZATION_ORIGIN_CONFLICT",
                }:
                    raise
                # Currency interpretation and competing observations are local
                # slice failures. Let later periods make progress; a later turn
                # can read the winning observation or retry the same reservation.
                logger.info(
                    "reporting slice failed obligation=%s code=%s",
                    obligation.reporting_obligation_id,
                    error.code,
                )
                turn.slices_failed.append(obligation.reporting_obligation_id)
                self._note_escalation(obligation, turn, now=now)
        if refused is not None:
            raise refused

    async def _acquire_owed_set(
        self, configuration: ReportingConfiguration, turn: WorkerTurn, *, now: datetime
    ) -> None:
        """Acquire a subscription's frozen obligations that have elapsed.

        The owed set is exactly the stored obligations; there is no grid to
        walk. Satisfied obligations are left alone by ``acquire_obligation``,
        and those with an official revision are skipped before dispatch.
        """
        from adcp.reporting.ledger.store import ReportingSubscriptionLedger

        if not isinstance(self._store, ReportingSubscriptionLedger):
            raise LedgerConflictError(
                "UNSUPPORTED_STORE", "this store cannot hold reporting-subscription owed sets"
            )
        worked = 0
        refused: ReportingRowCoverageError | None = None
        for obligation in await self._store.list_generation_obligations(
            configuration.generation_key
        ):
            if worked >= self._max_periods_per_turn or _utc(obligation.period.end) > _utc(now):
                break
            revisions = await self._store.list_revisions(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if any(item.finality == "official" for item in revisions):
                continue
            if await self._close_without_source(configuration, obligation, turn, now=now):
                continue
            if not await self._automatic_read_ready(configuration, obligation, now=now):
                continue
            worked += 1
            try:
                policy = self._settling_policy(configuration, obligation)
                if policy is None:
                    await self.acquire_obligation(configuration, obligation, turn=turn, now=now)
                else:
                    await self._acquire_with_settling_policy(
                        configuration, obligation, policy=policy, turn=turn, now=now
                    )
            except ReportingRowCoverageError as error:
                refused = refused or error
                await self._park_rows_outside_coverage(error, obligation, turn, now=now)
            except (ReportingCurrencyError, LedgerConflictError) as error:
                if isinstance(error, LedgerConflictError) and error.code != "OBSERVATION_CONFLICT":
                    raise
                turn.slices_failed.append(obligation.reporting_obligation_id)
                self._note_escalation(obligation, turn, now=now)
        if refused is not None:
            raise refused

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
        refused: ReportingRowCoverageError | None = None
        for identifier in identifiers:
            obligation = await self._store.get_obligation(
                account_id=configuration.account_id, reporting_obligation_id=identifier
            )
            if obligation is None or obligation.generation_key != configuration.generation_key:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer history is unavailable")
            if await self._close_without_source(configuration, obligation, turn, now=now):
                await progress.finish_producer_acquisition(
                    configuration, reporting_obligation_id=identifier
                )
                continue
            if not await self._automatic_read_ready(configuration, obligation, now=now):
                # Waiting for the declared source window is not completion.
                # Leave the durable work item available to a later turn.
                continue
            policy = self._settling_policy(configuration, obligation)
            finished = policy is None
            try:
                if policy is None:
                    await self.acquire_obligation(configuration, obligation, turn=turn, now=now)
                    if obligation.definition and isinstance(
                        obligation.definition.finality_policy, ReportingContractualCutoffPolicy
                    ):
                        finished = identifier not in turn._retry_keys_by_obligation
                else:
                    finished = await self._acquire_with_settling_policy(
                        configuration, obligation, policy=policy, turn=turn, now=now
                    )
            except ReportingRowCoverageError as error:
                # Immutable coverage cannot admit these rows on any later read:
                # retire the work item, as for other parked ledger conflicts.
                refused = refused or error
                await self._park_rows_outside_coverage(error, obligation, turn, now=now)
                finished = True
            except (ReportingCurrencyError, LedgerConflictError) as error:
                if isinstance(error, LedgerConflictError) and error.code not in {
                    "HISTORY_UNAVAILABLE",
                    "STABILIZATION_ORIGIN_UNAVAILABLE",
                    "STABILIZATION_ORIGIN_CONFLICT",
                    "EMPTY_DENOMINATOR",
                    "OBSERVATION_CONFLICT",
                }:
                    raise
                turn.slices_failed.append(identifier)
                self._note_escalation(obligation, turn, now=now)
                # Retain the existing corrupt-history parking check. A transient
                # currency failure or observation conflict must not retire work.
                finished = error.code != "OBSERVATION_CONFLICT" and (
                    policy is None or isinstance(error, LedgerConflictError)
                )
                if obligation.definition and isinstance(
                    obligation.definition.finality_policy, ReportingStabilizedPolicy
                ):
                    finished = False
            if finished:
                await progress.finish_producer_acquisition(
                    configuration, reporting_obligation_id=identifier
                )
        if refused is not None:
            raise refused

    async def _park_rows_outside_coverage(
        self,
        error: ReportingRowCoverageError,
        obligation: ReportingObligationRecord,
        turn: WorkerTurn,
        *,
        now: datetime,
    ) -> None:
        """Park one obligation whose rows fall outside its frozen coverage.

        Nothing was committed. Its coverage is immutable, so re-reading the
        source on every turn cannot help: the slice is blocked until a manual
        replay, and escalates like any failed slice. The worker loop keeps
        going so later periods still publish, then re-raises the first such
        error at the end of the turn, so ``run_worker`` and
        ``run_configuration`` callers see it rather than a quiet failure.
        """
        logger.warning(
            "reporting revision refused obligation=%s code=%s: %s",
            obligation.reporting_obligation_id,
            error.code,
            error,
        )
        turn.slices_failed.append(obligation.reporting_obligation_id)
        await self._schedule_retry(obligation, turn, now=now, blocked=True)
        self._note_escalation(obligation, turn, now=now)

    async def _close_without_source(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        turn: WorkerTurn,
        *,
        now: datetime,
    ) -> bool:
        """Settle an obligation that never reads its source; ``True`` when it was one.

        An account obligation whose frozen coverage is not full publishes no
        revision, unless its ``allow_partial`` generation can publish the
        covered subset: the status projection reports a withheld one as
        ``REPORTING_COVERAGE_INCOMPLETE``. A proven zero-buy snapshot obligation
        closes with a zero-row snapshot.
        """
        if _withheld(configuration, obligation):
            return True
        if _zero_buy_snapshot(obligation):
            await self._commit_zero_buy_snapshot(obligation, turn=turn, now=now)
            return True
        return False

    async def _commit_zero_buy_snapshot(
        self, obligation: ReportingObligationRecord, *, turn: WorkerTurn, now: datetime
    ) -> ReportingRevisionRecord | None:
        """Publish the zero-row snapshot of a proven zero-buy period, once.

        The empty denominator was proven when the period closed, so there is
        no source work: the revision is the explicit zero, with no rows and
        zero totals. An official close is never minted this way.
        """
        if await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        ):
            return None
        require_frozen_currency(obligation.currency)
        revision_id = (
            "rpr_"
            + hashlib.sha256(
                canonical_json_utf8_v1(["zero-buy-snapshot-v1", obligation.reporting_obligation_id])
            ).hexdigest()[:40]
        )
        totals: tuple[tuple[str, str], ...] = ()
        if self._revision_verifier is not None:
            _, canonical = self._revision_verifier.canonicalize([])
            totals = tuple((total.name, total.value) for total in canonical)
        revision = ReportingRevisionRecord(
            reporting_revision_id=revision_id,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            finality="snapshot",
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision_id,
                row_count=0,
                control_totals=totals,
                reporting_rows=[],
            ),
            row_count=0,
            control_totals=totals,
            observed_at=now,
            data_through=obligation.period.end,
            created_at=now,
        )
        if self._revision_verifier is not None:
            from adcp.reporting.materializer.publication import verified_publication

            revision = verified_publication(self._revision_verifier, obligation, revision, [])
        staging = self._store if isinstance(self._store, ReportingRowStagingStore) else None
        staged = staging is not None and bool(await staging.stage_revision_rows(revision, []))
        try:
            committed = await self._store.commit_revision(revision, [])
        finally:
            if staged and staging is not None:
                staging.discard_staged_revision_rows(revision)
        turn.revisions_committed.append(committed.reporting_revision_id)
        return committed

    async def _automatic_read_ready(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        now: datetime,
    ) -> bool:
        if _utc(now) < self._source_available_at(obligation):
            return False
        if _utc(now) >= self._scheduled_source_ready_at(configuration, obligation):
            return True
        if await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        ):
            # A successful manual snapshot has already passed the first read.
            # Its short settling or immediate official close cannot inherit
            # an automatic first-read delay it never needed.
            return True
        # A manual acquisition may have started work before its first automatic
        # phase. That frozen attempt keeps the ordinary persisted retry clock.
        retry = await self._get_retry(self._retry_keys(obligation)[0])
        return retry is not None and retry.attempt > 0

    def _scheduled_source_ready_at(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> datetime:
        """Spread the first automatic read; later observations retain that phase.

        The stable offset follows actual offering readiness, never precedes it,
        and consumes only available SLA headroom. Successful observations keep
        their persisted checked-at + cadence schedule without adding it again.
        Retries, explicit acquisition and official-close boundaries keep their
        existing clocks.
        """
        ready = self._source_available_at(obligation)
        policy = self._settling_policy(configuration, obligation)
        bound = min(
            self._read_jitter_window,
            (_utc(obligation.period.end) - _utc(obligation.period.start)) / 10,
            max(timedelta(0), _utc(obligation.period.expected_at) - ready),
            max(timedelta(0), _utc(obligation.automated_recovery_deadline_at) - ready),
        )
        if policy is not None:
            bound = min(bound, policy.restatement_cadence / 10)
        microseconds = bound // timedelta(microseconds=1)
        if microseconds <= 0:
            return ready
        seed = canonical_json_utf8_v1(
            [
                "reporting-first-read-jitter-v1",
                obligation.account_id,
                obligation.consumer_id,
                obligation.delivery_config_id,
                obligation.delivery_config_version,
                obligation.report_definition_id,
                _utc(obligation.period.start).isoformat(),
                _utc(obligation.period.end).isoformat(),
                self._offerings.offering_for(
                    _source_finality(obligation, obligation.required_finality)
                ),
            ]
        )
        offset = int.from_bytes(hashlib.sha256(seed).digest(), "big") % (microseconds + 1)
        return ready + timedelta(microseconds=offset)

    def _source_available_at(
        self,
        obligation: ReportingObligationRecord,
        *,
        target_finality: str | None = None,
        offering_id: str | None = None,
        worst_case: bool = False,
    ) -> datetime:
        end = _utc(obligation.period.end)
        output_finality = target_finality or obligation.required_finality
        policy = obligation.definition.finality_policy if obligation.definition else None
        cutoff = end
        if offering_id is None:
            offering_id = self._offerings.offering_for(
                _source_finality(obligation, output_finality)
            )
            if output_finality == "official" and isinstance(
                policy, ReportingContractualCutoffPolicy
            ):
                cutoff = contractual_cutoff_at(obligation, policy)
        if offering_id is None:
            # The acquisition path reports the existing missing-offering error.
            return cutoff
        offering = self._source.capabilities.offering(offering_id)
        lag = (
            offering.worst_case_availability_lag
            if worst_case
            else offering.expected_availability_lag
        )
        ready_at = end + timedelta(milliseconds=iso_duration_milliseconds_v1(lag))
        if isinstance(offering, AuthoritativeOfferingV1):
            zone = ZoneInfo(offering.source_timezone or obligation.period.source_timezone)
            local_day = end.astimezone(zone).date() + timedelta(days=offering.days_after_period_end)
            hour, minute = (int(part) for part in offering.source_local_ready_time.split(":"))
            local_ready = datetime.combine(local_day, datetime.min.time()).replace(
                hour=hour, minute=minute, tzinfo=zone
            )
            # Pick the later instant across a clock fold or gap. Ambiguous
            # local readiness must not make the scheduler read the source early.
            ready_at = max(
                ready_at,
                _utc(local_ready.replace(fold=0)),
                _utc(local_ready.replace(fold=1)),
            )
        return max(ready_at, cutoff)

    def _settling_policy(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        cutoff_reservation: bool = False,
    ) -> _SettlingPolicy | None:
        """Resolve the source policy or SDK fallback for a snapshot obligation."""
        offering_id = self._offerings.snapshot_offering_id
        selected = obligation.definition.finality_policy if obligation.definition else None
        if (
            obligation.required_finality != "snapshot"
            and not cutoff_reservation
            and not isinstance(selected, ReportingStabilizedPolicy)
        ) or offering_id is None:
            return None
        offering = self._source.capabilities.offering(offering_id)
        if not isinstance(offering, ProvisionalSnapshotOfferingV1):
            return None
        frozen = self._recovered_stabilization.get(configuration.generation_key)
        if frozen is not None:
            return _SettlingPolicy(frozen.window, frozen.cadence, frozen.official_close_lag)
        cadence = snapshot_restatement_cadence(offering, configuration.schedule.period_duration)
        close_lag = (
            timedelta(milliseconds=iso_duration_milliseconds_v1(offering.official_close_lag))
            if offering.official_close_lag is not None
            else None
        )
        return _SettlingPolicy(
            restatement_window=timedelta(
                milliseconds=iso_duration_milliseconds_v1(offering.restatement_window or "P3D")
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
        revisions = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        if any(item.finality == "official" for item in revisions):
            return True
        selected = obligation.definition.finality_policy if obligation.definition else None
        checkpoint_store = self._restatement_store()
        observation_store = self._observation_store()
        latest = await observation_store.get_provisional_observation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        scheduled = await self._latest_scheduled_snapshot(obligation, latest)
        checkpoint = await checkpoint_store.get_restatement_checkpoint(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        frozen = (
            scheduled.acquisition.policy
            if scheduled is not None
            else self._checkpoint_snapshot_policy(
                latest, checkpoint.provisional_until if checkpoint else None
            )
        )
        if frozen is not None:
            policy = _SettlingPolicy(frozen.window, frozen.cadence, frozen.official_close_lag)
        if isinstance(selected, ReportingStabilizedPolicy) and (
            obligation.required_finality == "official"
            or configuration.generation_key in self._recovered_stabilization
            or policy.official_close_lag is not None
            or (scheduled is not None and scheduled.acquisition.policy.until_stabilized)
        ):
            result = await self._acquire_stabilized(configuration, obligation, selected, turn, now)
            return result is not None and result.finality == "official"
        # A snapshot-only schedule can carry an unused definition policy. Its
        # automatic reads need only the ordinary observation publisher.
        acquire = (
            self._acquire_obligation
            if isinstance(selected, ReportingStabilizedPolicy)
            else self.acquire_obligation
        )
        explicit_close = (
            policy.official_close_lag is not None
            and self._offerings.offering_for(_source_finality(obligation, "official")) is not None
        )
        ordinal = checkpoint.next_observation if checkpoint is not None else len(revisions)
        pending = await self._pending_acquisition(obligation, ordinal=ordinal)
        if pending is not None:
            # Finish frozen source work before selecting a new finality. A
            # snapshot retry may cross the official-close boundary while the
            # authoritative offering is still unavailable.
            request = pending.request()
            if _utc(now) < self._source_available_at(obligation, offering_id=request.offering_id):
                return False
            await acquire(
                configuration,
                obligation,
                restate=bool(revisions),
                turn=turn,
                now=now,
                target_finality=pending.finality,
                track_settling=True,
            )
            revisions = await self._store.list_revisions(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if any(item.finality == "official" for item in revisions):
                return True
            if not explicit_close:
                published = await observation_store.get_provisional_observation(
                    account_id=obligation.account_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                return (
                    published is not None
                    and published.is_scheduled
                    and published.acquisition.ordinal == ordinal
                    and published.next_due_at is None
                )
            return False
        if not revisions:
            await acquire(
                configuration,
                obligation,
                turn=turn,
                now=now,
                target_finality="snapshot",
                track_settling=True,
            )
            return False

        if latest is not None and frozen is None:
            # A tracked manual retry advances the durable ordinal only. The
            # first automatic source read keeps its configured phase and window.
            checkpoint = None
        settles_at = _utc(obligation.period.end) + policy.restatement_window
        if scheduled is not None:
            settles_at = _utc(scheduled.provisional_until)
        elif checkpoint is not None and checkpoint.provisional_until is not None:
            settles_at = _utc(checkpoint.provisional_until)
        if scheduled is not None:
            next_due = scheduled.next_due_at
        elif checkpoint is not None:
            next_due = (
                min(_utc(checkpoint.checked_at) + policy.restatement_cadence, settles_at)
                if _utc(checkpoint.checked_at) < settles_at
                else None
            )
        else:
            # A direct revision has no scheduled observation time.
            next_due = self._scheduled_source_ready_at(configuration, obligation)
        # Preserve explicitly configured official-close boundary precedence.
        # Snapshot-only policies retain their final inclusive due read through
        # downtime; expiry alone is never an official publication.
        if not explicit_close or _utc(now) < settles_at:
            if next_due is None:
                return not explicit_close
            if _utc(now) >= _utc(next_due):
                await acquire(
                    configuration,
                    obligation,
                    restate=True,
                    turn=turn,
                    now=now,
                    target_finality="snapshot",
                    track_settling=True,
                )
                published = await observation_store.get_provisional_observation(
                    account_id=obligation.account_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                if (
                    not explicit_close
                    and published is not None
                    and published.is_scheduled
                    and published.next_due_at is None
                ):
                    return True
            return False

        assert policy.official_close_lag is not None
        closes_at = max(
            settles_at,
            _utc(obligation.period.end) + policy.official_close_lag,
            self._source_available_at(obligation, target_finality="official"),
        )
        if _utc(now) >= closes_at:
            await acquire(
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
        manual_replay: bool = False,
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
        return await self._acquire_obligation(
            configuration,
            obligation,
            restate=restate,
            turn=turn,
            now=now,
            target_finality=target_finality,
            track_settling=track_settling,
            manual_replay=manual_replay,
            admit_tracked_stabilization=True,
        )

    def _retry_keys(self, obligation: ReportingObligationRecord) -> tuple[str, str, str]:
        source = hashlib.sha256(
            canonical_json_utf8_v1(
                [self._offerings.publication_namespace, self._offerings.source_scope]
            )
        ).hexdigest()
        return (
            f"slice:{obligation.account_id}:{obligation.reporting_obligation_id}",
            f"account:{obligation.account_id}:{source}",
            f"source:{source}",
        )

    async def _get_retry(self, key: str) -> RetryScheduleEntry | None:
        return await self._retry_store.get_retry_schedule(scope_key=key)

    async def _record_retry(self, entry: RetryScheduleEntry) -> RetryScheduleEntry:
        return await self._retry_store.record_retry_schedule(entry)

    @staticmethod
    def _refresh_retry(turn: WorkerTurn, *, now: datetime) -> None:
        candidates = []
        for keys in turn._retry_keys_by_obligation.values():
            entries = [turn._retry_entries[key] for key in keys if key in turn._retry_entries]
            if any(entry.blocked for entry in entries):
                continue
            due = [entry.retry_not_before for entry in entries if entry.attempt > 0]
            if due:
                candidates.append(max(due, key=_utc))
        turn.earliest_retry_at = min(candidates, key=_utc) if candidates else None

    async def _retry_not_before(
        self, obligation: ReportingObligationRecord, turn: WorkerTurn, *, now: datetime
    ) -> bool:
        keys = self._retry_keys(obligation)
        schedules = [await self._get_retry(key) for key in keys]
        deferred = [
            entry
            for entry in schedules
            if entry is not None
            and entry.attempt > 0
            and (entry.blocked or _utc(entry.retry_not_before) > _utc(now))
        ]
        if not deferred:
            return False
        turn._retry_keys_by_obligation[obligation.reporting_obligation_id] = keys
        turn._retry_entries.update(
            (entry.scope_key, entry) for entry in schedules if entry is not None
        )
        self._refresh_retry(turn, now=now)
        return True

    async def _schedule_retry(
        self,
        obligation: ReportingObligationRecord,
        turn: WorkerTurn,
        *,
        now: datetime,
        scope: str = "slice",
        retry_after_seconds: float | None = None,
        blocked: bool = False,
        replayed: bool = False,
        first_core_delivery: _FirstCoreDelivery | None = None,
    ) -> None:
        # Dispatch ``now`` can be synthetic in tests and integrations. A slow
        # source may finish long after it, so anchor the cooldown at completion.
        completed_at = max(_utc(now), _utc(self._clock()))
        if first_core_delivery is not None and blocked:
            first_core_delivery.state = "consumed"
        keys = self._retry_keys(obligation)
        shared = keys[1 if scope == "account" else 2]
        affected = keys[:1] if scope == "slice" else ((shared,) if blocked else (keys[0], shared))
        turn._retry_keys_by_obligation[obligation.reporting_obligation_id] = keys
        for key in keys:
            current = await self._get_retry(key)
            if current is not None:
                turn._retry_entries[key] = current
        for key in affected:
            previous = await self._get_retry(key)
            attempt = (previous.attempt if previous is not None else 0) + 1
            # Stable per-scope jitter keeps retries spread across configurations
            # and gives identical decisions to workers sharing a store.
            jitter = (
                0.8
                + 0.4
                * int.from_bytes(hashlib.sha256(f"{key}:{attempt}".encode()).digest()[:2], "big")
                / 65535
            )
            exponential = self._retry_initial_delay * (2 ** min(attempt - 1, 30))
            delay = min(self._retry_max_delay, exponential * jitter)
            if retry_after_seconds is not None:
                try:
                    delay = max(delay, timedelta(seconds=retry_after_seconds))
                except OverflowError:
                    delay = timedelta.max
            if completed_at >= _utc(obligation.automated_recovery_deadline_at):
                delay = max(delay, self._post_deadline_retry_interval)
            try:
                retry_at = completed_at + delay
            except OverflowError:
                retry_at = datetime.max.replace(tzinfo=timezone.utc)
            entry = RetryScheduleEntry(
                key,
                completed_at if blocked else retry_at,
                attempt,
                blocked,
                recorded_at=completed_at,
                replayed=replayed,
            )
            stored = await self._record_retry(entry)
            turn._retry_entries[key] = stored
            if (
                first_core_delivery is not None
                and first_core_delivery.state == "dispatching"
                and key == keys[0]
            ):
                if stored == entry:
                    first_core_delivery.retry = stored
                    first_core_delivery.state = "retry"
                else:
                    first_core_delivery.state = "consumed"
        if replayed:
            # A manual probe supersedes an earlier terminal blast radius. The
            # new error's declared scope governs subsequent automatic work.
            for key in keys:
                if key in affected:
                    continue
                prior = await self._get_retry(key)
                if prior is not None and prior.blocked:
                    turn._retry_entries[key] = await self._record_retry(
                        RetryScheduleEntry(
                            key, completed_at, 0, recorded_at=completed_at, replayed=True
                        )
                    )
        self._refresh_retry(turn, now=completed_at)

    async def _clear_retry(
        self, obligation: ReportingObligationRecord, *, turn: WorkerTurn, now: datetime
    ) -> None:
        for key in self._retry_keys(obligation):
            prior = await self._get_retry(key)
            if prior is not None and prior.attempt:
                stored = await self._record_retry(
                    RetryScheduleEntry(key, _utc(now), 0, recorded_at=_utc(now))
                )
                turn._retry_entries[key] = stored
        turn._retry_keys_by_obligation.pop(obligation.reporting_obligation_id, None)
        self._refresh_retry(turn, now=now)

    async def _admitted_source_constituents(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> tuple[ReportingConstituent, ...] | None:
        from adcp.reporting.ledger.producer_progress import ReportingProducerProgress
        from adcp.reporting.production.contracts import _SourceAuthorizationRevokedError

        if not isinstance(self._store, ReportingProducerProgress):
            return None
        try:
            return await self._store.producer_constituents(configuration, obligation)
        except _SourceAuthorizationRevokedError:
            source_revoked(configuration.account_id)

    def _check_source_authorization(
        self,
        configuration: ReportingConfiguration,
        offering_id: str,
        admitted_constituents: tuple[ReportingConstituent, ...] | None,
    ) -> tuple[ReportingConstituent, ...] | None:
        from adcp.reporting.materializer.contracts import failure
        from adcp.reporting.production.contracts import (
            ReportingProductionSource,
            ReportingProductionSourceBinding,
            _SourceAuthorizationRevokedError,
        )

        require_account_work(configuration.account_id)
        if isinstance(self._source, ReportingProductionSource):
            try:
                binding = self._source.configuration_binding(configuration)
                if binding is None:
                    source_revoked(configuration.account_id)
                if type(binding) is not ReportingProductionSourceBinding:
                    raise failure("BINDING_MISMATCH")
                binding.check(configuration, self._source.capabilities, offering_id)
                pool = binding.constituents()
                if admitted_constituents is not None:
                    if configuration.owed_set:
                        # An owed obligation froze its own constituents at
                        # planning; the host must still authorize every one.
                        if not set(_constituent_keys(admitted_constituents)) <= set(
                            _constituent_keys(pool)
                        ):
                            raise failure("BINDING_MISMATCH")
                    elif coverage_denominator_fingerprint_v1(
                        pool
                    ) != coverage_denominator_fingerprint_v1(admitted_constituents):
                        raise failure("BINDING_MISMATCH")
                return admitted_constituents if configuration.owed_set else pool
            except _SourceAuthorizationRevokedError:
                raise
            except Exception:
                raise failure("BINDING_MISMATCH") from None
        return admitted_constituents

    @asynccontextmanager
    async def _source_publication(
        self,
        configuration: ReportingConfiguration,
        offering_id: str,
        *,
        admitted_constituents: tuple[ReportingConstituent, ...] | None,
        seals: ReportingSealStore | None = None,
    ) -> AsyncIterator[InlineSealPublisher | None]:
        from adcp.reporting.production.contracts import ReportingProductionSource

        if not isinstance(self._source, ReportingProductionSource):
            yield None
            return
        store = self._optional_publication_store()
        if store is None:
            raise TypeError("production source publication requires an SDK account lock")
        async with store.source_publication(configuration.account_id, seals=seals) as publish_seal:
            self._check_source_authorization(configuration, offering_id, admitted_constituents)
            yield publish_seal

    async def _execute_source(
        self,
        configuration: ReportingConfiguration,
        request: ReportingSourceSliceRequestV1,
        *,
        admitted_constituents: tuple[ReportingConstituent, ...] | None,
        cancel: asyncio.Event,
    ) -> ReportingSourceExecutorResult | _InlineStorageFailure:
        from adcp.reporting.inline_storage import InlineStorageError

        try:
            with bind_inline_publication(
                configuration.account_id,
                lambda seals: self._source_publication(
                    configuration,
                    request.offering_id,
                    admitted_constituents=admitted_constituents,
                    seals=seals,
                ),
            ):
                # Check inside the executing task: scheduling it is not dispatch.
                # Revocation never cancels a fetch that has already started.
                self._check_source_authorization(
                    configuration, request.offering_id, admitted_constituents
                )
                return await self._source.execute(request, cancel=cancel)
        except InlineStorageError as error:
            return _InlineStorageFailure(error.code)

    @asynccontextmanager
    async def _revision_publication(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        *,
        official: bool,
    ) -> AsyncIterator[None]:
        from adcp.reporting.production.contracts import ReportingProductionSource

        lock: ReportingSourcePublicationStore | None
        if official and obligation.definition and obligation.definition.finality_policy:
            lock = self._official_publication_lock(
                stabilized=isinstance(
                    obligation.definition.finality_policy, ReportingStabilizedPolicy
                )
            )
        else:
            lock = self._optional_publication_store()
        if not isinstance(self._source, ReportingProductionSource):
            if lock is not None:
                async with lock.source_publication(obligation.account_id):
                    yield
            else:
                yield
            return
        configuration = await self._retained_source_configuration(obligation)
        # Read immutable admission scope before taking the publication lock. It
        # is not an authorization grant: the callback is checked again inside
        # the lock against this exact mapping, including on replay.
        constituents = await self._admitted_source_constituents(configuration, obligation)
        published = tuple(part.constituent for part in manifest.coverage.constituents)
        if (
            constituents is not None
            and coverage_denominator_fingerprint_v1(constituents)
            != manifest.coverage.denominator_fingerprint
        ):
            raise LedgerConflictError(
                "MANIFEST_MISMATCH", "source denominator differs from admission"
            )
        async with self._source_publication(
            configuration, manifest.offering_id, admitted_constituents=published
        ):
            yield

    def _restatement_store(self) -> RestatementCheckpointStore:
        store = self._optional_restatement_store()
        if store is None:
            raise LedgerConflictError(
                "RESTATEMENT_CHECKPOINTS_NOT_SUPPORTED",
                "a source settling window requires explicitly implemented durable "
                "restatement checkpoint methods",
            )
        return store

    def _observation_store(self) -> ProvisionalObservationPublicationStore:
        store = self._optional_observation_store()
        if store is None:
            raise LedgerConflictError(
                "PROVISIONAL_OBSERVATIONS_NOT_SUPPORTED",
                "scheduled provisional reads require explicitly implemented atomic "
                "observation methods, including any decorating publisher's preparation",
            )
        return store

    async def _pending_acquisition(
        self, obligation: ReportingObligationRecord, *, ordinal: int
    ) -> ProvisionalAcquisition | None:
        if inspect.getattr_static(
            self._store, "get_provisional_acquisition", None
        ) is None or not isinstance(self._store, PendingProvisionalAcquisitionStore):
            return None
        acquisition = await self._store.get_provisional_acquisition(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            ordinal=ordinal,
        )
        if acquisition is not None and not acquisition.binds(obligation):
            raise LedgerConflictError(
                "OBSERVATION_CONFLICT",
                "retained source acquisition lacks matching consumer identity; "
                "operator reconciliation and a new configuration generation are required",
            )
        return acquisition

    def _note_unclosed_period(
        self,
        configuration: ReportingConfiguration,
        boundary: ReportingPeriodBoundary,
        turn: WorkerTurn,
        *,
        now: datetime,
        why: str,
    ) -> None:
        """Surface a period that cannot be closed yet; it stays uncommitted.

        Past the period's automated recovery deadline the would-be obligation's
        identifier joins ``turn.escalated``, the same signal an obligation that
        ran out of automated recovery raises, so a supervisor alerts instead of
        the period waiting silently forever.
        """
        logger.warning(
            "reporting period not closed account=%s config=%s@%s period=%s reason=%s",
            configuration.account_id,
            configuration.delivery_config_id,
            configuration.delivery_config_version,
            boundary.period_key,
            why,
        )
        deadline = boundary.expected_at + configuration.automated_recovery_window
        if _utc(now) >= _utc(deadline):
            turn.escalated.append(self._obligation_id(configuration, boundary))

    def _note_escalation(
        self,
        obligation: ReportingObligationRecord,
        turn: WorkerTurn,
        *,
        now: datetime,
        availability_retry: bool = False,
        target_finality: str | None = None,
        offering_id: str | None = None,
    ) -> None:
        """Record that this obligation has run out of automated recovery.

        The health projection derives ``delayed`` versus ``action_required``
        from the clock, so the worker does not set a state -- it only surfaces
        that the boundary has passed so a supervisor can alert instead of
        letting a dead feed idle inside a retry loop forever.
        """
        if availability_retry and _utc(now) < self._source_available_at(
            obligation,
            target_finality=target_finality,
            offering_id=offering_id,
            worst_case=True,
        ):
            return
        if _utc(now) >= _utc(obligation.automated_recovery_deadline_at):
            turn.escalated.append(obligation.reporting_obligation_id)

    def _verified_manifest(self, result: ReportingSourceExecutorResult) -> SourceBatchManifestV1:
        response = result.response
        manifest_bytes = result.manifest_bytes
        assert response is not None and manifest_bytes is not None
        return parse_verified_source_batch_manifest_v1(response.manifest, manifest_bytes)

    async def _read_rows(
        self,
        request: ReportingSourceSliceRequestV1,
        manifest: SourceBatchManifestV1,
        *,
        require_staged: bool = False,
    ) -> list[dict[str, Any]]:
        """Materialize the manifest's staged rows into the revision's frozen rows."""
        if self._object_reader is None:
            if require_staged:
                raise LedgerConflictError(
                    "STAGED_OBJECT_UNAVAILABLE",
                    "verified publication requires a staged object reader",
                )
            return []
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
            if hashlib.sha256(payload).hexdigest() != staged.sha256 or (
                require_staged and len(payload) != staged.byte_count
            ):
                raise LedgerConflictError(
                    "STAGED_OBJECT_MISMATCH",
                    f"staged object {staged.ordinal} does not match its manifest evidence",
                )
            rows.extend(_staged_object_rows(staged, payload))
        return rows

    async def commit_revision_from_manifest(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        *,
        rows: Sequence[dict[str, Any]],
        finality: str,
        finality_proof: FinalityProof | None = None,
        now: datetime | None = None,
        turn: WorkerTurn | None = None,
        acquisition: ProvisionalAcquisition | None = None,
    ) -> ReportingRevisionRecord:
        """Project a verified manifest into an immutable ledger revision.

        A snapshot restatement supersedes the current snapshot leaf; there is no
        edit path.  An official close is terminal, so a later source correction
        must arrive as an adjustment instead. ``now`` is a trusted publication
        instant, unlike the dispatch instant accepted by ``acquire_obligation``.
        Without it, a reserved publication samples the clock after staged verification.
        A new official revision requires proof of the obligation's selected policy.
        Replaying a publication retains its original times, policy fields and parent.
        """
        manifest = SourceBatchManifestV1.model_validate_json(manifest.model_dump_json())
        return await self._commit_revision_from_manifest(
            obligation,
            manifest,
            rows=rows,
            finality=finality,
            finality_proof=finality_proof,
            now=now,
            turn=turn,
            acquisition=acquisition,
            verified_source_rows=False,
        )

    async def _commit_publication(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        revision: ReportingRevisionRecord,
        rows: Sequence[dict[str, Any]],
        finality: str,
        now: datetime,
        acquisition: ProvisionalAcquisition | None,
        *,
        candidate_proof: FinalityProof | None,
        stabilized_publication: _StabilizedPublication | None,
        origin_store: SnapshotOriginStore | None,
        retained_revision_id: str | None = None,
    ) -> tuple[ReportingRevisionRecord, bool]:
        async with self._revision_publication(
            obligation, manifest, official=finality == "official"
        ):
            replay = None
            if finality == "official":
                if stabilized_publication is not None:
                    history = stabilized_publication.history
                    frozen = history[-1].acquisition.policy
                    selected = (
                        obligation.definition.finality_policy if obligation.definition else None
                    )
                    assert isinstance(selected, ReportingStabilizedPolicy)
                    limit = stabilization_history_limit(selected, frozen.cadence)
                    snapshots = await self._stabilization_snapshots(obligation)
                    if snapshots != stabilized_publication.snapshots or (
                        await self._scheduled_snapshot_history(obligation, limit) != history
                    ):
                        raise LedgerConflictError(
                            "OBSERVATION_CONFLICT",
                            "the active snapshot or retained history changed during stabilization",
                        )
                replay = await self._store.get_revision(
                    account_id=obligation.account_id,
                    reporting_revision_id=revision.reporting_revision_id,
                )
                if replay is not None and replay.finality == "official":
                    revision = await self._official_replay_revision(
                        obligation,
                        revision,
                        replay,
                        manifest,
                        acquisition,
                        proved_candidate=candidate_proof is not None,
                    )
                    acquisition = None
            if finality == "snapshot" and acquisition is None:
                siblings = await self._store.list_revisions(
                    account_id=obligation.account_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                if any(r.finality == "official" for r in siblings) and not any(
                    r.reporting_revision_id == revision.reporting_revision_id for r in siblings
                ):
                    raise LedgerConflictError(
                        "OFFICIAL_REVISION_TERMINAL",
                        "a new snapshot cannot follow the official revision",
                    )
            if acquisition is None:
                committed = await self._store.commit_revision(revision, rows)
            else:
                # ``now`` is the publication anchor, sampled after row acquisition.
                # The bounds check above already rejects acquired_at after now.
                if origin_store is not None:
                    await self._verify_tracked_snapshot_origin(
                        obligation,
                        acquisition,
                        origin_store,
                        revision,
                        retained_revision_id=retained_revision_id,
                    )
                committed = await self._observation_store().commit_provisional_observation(
                    ProvisionalObservation.from_manifest(
                        acquisition,
                        retained_revision_id or revision.reporting_revision_id,
                        now,
                        manifest,
                    ),
                    revision,
                    rows,
                )
        return committed, replay is not None

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
            or identity.consumer_id != obligation.consumer_id
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
        trigger: ReportingTriggerKind = "scheduled_poll",
        invocation_id: str | None = None,
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

        A direct manual snapshot uses a fresh invocation ID so an interrupted
        invocation cannot replay stale data or affect the scheduled cadence.
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
                for media_buy_id in _source_media_buy_ids(configuration, obligation)
            ]
        )
        if not resolved_constituents:
            raise LedgerConflictError(
                "EMPTY_DENOMINATOR",
                "an obligation with no media buys has no source work; it is a platform-owned "
                "no-op, not a slice",
            )
        resolved_finality = finality or obligation.required_finality
        source_finality = _source_finality(obligation, resolved_finality)
        execution_identity: list[str | int] = [
            obligation.reporting_obligation_id,
            offering_id,
            observation,
        ]
        if resolved_finality == "official" and source_finality == "snapshot":
            # A policy close requires its own post-cutoff read, even when the
            # same snapshot offering already sealed an observation at this ordinal.
            execution_identity.append("official")
        if invocation_id is not None:
            execution_identity.append(invocation_id)
        source_execution_key = (
            "rse-" + hashlib.sha256(canonical_json_utf8_v1(execution_identity)).hexdigest()[:40]
        )
        publication_class: ReportingPublicationClass = (
            "AUTHORITATIVE" if source_finality == "official" else "PROVISIONAL_SNAPSHOT"
        )
        return ReportingSourceSliceRequestV1(
            identity=ReportingSourceIdentityV1(
                account_id=obligation.account_id,
                consumer_id=obligation.consumer_id,
                delivery_config_id=obligation.delivery_config_id,
                delivery_config_version=obligation.delivery_config_version,
                report_definition_id=obligation.report_definition_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                period_key=obligation.period.period_key,
                source_execution_key=source_execution_key,
                run_id=_source_run_id(now),
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
            trigger=trigger,
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

    def _stabilization_requested(self, configuration: ReportingConfiguration) -> bool:
        definition = configuration.definition
        return (
            definition is not None
            and isinstance(definition.finality_policy, ReportingStabilizedPolicy)
            and self._official_close_requested(configuration)
        )

    def _official_close_requested(
        self, configuration: ReportingConfiguration | ReportingObligationRecord
    ) -> bool:
        return (
            configuration.required_finality == "official"
            or configuration.generation_key in self._recovered_stabilization
            or self._offerings.snapshot_requests_official_close
        )

    async def _scheduled_snapshot_history(
        self, obligation: ReportingObligationRecord, limit: int
    ) -> tuple[ProvisionalObservation, ...]:
        store = self._observation_history_store()
        read = store.list_provisional_observations
        parameters = inspect.signature(read).parameters.values()
        if any(p.name == "scheduled_snapshots_only" or p.kind == p.VAR_KEYWORD for p in parameters):
            return await read(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                limit=limit,
                scheduled_snapshots_only=True,
            )
        # Custom publishers with the original history interface can still prove a
        # run. Widen a mixed suffix only as far as the same finite evidence budget.
        count = limit
        while True:
            history = await read(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
                limit=count,
            )
            scheduled = tuple(o for o in history if o.is_scheduled_snapshot)
            if (
                len(scheduled) >= limit
                or len(history) < count
                or history[0].acquisition.ordinal == 0
            ):
                return scheduled[-limit:]
            if count == MAX_STABILIZATION_HISTORY_OBSERVATIONS:
                raise LedgerConflictError(
                    "STABILIZATION_HISTORY_LIMIT",
                    "mixed observation history requires a scheduled snapshot filter",
                )
            count = min(2 * count, MAX_STABILIZATION_HISTORY_OBSERVATIONS)

    async def _latest_scheduled_snapshot(
        self, obligation: ReportingObligationRecord, latest: ProvisionalObservation | None
    ) -> ProvisionalObservation | None:
        if latest is None or latest.is_scheduled_snapshot:
            return latest
        if inspect.getattr_static(self._store, "list_provisional_observations", None) is None:
            return None
        try:
            history = await self._scheduled_snapshot_history(obligation, 1)
        except LedgerConflictError as error:
            if error.code == "PROVISIONAL_OBSERVATION_HISTORY_NOT_SUPPORTED":
                return None
            raise
        return history[-1] if history else None

    def _checkpoint_snapshot_policy(
        self, latest: ProvisionalObservation | None, boundary: datetime | None
    ) -> ProvisionalPolicy | None:
        if (
            latest is not None
            and latest.acquisition.ordinal > 0
            and latest.acquisition.request().trigger == "manual_replay"
            and boundary is not None
            and not _implements_store_method(
                self._store, ProvisionalObservationStore.list_provisional_observations
            )
        ):
            # The manual reservation carries the frozen schedule. The checkpoint
            # retains the preceding scheduled read's anchor and boundary.
            return latest.acquisition.policy
        return None

    async def _recover_stabilization_schedule(
        self,
        configuration: ReportingConfiguration,
        *,
        required: bool = True,
    ) -> ProvisionalPolicy | None:
        """Restore the exact generation's frozen schedule before validating its history budget."""
        key = configuration.generation_key
        frozen = self._recovered_stabilization.get(key)
        if required or frozen is not None:
            self._stabilization_store()
        if frozen is not None:
            return frozen
        store = self._stabilization_schedule_store()
        if store is None:
            return None
        policy = await store.get_stabilization_schedule(key)
        if policy is not None:
            if not required:
                self._stabilization_store()
            self._recovered_stabilization[key] = policy
        return policy

    def _stabilization_schedule_store(self) -> StabilizationScheduleStore | None:
        if _implements_store_method(
            self._store, StabilizationScheduleStore.get_stabilization_schedule
        ):
            assert isinstance(self._store, StabilizationScheduleStore)
            return self._store
        return None

    async def _has_retained_official_close(
        self,
        key: ReportingConfigurationGenerationKey,
        *,
        publishing: bool = False,
        scheduled_reservation: tuple[str, int] | None = None,
        first_core_obligation: str | None = None,
    ) -> bool:
        """Check completed close evidence and any reservations that could affect this operation."""
        schedule_store = self._stabilization_schedule_store()
        if schedule_store is not None:
            schedule = await schedule_store.get_stabilization_schedule(key)
            if schedule is not None:
                return True
        # A negative stabilization lookup still requires checking the other close policies.
        snapshot = await self._store.open_snapshot(
            caller=ReportingDeliveryPrincipal(key.account_id, key.consumer_id),
            filters_fingerprint=f"official-close:{key.delivery_config_id}:{key.delivery_config_version}",
        )
        offset = 0
        page_size = 100
        while True:
            page = await self._store.read_page(
                snapshot=snapshot,
                consumer_id=key.consumer_id,
                delivery_config_ids=[key.delivery_config_id],
                media_buy_ids=None,
                changes_after_sequence=None,
                offset=offset,
                limit=page_size,
            )
            for obligation in page.obligations:
                if obligation.generation_key != key:
                    continue
                if await self._obligation_has_retained_close(
                    obligation,
                    publishing=publishing,
                    scheduled_reservation=scheduled_reservation,
                    first_core_delivery=first_core_obligation == obligation.reporting_obligation_id,
                ):
                    return True
            if not page.has_more:
                return False
            offset += page_size

    async def _obligation_has_retained_close(
        self,
        obligation: ReportingObligationRecord,
        *,
        publishing: bool,
        scheduled_reservation: tuple[str, int] | None,
        first_core_delivery: bool,
    ) -> bool:
        owner = dict(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        revisions = await self._store.list_revisions(**owner)
        if any(revision.finality == "official" for revision in revisions):
            return True
        observations = self._optional_observation_store()
        latest = await observations.get_provisional_observation(**owner) if observations else None
        if latest is not None and latest.acquisition.finality == "official":
            return True
        scheduled = await self._latest_scheduled_snapshot(obligation, latest)
        if scheduled is not None:
            schedule = scheduled.acquisition.stabilization_schedule(
                obligation.required_finality, pending=False
            )
            if schedule is not None:
                return True
        checkpoints = self._optional_restatement_store()
        checkpoint = await checkpoints.get_restatement_checkpoint(**owner) if checkpoints else None
        if checkpoint is not None and checkpoint.stabilization_policy is not None:
            return True
        lookup = _implements_store_method(
            self._store, PendingProvisionalAcquisitionStore.get_provisional_acquisition
        )
        if first_core_delivery and not revisions and latest is None and checkpoint is None:
            # An ordinary commit arbitrates reservations hidden behind a Core publisher.
            return lookup and await self._pending_acquisition(obligation, ordinal=0) is not None
        if not lookup and (
            scheduled_reservation is None
            or scheduled_reservation[0] != obligation.reporting_obligation_id
        ):
            # A manual read or another period cannot inspect this reservation by claiming it.
            return publishing or scheduled is None
        # An untracked manual revision can precede the first scheduled reservation.
        ordinal = checkpoint.next_observation if checkpoint else len(revisions)
        if lookup:
            pending = await self._pending_acquisition(obligation, ordinal=ordinal)
            if pending is not None and (
                pending.finality == "official"
                or pending.stabilization_schedule(obligation.required_finality, pending=True)
                is not None
            ):
                return True
        elif scheduled_reservation != (obligation.reporting_obligation_id, ordinal):
            # A completed ordinal cannot prove which publisher owns the next one.
            return True
        return scheduled is None and (checkpoints is None or checkpoint is not None)

    async def _acquire_stabilized(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        policy: ReportingStabilizedPolicy,
        turn: WorkerTurn,
        now: datetime,
        *,
        manual: bool = False,
        track_manual: bool = False,
    ) -> ReportingRevisionRecord | None:
        owner = dict(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        revisions = await self._store.list_revisions(**owner)
        if any(r.finality == "official" for r in revisions):
            return None
        await self._recover_stabilization_schedule(configuration)
        store = self._store
        assert isinstance(store, StabilizationStore)
        checkpoint_store = self._restatement_store()
        checkpoint = await checkpoint_store.get_restatement_checkpoint(**owner)
        if not manual and checkpoint is not None and checkpoint.waiting_for_stabilization(now):
            return None
        ordinal = checkpoint.next_observation if checkpoint else len(revisions)
        pending = await self._pending_acquisition(obligation, ordinal=ordinal)
        if pending is not None and pending.legacy_stabilized_close:
            if not await self._retire_stabilized_acquisition(
                configuration, obligation, pending, now
            ):
                return None
            ordinal += 1
            pending = await self._pending_acquisition(obligation, ordinal=ordinal)
        if pending is not None and pending.finality != "snapshot":
            raise LedgerConflictError(
                "OBSERVATION_CONFLICT", "stabilized finality requires a reserved snapshot output"
            )
        latest = await self._latest_scheduled_snapshot(
            obligation, await store.get_provisional_observation(**owner)
        )
        if latest is not None and pending is None:
            if (
                not manual
                and now < latest.next_stabilization_due_at
                and not stabilization_interval_elapsed(
                    policy, obligation.period.end, obligation.period.end, latest.checked_at
                )
            ):
                return None
            try:
                official = await self._promote_stabilized(obligation, policy, latest, turn)
            except LedgerConflictError as error:
                if (
                    error.code
                    not in {"STABILIZATION_ORIGIN_UNAVAILABLE", "STABILIZATION_ORIGIN_CONFLICT"}
                    or manual
                    or (_utc(now) < _utc(latest.next_stabilization_due_at))
                ):
                    raise
                official = None
            if official is not None:
                return official
            if not manual and _utc(now) < _utc(latest.next_stabilization_due_at):
                return None
        elif latest is None and not manual and pending is None:
            if _utc(now) < self._scheduled_source_ready_at(configuration, obligation):
                return None
        snapshot = await self.acquire_obligation(
            configuration,
            obligation,
            target_finality="snapshot",
            turn=turn,
            now=now,
            track_settling=not manual or track_manual,
            restate=bool(revisions),
            manual_replay=manual,
        )
        if snapshot is not None:
            observed = await store.get_provisional_observation(**owner)
            if (
                observed is not None
                and observed.is_scheduled_snapshot
                and observed.acquisition.ordinal == ordinal
            ):
                official = await self._promote_stabilized(obligation, policy, observed, turn)
                if official is not None:
                    return official
        return snapshot

    async def _promote_stabilized(
        self,
        obligation: ReportingObligationRecord,
        policy: ReportingStabilizedPolicy,
        latest: ProvisionalObservation,
        turn: WorkerTurn,
    ) -> ReportingRevisionRecord | None:
        evidence = await self._stabilized_publication(obligation, policy, latest)
        if evidence is None:
            return None
        return await self._commit_revision_from_manifest(
            obligation,
            evidence.manifest,
            rows=evidence.rows,
            finality="official",
            finality_proof=evidence.proof,
            now=self._clock(),
            turn=turn,
            acquisition=None,
            verified_source_rows=True,
            stabilized_publication=evidence,
        )

    async def _stabilized_publication(
        self,
        obligation: ReportingObligationRecord,
        policy: ReportingStabilizedPolicy,
        latest: ProvisionalObservation,
    ) -> _StabilizedPublication | None:
        limit = stabilization_history_limit(policy, latest.acquisition.policy.cadence)
        history = await self._scheduled_snapshot_history(obligation, limit)
        if not history or history[-1] != latest or len(history) > limit:
            raise LedgerConflictError(
                "OBSERVATION_CONFLICT", "stabilization history changed during proof"
            )
        owner = dict(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        snapshots = await self._stabilization_snapshots(obligation)
        leaf = select_reporting_revision(
            tuple(snapshots.values()), **owner, required_finality="snapshot"
        )
        if leaf.kind != "selected" or not leaf.revision.readable:
            raise LedgerConflictError(
                "HISTORY_UNAVAILABLE", "stabilization history requires a readable snapshot leaf"
            )
        candidates: list[
            tuple[ProvisionalObservation, SourceBatchManifestV1, ReportingRevisionRecord]
        ] = []
        previous: ProvisionalObservation | None = None
        revision: ReportingRevisionRecord | None = None
        for observation in history:
            acquisition = observation.acquisition
            if (
                not observation.is_scheduled_snapshot
                or not acquisition.binds(obligation)
                or (
                    previous is not None
                    and (
                        acquisition.ordinal <= previous.acquisition.ordinal
                        or acquisition.request().period.source_read_cutoff_at < previous.checked_at
                    )
                )
            ):
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "stabilization history acquisition differs"
                )
            previous = observation
            if revision is None or revision.reporting_revision_id != observation.revision_id:
                revision = await self._store.get_revision(
                    account_id=obligation.account_id, reporting_revision_id=observation.revision_id
                )
                if (
                    revision is None
                    or not revision.readable
                    or revision.finality != "snapshot"
                    or revision.account_id != obligation.account_id
                    or revision.reporting_obligation_id != obligation.reporting_obligation_id
                    or revision != snapshots.get(observation.revision_id)
                ):
                    raise LedgerConflictError(
                        "HISTORY_UNAVAILABLE", "stabilization history requires a readable snapshot"
                    )
            try:
                manifest = SourceBatchManifestV1.model_validate_json(observation.manifest_json)
                self._validate_source_manifest(
                    obligation,
                    acquisition.request(),
                    manifest,
                    cutoff_ceiling=observation.checked_at,
                    acquisition=acquisition,
                )
                if manifest.acquired_at > observation.checked_at:
                    raise ValueError("acquisition is later than its observation")
                if acquisition.observation_deadlines(observation.checked_at, manifest) != (
                    observation.provisional_until,
                    observation.next_due_at,
                ):
                    raise ValueError("observation deadline differs from its frozen schedule")
                if revision.source_publication_id == manifest.publication_id and (
                    revision.observed_at != manifest.observed_at
                    or revision.data_through != manifest.data_through
                    or revision.supersedes_reporting_revision_id
                    != acquisition.predecessor_revision_id
                ):
                    raise ValueError("originating snapshot differs from its publication")
                validate_snapshot_observation_binding(revision, observation)
                if not candidates or candidates[-1][0].revision_id != observation.revision_id:
                    await self._stabilization_store().verify_snapshot_origin(revision)
                if (
                    revision.managed_control_totals is not None
                    and revision.managed_control_totals
                    != _typed_control_totals(obligation, manifest)
                ):
                    raise ValueError("snapshot control totals differ")
            except (ValueError, LedgerConflictError) as error:
                if isinstance(error, LedgerConflictError) and error.code in {
                    "STABILIZATION_ORIGIN_UNAVAILABLE",
                    "STABILIZATION_ORIGIN_CONFLICT",
                }:
                    if observation == latest or (
                        error.code == "STABILIZATION_ORIGIN_CONFLICT"
                        and observation.revision_id == latest.revision_id
                    ):
                        raise
                    candidates.clear()
                    continue
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE",
                    "stabilization history differs from its snapshot publication",
                ) from error
            try:
                manifest.require_full_period_watermarks()
            except ValueError:
                # A provisional read may be complete enough to publish a snapshot
                # while still lacking full-window evidence for official finality.
                candidates.clear()
                continue
            if candidates and (
                not consecutive_stabilization_slots(candidates[-1][0], observation)
                or candidates[-1][1].content_fingerprint != manifest.content_fingerprint
                or not self._matching_snapshot_lineage(
                    snapshots, revision, candidates[-1][0].revision_id, manifest
                )
            ):
                candidates.clear()
            candidates.append((observation, manifest, revision))
        if not candidates or not stabilization_interval_elapsed(
            policy, obligation.period.end, candidates[0][0].checked_at, latest.checked_at
        ):
            return None
        if not self._matching_snapshot_lineage(
            snapshots, leaf.revision, latest.revision_id, candidates[-1][1]
        ):
            return None
        # Only a time-eligible run needs its immutable row bodies. Distinct
        # manual revisions in the chain must bind those same verified rows.
        verified: list[tuple[ProvisionalObservation, str, bytes]] = []
        rows: list[dict[str, Any]] = []
        row_binding = b""
        for observation, manifest, revision in candidates:
            if not verified or verified[-1][0].revision_id != observation.revision_id:
                rows = await self._retained_snapshot_rows(revision)
                row_binding = hashlib.sha256(rfc8785.dumps(rows)).digest()
            if verified and not self._matching_snapshot_lineage(
                snapshots, revision, verified[-1][0].revision_id, manifest, rows
            ):
                verified.clear()
            verified.append((observation, manifest.content_fingerprint, row_binding))
        proof = prove_stabilized_finality(obligation, policy, verified)
        if proof is None or not self._matching_snapshot_lineage(
            snapshots, leaf.revision, latest.revision_id, manifest, rows
        ):
            return None
        return _StabilizedPublication(proof, manifest, rows, history, snapshots)

    async def _stabilization_snapshots(
        self, obligation: ReportingObligationRecord
    ) -> dict[str, ReportingRevisionRecord]:
        revisions = await self._store.list_revisions(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        return {r.reporting_revision_id: r for r in revisions if r.finality == "snapshot"}

    @staticmethod
    def _matching_snapshot_lineage(
        snapshots: Mapping[str, ReportingRevisionRecord],
        revision: ReportingRevisionRecord,
        ancestor_id: str,
        manifest: SourceBatchManifestV1,
        rows: Sequence[dict[str, Any]] | None = None,
    ) -> bool:
        # The selected snapshot chain has one root and no forks or cycles.
        # Manual publications can preserve a scheduled run only when each
        # intervening immutable revision binds both its fingerprint and rows.
        ancestor = snapshots[ancestor_id]
        while revision.reporting_revision_id != ancestor_id:
            if (
                not revision.readable
                or revision.row_count != ancestor.row_count
                or revision.control_totals != ancestor.control_totals
                or revision.managed_control_totals != ancestor.managed_control_totals
                or revision.source_manifest_sha256
                != manifest.content_fingerprint.removeprefix("sha256:")
                or (
                    rows is not None
                    and recompute_revision_content_sha256(revision, rows)
                    != revision.revision_content_sha256
                )
                or revision.supersedes_reporting_revision_id is None
            ):
                return False
            revision = snapshots[revision.supersedes_reporting_revision_id]
        return True

    async def _retained_snapshot_rows(
        self, revision: ReportingRevisionRecord
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self._store.read_revision_rows(
                account_id=revision.account_id,
                reporting_revision_id=revision.reporting_revision_id,
                cursor=cursor,
            )
            if (
                page.reporting_revision_id not in (None, revision.reporting_revision_id)
                or page.total_count != revision.row_count
            ):
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE",
                    "stabilization history row page differs from its snapshot",
                )
            rows.extend(page.rows)
            if not page.has_more:
                break
            if (
                not page.rows
                or page.cursor is None
                or page.cursor == cursor
                or len(rows) >= revision.row_count
            ):
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "stabilization history rows are incomplete"
                )
            cursor = page.cursor
        try:
            validate_snapshot_rows(revision, rows)
        except LedgerConflictError as error:
            raise LedgerConflictError(
                "HISTORY_UNAVAILABLE",
                "stabilization history rows differ from their snapshot binding",
            ) from error
        return rows

    async def _acquire_obligation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        restate: bool = False,
        turn: WorkerTurn | None = None,
        now: datetime | None = None,
        target_finality: str | None = None,
        track_settling: bool = False,
        manual_replay: bool = False,
        admit_tracked_stabilization: bool = False,
    ) -> ReportingRevisionRecord | None:
        if configuration.generation_key != obligation.generation_key:
            raise LedgerConflictError(
                "CONFIGURATION_GENERATION_MISMATCH",
                "the source configuration must belong to the obligation's account and generation",
            )
        obligation = await self._stored_obligation(obligation)
        turn = turn or WorkerTurn()
        now = now or self._clock()
        if _withheld(configuration, obligation):
            return None
        if _zero_buy_snapshot(obligation):
            return await self._commit_zero_buy_snapshot(obligation, turn=turn, now=now)
        finality = target_finality or obligation.required_finality
        selected_policy = obligation.definition.finality_policy if obligation.definition else None
        if finality == "official" and isinstance(selected_policy, ReportingStabilizedPolicy):
            await self._claim_first_core_delivery(obligation, now=now, trigger=None)
            return await self._acquire_stabilized(
                configuration,
                obligation,
                selected_policy,
                turn,
                now,
                manual=manual_replay or (restate and not track_settling),
                track_manual=track_settling,
            )
        if not manual_replay and await self._retry_not_before(obligation, turn, now=now):
            retry_offering_id: str | None = None
            if track_settling:
                checkpoint = await self._restatement_store().get_restatement_checkpoint(
                    account_id=obligation.account_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                ordinal = (
                    checkpoint.next_observation
                    if checkpoint is not None
                    else len(
                        await self._store.list_revisions(
                            account_id=obligation.account_id,
                            reporting_obligation_id=obligation.reporting_obligation_id,
                        )
                    )
                )
                pending = await self._pending_acquisition(obligation, ordinal=ordinal)
                if pending is not None:
                    retry_offering_id = pending.request().offering_id
            keys = turn._retry_keys_by_obligation[obligation.reporting_obligation_id]
            retryable = not any(
                turn._retry_entries[key].blocked for key in keys if key in turn._retry_entries
            )
            self._note_escalation(
                obligation,
                turn,
                now=now,
                availability_retry=retryable,
                target_finality=finality,
                offering_id=retry_offering_id,
            )
            return None
        trigger: Literal["scheduled", "manual"] | None = None
        if finality == "snapshot" and not track_settling:
            trigger = "manual" if manual_replay or restate else "scheduled"
        first_core = await self._claim_first_core_delivery(obligation, now=now, trigger=trigger)
        try:
            return await self._acquire_due_obligation(
                configuration,
                obligation,
                turn=turn,
                now=now,
                finality=finality,
                restate=restate,
                track_settling=track_settling,
                manual_replay=manual_replay,
                admit_tracked_stabilization=admit_tracked_stabilization,
                first_core=first_core,
            )
        except (OSError, asyncio.CancelledError):
            if first_core is not None:
                first_core.interrupted()
            raise
        finally:
            if first_core is not None and first_core.state == "retry":
                self._first_core_deliveries[
                    (obligation.generation_key, obligation.reporting_obligation_id)
                ] = first_core

    async def _acquire_due_obligation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        turn: WorkerTurn,
        now: datetime,
        finality: str,
        restate: bool,
        track_settling: bool,
        manual_replay: bool,
        admit_tracked_stabilization: bool,
        first_core: _FirstCoreDelivery | None,
    ) -> ReportingRevisionRecord | None:
        selected_policy = obligation.definition.finality_policy if obligation.definition else None
        first_core_obligation = (
            obligation.reporting_obligation_id if first_core is not None else None
        )
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
        checkpoint_store = (
            self._restatement_store() if track_settling else self._observation_checkpoint_store()
        )
        checkpoint = (
            await checkpoint_store.get_restatement_checkpoint(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if checkpoint_store is not None
            else None
        )
        observation = checkpoint.next_observation if checkpoint else len(revisions)
        if (
            not manual_replay
            and checkpoint is not None
            and checkpoint.waiting_for_stabilization(now)
        ):
            return None
        pending = (
            await self._pending_acquisition(obligation, ordinal=observation)
            if checkpoint_store is not None
            else None
        )
        if (
            admit_tracked_stabilization
            and isinstance(selected_policy, ReportingStabilizedPolicy)
            and (
                track_settling
                or pending is not None
                or (checkpoint_store is not None and not restate and not manual_replay)
            )
        ):
            await self._recover_stabilization_schedule(configuration)
        if pending is not None:
            finality = pending.finality
        elif (
            finality == "snapshot"
            and track_settling
            and not manual_replay
            and isinstance(selected_policy, ReportingStabilizedPolicy)
        ):
            # Another worker can finish the due observation between selecting this
            # period and reading its checkpoint. Its successor has a fresh deadline.
            latest = await self._observation_store().get_provisional_observation(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            scheduled = await self._latest_scheduled_snapshot(obligation, latest)
            if scheduled is not None and _utc(now) < _utc(scheduled.next_stabilization_due_at):
                return None
        if current is not None and current.readable and not restate and pending is None:
            return None
        if finality == "official":
            if selected_policy is None:
                raise LedgerConflictError(
                    "FINALITY_POLICY_UNPROVEN", "official finality requires a selected policy"
                )
        manual_observation = manual_replay or (restate and not track_settling)
        reserve_snapshot = (
            finality == "snapshot" and not manual_observation and checkpoint_store is not None
        )
        await self._check_official_publication_lock(
            obligation,
            official=finality == "official",
            frozen=pending.policy if pending else None,
            scheduled_ordinal=observation if reserve_snapshot else None,
            first_core_obligation=first_core_obligation,
        )
        # Everything below needs the frozen code: the slice request carries it,
        # the manifest is checked against it, and the revision is written under
        # it. Gate here rather than earlier so a settled legacy obligation stays
        # the no-op it already was instead of becoming an error on every turn.
        require_frozen_currency(obligation.currency)

        source_finality = _source_finality(obligation, finality)
        offering_id = self._offerings.offering_for(source_finality)
        if offering_id is None:
            raise LedgerConflictError(
                "NO_OFFERING_FOR_FINALITY",
                f"this producer declares no source offering for {finality} reporting",
            )

        constituents = await self._admitted_source_constituents(configuration, obligation)
        constituents = self._check_source_authorization(configuration, offering_id, constituents)

        derived_close = finality == "official" and source_finality == "snapshot"
        manual_snapshot = manual_observation and finality == "snapshot" and pending is None
        if derived_close and isinstance(selected_policy, ReportingContractualCutoffPolicy):
            if _utc(now) < contractual_cutoff_at(obligation, selected_policy):
                return None
            if checkpoint_store is None:
                observation = await self._cutoff_observation_ordinal(obligation, observation)
        if first_core is not None and first_core.request is not None:
            request = first_core.request.model_copy(
                update={"deadline_at": _utc(now) + self._offerings.slice_timeout}
            )
        else:
            request = self._build_slice(
                configuration,
                obligation,
                offering_id,
                finality=finality,
                now=now,
                observation=observation,
                constituents=constituents,
                trigger="manual_replay" if manual_observation else "scheduled_poll",
                invocation_id=uuid4().hex if manual_snapshot else None,
            )
            if first_core is not None and first_core.state == "dispatching":
                first_core.request = request.model_copy(deep=True)
        acquisition = pending
        # An explicitly tracked first invocation must survive a failed fetch and restart.
        track_first_manual = manual_snapshot and track_settling and not restate and not revisions
        if (
            checkpoint_store is not None
            and acquisition is None
            and (not manual_snapshot or track_first_manual)
        ):
            acquisition = await self._reserve_observation(
                configuration,
                obligation,
                request,
                ordinal=observation,
                revisions=revisions,
                previous_boundary=checkpoint.provisional_until if checkpoint else None,
                derived_close=derived_close,
            )
            if acquisition is None:
                return None
        if acquisition is not None:
            if (
                isinstance(selected_policy, ReportingStabilizedPolicy)
                and acquisition.legacy_stabilized_close
            ):
                await self._retire_stabilized_acquisition(
                    configuration, obligation, acquisition, now
                )
                return None
            request = acquisition.request(deadline_at=_utc(now) + self._offerings.slice_timeout)
            finality = acquisition.finality
            if acquisition.output_finality and _utc(now) < _utc(
                request.period.source_read_cutoff_at
            ):
                return None
        request = request.model_copy(deep=True)
        full_authority = self._requires_source_authority(obligation)
        if full_authority:
            self._source_authority(obligation, request)
        cancel = asyncio.Event()
        execution = asyncio.create_task(
            self._execute_source(
                configuration,
                request.model_copy(deep=True),
                admitted_constituents=tuple(request.coverage.constituents),
                cancel=cancel,
            )
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(execution),
                timeout=self._offerings.slice_timeout.total_seconds(),
            )
        except asyncio.CancelledError:
            cancel.set()
            await cancel_and_settle(execution)
            raise
        except asyncio.TimeoutError:
            cancel.set()
            # wait_for's own cancellation join can be interrupted by a second
            # cancellation of this producer (notably on Python 3.10). Retain
            # the execution explicitly until even synchronous work has settled.
            await cancel_and_settle(execution)
            turn.slices_failed.append(obligation.reporting_obligation_id)
            await self._schedule_retry(
                obligation,
                turn,
                now=now,
                replayed=manual_replay,
                first_core_delivery=first_core,
            )
            self._note_escalation(
                obligation,
                turn,
                now=now,
                availability_retry=True,
                offering_id=request.offering_id,
            )
            return None

        if isinstance(result, _InlineStorageFailure):
            from adcp.reporting.inline_storage import InlineStorageError

            # Return closed data from the executor task: a raw driver exception
            # must not survive through a context manager or Task wakeup frame.
            if result.code == "RESOURCE_UNAVAILABLE" and first_core is not None:
                first_core.interrupted()
            raise InlineStorageError(result.code)

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
            await self._schedule_retry(
                obligation,
                turn,
                now=now,
                scope=error.scope,
                retry_after_seconds=error.retry_after_seconds,
                blocked=error.retry == "terminal",
                replayed=manual_replay,
                first_core_delivery=first_core,
            )
            self._note_escalation(
                obligation,
                turn,
                now=now,
                availability_retry=error.retry == "retryable",
                offering_id=request.offering_id,
            )
            return None

        manifest = self._verified_manifest(result)
        if request.coverage.expected == "full" and manifest.coverage.status != "full":
            raise LedgerConflictError(
                "MANIFEST_MISMATCH",
                "a full-coverage request cannot complete with partial or missing coverage",
            )
        self._validate_manifest_currency(obligation, manifest)
        if full_authority:
            assert result.response is not None
            if result.response.identity != request.identity:
                raise LedgerConflictError(
                    "MANIFEST_MISMATCH", "source response identity differs from its request"
                )
            self._validate_source_manifest(
                obligation,
                request,
                manifest,
                cutoff_ceiling=now,
                acquisition=acquisition,
            )
        try:
            rows = await self._read_rows(request, manifest, require_staged=full_authority)
        except LedgerConflictError:
            raise
        except Exception:
            if first_core is not None:
                first_core.interrupted()
            raise
        if _covered_subset_publishable(configuration.coverage_requirement, obligation.coverage):
            _require_covered_rows(obligation, rows)
        # Every acquisition: the source's rows stay inside the frozen coverage.
        _require_rows_within_frozen_coverage(
            obligation, rows, package_grain="package" in self._offerings.requested_dimensions
        )
        # ``now`` freezes dispatch/lease/cutoff decisions, not publication.
        # A conforming source can observe finality while acquisition is running.
        # Every successful scheduled read reaches the atomic observation/checkpoint commit.
        published_at = self._clock()
        if _utc(published_at) < _utc(now):
            raise LedgerConflictError(
                "PUBLICATION_TIME_INVALID",
                "producer clock regressed during acquisition; correct the clock before retrying",
            )
        proof = None
        if finality == "official":
            if _utc(manifest.acquired_at) > _utc(published_at):
                raise LedgerConflictError(
                    "PUBLICATION_TIME_INVALID",
                    "source acquisition is later than the publication clock",
                )
            try:
                proof = manifest_finality_proof(obligation, manifest, published_at)
            except LedgerConflictError as error:
                if (
                    not isinstance(selected_policy, ReportingContractualCutoffPolicy)
                    or error.code != "FINALITY_POLICY_UNPROVEN"
                ):
                    raise
                # A valid, stale seal cannot become a qualifying cutoff observation.
                # Retire its execution identity only after staged verification.
                turn.slices_failed.append(obligation.reporting_obligation_id)
                await self._schedule_retry(
                    obligation, turn, now=published_at, replayed=manual_replay
                )
                await self._retire_acquisition(
                    obligation,
                    ordinal=observation,
                    checked_at=published_at,
                    checkpoint_store=checkpoint_store,
                    checkpoint=checkpoint,
                )
                self._note_escalation(
                    obligation,
                    turn,
                    now=published_at,
                    availability_retry=True,
                    offering_id=request.offering_id,
                )
                return None
        untracked_restatement = (
            finality == "snapshot" and manual_observation and acquisition is None
        )
        try:
            committed = await self._commit_revision_from_manifest(
                obligation,
                manifest,
                rows=rows,
                finality=finality,
                finality_proof=proof,
                now=published_at,
                turn=turn,
                acquisition=acquisition,
                verified_source_rows=True,
                first_core_obligation=first_core_obligation,
                suppress_untracked_unchanged=untracked_restatement,
                admit_tracked_stabilization=admit_tracked_stabilization,
            )
        except LedgerConflictError as error:
            if error.code == "OFFICIAL_REVISION_TERMINAL" and untracked_restatement:
                return None
            if (
                error.code == "STABILIZATION_ORIGIN_CONFLICT"
                and acquisition is not None
                and isinstance(selected_policy, ReportingStabilizedPolicy)
                and (acquisition.policy.until_stabilized or acquisition.is_scheduled_snapshot)
                and (
                    admit_tracked_stabilization
                    or self._uses_stabilization_schedule(obligation, acquisition.policy)
                )
            ):
                # A sealed unchanged read cannot repair its predecessor's conflicting origin.
                retired = await self._retire_stabilized_acquisition(
                    configuration, obligation, acquisition, published_at
                )
                if retired and acquisition.is_scheduled_snapshot:
                    await self._schedule_retry(
                        obligation,
                        turn,
                        now=published_at,
                        retry_after_seconds=acquisition.policy.cadence.total_seconds(),
                    )
            raise
        if first_core is not None:
            first_core.state = "consumed"
        await self._clear_retry(obligation, turn=turn, now=published_at)
        return committed

    @staticmethod
    def _cutoff_ordinal_key(obligation: ReportingObligationRecord) -> str:
        return f"cutoff:{obligation.account_id}:{obligation.reporting_obligation_id}"

    async def _retire_stabilized_acquisition(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        acquisition: ProvisionalAcquisition,
        now: datetime,
    ) -> bool:
        """Advance an unusable reservation only while it still owns the observation cursor."""
        store = self._restatement_store()
        request = acquisition.request()
        owner = dict(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        async with self._official_publication_lock(stabilized=True).source_publication(
            obligation.account_id
        ):
            self._check_source_authorization(
                configuration, request.offering_id, tuple(request.coverage.constituents)
            )
            checkpoint = await store.get_restatement_checkpoint(**owner)
            if checkpoint is not None and checkpoint.next_observation != acquisition.ordinal:
                return False
            if any(r.finality == "official" for r in await self._store.list_revisions(**owner)):
                return False
            await store.record_restatement_checkpoint(
                RestatementCheckpoint.after_acquisition(
                    acquisition,
                    now,
                    checkpoint.provisional_until if checkpoint else None,
                    checkpoint,
                    retired_stabilized=True,
                )
            )
            return True

    async def _retire_acquisition(
        self,
        obligation: ReportingObligationRecord,
        *,
        ordinal: int,
        checked_at: datetime,
        checkpoint_store: RestatementCheckpointStore | None,
        checkpoint: RestatementCheckpoint | None,
    ) -> None:
        """Advance the acquisition cursor without publishing a revision or observation."""
        if checkpoint_store is not None:
            await checkpoint_store.record_restatement_checkpoint(
                RestatementCheckpoint(
                    obligation.account_id,
                    obligation.reporting_obligation_id,
                    checked_at,
                    ordinal + 1,
                    checkpoint.provisional_until if checkpoint else None,
                )
            )
        else:
            # Core ledgers have a durable retry schedule but no observation cursor.
            # Keep the attempt number independent from transient source backoff.
            await self._record_retry(
                RetryScheduleEntry(
                    self._cutoff_ordinal_key(obligation),
                    checked_at,
                    ordinal + 1,
                    recorded_at=checked_at,
                )
            )

    async def _cutoff_observation_ordinal(
        self, obligation: ReportingObligationRecord, ordinal: int
    ) -> int:
        retired = await self._get_retry(self._cutoff_ordinal_key(obligation))
        return max(ordinal, retired.attempt) if retired else ordinal

    async def _claim_first_core_delivery(
        self,
        obligation: ReportingObligationRecord,
        *,
        now: datetime,
        trigger: Literal["scheduled", "manual"] | None,
    ) -> _FirstCoreDelivery | None:
        """Claim the first attempt or let the scheduler resume its own recorded retry."""
        key = (obligation.generation_key, obligation.reporting_obligation_id)
        state = self._first_core_deliveries.get(key)
        if isinstance(state, _FirstCoreDelivery):
            if trigger != "scheduled" or (
                state.retry is not None and _utc(now) < _utc(state.retry.retry_not_before)
            ):
                return None
            retained = await self._get_retry(self._retry_keys(obligation)[0])
            if self._first_core_deliveries.get(key) is not state:
                return None
            if retained != state.retry:
                self._first_core_deliveries[key] = "attempted"
                return None
            delivery = replace(state, state="dispatching")
        elif state == "ready":
            delivery = _FirstCoreDelivery("dispatching" if trigger is not None else "consumed")
        else:
            return None
        self._first_core_deliveries[key] = "attempted"
        return delivery

    async def _retained_source_configuration(
        self, obligation: ReportingObligationRecord
    ) -> ReportingConfiguration:
        configuration = next(
            (
                candidate
                for candidate in await self._store.list_configurations(
                    caller=ReportingDeliveryPrincipal(
                        obligation.account_id, obligation.consumer_id
                    ),
                    delivery_config_ids=[obligation.delivery_config_id],
                )
                if candidate.generation_key == obligation.generation_key
            ),
            None,
        )
        if configuration is None:
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "source generation is unavailable")
        return configuration

    def _optional_restatement_store(self) -> RestatementCheckpointStore | None:
        explicit = all(
            inspect.getattr_static(self._store, name, None) is not None
            for name in ("get_restatement_checkpoint", "record_restatement_checkpoint")
        )
        if explicit and isinstance(self._store, RestatementCheckpointStore):
            return self._store
        return None

    def _observation_checkpoint_store(self) -> RestatementCheckpointStore | None:
        if self._optional_observation_store() is None:
            return None
        return self._optional_restatement_store()

    def _optional_observation_store(self) -> ProvisionalObservationPublicationStore | None:
        explicit = all(
            inspect.getattr_static(self._store, name, None) is not None
            for name in (
                "reserve_provisional_acquisition",
                "get_provisional_observation",
                "commit_provisional_observation",
            )
        )
        if explicit and isinstance(self._store, ProvisionalObservationPublicationStore):
            return self._store
        return None

    def _observation_history_store(self) -> ProvisionalObservationStore:
        store = self._observation_store()
        if not _implements_store_method(
            store, ProvisionalObservationStore.list_provisional_observations
        ) or not isinstance(store, ProvisionalObservationStore):
            raise LedgerConflictError(
                "PROVISIONAL_OBSERVATION_HISTORY_NOT_SUPPORTED",
                "stabilized finality requires explicitly implemented bounded observation history",
            )
        return store

    def _optional_publication_store(self) -> ReportingSourcePublicationStore | None:
        store = self._store
        if _implements_store_method(
            store, ReportingSourcePublicationStore.source_publication
        ) and isinstance(store, ReportingSourcePublicationStore):
            return store
        return None

    def _official_publication_lock(
        self, *, stabilized: bool = False
    ) -> ReportingSourcePublicationStore:
        store = self._optional_publication_store()
        if store is None:
            raise LedgerConflictError(
                (
                    "STABILIZATION_PUBLICATION_NOT_SUPPORTED"
                    if stabilized
                    else "OFFICIAL_PUBLICATION_NOT_SUPPORTED"
                ),
                "official finality requires an account publication lock",
            )
        return store

    async def _check_official_publication_lock(
        self,
        route: ReportingConfiguration | ReportingObligationRecord,
        *,
        official: bool = False,
        frozen: ProvisionalPolicy | None = None,
        check_generation: bool = True,
        scheduled_ordinal: int | None = None,
        first_core_obligation: str | None = None,
    ) -> None:
        policy = route.definition.finality_policy if route.definition else None
        if policy is None or self._optional_publication_store() is not None:
            return
        reservation = None
        if scheduled_ordinal is not None and isinstance(route, ReportingObligationRecord):
            reservation = (route.reporting_obligation_id, scheduled_ordinal)
        if (
            official
            or self._official_close_requested(route)
            or (
                frozen is not None
                and (frozen.official_close_lag is not None or frozen.until_stabilized)
            )
            or (
                check_generation
                and await self._has_retained_official_close(
                    route.generation_key,
                    publishing=True,
                    scheduled_reservation=reservation,
                    first_core_obligation=first_core_obligation,
                )
            )
        ):
            self._official_publication_lock(
                stabilized=isinstance(policy, ReportingStabilizedPolicy)
            )

    def _stabilization_store(self) -> StabilizationStore:
        history = self._observation_history_store()
        self._restatement_store()
        self._official_publication_lock(stabilized=True)
        for stub in (
            ProvisionalObservationPublicationStore.reserve_provisional_acquisition,
            ProvisionalObservationPublicationStore.get_provisional_observation,
            ProvisionalObservationPublicationStore.commit_provisional_observation,
            RestatementCheckpointStore.get_restatement_checkpoint,
            RestatementCheckpointStore.record_restatement_checkpoint,
            SnapshotOriginStore.verify_snapshot_origin,
            PendingProvisionalAcquisitionStore.get_provisional_acquisition,
            StabilizationScheduleStore.get_stabilization_schedule,
        ):
            if not _implements_store_method(history, stub):
                raise LedgerConflictError(
                    "STABILIZATION_STORAGE_NOT_SUPPORTED",
                    f"stabilized finality requires explicitly implemented {stub.__name__}",
                )
        assert isinstance(history, StabilizationStore)
        return history

    async def _reserve_observation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        request: ReportingSourceSliceRequestV1,
        *,
        ordinal: int,
        revisions: Sequence[ReportingRevisionRecord],
        previous_boundary: datetime | None,
        derived_close: bool,
    ) -> ProvisionalAcquisition | None:
        store = self._observation_store()
        latest = await store.get_provisional_observation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        scheduled = await self._latest_scheduled_snapshot(obligation, latest)
        selected = obligation.definition.finality_policy if obligation.definition else None
        if scheduled is not None:
            previous_boundary = scheduled.provisional_until
        frozen_policy = (
            scheduled.acquisition.policy
            if scheduled is not None
            else self._checkpoint_snapshot_policy(latest, previous_boundary)
        )
        if frozen_policy is None:
            if latest is not None:
                previous_boundary = None
            policy = self._settling_policy(configuration, obligation, cutoff_reservation=True)
            # An authoritative-only acquisition has no further due observation.
            frozen_policy = (
                ProvisionalPolicy(
                    policy.restatement_window, policy.restatement_cadence, policy.official_close_lag
                )
                if policy is not None
                else ProvisionalPolicy(
                    timedelta(0), iso_duration_to_timedelta(configuration.schedule.period_duration)
                )
            )
        if isinstance(selected, ReportingStabilizedPolicy) and self._uses_stabilization_schedule(
            obligation, frozen_policy
        ):
            stabilization_history_limit(selected, frozen_policy.cadence)
            frozen_policy = replace(frozen_policy, until_stabilized=True)
        await self._check_official_publication_lock(
            obligation,
            official=derived_close or request.publication_class == "AUTHORITATIVE",
            frozen=frozen_policy,
            scheduled_ordinal=ordinal if request.trigger == "scheduled_poll" else None,
        )
        if self._requires_source_authority(obligation):
            self._source_authority(obligation, request)
            self._check_source_authorization(
                configuration, request.offering_id, tuple(request.coverage.constituents)
            )
        candidate = ProvisionalAcquisition(
            request.model_dump_json(),
            ordinal,
            frozen_policy,
            self._current_snapshot_leaf(revisions),
            previous_boundary,
            output_finality="official" if derived_close else None,
        )
        if frozen_policy.until_stabilized:
            async with self._official_publication_lock(stabilized=True).source_publication(
                obligation.account_id
            ):
                current = await self._store.list_revisions(
                    account_id=obligation.account_id,
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                if any(revision.finality == "official" for revision in current):
                    return None
                acquisition = await store.reserve_provisional_acquisition(candidate)
        else:
            acquisition = await store.reserve_provisional_acquisition(candidate)
        if not acquisition.binds(obligation):
            raise LedgerConflictError(
                "OBSERVATION_CONFLICT",
                "frozen source acquisition does not bind the obligation's consumer; "
                "operator reconciliation and a new configuration generation are required",
            )
        # The returned ordinal may already have completed while the reservation was in flight.
        await self._check_official_publication_lock(
            obligation,
            official=acquisition.finality == "official",
            frozen=acquisition.policy,
            scheduled_ordinal=acquisition.ordinal if acquisition.is_scheduled_snapshot else None,
        )
        return acquisition

    def _requires_source_authority(self, obligation: ReportingObligationRecord) -> bool:
        from adcp.reporting.production.contracts import ReportingProductionSource

        return bool(obligation.definition and obligation.definition.finality_policy) or isinstance(
            self._source, ReportingProductionSource
        )

    def _source_authority(
        self, obligation: ReportingObligationRecord, request: ReportingSourceSliceRequestV1
    ) -> ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1:
        from adcp.reporting.conformance import (
            ReportingSourceConformanceError,
            _validate_request_against_capabilities,
        )

        self._validate_definition_binding(obligation, request.contract)
        try:
            offering = _validate_request_against_capabilities(self._source.capabilities, request)
        except ReportingSourceConformanceError as error:
            raise LedgerConflictError(
                "MANIFEST_MISMATCH", "request contradicts its declared source authority"
            ) from error
        if (
            obligation.definition
            and obligation.definition.finality_policy
            and not _supports_official_source_formats(offering)
        ):
            raise LedgerConflictError(
                "UNSUPPORTED_SOURCE_FORMAT",
                "official producer requires JSON or NDJSON with gzip or no compression",
            )
        return offering

    def _validate_source_manifest(
        self,
        obligation: ReportingObligationRecord,
        request: ReportingSourceSliceRequestV1,
        manifest: SourceBatchManifestV1,
        *,
        cutoff_ceiling: datetime,
        acquisition: ProvisionalAcquisition | None,
    ) -> None:
        from adcp.reporting.conformance import (
            ReportingSourceConformanceError,
            _validate_manifest_against_request,
        )

        cutoff = manifest.period.source_read_cutoff_at
        if _utc(cutoff) > _utc(cutoff_ceiling):
            raise LedgerConflictError(
                "MANIFEST_MISMATCH", "source cutoff exceeds the requested read"
            )
        if acquisition is not None and not self._matches_reservation(request, acquisition):
            raise LedgerConflictError(
                "MANIFEST_MISMATCH", "source acquisition differs from its frozen request"
            )
        # An immutable authoritative seal can predate the reservation for this
        # execution key. A reserved provisional read fixes its exact cutoff.
        if cutoff != request.period.source_read_cutoff_at:
            if _utc(cutoff) > _utc(request.period.source_read_cutoff_at) or (
                acquisition is not None and request.publication_class != "AUTHORITATIVE"
            ):
                raise LedgerConflictError(
                    "MANIFEST_MISMATCH", "source cutoff differs from its frozen request"
                )
            request = request.model_copy(
                update={
                    "identity": request.identity.model_copy(
                        update={"run_id": _source_run_id(cutoff)}
                    ),
                    "period": request.period.model_copy(update={"source_read_cutoff_at": cutoff}),
                }
            )
        try:
            offering = (
                self._source_authority(obligation, request)
                if self._requires_source_authority(obligation)
                else self._source.capabilities.offering(request.offering_id)
            )
            _validate_manifest_against_request(offering, request, manifest)
        except ReportingSourceConformanceError as error:
            raise LedgerConflictError(
                "MANIFEST_MISMATCH", "manifest does not bind the configured source request"
            ) from error

    @staticmethod
    def _matches_reservation(
        request: ReportingSourceSliceRequestV1, acquisition: ProvisionalAcquisition
    ) -> bool:
        return request == acquisition.request().model_copy(
            update={"deadline_at": request.deadline_at}
        )

    async def _commit_revision_from_manifest(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        *,
        rows: Sequence[dict[str, Any]],
        finality: str,
        finality_proof: FinalityProof | None,
        now: datetime | None,
        turn: WorkerTurn | None,
        acquisition: ProvisionalAcquisition | None,
        verified_source_rows: bool,
        stabilized_publication: _StabilizedPublication | None = None,
        first_core_obligation: str | None = None,
        suppress_untracked_unchanged: bool = False,
        admit_tracked_stabilization: bool = True,
    ) -> ReportingRevisionRecord:
        obligation = await self._stored_obligation(obligation)
        rows = subscription_rows(obligation, rows)
        selected_policy = obligation.definition.finality_policy if obligation.definition else None
        # A matching historical observation already retained its own route.
        # Check generation-wide evidence before admitting any fresh snapshot.
        scheduled_ordinal = None
        if verified_source_rows and acquisition is not None and acquisition.is_scheduled_snapshot:
            scheduled_ordinal = acquisition.ordinal
        await self._check_official_publication_lock(
            obligation,
            official=finality == "official",
            frozen=acquisition.policy if acquisition else None,
            check_generation=acquisition is None or verified_source_rows,
            scheduled_ordinal=scheduled_ordinal,
            first_core_obligation=first_core_obligation,
        )
        origin_store: SnapshotOriginStore | None = None
        if finality == "snapshot" and acquisition is not None:
            if acquisition.requires_snapshot_origin(
                obligation,
                explicitly_tracked=(
                    admit_tracked_stabilization
                    or obligation.generation_key in self._recovered_stabilization
                ),
            ):
                origin_store = self._stabilization_store()
            elif isinstance(
                selected_policy, ReportingStabilizedPolicy
            ) and _implements_store_method(self._store, SnapshotOriginStore.verify_snapshot_origin):
                assert isinstance(self._store, SnapshotOriginStore)
                origin_store = self._store
        self._validate_manifest_currency(obligation, manifest)
        if any(cell.status == "unsupported" for cell in manifest.metric_availability):
            try:
                offering = self._source.capabilities.offering(manifest.offering_id)
                _validate_metric_applicability(offering.metrics, manifest.metric_availability)
            except (KeyError, ValueError):
                raise LedgerConflictError(
                    "MANIFEST_MISMATCH",
                    "unsupported cells require partial metric support with a reason",
                ) from None
        sample_after_staging = now is None
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

        if acquisition is not None and finality == "snapshot":
            supersedes = acquisition.predecessor_revision_id

        control_totals = tuple((total.name, total.value) for total in manifest.control_totals)
        revision_id = f"rpr_{manifest.publication_id[4:44]}"
        stabilized = finality == "official" and isinstance(
            selected_policy, ReportingStabilizedPolicy
        )
        if stabilized and selected_policy is not None:
            digest = hashlib.sha256(
                canonical_json_utf8_v1(
                    [manifest.publication_id, "official", selected_policy.finality_policy_id]
                )
            ).hexdigest()
            revision_id = f"rpr_{digest[:40]}"
        prior = next((item for item in existing if item.reporting_revision_id == revision_id), None)
        # New revisions bind protocol-shape totals. A replay of a retained
        # pair-hashed revision must reproduce its original binding, because
        # retained evidence is never rehashed.
        typed_totals = (
            _typed_control_totals(obligation, manifest)
            if control_totals and (prior is None or prior.managed_control_totals is not None)
            else None
        )
        retained_official = prior is not None and prior.finality == finality == "official"
        candidate_proof = finality_proof if finality == "official" else None
        created_at = (
            now
            if prior is None or (retained_official and finality_proof is not None)
            else prior.created_at
        )
        if prior is not None:
            # Still reconstruct and verify the supplied content below. Merely
            # finding the ID must not bypass immutable-content validation.
            # On replay this retained parent wins over the acquisition's
            # predecessor, just as the retained creation time wins over now.
            supersedes = prior.supersedes_reporting_revision_id
        if (
            _utc(manifest.acquired_at) > _utc(now)
            or _utc(manifest.observed_at) > _utc(created_at)
            or _utc(manifest.finality_evidence.observed_at) > _utc(created_at)
        ):
            raise LedgerConflictError(
                "PUBLICATION_TIME_INVALID",
                "source observation or finality is outside the publication time bounds; "
                "check source evidence and the producer clock before retrying",
            )
        if finality != "official":
            finality_proof = None
            if manifest.publication_class != "PROVISIONAL_SNAPSHOT":
                raise LedgerConflictError(
                    "OBSERVATION_CONFLICT", "a snapshot requires a provisional source manifest"
                )
            if acquisition is not None:
                request = acquisition.request()
                if (
                    acquisition.finality != finality
                    or manifest.identity != request.identity
                    or manifest.publication_class != request.publication_class
                    or manifest.offering_id != request.offering_id
                    or manifest.period != request.period
                ):
                    raise LedgerConflictError(
                        "OBSERVATION_CONFLICT",
                        "snapshot differs from the reserved source acquisition",
                    )
                if not verified_source_rows and (selected_policy is not None or prior is not None):
                    replay = await self._retained_snapshot_replay(
                        obligation, manifest, rows, acquisition, prior, now, origin_store
                    )
                    if replay is not None:
                        return replay
                    await self._check_official_publication_lock(
                        obligation, frozen=acquisition.policy
                    )
                    self._validate_source_manifest(
                        obligation,
                        request,
                        manifest,
                        cutoff_ceiling=now,
                        acquisition=acquisition,
                    )
                    rows = await self._verify_supplied_rows(
                        request, manifest, rows, retained=prior is not None
                    )
        elif prior is not None and retained_official and finality_proof is None:
            # A retained immutable publication may be checked or replayed while
            # another acquisition is reserved. Its replay cannot advance that work.
            acquisition = None
            assert prior.finality_policy_id and prior.finality_basis and prior.finalized_at
            finality_proof = FinalityProof(
                prior.finality_policy_id, prior.finality_basis, prior.finalized_at
            )
        elif finality_proof is None:
            raise LedgerConflictError(
                "FINALITY_POLICY_UNPROVEN", "official finality requires policy proof"
            )
        else:
            if manifest.offering_id != self._offerings.offering_for(
                _source_finality(obligation, "official")
            ):
                raise LedgerConflictError(
                    "MANIFEST_MISMATCH", "official proof must use the selected source offering"
                )
            proof_anchor = (
                finality_proof.finalized_at
                if sample_after_staging and finality_proof.basis == "contractual_cutoff"
                else created_at
            )
            if _utc(proof_anchor) > _utc(created_at):
                raise LedgerConflictError(
                    "PUBLICATION_TIME_INVALID", "proof is later than the publication clock"
                )
            if stabilized:
                # A single source publication cannot prove stabilization. The account
                # publication lock protects the retained-history check at commit.
                finality_proof.validate_time(obligation, proof_anchor)
                if acquisition is not None:
                    raise LedgerConflictError(
                        "OBSERVATION_CONFLICT",
                        "commit the snapshot observation before stabilization",
                    )
            else:
                finality_proof.validate(obligation, manifest, proof_anchor)
            if stabilized or (retained_official and not verified_source_rows):
                acquisition = None
            elif not verified_source_rows:
                acquisition, rows = await self._verify_direct_official_publication(
                    obligation,
                    manifest,
                    rows,
                    acquisition,
                    revisions=existing,
                    created_at=created_at,
                )
        if (
            sample_after_staging
            and not verified_source_rows
            and prior is None
            and (acquisition is not None or finality == "official")
        ):
            completed_at = self._clock()
            if _utc(completed_at) < _utc(now):
                raise LedgerConflictError(
                    "PUBLICATION_TIME_INVALID",
                    "producer clock regressed during staged verification",
                )
            created_at = now = completed_at
            if finality == "official" and not stabilized:
                finality_proof = manifest_finality_proof(obligation, manifest, created_at)
        if stabilized and isinstance(selected_policy, ReportingStabilizedPolicy):
            if stabilized_publication is None:
                scheduled = await self._scheduled_snapshot_history(obligation, 1)
                stabilized_publication = (
                    await self._stabilized_publication(obligation, selected_policy, scheduled[-1])
                    if scheduled
                    else None
                )
            if (
                stabilized_publication is None
                or stabilized_publication.proof != finality_proof
                or stabilized_publication.manifest != manifest
                or (
                    rows is not stabilized_publication.rows
                    and rfc8785.dumps(list(rows))
                    != rfc8785.dumps(list(stabilized_publication.rows))
                )
            ):
                raise LedgerConflictError(
                    "REVISION_IMMUTABLE" if retained_official else "FINALITY_POLICY_UNPROVEN",
                    "the retained history does not prove the official evidence",
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
                control_total_evidence=typed_totals,
            ),
            row_count=manifest.row_count,
            control_totals=control_totals,
            managed_control_totals=typed_totals,
            observed_at=manifest.observed_at,
            data_through=manifest.data_through,
            created_at=created_at,
            supersedes_reporting_revision_id=supersedes,
            finality_basis=finality_proof.basis if finality_proof else None,
            finality_policy_id=finality_proof.finality_policy_id if finality_proof else None,
            finalized_at=finality_proof.finalized_at if finality_proof else None,
            source_publication_id=manifest.publication_id,
            source_manifest_sha256=manifest.content_fingerprint.split(":", 1)[-1],
        )
        if self._revision_verifier is not None:
            from adcp.reporting.materializer.publication import verified_publication

            # Pure validation of the source rows and totals; it needs no lock, and the
            # bytes staged below must be the bytes of the revision that commits.
            revision = verified_publication(self._revision_verifier, obligation, revision, rows)
        # An object-bound revision uploads its chunks before the ledger transaction,
        # never inside it, so a slow store cannot hold the account lock.
        staging = self._store if isinstance(self._store, ReportingRowStagingStore) else None
        retained = None
        if (
            finality == "snapshot"
            and supersedes
            and (suppress_untracked_unchanged or acquisition is not None)
        ):
            predecessor = await self._store.get_revision(
                account_id=obligation.account_id,
                reporting_revision_id=supersedes,
            )
            if predecessor is None:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "snapshot predecessor is missing")
            predecessor_content_sha256 = revision_content_sha256(
                reporting_revision_id=predecessor.reporting_revision_id,
                row_count=revision.row_count,
                control_totals=revision.control_totals,
                reporting_rows=rows,
                control_total_evidence=(
                    revision.managed_control_totals
                    if predecessor.managed_control_totals is not None
                    else None
                ),
            )
            if (
                predecessor.readable
                and predecessor.source_manifest_sha256
                == manifest.content_fingerprint.split(":", 1)[-1]
                and predecessor.revision_content_sha256 == predecessor_content_sha256
            ):
                retained = predecessor
                if acquisition is None:
                    return retained
        staged = staging is not None and bool(await staging.stage_revision_rows(revision, rows))
        try:
            committed, replayed = await self._commit_publication(
                obligation,
                manifest,
                revision,
                rows,
                finality,
                now,
                acquisition,
                candidate_proof=candidate_proof,
                stabilized_publication=stabilized_publication,
                origin_store=origin_store,
                retained_revision_id=(
                    retained.reporting_revision_id if retained is not None else None
                ),
            )
        finally:
            if staged and staging is not None:
                staging.discard_staged_revision_rows(revision)
        if (
            prior is None
            and not replayed
            and (
                retained is None
                or committed.reporting_revision_id != retained.reporting_revision_id
            )
        ):
            turn.revisions_committed.append(committed.reporting_revision_id)
        return committed

    async def _retained_snapshot_replay(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        rows: Sequence[dict[str, Any]],
        acquisition: ProvisionalAcquisition,
        prior: ReportingRevisionRecord | None,
        now: datetime,
        origin_store: SnapshotOriginStore | None,
    ) -> ReportingRevisionRecord | None:
        store = self._observation_store()
        latest = await store.get_provisional_observation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        if latest is None or latest.acquisition.ordinal < acquisition.ordinal:
            return None
        revision_id = (
            prior.reporting_revision_id
            if prior is not None
            else acquisition.predecessor_revision_id
        )
        if revision_id is None:
            return None
        observation = ProvisionalObservation.from_manifest(acquisition, revision_id, now, manifest)
        is_latest = latest.acquisition.ordinal == acquisition.ordinal
        if is_latest and (
            latest.acquisition != acquisition
            or json.loads(latest.manifest_json) != json.loads(observation.manifest_json)
            or latest.revision_id != revision_id
        ):
            await self._verify_supplied_rows(
                acquisition.request(), manifest, rows, retained=prior is not None
            )
            raise LedgerConflictError("OBSERVATION_CONFLICT", "retained snapshot binding differs")
        if not acquisition.binds(obligation):
            raise LedgerConflictError("OBSERVATION_CONFLICT", "retained snapshot owner differs")
        async with self._revision_publication(obligation, manifest, official=False):
            revision = await self._store.get_revision(
                account_id=obligation.account_id, reporting_revision_id=revision_id
            )
            if (
                revision is None
                or revision.finality != "snapshot"
                or revision.reporting_obligation_id != obligation.reporting_obligation_id
            ):
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "retained snapshot is missing")
            source_id = revision.source_publication_id
            if source_id is None or revision_id != f"rpr_{source_id[4:44]}":
                raise LedgerConflictError("REVISION_IMMUTABLE", "retained snapshot origin differs")
            if prior is not None and (
                revision.source_publication_id != manifest.publication_id
                or revision.observed_at != manifest.observed_at
                or revision.data_through != manifest.data_through
                or revision.supersedes_reporting_revision_id != acquisition.predecessor_revision_id
            ):
                raise LedgerConflictError(
                    "REVISION_IMMUTABLE", "retained source publication differs"
                )
            if (
                revision.row_count != manifest.row_count
                or revision.control_totals
                != tuple((total.name, total.value) for total in manifest.control_totals)
                or (
                    revision.managed_control_totals is not None
                    and revision.managed_control_totals
                    != _typed_control_totals(obligation, manifest)
                )
            ):
                raise LedgerConflictError("REVISION_IMMUTABLE", "retained source totals differ")
            if is_latest:
                validate_retained_provisional_observation(revision, latest, rows)
            else:
                validate_snapshot_rows(revision, rows)
            if origin_store is not None:
                await self._verify_tracked_snapshot_origin(
                    obligation, acquisition, origin_store, revision
                )
            elif prior is None and _implements_store_method(
                self._store, SnapshotOriginStore.verify_snapshot_origin
            ):
                assert isinstance(self._store, SnapshotOriginStore)
                await self._store.verify_snapshot_origin(revision)
            if is_latest and (prior is not None or latest.revision_publication_id is not None):
                return revision
            # The atomic publisher reads this exact ordinal, its stored witness and revision.
            try:
                committed = await store.commit_provisional_observation(observation, revision, rows)
            except LedgerConflictError as error:
                if error.code != "OBSERVATION_CONFLICT":
                    raise
                return None
            if committed.source_publication_id != source_id:
                raise LedgerConflictError("REVISION_IMMUTABLE", "retained snapshot origin differs")
            return committed

    async def _verify_direct_official_publication(
        self,
        obligation: ReportingObligationRecord,
        manifest: SourceBatchManifestV1,
        rows: Sequence[dict[str, Any]],
        acquisition: ProvisionalAcquisition | None,
        *,
        revisions: Sequence[ReportingRevisionRecord],
        created_at: datetime,
    ) -> tuple[ProvisionalAcquisition | None, list[dict[str, Any]]]:
        configuration = await self._retained_source_configuration(obligation)
        constituents = await self._admitted_source_constituents(configuration, obligation)
        constituents = self._check_source_authorization(
            configuration, manifest.offering_id, constituents
        )
        ordinal = len(revisions)
        checkpoint = None
        checkpoint_store = self._observation_checkpoint_store()
        if checkpoint_store is not None:
            checkpoint = await checkpoint_store.get_restatement_checkpoint(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            if checkpoint is not None:
                ordinal = checkpoint.next_observation
        elif obligation.definition and isinstance(
            obligation.definition.finality_policy, ReportingContractualCutoffPolicy
        ):
            ordinal = await self._cutoff_observation_ordinal(obligation, ordinal)
        pending = await self._pending_acquisition(obligation, ordinal=ordinal)
        if pending is not None:
            if (
                acquisition is not None and acquisition != pending
            ) or pending.finality != "official":
                raise LedgerConflictError(
                    "OBSERVATION_CONFLICT",
                    "finish the reserved acquisition before publishing this manifest",
                )
            acquisition = pending
        reserved = acquisition.request() if acquisition is not None else None
        request = self._build_slice(
            configuration,
            obligation,
            manifest.offering_id,
            finality="official",
            now=(
                reserved.period.source_read_cutoff_at
                if reserved
                else manifest.period.source_read_cutoff_at
            ),
            constituents=constituents,
            observation=ordinal,
            trigger=reserved.trigger if reserved else "manual_replay",
        )
        if acquisition is not None and acquisition.finality != "official":
            raise LedgerConflictError("OBSERVATION_CONFLICT", "reserved output finality differs")
        self._validate_source_manifest(
            obligation, request, manifest, cutoff_ceiling=created_at, acquisition=acquisition
        )
        staged_rows = await self._verify_supplied_rows(request, manifest, rows)
        if checkpoint_store is not None:
            # Staged reads release the ledger lock. Reserve the verified request before
            # publication so an ordinary snapshot that won meanwhile is never skipped.
            winner = await self._reserve_observation(
                configuration,
                obligation,
                request,
                ordinal=ordinal,
                revisions=revisions,
                previous_boundary=checkpoint.provisional_until if checkpoint else None,
                derived_close=request.publication_class == "PROVISIONAL_SNAPSHOT",
            )
            if (
                winner is None
                or (acquisition is not None and acquisition != winner)
                or winner.finality != "official"
                or not self._matches_reservation(request, winner)
            ):
                raise LedgerConflictError(
                    "OBSERVATION_CONFLICT", "another source acquisition won the reservation"
                )
            acquisition = winner
        return acquisition, staged_rows

    async def _verify_supplied_rows(
        self,
        request: ReportingSourceSliceRequestV1,
        manifest: SourceBatchManifestV1,
        rows: Sequence[dict[str, Any]],
        *,
        retained: bool = False,
    ) -> list[dict[str, Any]]:
        staged_rows = await self._read_rows(request, manifest, require_staged=True)
        if rfc8785.dumps(staged_rows) != rfc8785.dumps(list(rows)):
            raise LedgerConflictError(
                "REVISION_IMMUTABLE" if retained else "STAGED_OBJECT_MISMATCH",
                "supplied rows differ from verified staged objects",
            )
        return staged_rows

    def _uses_stabilization_schedule(
        self, obligation: ReportingObligationRecord, policy: ProvisionalPolicy
    ) -> bool:
        return (
            obligation.required_finality == "official"
            or policy.until_stabilized
            or policy.official_close_lag is not None
            or obligation.generation_key in self._recovered_stabilization
        )

    async def _verify_tracked_snapshot_origin(
        self,
        obligation: ReportingObligationRecord,
        acquisition: ProvisionalAcquisition,
        witness: SnapshotOriginStore,
        revision: ReportingRevisionRecord,
        *,
        retained_revision_id: str | None = None,
    ) -> None:
        if retained_revision_id is not None:
            predecessor = await self._store.get_revision(
                account_id=obligation.account_id, reporting_revision_id=retained_revision_id
            )
            if predecessor is None:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "snapshot predecessor is missing")
            if not predecessor.readable:
                # The atomic publisher can use the fresh snapshot when the
                # predecessor loses readability during staging.
                await witness.verify_snapshot_origin(revision)
            revision = predecessor
        await witness.verify_snapshot_origin(revision)
        if revision.reporting_revision_id != acquisition.predecessor_revision_id:
            return
        selected = obligation.definition.finality_policy if obligation.definition else None
        if isinstance(selected, ReportingStabilizedPolicy) and self._uses_stabilization_schedule(
            obligation, acquisition.policy
        ):
            limit = stabilization_history_limit(selected, acquisition.policy.cadence)
            for observed in await self._scheduled_snapshot_history(obligation, limit):
                if observed.revision_id == revision.reporting_revision_id:
                    validate_snapshot_origin(revision, observed.snapshot_origin)

    async def _official_replay_revision(
        self,
        obligation: ReportingObligationRecord,
        candidate: ReportingRevisionRecord,
        retained: ReportingRevisionRecord,
        manifest: SourceBatchManifestV1,
        acquisition: ProvisionalAcquisition | None,
        *,
        proved_candidate: bool,
    ) -> ReportingRevisionRecord:
        """Compare official source evidence inside the account publication lock."""
        observation = None
        policy = obligation.definition.finality_policy if obligation.definition else None
        if isinstance(policy, ReportingStabilizedPolicy):
            scheduled = await self._scheduled_snapshot_history(obligation, 1)
            observation = scheduled[-1] if scheduled else None
        else:
            store = self._optional_observation_store()
            if store is not None:
                observation = await store.get_provisional_observation(
                    account_id=retained.account_id,
                    reporting_obligation_id=retained.reporting_obligation_id,
                )
                if (
                    observation is not None
                    and observation.revision_id != retained.reporting_revision_id
                ):
                    observation = None
        if observation is None:
            if proved_candidate or isinstance(policy, ReportingStabilizedPolicy):
                raise LedgerConflictError(
                    "REVISION_IMMUTABLE", "the winning official source evidence is unavailable"
                )
            if isinstance(policy, (ReportingSourceFinalPolicy, ReportingContractualCutoffPolicy)):
                try:
                    proof = manifest_finality_proof(obligation, manifest, retained.created_at)
                except LedgerConflictError as error:
                    raise LedgerConflictError(
                        "REVISION_IMMUTABLE",
                        "the manifest contradicts the retained source finality",
                    ) from error
                if (
                    manifest.offering_id
                    != self._offerings.offering_for(finality_policy_source_class(policy))
                    or proof.finality_policy_id != retained.finality_policy_id
                    or proof.basis != retained.finality_basis
                    or proof.finalized_at != retained.finalized_at
                ):
                    raise LedgerConflictError(
                        "REVISION_IMMUTABLE", "official source evidence differs"
                    )
        elif json.loads(observation.manifest_json) != manifest.model_dump(mode="json") or (
            acquisition is not None and observation.acquisition != acquisition
        ):
            raise LedgerConflictError("REVISION_IMMUTABLE", "official source observation differs")
        # Creation and contractual finality are sampled by each producer. A
        # source-final timestamp belongs to the provider's immutable evidence.
        replay = replace(
            candidate,
            created_at=retained.created_at,
            finalized_at=(
                retained.finalized_at
                if candidate.finality_basis == "contractual_cutoff"
                else candidate.finalized_at
            ),
        )
        if managed_revision_metadata(replay) != managed_revision_metadata(retained):
            raise LedgerConflictError("REVISION_IMMUTABLE", "official revision evidence differs")
        return replay


def _source_media_buy_ids(
    configuration: ReportingConfiguration, obligation: ReportingObligationRecord
) -> tuple[str, ...]:
    """The buys a slice asks the source for: an ``allow_partial`` period's covered ones.

    Every other obligation asks for its whole frozen denominator, as before.
    A publishable ``allow_partial`` period asks for its fully covered buys so
    the revision never carries an uncovered buy's delivery. The logical slice
    identity still names the whole denominator.
    """
    if _covered_subset_publishable(configuration.coverage_requirement, obligation.coverage):
        assert obligation.coverage is not None
        return obligation.coverage.fully_covered_media_buy_ids
    return obligation.media_buy_ids


def _logical_slice_fingerprint(obligation: ReportingObligationRecord, offering_id: str) -> str:
    from adcp.reporting.canonical_json import reporting_fingerprint_v1

    return reporting_fingerprint_v1(
        {
            "account_id": obligation.account_id,
            "consumer_id": obligation.consumer_id,
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


def _advertised_reporting_delivery(
    *,
    escalation: ReportingDeliveryEscalation,
    consumer_status_task: bool,
    offerings: Sequence[Mapping[str, Any]],
    automated_recovery_window: timedelta,
    status_retention_days: int,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared capability projection; discovery does not need a producer binding."""
    if not consumer_status_task and escalation.consumer_mismatch_escalation is not None:
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
    payload.update(escalation.to_wire())
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
