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
from typing import TYPE_CHECKING, Any, Literal

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
    WorkerTurn,
    revision_content_sha256,
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
        self.steps: dict[str, deque[Exception | Barrier]] = defaultdict(deque)
        self.hits: list[str] = []

    def at(self, point: str, *steps: Exception | Barrier) -> None:
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


async def drain_until_idle(
    producer: ReportingProducer,
    clock: ManualClock,
    *,
    idle_turns: int = 1,
    max_turns: int = 32,
) -> tuple[WorkerTurn, ...]:
    """Drain a known number of configurations with a finite retry/lease budget.

    Set idle_turns to the number of configurations: one idle account must not
    hide pending work in its neighbour. Moving the clock also orders PG leases.
    """
    if not 1 <= idle_turns <= max_turns:
        raise ValueError("idle_turns must fit the positive turn budget")
    turns: list[WorkerTurn] = []
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
            staged_payload = await self.blobs.put("stage", account_id, ref, payload, digest)
            assert staged_payload == payload
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
            self.store = PgReportingReconciliationStore(pool=pool, clock=self.clock)
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
    backend: Backend, *, initialize: bool = True
) -> AsyncIterator[ReliableHarness]:
    clock = ManualClock()
    if backend == "memory":
        harness = ReliableHarness(
            InMemoryReportingReconciliationStore(clock=clock), clock, _BytesStore()
        )
        yield harness
    else:
        async with isolated_reporting_pool() as pool:
            harness = ReliableHarness(
                PgReportingReconciliationStore(pool=pool, clock=clock), clock, _BytesStore(pool)
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
    rows = await harness.store.read_revision_rows(
        account_id=obligation.account_id, reporting_revision_id=revision.reporting_revision_id
    )
    payload = b"".join(canonical_json_utf8_v1(row) + b"\n" for row in rows.rows)
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
