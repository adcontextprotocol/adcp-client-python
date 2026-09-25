"""Publication clocks are distinct from dispatch and immutable source evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from adcp.reporting import (
    ExpectedReportingPeriod,
    ReportingLedger,
    evaluate_reporting_ledger,
)
from adcp.reporting.conformance import validate_reporting_source_execution
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    PgReportingLedgerStore,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingProducer,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
)
from adcp.reporting.materializer import ReportingWriterCapability, reference_verifier
from adcp.reporting.source import SourceBatchManifestV1, parse_verified_source_batch_manifest_v1
from adcp.types import GetReportingStatusResponse
from adcp.validation.schema_loader import get_named_validator

from ._generation_support import END, START, isolated_reporting_pool
from ._production_support import Source

TURN = END + timedelta(hours=1)
PUBLISHED = TURN + timedelta(seconds=2)
ROWS = [{"row_id": "1", "impressions": 5, "spend": "1.25", "currency": "USD"}]


@dataclass
class Clock:
    now: datetime = TURN

    def __call__(self):
        return self.now


class RecordedSource(Source):
    async def execute(self, request, *, cancel, heartbeat=None):
        await asyncio.sleep(0)  # The source can complete after the dispatch instant.
        result = await super().execute(request, cancel=cancel, heartbeat=heartbeat)
        self.executions.append((request, result))
        return result


class DelayedReader:
    def __init__(self, reader, complete):
        self.reader, self.complete = reader, complete
        self.calls = 0

    async def read(self, **kwargs):
        result = await self.reader.read(**kwargs)
        await asyncio.sleep(0)
        self.complete()
        self.calls += 1
        return result


@pytest.fixture(params=["memory", "postgres"])
async def store(request):
    if request.param == "memory":
        yield InMemoryReportingLedgerStore(clock=lambda: datetime.now(timezone.utc))
    else:
        async with isolated_reporting_pool() as pool:
            ledger = PgReportingLedgerStore(pool=pool)
            await ledger.create_schema()
            yield ledger


async def setup(
    store, path, *, observed=TURN, completion=PUBLISHED, real=False, finality="official"
):
    verifier = reference_verifier(
        ReportingWriterCapability(
            "warehouse_materialization",
            "fixture-sql",
            "jsonl",
            "canonical_digest",
            "destination",
            "immutable_location",
            "sha256",
            "conditional_create",
        )
    )
    key = verifier.key
    clock = Clock()
    source = RecordedSource(
        key,
        path,
        ROWS,
        official=finality == "official",
        product_ids=(key.report_definition_id,),
        clock=(lambda: datetime.now(timezone.utc)) if real else (lambda: observed),
    )
    source.executions = []
    config = ReportingConfiguration(
        "publication-clock",
        1,
        "account-clock",
        key.report_definition_id,
        key.reporting_profile,
        "analytics",
        ReportingScheduleSpec("PT1H", "PT1H", period_anchor=START),
        finality,
        activated_at=START,
        deactivated_at=END,
        media_buy_ids=("mb-clock",),
        definition=key.definition,
    )
    await store.put_configuration(config)

    def complete_read():
        if not real:
            clock.now = completion

    reader = DelayedReader(source.reader, complete_read)

    def producer():
        return ReportingProducer(
            source=source,
            store=store,
            object_reader=reader,
            offerings=ProducerOfferings(
                official_offering_id=source.source_id if finality == "official" else None,
                snapshot_offering_id=source.source_id if finality == "snapshot" else None,
                publication_namespace=source.capabilities.offerings[0].publication_namespace,
                source_scope=source.capabilities.source_scope,
            ),
            revision_verifier=verifier,
            max_periods_per_turn=1,
            **({} if real else {"clock": clock}),
        )

    return config, source, reader, clock, producer


async def public_outcome(store, config):
    payload = await ReportingStatusHandler(store).handle(
        {
            "adcp_version": "3.2-rc.4",
            "account": {"account_id": config.account_id},
            "view": "periods",
            "period": {"start": START.isoformat(), "end": END.isoformat()},
        },
        caller=ReportingStatusCaller(account_id=config.account_id, consumer_id="clock-buyer"),
    )
    response = GetReportingStatusResponse.model_validate(payload)
    validator = get_named_validator("core/reporting-revision.json", version="3.2.0-rc.4")
    assert validator is not None
    for revision in payload["revisions"]:
        validator.validate(revision)
    ledger = ReportingLedger(
        ledger_snapshot_id=response.ledger_snapshot_id,
        ledger_as_of=response.ledger_as_of,
        account_id=response.account_id,
        scope=response.scope,
        obligations=response.periods,
        revisions=response.revisions,
        materializations=response.materializations,
        receipts=response.receipts,
    )
    expected = [
        ExpectedReportingPeriod(
            config.delivery_config_id,
            1,
            config.report_definition_id,
            config.feed_purpose,
            config.reporting_profile,
            config.media_buy_ids,
            START.isoformat(),
            END.isoformat(),
        )
    ]
    return response, evaluate_reporting_ledger(ledger, expected_periods=expected)


@pytest.mark.parametrize("observed", [END, TURN, TURN + timedelta(seconds=1)])
async def test_creation_follows_acquisition_and_staged_read(store, tmp_path, observed):
    config, source, reader, clock, factory = await setup(
        store, tmp_path / "source", observed=observed
    )
    turn = await factory().run_worker()
    assert len(turn.revisions_committed) == len(source.executions) == reader.calls == 1
    request, result = source.executions[0]
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=result,
        object_reader=source.reader,
        clock=clock,
    )
    response, outcome = await public_outcome(store, config)
    revision = response.revisions[0]
    assert outcome.definitive, [o.reasons for o in outcome.obligations]
    assert request.period.source_read_cutoff_at == TURN
    assert revision.created_at == PUBLISHED
    assert revision.observed_at == revision.finalized_at == manifest.observed_at == observed
    assert revision.data_through == END


async def test_real_clock_observation_after_dispatch_is_definitive(store, tmp_path):
    config, source, reader, _, factory = await setup(store, tmp_path / "source", real=True)
    turn = await factory().run_worker()
    assert len(turn.revisions_committed) == reader.calls == 1
    request, result = source.executions[0]
    manifest = await validate_reporting_source_execution(
        capabilities=source.capabilities,
        request=request,
        result=result,
        object_reader=source.reader,
    )
    response, outcome = await public_outcome(store, config)
    revision = response.revisions[0]
    assert outcome.definitive, [o.reasons for o in outcome.obligations]
    assert request.period.source_read_cutoff_at < manifest.observed_at <= revision.created_at
    assert revision.observed_at == revision.finalized_at == manifest.observed_at


@pytest.mark.parametrize(
    "observed,completion",
    [(TURN + timedelta(seconds=30), PUBLISHED), (END, TURN - timedelta(seconds=1))],
    ids=["source-clock-in-future", "trusted-clock-regresses-after-dispatch"],
)
async def test_invalid_publication_clock_stops_before_immutable_write(
    store, tmp_path, observed, completion, monkeypatch
):
    config, source, _, _, factory = await setup(
        store,
        tmp_path / "source",
        observed=observed,
        completion=completion,
    )
    writes = []
    original = store.commit_revision

    async def commit(revision, rows):
        writes.append(revision)
        return await original(revision, rows)

    monkeypatch.setattr(store, "commit_revision", commit)
    with pytest.raises(LedgerConflictError) as raised:
        await factory().run_worker()
    assert raised.value.code == "PUBLICATION_TIME_INVALID"
    assert not writes
    request, result = source.executions[0]
    manifest = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    assert manifest.observed_at == observed  # Original source evidence was not backdated.
    correction_condition_1 = (
        await store.list_revisions(
            account_id=config.account_id,
            reporting_obligation_id=request.identity.reporting_obligation_id,
        )
        == ()
    )
    assert correction_condition_1


@pytest.mark.parametrize("finality", ["official", "snapshot"])
async def test_same_publication_replay_preserves_committed_time_and_source_evidence(
    store, tmp_path, finality
):
    config, source, reader, clock, factory = await setup(
        store, tmp_path / "source", finality=finality
    )
    producer = factory()
    turn = await producer.run_worker()
    assert len(turn.revisions_committed) == 1
    request, result = source.executions[0]
    manifest = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    obligation = await store.get_obligation(
        account_id=config.account_id,
        reporting_obligation_id=request.identity.reporting_obligation_id,
    )
    first = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert obligation is not None and first is not None
    expected_revisions = (first,)
    if finality == "snapshot":
        clock.now = PUBLISHED + timedelta(minutes=1)
        reader.complete = lambda: None
        restated = await producer.acquire_obligation(config, obligation, restate=True)
        assert restated is not None
        assert restated.supersedes_reporting_revision_id == first.reporting_revision_id
        expected_revisions = (first, restated)
    clock.now = PUBLISHED + timedelta(minutes=10)
    for caller in (producer, factory()):
        replay = await caller.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality=finality,
            now=clock.now,
        )
        assert replay == first
    correction_condition_2 = (
        await store.list_revisions(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        == expected_revisions
    )
    assert correction_condition_2
    changed = [dict(ROWS[0], row_id="changed")]
    with pytest.raises(LedgerConflictError) as conflict:
        await factory().commit_revision_from_manifest(
            obligation,
            manifest,
            rows=changed,
            finality=finality,
            now=clock.now,
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"


async def test_explicit_dispatch_time_and_source_temporal_negatives(store, tmp_path):
    config, source, _, _, factory = await setup(store, tmp_path / "source")
    producer = factory()
    obligations = await producer.close_elapsed_periods(config, now=TURN)
    assert len(obligations) == 1
    first = await producer.acquire_obligation(config, obligations[0], now=TURN)
    request, result = source.executions[0]
    manifest = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    assert first is not None and request.period.source_read_cutoff_at == TURN
    assert first.created_at == PUBLISHED
    for updates in (
        {"observed_at": END - timedelta(seconds=1)},
        {
            "finality_evidence": {
                **manifest.finality_evidence.model_dump(),
                "observed_at": manifest.acquired_at + timedelta(seconds=1),
            }
        },
    ):
        with pytest.raises(ValidationError):
            SourceBatchManifestV1.model_validate({**manifest.model_dump(), **updates})


@pytest.mark.parametrize("future_acquisition", [False, True])
async def test_direct_commit_uses_explicit_publication_time_before_any_write(
    store, tmp_path, monkeypatch, future_acquisition
):
    config, source, reader, clock, factory = await setup(store, tmp_path / "source")
    producer = factory()
    obligations = await producer.close_elapsed_periods(config, now=TURN)

    async def unavailable(**kwargs):
        raise OSError("fixture staged read interrupted")

    # Retain a real source publication after an interrupted read, before any
    # revision exists. The public commit primitive can then retry that manifest.
    with monkeypatch.context() as patch:
        patch.setattr(reader, "read", unavailable)
        with pytest.raises(OSError, match="fixture staged read interrupted"):
            await producer.acquire_obligation(config, obligations[0], now=TURN)
    _, result = source.executions[0]
    manifest = parse_verified_source_batch_manifest_v1(
        result.response.manifest, result.manifest_bytes
    )
    clock.now = PUBLISHED + timedelta(minutes=10)
    if future_acquisition:
        # Source-contract-valid evidence may still be in the future relative to
        # the trusted publication clock. It must fail at the producer boundary.
        manifest = SourceBatchManifestV1.model_validate(
            {**manifest.model_dump(), "acquired_at": PUBLISHED + timedelta(seconds=1)}
        )
        writes = []
        commit = store.commit_revision

        async def record_write(*args, **kwargs):
            writes.append(args)
            return await commit(*args, **kwargs)

        monkeypatch.setattr(store, "commit_revision", record_write)
        with pytest.raises(LedgerConflictError) as raised:
            await producer.commit_revision_from_manifest(
                obligations[0], manifest, rows=ROWS, finality="official", now=PUBLISHED
            )
        assert raised.value.code == "PUBLICATION_TIME_INVALID"
        assert not writes
        correction_condition_3 = (
            await store.list_revisions(
                account_id=config.account_id,
                reporting_obligation_id=obligations[0].reporting_obligation_id,
            )
            == ()
        )
        assert correction_condition_3
    else:
        committed = await producer.commit_revision_from_manifest(
            obligations[0], manifest, rows=ROWS, finality="official", now=PUBLISHED
        )
        assert committed.created_at == PUBLISHED
        assert committed.observed_at == committed.finalized_at == TURN
