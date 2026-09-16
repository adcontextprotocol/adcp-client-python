"""Private deterministic building blocks for Reliable Reporting conformance slices.

Domain writes always go through the real memory/PostgreSQL stores. The private
publisher seam only prepares typed evidence before Core's revision write; it is
not a public Managed Delivery worker. PostgreSQL artifacts live in the fixture's
isolated schema. Memory restart uses an explicit test image, not SDK durability.
Real time is used only for bounded deadlock watchdogs, never reporting evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

import pytest

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_capabilities,
    redacted_contract_identity,
)
from adcp.reporting.inline_source import (
    InlineFetch,
    InlineFetchResult,
    InlineReportingSource,
    InMemoryStagingStore,
    MetricEvidence,
    SealedSlice,
)
from adcp.reporting.ledger import (
    InMemoryReportingReconciliationStore,
    PgReportingReconciliationStore,
    ProducerOfferings,
    ReportingCanonicalDigest,
    ReportingConfiguration,
    ReportingControlTotalRecord,
    ReportingDefinitionBinding,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingObligationRecord,
    ReportingPhysicalChecksum,
    ReportingProducer,
    ReportingResourceRecord,
    ReportingRevisionReceiptRecord,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    ReportingVerificationRecord,
    revision_content_sha256,
)
from adcp.reporting.outbox import (
    InMemoryReportingOutbox,
    PgReportingOutbox,
    ReportingEnvelopeCipher,
    ReportingNotificationSubscription,
    ReportingNotificationWorker,
    ReportingSigningMaterial,
)
from adcp.reporting.source import (
    MetricOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutorResult,
    ReportingSourceSliceRequestV1,
    SourceBatchManifestReferenceV1,
    SourceBatchManifestV1,
    parse_verified_source_batch_manifest_v1,
    reporting_source_capabilities_sha256_v1,
)

from ._generation_support import END, NOW, START, isolated_reporting_pool

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

Store = InMemoryReportingReconciliationStore | PgReportingReconciliationStore
Backend = Literal["memory", "postgres"]
METRICS = ("impressions", "clicks", "spend")


@dataclass
class ManualClock:
    now: datetime = NOW

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("ManualClock requires an aware instant")

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta = timedelta(milliseconds=1)) -> datetime:
        if delta < timedelta(0):
            raise ValueError("ManualClock cannot move backwards")
        self.now += delta
        return self.now


@dataclass
class Barrier:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    async def pause(self) -> None:
        self.entered.set()
        await asyncio.wait_for(self.released.wait(), timeout=10)

    async def wait(self) -> None:
        await asyncio.wait_for(self.entered.wait(), timeout=10)

    def release(self) -> None:
        self.released.set()


class FailurePlan:
    """Finite, named fault/barrier scripts; no probabilistic failure or sleeps."""

    def __init__(self) -> None:
        self.steps: dict[str, deque[BaseException | Barrier]] = defaultdict(deque)
        self.hits: list[str] = []

    def at(self, point: str, *steps: BaseException | Barrier) -> None:
        self.steps[point].extend(steps)

    async def hit(self, point: str) -> None:
        self.hits.append(point)
        if self.steps[point]:
            step = self.steps[point].popleft()
            if isinstance(step, Barrier):
                await step.pause()
            else:
                raise step


FetchStep = (
    InlineFetchResult
    | None
    | Exception
    | Callable[[ReportingSourceSliceRequestV1], InlineFetchResult | None]
)


class ScriptedSource:
    """Account-scoped sync/async callbacks with exactly the scripted read count."""

    def __init__(self, **scripts: Sequence[FetchStep]) -> None:
        self.steps = {account: deque(steps) for account, steps in scripts.items()}
        self.requests: list[ReportingSourceSliceRequestV1] = []
        self.thread_ids: list[int] = []
        self.failures = FailurePlan()
        self._lock = threading.Lock()

    def sync(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult | None:
        with self._lock:
            self.requests.append(request)
            self.thread_ids.append(threading.get_ident())
            queue = self.steps.get(request.identity.account_id)
            if not queue:
                raise AssertionError(f"unscripted source read for {request.identity.account_id}")
            step = queue.popleft()
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step

    async def async_fetch(self, request: ReportingSourceSliceRequestV1) -> InlineFetchResult | None:
        await self.failures.hit("fetch.before")
        result = self.sync(request)
        await self.failures.hit("fetch.after")
        return result


class _DidWork(Protocol):
    @property
    def did_work(self) -> bool: ...


_TurnT = TypeVar("_TurnT", bound=_DidWork, covariant=True)


class _Runnable(Protocol[_TurnT]):
    async def run_worker(self) -> _TurnT: ...


async def drain_until_idle(
    producer: _Runnable[_TurnT],
    clock: ManualClock,
    *,
    idle_turns: int = 1,
    max_turns: int = 32,
) -> tuple[_TurnT, ...]:
    """Drain a known number of configurations with a finite retry/lease budget.

    Set idle_turns to the number of configurations: one idle account must not
    hide pending work in its neighbour. Moving the clock also orders PG leases.
    """
    if not 1 <= idle_turns <= max_turns:
        raise ValueError("idle_turns must fit the positive turn budget")
    turns: list[_TurnT] = []
    idle = 0
    for _ in range(max_turns):
        turn = await asyncio.wait_for(producer.run_worker(), timeout=10)
        turns.append(turn)
        idle = 0 if turn.did_work else idle + 1
        clock.advance()
        if idle == idle_turns:
            return tuple(turns)
    raise AssertionError(f"worker did not become idle within {max_turns} turns")


class _BytesStore:
    """Immutable test artifacts, independent of the production ledger tables."""

    def __init__(self, pool: AsyncConnectionPool | None = None) -> None:
        self.pool = pool
        self.values: dict[tuple[str, str, str, str], bytes] = {}

    async def create_schema(self) -> None:
        if self.pool is not None:
            async with self.pool.connection() as connection:
                await connection.execute(
                    "CREATE TABLE IF NOT EXISTS reliable_test_bytes ("
                    "namespace text NOT NULL, account_id text NOT NULL, logical_id text NOT NULL,"
                    "generation text NOT NULL, payload bytea NOT NULL,"
                    "PRIMARY KEY(namespace, account_id, logical_id, generation))"
                )

    async def get(
        self, namespace: str, account: str, key: str, generation: str = ""
    ) -> bytes | None:
        if self.pool is None:
            return self.values.get((namespace, account, key, generation))
        async with self.pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT payload FROM reliable_test_bytes WHERE namespace=%s"
                    " AND account_id=%s AND logical_id=%s AND generation=%s",
                    (namespace, account, key, generation),
                )
            ).fetchone()
        return bytes(row[0]) if row else None

    async def put(
        self, namespace: str, account: str, key: str, payload: bytes, generation: str = ""
    ) -> bytes:
        if self.pool is None:
            return self.values.setdefault((namespace, account, key, generation), payload)
        async with self.pool.connection() as connection:
            await connection.execute(
                "INSERT INTO reliable_test_bytes VALUES (%s,%s,%s,%s,%s)" " ON CONFLICT DO NOTHING",
                (namespace, account, key, generation, payload),
            )
        winner = await self.get(namespace, account, key, generation)
        assert winner is not None
        return winner


class _Staging:
    def __init__(self, blobs: _BytesStore, failures: FailurePlan) -> None:
        self.blobs = blobs
        self.failures = failures
        self.memory = InMemoryStagingStore() if blobs.pool is None else None

    async def stage(
        self, *, account_id: str, source_execution_key: str, ordinal: int, payload: bytes
    ) -> tuple[str, str]:
        await self.failures.hit("stage.before")
        if self.memory is not None:
            result = await self.memory.stage(
                account_id=account_id,
                source_execution_key=source_execution_key,
                ordinal=ordinal,
                payload=payload,
            )
        else:
            ref = f"{source_execution_key}.{ordinal}"
            digest = hashlib.sha256(payload).hexdigest()
            assert await self.blobs.put("stage", account_id, ref, payload, digest) == payload
            result = ref, digest
        await self.failures.hit("stage.after")
        return result

    async def read(
        self,
        *,
        object_ref: str,
        object_generation: str,
        account_id: str,
        source_scope: Mapping[str, Any],
        cancel: asyncio.Event,
    ) -> bytes:
        if self.memory is not None:
            return await self.memory.read(
                object_ref=object_ref,
                object_generation=object_generation,
                account_id=account_id,
                source_scope=source_scope,
                cancel=cancel,
            )
        payload = await self.blobs.get("stage", account_id, object_ref, object_generation)
        if payload is None:
            raise PermissionError("staged object is outside the requested account scope")
        return payload


class _Seals:
    def __init__(self, blobs: _BytesStore, failures: FailurePlan) -> None:
        self.blobs = blobs
        self.failures = failures

    @staticmethod
    def _decode(payload: bytes) -> SealedSlice:
        value = json.loads(payload)
        return SealedSlice(
            SourceBatchManifestReferenceV1.model_validate(value["reference"]),
            value["manifest"].encode("utf-8"),
        )

    async def get(self, *, account_id: str, source_execution_key: str) -> SealedSlice | None:
        value = await self.blobs.get("seal", account_id, source_execution_key)
        return self._decode(value) if value is not None else None

    async def put(
        self, *, account_id: str, source_execution_key: str, sealed: SealedSlice
    ) -> SealedSlice:
        await self.failures.hit("seal.before")
        payload = canonical_json_utf8_v1(
            {
                "reference": sealed.reference.model_dump(mode="json"),
                "manifest": sealed.manifest_bytes.decode("utf-8"),
            }
        )
        winner = await self.blobs.put("seal", account_id, source_execution_key, payload)
        await self.failures.hit("seal.after")
        return self._decode(winner)


class DeterministicDestinationStore:
    """Immutable writes keyed by authenticated account and exact revision."""

    namespace = "destination"

    def __init__(self, blobs: _BytesStore, failures: FailurePlan) -> None:
        self.blobs = blobs
        self.failures = failures

    async def write(self, account: str, revision_id: str, payload: bytes) -> str:
        await self.failures.hit(f"{self.namespace}.before")
        winner = await self.blobs.put(self.namespace, account, revision_id, payload)
        if winner != payload:
            raise ValueError("immutable destination identity has different bytes")
        await self.failures.hit(f"{self.namespace}.after")
        return hashlib.sha256(payload).hexdigest()

    async def read(self, account: str, revision_id: str) -> bytes | None:
        return await self.blobs.get(self.namespace, account, revision_id)


class DeterministicReceiverStore(DeterministicDestinationStore):
    namespace = "receiver"


def configuration(account: str, *, finality: str = "snapshot") -> ReportingConfiguration:
    contract = redacted_contract_identity
    return ReportingConfiguration(
        account_id=account,
        delivery_config_id="shared-config",
        delivery_config_version=1,
        report_definition_id=contract.report_definition_id,
        reporting_profile=contract.reporting_profile,
        feed_purpose="analytics",
        schedule=ReportingScheduleSpec("PT1H", "PT1H", period_anchor=START),
        required_finality=finality,
        activated_at=START,
        deactivated_at=END,
        media_buy_ids=("shared-media-buy",),
        definition=ReportingDefinitionBinding(
            report_definition_uri=contract.report_definition_uri,
            report_definition_sha256=contract.report_definition_sha256,
            schema_version=contract.schema_version,
            schema_uri=contract.schema_uri,
            schema_sha256=contract.schema_sha256,
        ),
    )


def capabilities() -> ReportingSourceCapabilitiesV1:
    base = redacted_capabilities()
    draft = base.model_copy(
        update={
            "offerings": [
                offering.model_copy(
                    update={
                        "source_timezone": "UTC",
                        "product_ids": [redacted_contract_identity.report_definition_id],
                        "metrics": [
                            MetricOfferingV1(
                                name=metric,
                                semantic_contract_id=f"fixture.{metric}",
                                semantic_contract_version=str(index),
                                semantic_contract_sha256=str(index) * 64,
                            )
                            for index, metric in enumerate(METRICS, 1)
                        ],
                    }
                )
                for offering in base.offerings
            ]
        }
    )
    return ReportingSourceCapabilitiesV1.model_validate(
        {
            **draft.model_dump(),
            "capabilities_sha256": reporting_source_capabilities_sha256_v1(draft),
        }
    )


def complete_fetch(request: ReportingSourceSliceRequestV1) -> InlineFetchResult:
    return InlineFetchResult(
        rows=[
            {
                "media_buy_id": item.media_buy_id,
                "impressions": 5,
                "clicks": 0,
                "spend": amount,
                "currency": request.currency,
            }
            for item in request.coverage.constituents
            for amount in ("0.10", "0.20")
        ],
        currency=request.currency,
        cell_availability={
            item.constituent_id: {
                "impressions": MetricEvidence.present(request.period.end),
                "clicks": MetricEvidence.explicit_zero(data_through=request.period.end),
                "spend": MetricEvidence.present(request.period.end),
            }
            for item in request.coverage.constituents
        },
    )


def verified(result: ReportingSourceExecutorResult) -> SourceBatchManifestV1:
    assert result.ok, result.error
    assert result.response is not None and result.manifest_bytes is not None
    return parse_verified_source_batch_manifest_v1(result.response.manifest, result.manifest_bytes)


class _RecordingSource:
    def __init__(
        self, source: InlineReportingSource, manifests: dict[tuple[str, str], Any]
    ) -> None:
        self.source = source
        self.manifests = manifests

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1:
        return self.source.capabilities

    async def execute(
        self,
        request: ReportingSourceSliceRequestV1,
        *,
        cancel: asyncio.Event,
        heartbeat: Callable[[], None] | None = None,
    ) -> ReportingSourceExecutorResult:
        result = await self.source.execute(request, cancel=cancel, heartbeat=heartbeat)
        if result.ok:
            manifest = verified(result)
            self.manifests[(request.identity.account_id, manifest.content_fingerprint)] = manifest
        return result


class _PreparedRevisionStore:
    """The test publisher pins typed totals/digest before the real store validates.

    Core's worker deliberately has no Managed Delivery publisher yet. Delegation
    keeps its complete worker path and both stores' currency/content validators;
    this seam neither changes retained revisions nor bypasses an admission gate.
    """

    def __init__(self, harness: ReliableHarness) -> None:
        self.harness = harness

    def __getattr__(self, name: str) -> Any:
        return getattr(self.harness.store, name)

    async def commit_revision(
        self, revision: ReportingRevisionRecord, rows: Sequence[dict[str, Any]]
    ) -> ReportingRevisionRecord:
        fingerprint = f"sha256:{revision.source_manifest_sha256}"
        manifest = self.harness.manifests[(revision.account_id, fingerprint)]
        totals = tuple(
            ReportingControlTotalRecord(total.name, total.value, total.value_type, total.unit)
            for total in manifest.control_totals
        )
        prepared = replace(
            revision,
            managed_control_totals=totals,
            canonical_content_digest=ReportingCanonicalDigest(
                value=hashlib.sha256(canonical_json_utf8_v1(list(rows))).hexdigest(),
                canonicalization_id="fixture-rows-v1",
                canonicalization_uri="https://contracts.example.test/fixture-rows-v1.json",
                canonicalization_sha256="a" * 64,
            ),
            revision_content_sha256=revision_content_sha256(
                reporting_revision_id=revision.reporting_revision_id,
                row_count=revision.row_count,
                control_totals=revision.control_totals,
                reporting_rows=rows,
                control_total_evidence=totals,
            ),
        )
        await self.harness.failures.hit("revision.before")
        retained = await self.harness.store.commit_revision(prepared, rows)
        await self.harness.failures.hit("revision.after")
        return retained


@dataclass
class ReliableHarness:
    store: Store
    clock: ManualClock
    blobs: _BytesStore
    failures: FailurePlan = field(default_factory=FailurePlan)
    notifications: bool = False
    currencies: dict[str, str] = field(default_factory=lambda: {"eur": "EUR", "usd": "USD"})
    manifests: dict[tuple[str, str], SourceBatchManifestV1] = field(default_factory=dict)
    staging: _Staging = field(init=False)
    seals: _Seals = field(init=False)
    destination: DeterministicDestinationStore = field(init=False)
    receiver: DeterministicReceiverStore = field(init=False)

    def __post_init__(self) -> None:
        self.staging = _Staging(self.blobs, self.failures)
        self.seals = _Seals(self.blobs, self.failures)
        self.destination = DeterministicDestinationStore(self.blobs, self.failures)
        self.receiver = DeterministicReceiverStore(self.blobs, self.failures)

    def source(self, fetch: InlineFetch) -> InlineReportingSource:
        return InlineReportingSource(
            capabilities=capabilities(),
            fetch=fetch,
            staging=self.staging,
            seals=self.seals,
            clock=self.clock,
        )

    def producer(self, source: InlineReportingSource, *, managed: bool = True) -> ReportingProducer:
        return ReportingProducer(
            store=_PreparedRevisionStore(self) if managed else self.store,
            source=_RecordingSource(source, self.manifests),
            object_reader=self.staging,
            offerings=ProducerOfferings(
                snapshot_offering_id=SNAPSHOT_OFFERING_ID,
                official_offering_id=OFFICIAL_OFFERING_ID,
                publication_namespace="reporting-source:fixture",
                source_scope=dict(redacted_capabilities().source_scope),
                requested_metrics=METRICS,
            ),
            currency_resolver=lambda config, obligation: self.currencies[config.account_id],
            clock=self.clock,
        )

    async def commit_slice(
        self,
        producer: ReportingProducer,
        obligation: ReportingObligationRecord,
        request: ReportingSourceSliceRequestV1,
        result: ReportingSourceExecutorResult,
        *,
        finality: str = "snapshot",
    ) -> ReportingRevisionRecord:
        manifest = verified(result)
        self.manifests[(obligation.account_id, manifest.content_fingerprint)] = manifest
        return await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=await producer._read_rows(request, manifest),
            finality=finality,
        )

    async def restart(self) -> None:
        """Replace all process-local clients; memory explicitly restores a test image."""
        if self.blobs.pool is not None:
            from psycopg_pool import AsyncConnectionPool

            old = self.blobs.pool
            await old.close()
            pool = AsyncConnectionPool(
                old.conninfo, kwargs=old.kwargs, min_size=2, max_size=8, open=False
            )
            await pool.open(wait=True)
            self.blobs = _BytesStore(pool)
            self.store = PgReportingReconciliationStore(
                pool=pool, clock=self.clock, notifications=self.notifications
            )
            self.__post_init__()
        else:
            # This is an explicit test fixture image, not an SDK persistence API.
            state = deepcopy(
                {
                    name: value
                    for name, value in vars(self.store).items()
                    if name not in {"_lock", "_clock"}
                }
            )
            stage = deepcopy(self.staging.memory)
            blobs = deepcopy(self.blobs.values)
            self.store = InMemoryReportingReconciliationStore(clock=self.clock)
            vars(self.store).update(state)
            self.blobs = _BytesStore()
            self.blobs.values = blobs
            self.__post_init__()
            self.staging.memory = stage
        self.manifests.clear()
        await self.store.create_schema()


@asynccontextmanager
async def reliable_factory(
    backend: Backend,
    *,
    initialize: bool = True,
    notifications: bool = False,
    autocommit: bool = False,
) -> AsyncIterator[ReliableHarness]:
    clock = ManualClock()
    if backend == "memory":
        harness = ReliableHarness(
            InMemoryReportingReconciliationStore(clock=clock, notifications=notifications),
            clock,
            _BytesStore(),
            notifications=notifications,
        )
        yield harness
    else:
        async with isolated_reporting_pool(autocommit=autocommit) as pool:
            harness = ReliableHarness(
                PgReportingReconciliationStore(pool=pool, clock=clock, notifications=notifications),
                clock,
                _BytesStore(pool),
                notifications=notifications,
            )
            if initialize:
                await harness.store.create_schema()
            await harness.blobs.create_schema()
            try:
                yield harness
            finally:
                if harness.blobs.pool is not None and harness.blobs.pool is not pool:
                    await harness.blobs.pool.close()


@pytest.fixture(params=["memory", "postgres"])
async def reliable(request: pytest.FixtureRequest) -> AsyncIterator[ReliableHarness]:
    async with reliable_factory(request.param) as harness:
        yield harness


@dataclass(frozen=True)
class PublishedRecords:
    binding: ReportingDestinationBinding
    delivery: ReportingObligationDeliveryRecord
    attempt: ReportingMaterializationAttempt
    outcome: ReportingMaterializationRecord
    receipt: ReportingRevisionReceiptRecord


async def publication_records(
    harness: ReliableHarness,
    obligation: ReportingObligationRecord,
    revision: ReportingRevisionRecord,
    *,
    suffix: str = "shared",
) -> PublishedRecords:
    """Prepare immutable destination observations from an exact revision read.

    Callers commit the attempt/outcome/receipt explicitly so tests can interleave
    failures and invalid writes without a second copy of the state machine.
    """
    scope = ReportingDeliveryScope(
        obligation.generation_key, "buyer", obligation.reporting_obligation_id
    )
    binding = ReportingDestinationBinding(
        generation_key=scope.generation_key,
        consumer_id=scope.consumer_id,
        destination_ref="shared-destination",
        trusted_binding_ref="trusted-fixture-binding",
        method="file_transfer",
        transport="test-storage",
        verification_profile="canonical_digest",
        reconciliation_mode="consumer_receipt",
        feed_purpose="analytics",
        resource_retention_days=400,
        created_at=START,
        format="jsonl",
        reader_compatibility=("jsonl-v1",),
    )
    assert obligation.currency is not None
    delivery = ReportingObligationDeliveryRecord(
        scope,
        obligation.currency,
        obligation.created_at + timedelta(days=400),
        obligation.created_at,
    )
    await harness.store.put_destination_binding(binding)
    await harness.store.bind_obligation_delivery(delivery)
    all_rows = []
    cursor = None
    seen = set()
    while True:
        page = await harness.store.read_revision_rows(
            account_id=obligation.account_id,
            reporting_revision_id=revision.reporting_revision_id,
            cursor=cursor,
            limit=500,
        )
        assert page.reporting_revision_id == revision.reporting_revision_id
        assert page.total_count == revision.row_count
        assert page.has_more == (page.cursor is not None)
        all_rows.extend(page.rows)
        if not page.has_more:
            break
        assert page.rows and page.cursor not in seen
        seen.add(page.cursor)
        cursor = page.cursor
    assert len(all_rows) == revision.row_count
    payload = b"".join(canonical_json_utf8_v1(row) + b"\n" for row in all_rows)
    digest = await harness.destination.write(
        obligation.account_id, revision.reporting_revision_id, payload
    )
    attempt = ReportingMaterializationAttempt(
        scope, revision.reporting_revision_id, f"materialization-{suffix}", 1, harness.clock()
    )
    at = harness.clock()
    resource = ReportingResourceRecord(
        resource_ref=f"resource-{suffix}",
        kind="manifest",
        location=f"reports/{suffix}/manifest.json",
        immutability="immutable_location",
        expires_at=at + timedelta(days=400),
        manifest_sha256=hashlib.sha256(
            canonical_json_utf8_v1({"sha256": digest, "rows": revision.row_count})
        ).hexdigest(),
        object_refs=(f"reports/{suffix}/part-000.jsonl",),
        reader_compatibility=binding.reader_compatibility,
    )
    assert revision.managed_control_totals is not None
    verification = ReportingVerificationRecord(
        verified_at=at,
        verification_path="producer",
        verification_profile="canonical_digest",
        row_count=revision.row_count,
        control_totals=revision.managed_control_totals,
        canonical_content_digest=revision.canonical_content_digest,
        physical_checksums=(ReportingPhysicalChecksum(resource.object_refs[0], "sha256", digest),),
        verified_format="jsonl",
    )
    outcome = ReportingMaterializationRecord(
        scope,
        revision.reporting_revision_id,
        attempt.reporting_materialization_id,
        "available",
        at,
        resource=resource,
        verification=verification,
    )
    receipt = ReportingRevisionReceiptRecord(
        scope=scope,
        reporting_receipt_id=f"receipt-{suffix}-00000001",
        reporting_revision_id=revision.reporting_revision_id,
        reporting_materialization_id=attempt.reporting_materialization_id,
        status="accepted",
        verification_profile="canonical_digest",
        observed_row_count=revision.row_count,
        observed_control_totals=revision.managed_control_totals,
        observed_canonical_content_digest=revision.canonical_content_digest,
        observed_at=at,
        consumer_commit_ref=f"load-{suffix}",
    )
    return PublishedRecords(binding, delivery, attempt, outcome, receipt)


class SimulatedCrash(BaseException):
    """Abrupt service death, deliberately outside ordinary retry catches."""


def notification_subscription(
    account: str = "acct_a",
    subscriber: str = "buyer",
    *,
    principal: str = "buyer",
    events: tuple[str, ...] = ("reporting.ledger_changed", "reporting.delivery_ready"),
    url: str = "https://receiver.example.test/reporting?token=URL_SECRET",
    **changes: Any,
) -> ReportingNotificationSubscription:
    values: dict[str, Any] = dict(
        account_id=account,
        subscriber_id=subscriber,
        principal_id=principal,
        url=url,
        event_types=events,
        configuration_revision="registration-1",
        authorization_ref="authorized-principal-1",
        proof_of_control_ref="account-challenge-1",
        signing_scope_id="seller-signing-scope-1",
        active=True,
        authorized=True,
        proof_valid=True,
    )
    values.update(changes)
    return ReportingNotificationSubscription(**values)


class ScriptedSubscriptions:
    """Normalized trusted account configurations, with one atomic list read."""

    def __init__(self, failures: FailurePlan) -> None:
        self.failures = failures
        self.values: dict[tuple[str, str], ReportingNotificationSubscription] = {}
        self.lists: list[tuple[str, str]] = []
        self.gets: list[tuple[str, str, str]] = []

    def put(self, value: ReportingNotificationSubscription) -> None:
        self.values[(value.account_id, value.subscriber_id)] = value

    async def list_active(self, *, account_id: str, notification_type: str):
        self.lists.append((account_id, notification_type))
        await self.failures.hit("subscriptions.list.before")
        snapshot = tuple(
            value
            for (account, _), value in self.values.items()
            if account == account_id and value.active and notification_type in value.event_types
        )
        await self.failures.hit("subscriptions.list.after")
        return snapshot

    async def get_active(self, *, account_id: str, subscriber_id: str, notification_type: str):
        self.gets.append((account_id, subscriber_id, notification_type))
        await self.failures.hit("subscriptions.get")
        return self.values.get((account_id, subscriber_id))


class ScriptedSigning:
    def __init__(self, failures: FailurePlan) -> None:
        self.failures = failures
        self.generation = 1
        self.calls: list[tuple[str, str, str]] = []

    async def resolve(self, *, account_id: str, principal_id: str, signing_scope_id: str):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.calls.append((account_id, principal_id, signing_scope_id))
        await self.failures.hit("signing.resolve")
        return ReportingSigningMaterial(
            Ed25519PrivateKey.from_private_bytes(bytes([self.generation]) * 32),
            f"https://seller.example.test/keys#key-{self.generation}",
            "ed25519",
            frozenset({"ed25519"}),
        )


def notification_verification_keys() -> list[dict[str, Any]]:
    """The exact public-key fixture used by the separate receiver process."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    result = []
    for generation in (1, 2):
        public = Ed25519PrivateKey.from_private_bytes(bytes([generation]) * 32).public_key()
        result.append(
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "alg": "EdDSA",
                "use": "sig",
                "adcp_use": "request-signing",
                "key_ops": ["verify"],
                "kid": f"https://seller.example.test/keys#key-{generation}",
                "x": base64.urlsafe_b64encode(
                    public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                )
                .rstrip(b"=")
                .decode(),
            }
        )
    return result


@dataclass(frozen=True)
class ReceivedNotification:
    account_id: str
    subscriber_id: str
    idempotency_key: str
    body: bytes = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    target: str = field(repr=False)


class ScriptedNotificationReceiver:
    """HTTP/1 byte peer below the SDK's actual pinning and signing paths.

    We replace only the network socket. The production resolver, SSRF policy,
    pinning backend, sender, httpx/httpcore HTTP encoding, and signature code all
    run. This receiver persists accepted bytes in the same deterministic private
    receiver store used by the preceding reporting slice.
    """

    def __init__(self, harness: ReliableHarness) -> None:
        self.harness = harness
        self.received: list[ReceivedNotification] = []
        self.connections: list[tuple[str, int]] = []
        self.responses: dict[str, deque[int | Exception]] = defaultdict(deque)
        self.dns_addresses: dict[str, list[str]] = {}
        self.dns_calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket
        from types import SimpleNamespace

        from httpcore._backends.anyio import AnyIOBackend

        from adcp import webhook_auth
        from adcp.signing import signer

        # The signing library has its own crypto clock. Keep even that private
        # test dependency deterministic without replacing global time.time().
        crypto_time = SimpleNamespace(time=lambda: self.harness.clock().timestamp())
        monkeypatch.setattr(signer, "time", crypto_time)
        monkeypatch.setattr(webhook_auth, "time", crypto_time)

        original_resolve = socket.getaddrinfo
        receiver = self

        def resolve(host, port, *args, **kwargs):
            if isinstance(host, bytes):
                host = host.decode("ascii")
            if str(host).endswith(".example.test"):
                receiver.dns_calls.append(host)
                addresses = receiver.dns_addresses.get(host, ["8.8.8.8"])
                address = addresses.pop(0) if len(addresses) > 1 else addresses[0]
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]
            return original_resolve(host, port, *args, **kwargs)

        async def connect(backend, host, port, **kwargs):
            receiver.connections.append((host, port))
            # The parent of the SDK pinning backend receives an IP, never an
            # attacker-controlled hostname to resolve for a second time.
            assert host in {"8.8.8.8", "1.1.1.1"}
            assert port == 443
            return _NotificationStream(receiver)

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)

    async def respond(self, raw: bytes) -> bytes:
        head, body = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:])
        headers = {key.lower(): value for key, value in headers.items()}
        value = json.loads(body)
        subscriber = value["subscriber_id"]
        await self.harness.failures.hit("http.before")
        await self.harness.failures.hit(f"http.before:{subscriber}")
        script = self.responses[subscriber]
        step = script.popleft() if script else 200
        if isinstance(step, Exception):
            raise step
        self.received.append(
            ReceivedNotification(
                value["account_id"],
                subscriber,
                value["idempotency_key"],
                body,
                headers,
                lines[0].split(" ")[1],
            )
        )
        if 200 <= step < 300:
            await self.harness.receiver.write(value["account_id"], value["idempotency_key"], body)
            await self.harness.failures.hit("http.accepted")
        response_body = b"provider token=DO_NOT_PERSIST"
        redirect = b"Location: https://169.254.169.254/secret\r\n" if step == 302 else b""
        return (
            f"HTTP/1.1 {step} Test\r\nContent-Length: {len(response_body)}\r\n".encode()
            + redirect
            + b"\r\n"
            + response_body
        )


class _NotificationStream:
    def __init__(self, receiver: ScriptedNotificationReceiver) -> None:
        self.receiver, self.request, self.response = receiver, bytearray(), None

    async def write(self, buffer, timeout=None):
        self.request.extend(buffer)

    async def read(self, max_bytes, timeout=None):
        if self.response is None:
            self.response = await self.receiver.respond(bytes(self.request))
        chunk, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return chunk

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    async def aclose(self):
        pass

    def get_extra_info(self, info):
        return None


@dataclass(frozen=True)
class NotificationTurn:
    did_work: bool


class NotificationRunner:
    """Adapter for the shared bounded drain_until_idle state-machine driver."""

    def __init__(self, worker: ReportingNotificationWorker, accounts: tuple[str, ...]) -> None:
        self.worker, self.accounts, self.turn = worker, accounts, 0

    async def run_worker(self) -> NotificationTurn:
        account = self.accounts[self.turn % len(self.accounts)]
        self.turn += 1
        expanded = await self.worker.expand_one(account_id=account)
        delivered = await self.worker.deliver_one(account_id=account)
        return NotificationTurn(expanded or delivered)


@dataclass
class NotificationHarness:
    reliable: ReliableHarness
    subscriptions: ScriptedSubscriptions = field(init=False)
    signing: ScriptedSigning = field(init=False)
    receiver: ScriptedNotificationReceiver = field(init=False)
    cipher: ReportingEnvelopeCipher = field(
        default_factory=lambda: ReportingEnvelopeCipher(b"e" * 32)
    )

    def __post_init__(self) -> None:
        self.subscriptions = ScriptedSubscriptions(self.reliable.failures)
        self.signing = ScriptedSigning(self.reliable.failures)
        self.receiver = ScriptedNotificationReceiver(self.reliable)
        self.subscriptions.put(notification_subscription())

    @property
    def outbox(self):
        if self.reliable.blobs.pool is not None:
            return PgReportingOutbox(pool=self.reliable.blobs.pool, clock=self.reliable.clock)
        return InMemoryReportingOutbox(self.reliable.store)

    def worker(self, **kwargs: Any) -> ReportingNotificationWorker:
        return ReportingNotificationWorker(
            outbox=self.outbox,
            subscriptions=self.subscriptions,
            signing=self.signing,
            cipher=self.cipher,
            clock=self.reliable.clock,
            **kwargs,
        )

    async def drain(self, *accounts: str):
        selected = accounts or ("acct_a",)
        return await drain_until_idle(
            NotificationRunner(self.worker(), selected),
            self.reliable.clock,
            idle_turns=len(selected),
        )


@pytest.fixture(params=["memory", "postgres"])
async def notification_harness(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    async with reliable_factory(request.param, notifications=True, autocommit=True) as reliable:
        harness = NotificationHarness(reliable)
        harness.receiver.install(monkeypatch)
        yield harness


@dataclass
class ServiceProcess:
    """A separately pooled service controlled by newline-JSON barriers.

    Waits have watchdog bounds. Neither process orchestration nor convergence
    uses timing sleeps. Configuration travels over stdin, never command args.
    """

    process: asyncio.subprocess.Process
    role: str
    last_point: str = "spawned"
    lifetime_expired: bool = False

    def diagnostic(self, action: str) -> str:
        return (
            f"reporting_matrix role={self.role} pid={self.process.pid} action={action}"
            f" last_point={self.last_point} exit={self.process.returncode}"
            f" lifetime_expired={self.lifetime_expired}"
        )

    def trace(self, action: str) -> None:
        # Visible under pytest -s; no routing data, secrets, or child prose.
        print(self.diagnostic(action), flush=True)

    async def send(self, **value: Any) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        try:
            await asyncio.wait_for(self.process.stdin.drain(), 5)
        except (TimeoutError, asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            raise AssertionError(self.diagnostic("stdin_failed")) from None

    async def event(self, point: str, *, timeout_seconds: float = 30) -> dict[str, Any]:
        assert self.process.stdout is not None
        self.trace(f"waiting:{point}")
        try:
            raw = await asyncio.wait_for(self.process.stdout.readline(), timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssertionError(self.diagnostic(f"deadline:{point}")) from None
        if not raw:
            # Reading stderr to EOF here can itself hang on a living child;
            # arbitrary tracebacks are also inappropriate diagnostics.
            raise AssertionError(self.diagnostic(f"eof_before:{point}"))
        value: dict[str, Any] = json.loads(raw)
        allowed = {
            "point",
            "classification",
            "stage",
            "attempt",
            "backend_pid",
            "port",
            "did_work",
            "module_origin",
            "source_sha",
        }
        assert set(value).issubset(allowed), self.diagnostic("invalid_child_protocol")
        self.last_point = value["point"]
        self.trace(f"received:{self.last_point}")
        assert value["point"] == point, (self.diagnostic(f"expected:{point}"), value)
        return value

    async def finish(self, *, code: int = 0) -> None:
        try:
            actual = await asyncio.wait_for(self.process.wait(), 15)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssertionError(self.diagnostic("exit_deadline")) from None
        assert actual == code, self.diagnostic(f"expected_exit:{code}")
        self.trace("exited")

    async def kill(self) -> None:
        if self.process.returncode is None:
            self.trace("kill_owned_process")
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 15)
        except (TimeoutError, asyncio.TimeoutError):
            raise AssertionError(self.diagnostic("kill_deadline")) from None

    async def watchdog(self) -> None:
        try:
            await asyncio.wait_for(self.process.wait(), 90)
        except (TimeoutError, asyncio.TimeoutError):
            self.lifetime_expired = True
            self.trace("lifetime_deadline")
            await self.kill()


@asynccontextmanager
async def service_process(
    pool: AsyncConnectionPool, role: str, **settings: Any
) -> AsyncIterator[ServiceProcess]:
    import sys

    process = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            (
                "tests.conformance.reporting._status_old_process"
                if role.startswith("old_")
                else "tests.conformance.reporting._reliable_process"
            ),
            role,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ),
        10,
    )
    service = ServiceProcess(process, role)
    service.trace("spawned")
    watchdog = asyncio.create_task(service.watchdog())
    try:
        await service.send(conninfo=pool.conninfo, pool_kwargs=pool.kwargs, **settings)
        yield service
    finally:
        await service.kill()
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
