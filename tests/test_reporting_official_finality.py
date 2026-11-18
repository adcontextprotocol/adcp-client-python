"""Official publication binds an exact policy to its qualifying source acquisition."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore
from pydantic import ValidationError

from adcp.reporting.evidence import ReportingControlTotalRecord
from adcp.reporting.fixtures import (
    OFFICIAL_OFFERING_ID,
    SNAPSHOT_OFFERING_ID,
    redacted_completed_result,
)
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
)
from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    LedgerConflictError,
    ReportingConfigurationGenerationKey,
    ReportingContractualCutoffPolicy,
    ReportingProducer,
    ReportingRevisionRecord,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
    ReportingStatusCaller,
    ReportingStatusHandler,
    revision_content_sha256,
)
from adcp.reporting.ledger.finality import (
    FinalityProof,
    manifest_finality_proof,
    source_finality_proof,
)
from adcp.reporting.ledger.provisional import (
    ProvisionalAcquisition,
    ProvisionalObservation,
    ProvisionalPolicy,
)
from adcp.reporting.service import ReliableReportingConfigurationError, ReliableReportingService
from adcp.reporting.source import (
    ReportingFinalityEvidenceV1,
    ReportingSourceExecutorResult,
    SourceBatchManifestV1,
    coverage_denominator_fingerprint_v1,
    publication_content_fingerprint_v1,
    source_batch_object_set_sha256_v1,
)
from adcp.reporting.testing import DeterministicReportingClock
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_source_availability import (
    END,
    PROVIDER_ROW,
    ProgressStore,
    _capabilities,
    _closing_capabilities,
    _harness,
)

ROWS = [{"campaign_id": "campaign-redacted-1", "impressions": 10}]


async def test_standalone_stabilization_requires_account_lock_before_obligations() -> None:
    class UnlockedHistory(InMemoryReportingLedgerStore):
        source_publication = None

    store = UnlockedHistory()
    with pytest.raises(LedgerConflictError, match="publication") as error:
        await _stabilized_harness(lambda **kwargs: store)
    assert error.value.code == "STABILIZATION_PUBLICATION_NOT_SUPPORTED"
    assert not store._obligations


async def test_standalone_stabilization_rejects_an_unbound_snapshot_source() -> None:
    producer, _, config, _, _, _ = await _stabilized_harness(InMemoryReportingLedgerStore)
    store = InMemoryReportingLedgerStore()
    await store.put_configuration(config)
    producer._store = store
    producer._offerings = replace(producer._offerings, snapshot_offering_id=None)
    with pytest.raises(LedgerConflictError, match="snapshot"):
        await producer.close_elapsed_periods(config)
    assert not store._obligations


@pytest.mark.parametrize(
    "missing",
    ["verify_snapshot_origin", "get_provisional_acquisition", "get_stabilization_schedule"],
)
async def test_standalone_stabilization_checks_durable_apis_before_storage(missing: str) -> None:
    class IncompletePublisher(InMemoryReportingLedgerStore):
        async def find_obligation(self, **kwargs: Any) -> Any:
            pytest.fail("admission read obligations from an incomplete publisher")

    store = IncompletePublisher()
    setattr(store, missing, None)
    with pytest.raises(LedgerConflictError, match=missing):
        await _stabilized_harness(lambda **kwargs: store)
    assert not store._obligations


async def test_native_recovery_reads_the_schedule_for_the_generation(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, _, _, _ = await _stabilized_harness(store_factory)
    await producer.run_configuration(config)

    async def forbid_full_ledger(**kwargs: Any) -> Any:
        pytest.fail("recovery should look up the generation's scheduled observation")

    monkeypatch.setattr(store, "read_page", forbid_full_ledger)
    frozen = await producer._recover_stabilization_schedule(config)
    assert frozen and frozen.cadence == timedelta(hours=6)
    other = replace(config, delivery_config_version=2)
    assert await producer._recover_stabilization_schedule(other) is None


@pytest.mark.parametrize("legacy", [False, True])
async def test_recovery_finds_the_pending_snapshot_before_the_first_observation(
    store_factory: Any, legacy: bool
) -> None:
    store = store_factory()
    assert (
        await store.get_stabilization_schedule(
            ReportingConfigurationGenerationKey("account-redacted", "buyer", "availability", 1)
        )
        is None
    )
    producer, _, config, obligation, _, clock = await _stabilized_harness(lambda **kwargs: store)
    request = producer._build_slice(
        config,
        obligation,
        SNAPSHOT_OFFERING_ID,
        finality="official" if legacy else "snapshot",
        now=clock[0],
    )
    policy = ProvisionalPolicy(timedelta(days=1), timedelta(hours=6), until_stabilized=not legacy)
    await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(), 0, policy, output_finality="official" if legacy else None
        )
    )
    assert await producer._recover_stabilization_schedule(config) == policy
    assert (
        await producer._recover_stabilization_schedule(replace(config, delivery_config_version=2))
        is None
    )


async def test_previous_checkpoint_schema_recovers_schedule_before_initialize() -> None:
    async with isolated_reporting_pool() as pool:
        producer, store, config, _, _, _ = await _stabilized_harness(
            partial(PgReportingLedgerStore, pool=pool)
        )
        await producer.run_configuration(config)
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE reporting_restatement_checkpoints DROP COLUMN stabilization_policy,"
                " DROP COLUMN next_stabilization_due_at"
            )
        policy = await producer._recover_stabilization_schedule(config)
        assert policy is not None and policy.cadence == timedelta(hours=6)
        await store.create_schema()
        assert await store.get_stabilization_schedule(config.generation_key) == policy


@pytest.mark.parametrize("tracked", [False, True])
async def test_manual_change_between_equal_scheduled_reads_preserves_stabilization(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, tracked: bool
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    clock[0] = END + timedelta(hours=8)
    prior = await store.get_provisional_observation(**owner)
    if tracked:
        request = producer._build_slice(
            config,
            obligation,
            SNAPSHOT_OFFERING_ID,
            finality="snapshot",
            now=clock[0],
            observation=2,
            trigger="manual_replay",
        )
        await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                request.model_dump_json(), 2, prior.acquisition.policy, prior.revision_id
            )
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            "tests.test_reporting_source_availability.PROVIDER_ROW",
            PROVIDER_ROW.replace("10", "11"),
        )
        manual = await producer.acquire_obligation(
            config, obligation, restate=True, target_finality="snapshot", manual_replay=True
        )
    assert manual is not None and manual.reporting_revision_id != prior.revision_id
    clock[0] = END + timedelta(hours=13)
    await producer.run_configuration(config)
    revisions = await store.list_revisions(**owner)
    assert [r.finalized_at for r in revisions if r.finality == "official"] == [clock[0]]
    assert len(calls) == 4


async def test_unbound_reused_stabilization_history_recovers_after_fresh_evidence(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7, 13, 19, 25):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    read = store.list_provisional_observations

    async def base_history(**kwargs: Any) -> Any:
        return tuple(
            replace(o, revision_publication_id=None) if 0 < o.acquisition.ordinal <= 4 else o
            for o in await read(**kwargs)
        )

    monkeypatch.setattr(store, "list_provisional_observations", base_history)
    with pytest.raises(LedgerConflictError, match="exact source publication"):
        await producer.acquire_obligation(config, obligation)
    assert all(r.finality == "snapshot" for r in await store.list_revisions(**owner))
    for hour in (31, 37, 43):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(config, obligation)
        official = [r for r in await store.list_revisions(**owner) if r.finality == "official"]
        assert [r.finalized_at for r in official] == ([clock[0]] if hour == 43 else [])


async def test_stabilized_close_keeps_frozen_route_when_offering_drops_close_lag(
    store_factory: Any,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        store_factory,
        capabilities=_capabilities(
            restatement_window="P3D", restatement_cadence="PT6H", official_close_lag="P4D"
        ),
        finality_policy=ReportingStabilizedPolicy("stable-12h", "PT0S", "PT12H"),
    )
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    producer._source.capabilities = _capabilities(
        restatement_window="PT6H", restatement_cadence="PT6H", official_close_lag=None
    )
    for hour in (7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    revisions = await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert [r.finalized_at for r in revisions if r.finality == "official"] == [clock[0]]


async def test_manual_official_reservation_cannot_replace_the_scheduled_generation_cadence(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, _, _, clock = await _stabilized_harness(store_factory)
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    period_end = END + timedelta(days=1)
    clock[0] = period_end + timedelta(hours=1)
    (obligation,) = await producer.close_elapsed_periods(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    request = producer._build_slice(
        config,
        obligation,
        SNAPSHOT_OFFERING_ID,
        finality="official",
        trigger="manual_replay",
        now=clock[0],
    )
    await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(),
            0,
            ProvisionalPolicy(timedelta(days=1), timedelta(hours=12)),
            output_finality="official",
        )
    )
    producer._source.capabilities = _capabilities(
        restatement_window="P1D", restatement_cadence="PT1H", official_close_lag="P2D"
    )
    restarted = _restart_producer(producer, store, clock)
    for hour, impressions in ((1, 10), (7, 11), (13, 10)):
        clock[0] = period_end + timedelta(hours=hour)
        monkeypatch.setattr(
            "tests.test_reporting_source_availability.PROVIDER_ROW",
            PROVIDER_ROW.replace("10", str(impressions)),
        )
        await restarted.run_configuration(config)
    revisions = await store.list_revisions(**owner)
    assert all(revision.finality == "snapshot" for revision in revisions)
    observations = await store.list_provisional_observations(**owner, limit=4)
    assert [item.checked_at for item in observations] == [
        period_end + timedelta(hours=h) for h in (1, 7, 13)
    ]
    assert [item.acquisition.ordinal for item in observations] == [1, 2, 3]
    assert all(item.acquisition.policy.cadence == timedelta(hours=6) for item in observations)
    for item, impressions in zip(observations, (10, 11, 10)):
        page = await store.read_revision_rows(
            account_id=config.account_id, reporting_revision_id=item.revision_id
        )
        assert page.rows == ({"campaign_id": "campaign-redacted-1", "impressions": impressions},)


@pytest.mark.parametrize("period_already_created", [False, True])
async def test_standalone_period_recovers_generation_schedule_after_close_lag_removal(
    store_factory: Any, period_already_created: bool
) -> None:
    producer, store, config, _, _, clock = await _stabilized_harness(store_factory, official=False)
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    period_end = END + timedelta(days=1)
    clock[0] = period_end + timedelta(hours=1)
    if period_already_created:
        await producer.close_elapsed_periods(config)
    producer._source.capabilities = _capabilities(
        restatement_window="PT1H", restatement_cadence="PT1H", official_close_lag=None
    )
    restarted = _restart_producer(producer, store, clock)
    for hour in (1, 2, 7, 13):
        clock[0] = period_end + timedelta(hours=hour)
        await restarted.run_configuration(config)
    obligation = await store.find_obligation(
        account_id=config.account_id,
        consumer_id=config.consumer_id,
        delivery_config_id=config.delivery_config_id,
        delivery_config_version=config.delivery_config_version,
        period_start=END,
        period_end=period_end,
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    observations = await store.list_provisional_observations(**owner, limit=5)
    assert [item.checked_at for item in observations] == [
        period_end + timedelta(hours=h) for h in (1, 7, 13)
    ]
    assert all(item.acquisition.policy.cadence == timedelta(hours=6) for item in observations)
    assert all(item.acquisition.policy.until_stabilized for item in observations)
    assert [
        r.finalized_at for r in await store.list_revisions(**owner) if r.finality == "official"
    ] == [clock[0]]


@pytest.mark.parametrize(
    "missing",
    ["verify_snapshot_origin", "get_provisional_acquisition", "get_stabilization_schedule"],
)
@pytest.mark.parametrize("official,track", [(False, True), (True, True), (True, False)])
async def test_direct_tracked_snapshot_requires_stabilization_capabilities_before_dispatch(
    store_factory: Any, missing: str, official: bool, track: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, _ = await _stabilized_harness(
        store_factory, official=official
    )
    producer._source.capabilities = _capabilities(official_close_lag=None)
    monkeypatch.setattr(store, missing, None)
    with pytest.raises(LedgerConflictError, match=missing):
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", track_settling=track
        )
    assert not calls
    assert (
        await store.get_provisional_observation(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )


def _stabilization_publisher(store: Any) -> Any:
    class Publisher:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

    publisher = Publisher()
    for name in (
        "verify_snapshot_origin",
        "get_provisional_acquisition",
        "get_stabilization_schedule",
        "list_provisional_observations",
        "source_publication",
        "get_restatement_checkpoint",
        "record_restatement_checkpoint",
        "reserve_provisional_acquisition",
        "get_provisional_observation",
        "commit_provisional_observation",
    ):
        setattr(publisher, name, getattr(store, name))
    return publisher


def _restart_producer(
    producer: ReportingProducer, publisher: Any, clock: list[datetime]
) -> ReportingProducer:
    return ReportingProducer(
        source=producer._source,
        store=publisher,
        retry_store=producer._retry_store,
        offerings=producer._offerings,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
        read_jitter_window=timedelta(0),
    )


@pytest.mark.parametrize(
    "failure",
    [
        "get_stabilization_schedule",
        "get_provisional_acquisition",
        "verify_snapshot_origin",
        "origin",
    ],
)
@pytest.mark.parametrize("official", [True, False])
async def test_reserved_manifest_requires_stabilization_evidence_before_first_snapshot(
    store_factory: Any, failure: str, official: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        store_factory, official=official
    )
    producer._source.capabilities = _capabilities(official_close_lag=None)
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="snapshot", now=clock[0]
    )
    reservation = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(), 0, ProvisionalPolicy(timedelta(days=1), timedelta(hours=6))
        )
    )
    await producer._source.execute(request)
    manifest = producer._source.manifest
    publisher = _stabilization_publisher(store)
    if failure == "origin":

        async def contradict(candidate: ReportingRevisionRecord) -> None:
            assert candidate.source_publication_id == manifest.publication_id
            assert not await store.list_revisions(**owner)
            assert await store.get_provisional_observation(**owner) is None
            assert await store.get_restatement_checkpoint(**owner) is None
            raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "publisher origin differs")

        monkeypatch.setattr(publisher, "verify_snapshot_origin", contradict)
    else:
        monkeypatch.setattr(publisher, failure, None)
    fresh = _restart_producer(producer, publisher, clock)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    with pytest.raises(LedgerConflictError, match=failure):
        await fresh.commit_revision_from_manifest(
            replace(obligation, definition=None),
            manifest,
            rows=ROWS,
            finality="snapshot",
            acquisition=reservation,
        )
    assert not await store.list_revisions(**owner)
    assert await store.get_provisional_observation(**owner) is None
    assert await store.get_restatement_checkpoint(**owner) is None
    assert await store.get_provisional_acquisition(**owner, ordinal=0) == reservation
    fresh._store = store
    published = await fresh.commit_revision_from_manifest(
        obligation, manifest, rows=ROWS, finality="snapshot", acquisition=reservation
    )
    assert published.source_publication_id == manifest.publication_id
    assert (await store.get_restatement_checkpoint(**owner)).next_observation == 1


@pytest.mark.parametrize("entry", ["manifest", "acquire", "ledger"])
@pytest.mark.parametrize("official", [True, False])
async def test_manual_snapshot_reservation_without_stabilization_flag_checks_predecessor_origin(
    store_factory: Any, entry: str, official: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        store_factory, official=official
    )
    producer._source.capabilities = _capabilities(
        restatement_window="P3D", restatement_cadence="PT6H", official_close_lag=None
    )
    first = await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", track_settling=True
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    observations = await store.list_provisional_observations(**owner, limit=2)
    checkpoint = await store.get_restatement_checkpoint(**owner)
    clock[0] += timedelta(hours=1)
    request = producer._build_slice(
        config,
        obligation,
        SNAPSHOT_OFFERING_ID,
        finality="snapshot",
        trigger="manual_replay",
        observation=1,
        now=clock[0],
    )
    reservation = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(),
            1,
            ProvisionalPolicy(timedelta(days=3), timedelta(hours=6)),
            first.reporting_revision_id,
        )
    )
    assert "until_stabilized" not in reservation.to_wire()["policy"]
    publisher = _stabilization_publisher(store)

    async def contradict(predecessor: ReportingRevisionRecord) -> None:
        assert predecessor == first
        raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "publisher origin differs")

    monkeypatch.setattr(
        store if entry == "ledger" else publisher, "verify_snapshot_origin", contradict
    )
    fresh = _restart_producer(producer, publisher, clock)
    with pytest.raises(LedgerConflictError, match="publisher origin differs"):
        if entry == "ledger":
            await producer._source.execute(request)
            observation = ProvisionalObservation.from_manifest(
                reservation, first.reporting_revision_id, clock[0], producer._source.manifest
            )
            await store.commit_provisional_observation(observation, None, ROWS)
        elif entry == "manifest":
            await producer._source.execute(request)
            await fresh.commit_revision_from_manifest(
                obligation,
                producer._source.manifest,
                rows=ROWS,
                finality="snapshot",
                acquisition=reservation,
            )
        else:
            await fresh.acquire_obligation(
                config, obligation, target_finality="snapshot", manual_replay=True, restate=True
            )
    assert await store.list_provisional_observations(**owner, limit=2) == observations
    assert await store.get_restatement_checkpoint(**owner) == checkpoint
    assert await store.get_provisional_acquisition(**owner, ordinal=1) == reservation
    monkeypatch.undo()
    await producer._source.execute(request)
    manifest = producer._source.manifest
    if entry == "ledger":
        observation = ProvisionalObservation.from_manifest(
            reservation, first.reporting_revision_id, clock[0], manifest
        )
        origin = first.source_publication_id
        different_origin = origin[:-1] + ("0" if origin[-1] != "0" else "1")
        with pytest.raises(LedgerConflictError, match="origin|publication"):
            await store.commit_provisional_observation(
                replace(observation, revision_publication_id=different_origin), None, ROWS
            )
        assert await store.get_restatement_checkpoint(**owner) == checkpoint
    retried = await fresh.commit_revision_from_manifest(
        obligation, manifest, rows=ROWS, finality="snapshot", acquisition=reservation
    )
    assert retried == first
    latest = await store.get_provisional_observation(**owner)
    assert latest.revision_publication_id == first.source_publication_id
    if entry == "ledger":
        with pytest.raises(LedgerConflictError, match="origin|publication"):
            await store.commit_provisional_observation(
                replace(latest, revision_publication_id=different_origin), None, ROWS
            )


async def test_reserved_snapshot_replay_checks_the_publishers_witness(store_factory: Any) -> None:
    producer, store, config, obligation, _, _ = await _stabilized_harness(store_factory)
    await producer.run_configuration(config)
    first = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    publisher = _stabilization_publisher(store)

    async def contradict(candidate: ReportingRevisionRecord) -> None:
        assert candidate.reporting_revision_id == first.revision_id
        raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "publisher origin differs")

    publisher.verify_snapshot_origin = contradict
    producer._store = publisher
    with pytest.raises(LedgerConflictError, match="publisher origin differs"):
        await producer.commit_revision_from_manifest(
            obligation,
            producer._source.manifest,
            rows=ROWS,
            finality="snapshot",
            acquisition=first.acquisition,
        )


async def test_unchanged_scheduled_observation_respects_the_publishers_origin_witness(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    await producer.run_configuration(config)
    first = await store.get_provisional_observation(**owner)
    publisher = _stabilization_publisher(store)

    async def conflicting_origin(revision: ReportingRevisionRecord) -> None:
        assert revision.reporting_revision_id == first.revision_id
        raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "publisher origin differs")

    monkeypatch.setattr(publisher, "verify_snapshot_origin", conflicting_origin)
    producer._store = publisher
    clock[0] += timedelta(hours=6)
    await producer.run_configuration(config)
    assert await store.list_provisional_observations(**owner, limit=3) == (first,)
    assert all(r.finality == "snapshot" for r in await store.list_revisions(**owner))
    clock[0] += timedelta(hours=1)
    await producer.run_configuration(config)
    assert len(calls) == 2


async def test_direct_tracked_unchanged_snapshot_checks_the_admitted_witness_without_live_close(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        store_factory, official=False
    )
    producer._source.capabilities = _capabilities(
        restatement_window="P3D", restatement_cadence="PT6H", official_close_lag=None
    )
    publisher = _stabilization_publisher(store)
    producer._store = publisher
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    await producer.acquire_obligation(config, obligation, track_settling=True)
    first = await store.get_provisional_observation(**owner)

    async def conflicting_origin(revision: ReportingRevisionRecord) -> None:
        raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "publisher origin differs")

    monkeypatch.setattr(publisher, "verify_snapshot_origin", conflicting_origin)
    clock[0] += timedelta(hours=6)
    with pytest.raises(LedgerConflictError, match="publisher origin differs"):
        await producer.acquire_obligation(config, obligation, restate=True, track_settling=True)
    assert await store.list_provisional_observations(**owner, limit=3) == (first,)


@pytest.mark.parametrize("unchanged", ["PT12H", "P30D"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_stabilized_retires_base_shaped_derived_official_reservation(
    store_factory: Any, unchanged: str, wrapped: bool
) -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        store_factory,
        official=True,
        capabilities=_capabilities(restatement_window="P1D", restatement_cadence="PT6H"),
        finality_policy=ReportingStabilizedPolicy("stable", "PT0S", unchanged),
    )
    clock[0] = END + timedelta(hours=1)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="official", now=clock[0]
    )
    reservation = ProvisionalAcquisition(
        request.model_dump_json(),
        0,
        ProvisionalPolicy(timedelta(hours=6), timedelta(hours=6), timedelta(hours=6)),
        output_finality="official",
    )
    await store.reserve_provisional_acquisition(reservation)
    producer._source.capabilities = _capabilities(
        restatement_window="P1D", restatement_cadence="PT1H"
    )
    assert "until_stabilized" not in reservation.to_wire()["policy"]

    publisher = _stabilization_publisher(store) if wrapped else store
    fresh = _restart_producer(producer, publisher, clock)

    async def interrupt_replacement(*args: Any, **kwargs: Any) -> Any:
        raise OSError("interrupted before successor reservation")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fresh, "_reserve_observation", interrupt_replacement)
        with pytest.raises(OSError, match="before successor reservation"):
            await fresh.acquire_obligation(config, obligation)
    assert await store.get_stabilization_schedule(config.generation_key) == reservation.policy
    fresh = _restart_producer(producer, publisher, clock)
    for hour in (1, 1, 2, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await fresh.acquire_obligation(config, obligation)
    history = await store.list_provisional_observations(**owner, limit=8)
    assert [o.acquisition.ordinal for o in history] == [1, 2, 3]
    assert [o.checked_at for o in history] == [END + timedelta(hours=h) for h in (1, 7, 13)]
    assert all(o.acquisition.policy.cadence == timedelta(hours=6) for o in history)
    assert all(o.is_scheduled_snapshot for o in history)
    assert len(calls) == 3 and all(
        r.identity.source_execution_key != reservation.execution_key
        for r in producer._source.requests
    )
    assert sorted(r.finality for r in await store.list_revisions(**owner)) == (
        ["official", "snapshot"] if unchanged == "PT12H" else ["snapshot"]
    )


async def test_stabilization_cannot_rebind_a_contradictory_snapshot_origin(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7, 13, 19, 25):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    (snapshot,) = await store.list_revisions(**owner)
    origin = snapshot.source_publication_id
    forged = origin[:-1] + ("1" if origin[-1] == "0" else "0")
    if isinstance(store, InMemoryReportingLedgerStore):
        store._revisions[snapshot.reporting_revision_id] = replace(
            snapshot, source_publication_id=forged
        )
    else:
        async with store._pool.connection() as connection:
            await connection.execute(
                "UPDATE reporting_revisions SET source_publication_id=%s"
                " WHERE reporting_revision_id=%s",
                (forged, snapshot.reporting_revision_id),
            )
    # A previous publisher may already have copied the bad ID into an entire suffix.
    read = store.list_provisional_observations

    async def rebound_suffix(**kwargs: Any) -> Any:
        return tuple(
            replace(o, revision_publication_id=forged) if o.acquisition.ordinal > 0 else o
            for o in await read(**kwargs)
        )

    with monkeypatch.context() as patch:
        patch.setattr(store, "list_provisional_observations", rebound_suffix)
        with pytest.raises(LedgerConflictError, match="history"):
            await producer.acquire_obligation(config, obligation)

    async def crash_before_retry(*args: Any, **kwargs: Any) -> Any:
        raise OSError("interrupted retry write")

    clock[0] = END + timedelta(hours=31)
    with monkeypatch.context() as patch:
        patch.setattr(producer, "_schedule_retry", crash_before_retry)
        with pytest.raises(OSError, match="interrupted retry write"):
            await producer.acquire_obligation(config, obligation)
    producer = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
    )
    for hour in (31, 36, 37, 43, 49):
        clock[0] = END + timedelta(hours=hour)
        with suppress(LedgerConflictError):
            await producer.acquire_obligation(config, obligation)
    history = await store.list_provisional_observations(**owner, limit=16)
    assert all(o.revision_publication_id != forged for o in history)
    assert all(r.finality == "snapshot" for r in await store.list_revisions(**owner))
    assert len(calls) == 9
    assert len({r.identity.source_execution_key for r in producer._source.requests}) == 9

    monkeypatch.setattr(
        "tests.test_reporting_source_availability.PROVIDER_ROW", PROVIDER_ROW.replace("10", "11")
    )
    for hour in (55, 61, 67):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(config, obligation)
        official = [r for r in await store.list_revisions(**owner) if r.finality == "official"]
        assert [r.finalized_at for r in official] == ([clock[0]] if hour == 67 else [])


async def test_scheduled_reservation_waiting_behind_stabilized_official_is_terminal(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    clock[0] = END + timedelta(hours=19)
    inside_official = asyncio.Event()
    reaching_reservation = asyncio.Event()
    release = asyncio.Event()
    reserve = store.reserve_provisional_acquisition
    publication_lock = store.source_publication
    commit = store.commit_revision
    reservations = []

    async def hold_official(*args: Any, **kwargs: Any) -> Any:
        inside_official.set()
        await release.wait()
        return await commit(*args, **kwargs)

    @asynccontextmanager
    async def wait_for_lock(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if inside_official.is_set():
            reaching_reservation.set()
        async with publication_lock(*args, **kwargs) as publisher:
            yield publisher

    async def at_reservation(*args: Any, **kwargs: Any) -> Any:
        reaching_reservation.set()
        reservations.append(args[0])
        return await reserve(*args, **kwargs)

    monkeypatch.setattr(store, "source_publication", wait_for_lock)
    monkeypatch.setattr(store, "reserve_provisional_acquisition", at_reservation)
    monkeypatch.setattr(store, "commit_revision", hold_official)
    close = asyncio.create_task(producer.acquire_obligation(config, obligation))
    await asyncio.wait_for(inside_official.wait(), timeout=5)
    attempt = asyncio.create_task(
        producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    )
    try:
        await asyncio.wait_for(reaching_reservation.wait(), timeout=5)
    finally:
        release.set()
    official = await asyncio.wait_for(close, timeout=5)
    assert official is not None and official.finality == "official"
    assert await asyncio.wait_for(attempt, timeout=5) is None
    assert reservations == []
    assert await store.get_provisional_acquisition(**owner, ordinal=3) is None
    assert len(calls) == 3


async def test_untracked_manual_publication_cannot_follow_stabilized_official(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    clock[0] = END + timedelta(hours=14)
    paused = asyncio.Event()
    resumed = asyncio.Event()
    commit = producer._commit_publication
    candidates = []

    async def hold_manual(*args: Any, **kwargs: Any) -> Any:
        if args[4] == "snapshot":
            candidates.append((args[2], args[3]))
            paused.set()
            await resumed.wait()
        return await commit(*args, **kwargs)

    monkeypatch.setattr(producer, "_commit_publication", hold_manual)
    monkeypatch.setattr(
        "tests.test_reporting_source_availability.PROVIDER_ROW", PROVIDER_ROW.replace("10", "11")
    )
    manual = asyncio.create_task(
        producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, manual_replay=True
        )
    )
    try:
        await asyncio.wait_for(paused.wait(), timeout=5)
        official = await producer.acquire_obligation(config, obligation)
        assert official is not None and official.finality == "official"
    finally:
        resumed.set()
    assert await asyncio.wait_for(manual, timeout=5) is None
    (snapshot,) = [r for r in await store.list_revisions(**owner) if r.finality == "snapshot"]
    assert await store.commit_revision(snapshot, ROWS) == snapshot
    with pytest.raises(LedgerConflictError, match="official"):
        await store.commit_revision(*candidates[0])


@pytest.mark.parametrize("publish_manual", [True, False])
async def test_tracked_manual_snapshot_preserves_retired_scheduled_deadline(
    store_factory: Any, publish_manual: bool
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    await producer.run_configuration(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    first = await store.get_provisional_observation(**owner)
    for ordinal, hour, trigger in [(1, 7, "scheduled_poll"), (2, 8, "manual_replay")]:
        clock[0] = END + timedelta(hours=hour)
        request = producer._build_slice(
            config,
            obligation,
            SNAPSHOT_OFFERING_ID,
            finality="snapshot",
            now=clock[0],
            observation=ordinal,
            trigger=trigger,
        )
        acquisition = await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                request.model_dump_json(), ordinal, first.acquisition.policy, first.revision_id
            )
        )
        if trigger == "scheduled_poll":
            assert await producer._retire_stabilized_acquisition(
                config, obligation, acquisition, clock[0]
            )
        elif not publish_manual:

            async def reject_manual(*args: Any, **kwargs: Any) -> Any:
                raise LedgerConflictError("STABILIZATION_ORIGIN_CONFLICT", "conflicting origin")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(store, "commit_provisional_observation", reject_manual)
                with pytest.raises(LedgerConflictError, match="conflicting origin"):
                    await producer.acquire_obligation(
                        config,
                        obligation,
                        target_finality="snapshot",
                        restate=True,
                        manual_replay=True,
                    )
        else:
            await producer.acquire_obligation(
                config, obligation, target_finality="snapshot", restate=True, manual_replay=True
            )
    checkpoint = await store.get_restatement_checkpoint(**owner)
    assert checkpoint.next_observation == 3
    assert checkpoint.next_stabilization_due_at == END + timedelta(hours=13)
    await producer.acquire_obligation(config, obligation)
    assert len(calls) == 2
    clock[0] = END + timedelta(hours=13)
    await producer.acquire_obligation(config, obligation)
    assert len(calls) == 3


async def test_native_ledger_rejects_a_new_bare_snapshot_after_official(store_factory: Any) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    (snapshot,) = [r for r in await store.list_revisions(**owner) if r.finality == "snapshot"]
    later_id = "rpr_" + "f" * 40
    later = replace(
        snapshot,
        reporting_revision_id=later_id,
        supersedes_reporting_revision_id=snapshot.reporting_revision_id,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=later_id,
            row_count=snapshot.row_count,
            control_totals=snapshot.control_totals,
            reporting_rows=ROWS,
            control_total_evidence=snapshot.managed_control_totals,
        ),
    )
    with pytest.raises(LedgerConflictError, match="official"):
        await store.commit_revision(later, ROWS)
    assert await store.commit_revision(snapshot, ROWS) == snapshot


@pytest.mark.parametrize("change", ["none", "warnings", "basis"])
async def test_proofless_stabilized_replay_requires_its_qualifying_observation(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        InMemoryReportingLedgerStore
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    manifest = producer._source.manifest
    if change == "warnings":
        manifest = manifest.model_copy(update={"warnings": ["different evidence"]})
    if change == "basis":
        (official,) = (r for r in store._revisions.values() if r.finality == "official")
        store._revisions[official.reporting_revision_id] = replace(
            official, finality_basis="source_final"
        )

    async def missing(**kwargs: Any) -> tuple[Any, ...]:
        return ()

    monkeypatch.setattr(store, "list_provisional_observations", missing)
    assert await producer.acquire_obligation(config, obligation) is None
    with pytest.raises(LedgerConflictError, match="evidence|history"):
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=ROWS, finality="official"
        )


async def test_proofless_stabilized_replay_requires_the_qualifying_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        InMemoryReportingLedgerStore
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    read = store.list_provisional_observations

    async def truncated(**kwargs: Any) -> Any:
        return (await read(**kwargs))[-1:]

    monkeypatch.setattr(store, "list_provisional_observations", truncated)
    with pytest.raises(LedgerConflictError, match="history|evidence"):
        await producer.commit_revision_from_manifest(
            obligation, producer._source.manifest, rows=ROWS, finality="official"
        )


async def _stabilized_harness(
    store_factory: Any, *, minimum_age: str = "PT0S", official: bool = True
) -> tuple[Any, ...]:
    result = await _harness(
        store_factory,
        official=official,
        capabilities=_capabilities(
            restatement_window="PT6H", restatement_cadence="PT6H", official_close_lag="PT6H"
        ),
        finality_policy=ReportingStabilizedPolicy("stable-12h", minimum_age, "PT12H"),
    )
    result[-1][0] = END + timedelta(hours=1)
    return result


@pytest.mark.parametrize("official", [False, True])
async def test_stabilized_close_restarts_from_the_qualifying_observation(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, official: bool
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(
        store_factory, official=official
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    await producer.run_configuration(config)
    clock[0] += timedelta(hours=6)
    await producer.run_configuration(config)
    clock[0] += timedelta(hours=6)
    qualified_at = clock[0]
    original = store.commit_revision

    async def interrupted(revision: Any, rows: Any) -> Any:
        if revision.finality == "official":
            raise OSError("interrupted official commit")
        return await original(revision, rows)

    with monkeypatch.context() as patch:
        patch.setattr(store, "commit_revision", interrupted)
        with pytest.raises(OSError, match="interrupted official commit"):
            await producer.run_configuration(config)
    history = await store.list_provisional_observations(**owner, limit=4)
    assert [o.checked_at for o in history] == [END + timedelta(hours=h) for h in (1, 7, 13)]
    (snapshot,) = await store.list_revisions(**owner)
    assert all(o.revision_id == snapshot.reporting_revision_id for o in history)
    clock[0] += timedelta(minutes=10)
    fresh = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
    )
    turn = await fresh.run_configuration(config)
    closed = next(r for r in await store.list_revisions(**owner) if r.finality == "official")
    assert turn.revisions_committed == [closed.reporting_revision_id]
    assert closed.reporting_revision_id != snapshot.reporting_revision_id
    assert closed.supersedes_reporting_revision_id is None
    assert closed.finality_policy_id == "stable-12h" and closed.finality_basis == "stabilized"
    assert closed.finalized_at == qualified_at and closed.created_at == clock[0]
    manifest = SourceBatchManifestV1.model_validate_json(history[-1].manifest_json)
    assert closed.source_publication_id == manifest.publication_id
    assert (
        await store.read_revision_rows(
            account_id=config.account_id, reporting_revision_id=closed.reporting_revision_id
        )
    ).rows == tuple(ROWS)
    assert (
        await fresh.commit_revision_from_manifest(
            obligation, manifest, rows=ROWS, finality="official"
        )
        == closed
    )
    assert not (await fresh.run_configuration(config)).revisions_committed
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("reset", "hours"),
    [("changed", (1, 7, 13, 19)), ("gap", (1, 8, 14, 20)), ("age", (1, 7, 13, 19))],
)
async def test_stabilized_requires_a_complete_unchanged_run(
    reset: str, hours: tuple[int, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(
        ProgressStore, minimum_age="PT19H" if reset == "age" else "PT0S"
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for idx, hour in enumerate(hours):
        clock[0] = END + timedelta(hours=hour)
        if reset == "changed" and idx == 1:
            monkeypatch.setattr(
                "tests.test_reporting_source_availability.PROVIDER_ROW",
                PROVIDER_ROW.replace("10", "11"),
            )
        await producer.run_configuration(config)
        revisions = await store.list_revisions(**owner)
        assert any(r.finality == "official" for r in revisions) == (idx == 3)
        assert (obligation.reporting_obligation_id in store.pending) == (idx != 3)
    assert len(calls) == 4
    assert sum(r.finality == "snapshot" for r in revisions) == (2 if reset == "changed" else 1)


@pytest.mark.parametrize("corruption", ["unreadable", "publication", "owner", "unchanged-manifest"])
async def test_stabilized_requires_readable_history_and_retained_provenance(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    for hour in (1, 7):
        clock[0] = END + timedelta(hours=hour)
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    (snapshot,) = await store.list_revisions(**owner)
    if corruption == "unreadable":
        await store.set_revision_readable(
            account_id=config.account_id,
            reporting_revision_id=snapshot.reporting_revision_id,
            readable=False,
        )
    elif corruption in {"publication", "owner"}:
        get = store.get_revision

        async def corrupt_revision(**kwargs: Any) -> Any:
            revision = await get(**kwargs)
            if revision == snapshot:
                if corruption == "owner":
                    return replace(revision, account_id="different-account")
                return replace(revision, source_publication_id="src-" + "0" * 64)
            return revision

        monkeypatch.setattr(store, "get_revision", corrupt_revision)
    else:
        read = store.list_provisional_observations

        async def corrupt_history(**kwargs: Any) -> Any:
            observations = await read(**kwargs)
            return tuple(
                (
                    replace(
                        o,
                        manifest_json=json.dumps(
                            {**json.loads(o.manifest_json), "publication_id": "src-" + "0" * 64}
                        ),
                    )
                    if o.acquisition.ordinal == 1
                    else o
                )
                for o in observations
            )

        monkeypatch.setattr(store, "list_provisional_observations", corrupt_history)
    clock[0] += timedelta(hours=6)
    with pytest.raises(LedgerConflictError, match="history"):
        await producer.run_configuration(config)
    assert all(r.finality == "snapshot" for r in await store.list_revisions(**owner))


async def test_stabilized_manual_history_cannot_move_the_due_time_or_displace_scheduled_reads(
    store_factory: Any,
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", track_settling=True
    )
    first = await store.get_provisional_observation(**owner)
    for ordinal in range(1, 7):
        clock[0] += timedelta(minutes=5)
        request = producer._build_slice(
            config,
            obligation,
            SNAPSHOT_OFFERING_ID,
            finality="snapshot",
            now=clock[0],
            observation=ordinal,
            trigger="manual_replay",
        )
        await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                request.model_dump_json(), ordinal, first.acquisition.policy, first.revision_id
            )
        )
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, manual_replay=True
        )
    await producer.run_configuration(config)
    assert len(calls) == 7
    for hour in (7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    history = await store.list_provisional_observations(**owner, limit=10)
    assert [o.acquisition.ordinal for o in history if o.is_scheduled] == [0, 7, 8]
    assert [r.finality for r in await store.list_revisions(**owner)].count("official") == 1


async def test_stabilized_progress_survives_a_temporarily_unreadable_history() -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(ProgressStore)
    await producer.run_configuration(config)
    clock[0] += timedelta(hours=6)
    await producer.run_configuration(config)
    (snapshot,) = await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    key = dict(account_id=config.account_id, reporting_revision_id=snapshot.reporting_revision_id)
    await store.set_revision_readable(**key, readable=False)
    clock[0] += timedelta(hours=6)
    turn = await producer.run_configuration(config)
    assert obligation.reporting_obligation_id in turn.slices_failed
    assert obligation.reporting_obligation_id in store.pending
    await store.set_revision_readable(**key, readable=True)
    assert (await producer.run_configuration(config)).revisions_committed
    assert obligation.reporting_obligation_id not in store.pending


async def test_stabilized_history_rejects_an_extended_frozen_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer, store, config, _, _, clock = await _stabilized_harness(InMemoryReportingLedgerStore)
    await producer.run_configuration(config)
    read = store.list_provisional_observations

    async def extended(**kwargs: Any) -> Any:
        return tuple(
            replace(o, next_due_at=o.next_due_at + timedelta(hours=1)) for o in await read(**kwargs)
        )

    monkeypatch.setattr(store, "list_provisional_observations", extended)
    clock[0] += timedelta(hours=7)
    with pytest.raises(LedgerConflictError, match="history"):
        await producer.run_configuration(config)


async def test_stabilized_unchanged_suffix_binds_the_complete_origin_publication(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(store_factory)
    for hour in (1, 7, 13, 19, 25):
        clock[0] = END + timedelta(hours=hour)
        snapshot = await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, track_settling=True
        )
    assert snapshot is not None
    original = snapshot.source_publication_id
    forged = original[:-1] + ("1" if original[-1] == "0" else "0")
    get = store.get_revision

    async def replace_origin(**kwargs: Any) -> Any:
        revision = await get(**kwargs)
        return replace(revision, source_publication_id=forged) if revision == snapshot else revision

    monkeypatch.setattr(store, "get_revision", replace_origin)
    with pytest.raises(LedgerConflictError, match="history"):
        await producer.acquire_obligation(config, obligation)


async def test_stabilized_replay_compares_the_detached_qualifying_manifest() -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        InMemoryReportingLedgerStore
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    observed = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    manifest = SourceBatchManifestV1.model_validate_json(observed.manifest_json)
    other = manifest.model_copy(update={"warnings": ["other source evidence"]})
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, other, rows=ROWS, finality="official"
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"


async def test_settled_stabilized_obligation_needs_no_acquisition_history() -> None:
    from tests.test_reporting_official_acquisition import CoreStore

    producer, store, config, obligation, calls, clock = await _stabilized_harness(
        InMemoryReportingLedgerStore
    )
    for hour in (1, 7, 13):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    reader = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=CoreStore(store),
        retry_store=store,
        clock=lambda: clock[0],
    )
    assert await reader.acquire_obligation(config, obligation) is None
    assert not (await reader.run_configuration(config)).did_work
    assert len(calls) == 3


async def test_stabilized_unchanged_replay_binds_the_observations_own_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer, store, config, obligation, _, clock = await _stabilized_harness(
        InMemoryReportingLedgerStore
    )
    for hour in (1, 7):
        clock[0] = END + timedelta(hours=hour)
        await producer.run_configuration(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    observed = await store.get_provisional_observation(**owner)
    other = SourceBatchManifestV1.model_validate_json(observed.manifest_json).model_copy(
        update={"publication_id": "src-" + "0" * 64}
    )

    async def corrupted(**kwargs: Any) -> Any:
        return replace(observed, manifest_json=other.model_dump_json())

    monkeypatch.setattr(store, "get_provisional_observation", corrupted)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, other, rows=ROWS, finality="snapshot", acquisition=observed.acquisition
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"


async def test_stabilized_workers_cannot_consume_a_future_observation(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, obligation, calls, clock = await _stabilized_harness(store_factory)
    await producer.run_configuration(config)
    clock[0] += timedelta(hours=6)
    other = ReportingProducer(
        source=producer._source,
        offerings=producer._offerings,
        store=store,
        object_reader=producer._object_reader,
        clock=lambda: clock[0],
    )
    acquire = producer.acquire_obligation

    async def after_other_worker(*args: Any, **kwargs: Any) -> Any:
        await other.run_configuration(config)
        return await acquire(*args, **kwargs)

    monkeypatch.setattr(producer, "acquire_obligation", after_other_worker)
    await producer.run_configuration(config)
    history = await store.list_provisional_observations(
        account_id=config.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        limit=4,
    )
    assert [o.acquisition.ordinal for o in history] == [0, 1]
    assert len(calls) == 2


async def test_stabilized_restart_reads_at_the_admitted_cadence_after_capabilities_change() -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        official=True,
        capabilities=_capabilities(restatement_window="P1D", restatement_cadence="PT6H"),
        finality_policy=ReportingStabilizedPolicy("stable", "PT0S", "P30D"),
    )
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    producer._source.capabilities = _capabilities(
        restatement_window="P1D", restatement_cadence="PT1H"
    )
    clock[0] += timedelta(hours=6)
    await producer.run_configuration(config)
    assert len(calls) == 2
    observed = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert observed.next_due_at == clock[0] + timedelta(hours=6)


@pytest.fixture(params=["memory", "postgres"])
async def store_factory(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryReportingLedgerStore
    else:
        async with isolated_reporting_pool() as pool:
            yield partial(PgReportingLedgerStore, pool=pool)


@pytest.mark.parametrize("configure_before_init", [True, False])
async def test_restart_resumes_ordinary_snapshot_with_unused_stabilized_policy(
    store_factory: Any, configure_before_init: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_reliable_reporting_service import (
        DEFINITION,
        NOW,
        _configuration,
        _finality_service,
        _rows,
    )

    config = replace(
        _configuration(),
        activated_at=NOW.replace(hour=1, minute=0),
        deactivated_at=NOW.replace(hour=2, minute=0),
        definition=replace(
            DEFINITION, finality_policy=ReportingStabilizedPolicy("unused", "PT0S", "PT1H")
        ),
    )
    clock = DeterministicReportingClock(NOW)
    store = store_factory(clock=clock)
    monkeypatch.setattr(store, "get_stabilization_schedule", None)
    monkeypatch.setattr(store, "verify_snapshot_origin", None)

    def service() -> ReliableReportingService:
        return _finality_service(
            config,
            snapshot=True,
            official=False,
            store=store,
            clock=clock,
            cadence="PT1H",
            results=(InlineFetchResult(rows=_rows(10), data_through=config.deactivated_at),),
        )

    original = service()
    await original.configure(config)
    created = await original.run_worker()
    (identifier,) = created.configurations[config.generation_key].revisions_committed
    await original.close()
    clock.advance(timedelta(hours=1))
    restarted = service()
    if configure_before_init:
        await restarted.configure(config)
    await restarted.initialize()
    if not configure_before_init:
        await restarted.configure(config)
    turn = await restarted.run_worker()
    assert not turn.configuration_errors
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=identifier
    )
    history = await store.list_provisional_observations(
        account_id=config.account_id,
        reporting_obligation_id=revision.reporting_obligation_id,
        limit=3,
    )
    assert [o.revision_id for o in history] == [identifier, identifier]
    assert revision.finality == "snapshot"


@pytest.mark.parametrize("interleave", [True, False])
@pytest.mark.parametrize("schedule_lookup", [True, False])
async def test_standalone_new_snapshot_with_unused_policy_needs_no_optional_lookups(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, interleave: bool, schedule_lookup: bool
) -> None:
    store = store_factory()
    for name in ("source_publication", "get_provisional_acquisition"):
        monkeypatch.setattr(store, name, None)
    if not schedule_lookup:
        monkeypatch.setattr(store, "get_stabilization_schedule", None)
    producer, _, config, obligation, _, clock = await _harness(
        lambda **kwargs: store, finality_policy=ReportingContractualCutoffPolicy("unused", "P2D")
    )
    if interleave:
        other = replace(config, delivery_config_id="other")
        await store.put_configuration(other)
        await producer.close_elapsed_periods(other)
    clock[0] = END + timedelta(hours=1)
    turn = await producer.run_configuration(config)
    (identifier,) = turn.revisions_committed
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=identifier
    )
    assert (
        revision.reporting_obligation_id == obligation.reporting_obligation_id
        and revision.finality == "snapshot"
    )


@pytest.mark.parametrize("period", ["admitted", "external"])
async def test_snapshot_admission_checks_another_publishers_pending_close(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, period: str
) -> None:
    store = store_factory()
    for name in ("source_publication", "get_stabilization_schedule", "get_provisional_acquisition"):
        monkeypatch.setattr(store, name, None)
    producer, _, config, obligation, calls, clock = await _harness(
        lambda **kwargs: store, finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    if period == "external":
        clock[0] += timedelta(days=1)
        boundary = producer._elapsed_periods(config, now=clock[0])[-1]
        obligation = await store.commit_obligation(
            replace(
                obligation,
                reporting_obligation_id=producer._obligation_id(config, boundary),
                period=boundary,
                scope_resolved_at=boundary.end,
                automated_recovery_deadline_at=boundary.expected_at,
            )
        )
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="snapshot", now=clock[0]
    )
    await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(),
            0,
            ProvisionalPolicy(timedelta(days=3), timedelta(hours=1), timedelta(days=2)),
        )
    )
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.acquire_obligation(config, obligation, target_finality="snapshot")
    assert calls == []
    assert not await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )


@pytest.mark.parametrize("completed_ordinary", [False, True])
@pytest.mark.parametrize("operation", ["manual", "next_period"])
async def test_first_snapshot_admission_cannot_hide_a_later_publishers_close(
    store_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    completed_ordinary: bool,
    operation: str,
) -> None:
    store = store_factory()
    publication = store.source_publication
    lookup = store.get_provisional_acquisition
    for name in ("source_publication", "get_stabilization_schedule", "get_provisional_acquisition"):
        monkeypatch.setattr(store, name, None)
    producer, _, config, obligation, calls, clock = await _harness(
        lambda **kwargs: store, finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    clock[0] = END + timedelta(hours=1)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    if completed_ordinary:
        await producer.run_configuration(config)
        ordinary = await store.get_provisional_observation(**owner)
        assert ordinary.acquisition.policy.official_close_lag is None
    revisions = await store.list_revisions(**owner)
    previous_calls = list(calls)
    ordinal = int(completed_ordinary)
    request = producer._build_slice(
        config,
        obligation,
        SNAPSHOT_OFFERING_ID,
        finality="snapshot",
        now=clock[0],
        observation=ordinal,
    )
    async with publication(config.account_id):
        winning = await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                request.model_dump_json(),
                ordinal,
                ProvisionalPolicy(timedelta(days=3), timedelta(hours=1), timedelta(days=2)),
                revisions[-1].reporting_revision_id if revisions else None,
            )
        )
    assert winning.policy.official_close_lag == timedelta(days=2)
    clock[0] += timedelta(days=1)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        if operation == "manual":
            await producer.acquire_obligation(
                config, obligation, target_finality="snapshot", restate=True, manual_replay=True
            )
        else:
            await producer.close_elapsed_periods(config)
    assert calls == previous_calls
    assert await lookup(**owner, ordinal=ordinal) == winning
    assert await store.list_revisions(**owner) == revisions
    later = await store.find_obligation(
        account_id=config.account_id,
        consumer_id=config.consumer_id,
        delivery_config_id=config.delivery_config_id,
        delivery_config_version=config.delivery_config_version,
        period_start=END,
        period_end=END + timedelta(days=1),
    )
    assert later is None


def _stabilized_only_lookup(
    store: Any,
) -> Callable[[ReportingConfigurationGenerationKey], Awaitable[ProvisionalPolicy | None]]:
    lookup = store.get_stabilization_schedule

    async def read(generation: ReportingConfigurationGenerationKey) -> ProvisionalPolicy | None:
        schedule = await lookup(generation)
        return schedule if schedule is not None and schedule.until_stabilized else None

    return read


async def test_completed_reservation_cannot_authorize_dispatch_past_a_competing_close(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_factory()
    publication = store.source_publication
    lookup = store.get_provisional_acquisition
    reserve = store.reserve_provisional_acquisition
    for name in ("source_publication", "get_stabilization_schedule", "get_provisional_acquisition"):
        monkeypatch.setattr(store, name, None)
    producer, _, config, obligation, calls, clock = await _harness(
        lambda **kwargs: store, finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    clock[0] = END + timedelta(hours=1)

    async def finish_and_reserve_close(candidate: ProvisionalAcquisition) -> ProvisionalAcquisition:
        winning = await reserve(candidate)
        # The locked publisher finishes this ordinal and reserves the next
        # close before the unlocked worker gets its reservation back.
        with monkeypatch.context() as locked:
            locked.setattr(store, "source_publication", publication)
            locked.setattr(store, "get_provisional_acquisition", lookup)
            locked.setattr(store, "reserve_provisional_acquisition", reserve)
            peer = _restart_producer(producer, store, clock)
            revision = await peer.acquire_obligation(config, obligation, target_finality="snapshot")
            request = peer._build_slice(
                config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], observation=1
            )
            async with publication(config.account_id):
                await reserve(
                    ProvisionalAcquisition(
                        request.model_dump_json(),
                        1,
                        replace(candidate.policy, official_close_lag=timedelta(days=2)),
                        revision.reporting_revision_id,
                    )
                )
        return winning

    monkeypatch.setattr(store, "reserve_provisional_acquisition", finish_and_reserve_close)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.acquire_obligation(config, obligation, target_finality="snapshot")
    assert calls == [clock[0]]
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert (await store.get_restatement_checkpoint(**owner)).next_observation == 1
    assert (await lookup(**owner, ordinal=1)).policy.official_close_lag == timedelta(days=2)


async def test_new_ordinary_worker_can_publish_its_first_overdue_period(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    producer, store, config, _, calls, clock = await _harness(
        store_factory, finality_policy=ReportingContractualCutoffPolicy("unused", "P2D")
    )
    for name in ("source_publication", "get_stabilization_schedule", "get_provisional_acquisition"):
        monkeypatch.setattr(store, name, None)
    fresh = replace(config, delivery_config_id="new-overdue")
    await store.put_configuration(fresh)
    clock[0] = END + timedelta(days=1, hours=1)
    turn = await producer.run_configuration(fresh)
    (identifier,) = turn.revisions_committed
    revision = await store.get_revision(
        account_id=fresh.account_id, reporting_revision_id=identifier
    )
    obligation = await store.get_obligation(
        account_id=fresh.account_id, reporting_obligation_id=revision.reporting_obligation_id
    )
    assert len(calls) == 1
    assert obligation.period.end == END and revision.finality == "snapshot"


@pytest.mark.parametrize("completed", [False, True])
async def test_new_period_checks_an_authoritative_close_with_an_ordinary_predecessor(
    store_factory: Any, monkeypatch: pytest.MonkeyPatch, completed: bool
) -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        store_factory, finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final")
    )
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    ordinary = await store.get_provisional_observation(**owner)
    assert ordinary.acquisition.policy.official_close_lag is None
    request = producer._build_slice(
        config, obligation, OFFICIAL_OFFERING_ID, finality="official", now=clock[0], observation=1
    )
    async with store.source_publication(config.account_id):
        reserved = await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(request.model_dump_json(), 1, ordinary.acquisition.policy)
        )
    assert reserved.finality == "official"
    if completed:
        closed = await producer.acquire_obligation(config, obligation, target_finality="official")
        assert closed.finality == "official"
    before = list(calls)
    monkeypatch.setattr(store, "source_publication", None)
    monkeypatch.setattr(store, "get_stabilization_schedule", _stabilized_only_lookup(store))
    assert await store.get_stabilization_schedule(config.generation_key) is None
    clock[0] += timedelta(days=1)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.close_elapsed_periods(config)
    assert calls == before


@pytest.mark.parametrize("configure_before_init", [True, False])
@pytest.mark.parametrize(
    "policy,missing",
    [
        (ReportingStabilizedPolicy("stable", "PT0S", "PT6H"), "get_stabilization_schedule"),
        (ReportingSourceFinalPolicy("provider", "closed"), "source_publication"),
        (ReportingContractualCutoffPolicy("cutoff", "P2D"), "source_publication"),
    ],
)
async def test_recovery_checks_pending_close_after_untracked_manual_revision(
    store_factory: Any,
    configure_before_init: bool,
    policy: Any,
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_reliable_reporting_service import (
        DEFINITION,
        NOW,
        _configuration,
        _finality_service,
        _rows,
    )

    config = replace(
        _configuration(),
        activated_at=NOW.replace(hour=1, minute=0),
        deactivated_at=NOW.replace(hour=2, minute=0),
        definition=replace(DEFINITION, finality_policy=policy),
    )
    clock = DeterministicReportingClock(NOW)
    store = store_factory(clock=clock)
    original = _finality_service(
        config,
        snapshot=True,
        official=isinstance(policy, ReportingSourceFinalPolicy),
        close=True,
        store=store,
        clock=clock,
        cadence="PT1H",
        results=(InlineFetchResult(rows=_rows(10), data_through=config.deactivated_at), None),
    )
    await original.configure(config)
    await original.initialize()
    producer = original._bindings[config.generation_key].producer
    (obligation,) = await producer.close_elapsed_periods(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    manual = await producer.acquire_obligation(config, obligation, manual_replay=True)
    assert manual is not None
    await producer.run_configuration(config)
    assert await store.get_restatement_checkpoint(**owner) is None
    assert await store.get_provisional_acquisition(**owner, ordinal=0) is None
    pending = await store.get_provisional_acquisition(**owner, ordinal=1)
    assert pending is not None and pending.policy.official_close_lag is not None
    await original.close()
    monkeypatch.setattr(store, missing, None)
    if missing == "source_publication":
        monkeypatch.setattr(store, "get_stabilization_schedule", None)
    recovered = _finality_service(
        config,
        snapshot=True,
        official=isinstance(policy, ReportingSourceFinalPolicy),
        cadence="PT1H",
        store=store,
        clock=clock,
    )
    expected_error = "publication lock" if missing == "source_publication" else missing
    with pytest.raises(ReliableReportingConfigurationError, match=expected_error):
        if configure_before_init:
            await recovered.configure(config)
        await recovered.initialize()
    assert config.generation_key not in recovered._bindings
    assert await store.get_restatement_checkpoint(**owner) is None
    assert await store.get_provisional_acquisition(**owner, ordinal=1) == pending


@pytest.mark.parametrize(
    "policy",
    [
        ReportingSourceFinalPolicy("provider", "delivery.is_final"),
        ReportingContractualCutoffPolicy("cutoff", "P2D"),
    ],
)
@pytest.mark.parametrize("operation", ["reserve", "acquire", "direct", "next_period"])
@pytest.mark.parametrize("stabilized_only_lookup", [False, True])
async def test_frozen_explicit_close_requires_lock_before_snapshot_work(
    store_factory: Any,
    policy: Any,
    operation: str,
    stabilized_only_lookup: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        store_factory,
        capabilities=_capabilities(
            restatement_window="P3D", restatement_cadence="PT1H", official_close_lag="P3D"
        ),
        finality_policy=policy,
    )
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    first = await store.get_provisional_observation(**owner)
    assert first.acquisition.policy.official_close_lag == timedelta(days=3)
    revisions = await store.list_revisions(**owner)
    producer._source.capabilities = _capabilities(
        restatement_window="P3D", restatement_cadence="PT1H"
    )
    clock[0] += timedelta(hours=1)
    restarted = _restart_producer(producer, store, clock)
    monkeypatch.setattr(store, "source_publication", None)
    if stabilized_only_lookup:
        monkeypatch.setattr(store, "get_stabilization_schedule", _stabilized_only_lookup(store))
        assert await store.get_stabilization_schedule(config.generation_key) is None
    request = restarted._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="snapshot", now=clock[0], observation=1
    )
    with pytest.raises(LedgerConflictError, match="publication lock"):
        if operation == "reserve":
            await restarted._reserve_observation(
                config,
                obligation,
                request,
                ordinal=1,
                revisions=revisions,
                previous_boundary=first.provisional_until,
                derived_close=False,
            )
        elif operation == "acquire":
            await restarted.acquire_obligation(
                config, obligation, restate=True, target_finality="snapshot"
            )
        elif operation == "direct":
            _, manifest, restarted._object_reader = redacted_completed_result(
                request, page_bodies=[PROVIDER_ROW]
            )
            await restarted.commit_revision_from_manifest(
                obligation, manifest, rows=ROWS, finality="snapshot"
            )
        else:
            clock[0] += timedelta(days=1)
            await restarted.close_elapsed_periods(config)
    assert len(calls) == 1
    assert await store.get_provisional_acquisition(**owner, ordinal=1) is None
    assert await store.list_revisions(**owner) == revisions


@pytest.mark.parametrize("configure_before_init", [True, False])
@pytest.mark.parametrize(
    "policy",
    [
        ReportingSourceFinalPolicy("source", "closed"),
        ReportingContractualCutoffPolicy("cutoff", "P1D"),
        ReportingStabilizedPolicy("stable", "PT0S", "PT6H"),
    ],
)
@pytest.mark.parametrize(
    "state",
    ["new", "ordinary", "completed", "stabilized_lookup", "manual_after_close", "ambiguous"],
)
async def test_snapshot_admission_distinguishes_new_from_retained_close(
    store_factory: Any,
    configure_before_init: bool,
    policy: Any,
    state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_reliable_reporting_service import (
        DEFINITION,
        NOW,
        _configuration,
        _finality_service,
        _rows,
    )

    config = replace(
        _configuration(),
        activated_at=NOW.replace(hour=1, minute=0),
        deactivated_at=NOW.replace(hour=2, minute=0),
        definition=replace(DEFINITION, finality_policy=policy),
    )
    clock = DeterministicReportingClock(NOW)
    store = store_factory(clock=clock)
    result = InlineFetchResult(rows=_rows(10), data_through=config.deactivated_at)
    if state != "new":
        original = _finality_service(
            config,
            snapshot=True,
            official=isinstance(policy, ReportingSourceFinalPolicy),
            close=state != "ordinary",
            store=store,
            clock=clock,
            cadence="PT1H",
            results=(result, result),
        )
        await original.configure(config)
        await original.initialize()
        producer = original._bindings[config.generation_key].producer
        (obligation,) = await producer.close_elapsed_periods(config)
        owner = dict(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        if state != "ambiguous":
            await producer.run_configuration(config)
            retained = await store.get_provisional_observation(**owner)
            assert retained.acquisition.policy.official_close_lag == (
                None if state == "ordinary" else timedelta(days=2)
            )
            if state == "manual_after_close":
                clock.advance(timedelta(minutes=5))
                request = producer._build_slice(
                    config,
                    obligation,
                    SNAPSHOT_OFFERING_ID,
                    finality="snapshot",
                    now=clock(),
                    observation=1,
                    trigger="manual_replay",
                )
                await store.reserve_provisional_acquisition(
                    ProvisionalAcquisition(
                        request.model_dump_json(),
                        1,
                        retained.acquisition.policy,
                        retained.revision_id,
                    )
                )
                await producer.acquire_obligation(
                    config,
                    obligation,
                    restate=True,
                    target_finality="snapshot",
                    manual_replay=True,
                    track_settling=True,
                )
                latest = await store.get_provisional_observation(**owner)
                assert latest.acquisition.ordinal == 1 and not latest.is_scheduled
        await original.close()
        if state == "ordinary":
            clock.advance(timedelta(hours=1))

    monkeypatch.setattr(store, "source_publication", None)
    monkeypatch.setattr(
        store,
        "get_stabilization_schedule",
        _stabilized_only_lookup(store) if state == "stabilized_lookup" else None,
    )
    monkeypatch.setattr(store, "verify_snapshot_origin", None)
    if state in {"completed", "stabilized_lookup", "manual_after_close"}:

        async def only_pending(**kwargs: Any) -> None:
            # A completed reservation is no longer returned by the pending-only API.
            return None

        monkeypatch.setattr(store, "get_provisional_acquisition", only_pending)
        checkpoint = store.get_restatement_checkpoint

        async def historical_checkpoint(**kwargs: Any) -> Any:
            value = await checkpoint(**kwargs)
            return (
                replace(value, stabilization_policy=None, next_stabilization_due_at=None)
                if value
                else value
            )

        monkeypatch.setattr(store, "get_restatement_checkpoint", historical_checkpoint)
        if state == "completed" or (
            state == "stabilized_lookup" and not isinstance(policy, ReportingStabilizedPolicy)
        ):
            # A custom atomic publisher can expose its latest read without a history interface.
            read = store.list_provisional_observations

            async def read_latest(**kwargs: Any) -> ProvisionalObservation | None:
                history = await read(**kwargs, limit=1)
                return history[-1] if history else None

            monkeypatch.setattr(store, "get_provisional_observation", read_latest)
            monkeypatch.setattr(store, "list_provisional_observations", None)
    else:
        monkeypatch.setattr(store, "get_provisional_acquisition", None)
    service = _finality_service(
        config,
        snapshot=True,
        official=isinstance(policy, ReportingSourceFinalPolicy),
        store=store,
        clock=clock,
        cadence="PT1H",
        results=(result,),
    )

    async def start() -> None:
        if configure_before_init:
            await service.configure(config)
        await service.initialize()
        if not configure_before_init:
            await service.configure(config)

    if state == "new":
        await start()
        turn = await service.run_worker()
        (identifier,) = turn.configurations[config.generation_key].revisions_committed
        revision = await store.get_revision(
            account_id=config.account_id, reporting_revision_id=identifier
        )
        assert revision.finality == "snapshot"
    elif state == "ordinary":
        await start()
        turn = await service.run_worker()
        assert not turn.configuration_errors
        history = await store.list_provisional_observations(**owner, limit=3)
        assert [item.revision_id for item in history] == [
            retained.revision_id,
            retained.revision_id,
        ]
    else:
        expected = (
            "get_stabilization_schedule"
            if isinstance(policy, ReportingStabilizedPolicy) and state != "stabilized_lookup"
            else "publication lock"
        )
        with pytest.raises(ReliableReportingConfigurationError, match=expected):
            await start()
        assert config.generation_key not in service._bindings


def test_source_final_evidence_ref_accepts_512_characters() -> None:
    evidence = ReportingFinalityEvidenceV1(
        basis="provider_declared", observed_at=END, evidence_ref="s" * 512
    )
    assert evidence.evidence_ref == "s" * 512


@pytest.mark.parametrize("signal", ["", "s" * 513])
def test_source_final_evidence_ref_rejects_out_of_bounds(signal: str) -> None:
    with pytest.raises(ValidationError):
        ReportingFinalityEvidenceV1(basis="provider_declared", observed_at=END, evidence_ref=signal)


@pytest.mark.parametrize(
    ("basis", "signal"),
    [("provider_declared", "delivery.is_final"), ("provider_job_terminal", "s" * 512)],
)
async def test_source_final_emits_exact_selected_policy(basis: str, signal: str) -> None:
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("delivery-final-v1", signal)
    )
    provider = producer._source
    provider.basis, provider.signal = basis, signal
    provider.read_delay = timedelta(seconds=3)
    observed = clock[0]

    revision = await producer.acquire_obligation(config, obligation)

    assert revision is not None and revision.finality == "official"
    assert revision.finality_policy_id == "delivery-final-v1"
    assert revision.finality_basis == "source_final"
    assert revision.finalized_at == observed < revision.created_at
    assert provider.requests[0].publication_class == "AUTHORITATIVE"
    assert provider.requests[0].offering_id == OFFICIAL_OFFERING_ID


@pytest.mark.parametrize(
    ("basis", "signal"),
    [
        ("provider_declared", "Delivery.is_final"),
        ("provider_declared", None),
        ("elapsed_settlement_window", "delivery.is_final"),
    ],
)
async def test_source_final_rejects_unproven_signal(basis: str, signal: str | None) -> None:
    producer, store, config, obligation, _, _ = await _harness(
        official=True,
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "delivery.is_final"),
    )
    producer._source.basis, producer._source.signal = basis, signal
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation)
    assert conflict.value.code == "MANIFEST_MISMATCH"
    assert not await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )


async def test_source_final_refuses_evidence_before_obligation_period_end() -> None:
    policy = ReportingSourceFinalPolicy("selected", "source-evidence-redacted")
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    later_end = END + timedelta(hours=1)
    later = replace(
        obligation, period=replace(obligation.period, end=later_end), scope_resolved_at=later_end
    )
    with pytest.raises(LedgerConflictError, match="source signal"):
        source_finality_proof(later, manifest, policy)


async def test_source_final_refuses_a_manifest_from_another_offering() -> None:
    policy = ReportingSourceFinalPolicy("selected", "source-evidence-redacted")
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    producer._offerings = replace(producer._offerings, official_offering_id="other-authority")
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="official",
            finality_proof=source_finality_proof(obligation, manifest, policy),
        )
    assert conflict.value.code == "MANIFEST_MISMATCH"


async def test_contractual_cutoff_promotes_a_post_cutoff_snapshot() -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        official=True,
        finality_policy=ReportingContractualCutoffPolicy("two-hour-cutoff", "PT2H"),
        capabilities=_closing_capabilities(),
    )
    producer._offerings = replace(producer._offerings, official_offering_id=None)
    producer._source.read_delay = timedelta(seconds=3)
    cutoff = END + timedelta(hours=2)
    clock[0] = cutoff - timedelta(microseconds=1)
    assert not (await producer.run_configuration(config)).revisions_committed
    assert await producer.acquire_obligation(config, obligation, manual_replay=True) is None
    assert not calls
    clock[0] = cutoff
    turn = await producer.run_configuration(config)

    assert len(turn.revisions_committed) == 1
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert revision is not None and revision.finality == "official"
    assert revision.finality_policy_id == "two-hour-cutoff"
    assert revision.finality_basis == "contractual_cutoff"
    assert revision.observed_at == cutoff
    assert revision.finalized_at == revision.created_at == cutoff + timedelta(seconds=3)
    request = producer._source.requests[0]
    assert (request.offering_id, request.publication_class) == (
        SNAPSHOT_OFFERING_ID,
        "PROVISIONAL_SNAPSHOT",
    )
    assert request.coverage.expected == "full"


async def test_manual_contractual_cutoff_acquires_fresh_content_after_unchanged_reads(
    monkeypatch: pytest.MonkeyPatch,
    store_factory: Any,
) -> None:
    scheduled, store, config, obligation, _, clock = await _harness(
        store_factory,
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("three-hours", "PT3H"),
    )
    staging = InMemoryStagingStore()
    calls = []

    def fetch(request: Any) -> list[dict[str, Any]]:
        calls.append(request)
        return [{"media_buy_id": "media-buy-redacted", "impressions": 10, "spend": "1.25"}]

    source = InlineReportingSource(
        capabilities=scheduled._source.capabilities,
        fetch=fetch,
        clock=lambda: clock[0],
        staging=staging,
        seals=InMemorySealStore(),
    )
    producer = ReportingProducer(
        source=source,
        store=store,
        offerings=scheduled._offerings,
        object_reader=staging,
        clock=lambda: clock[0],
        read_jitter_window=timedelta(0),
    )
    clock[0] = END + timedelta(hours=1)
    first = await producer.acquire_obligation(config, obligation, track_settling=True)
    assert first is not None and first.finality == "snapshot"
    clock[0] += timedelta(hours=1)
    assert (
        await producer.acquire_obligation(config, obligation, restate=True, track_settling=True)
        == first
    )
    clock[0] += timedelta(hours=1)

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted staged read")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "read", interrupted)
        with pytest.raises(OSError, match="interrupted staged read"):
            await producer.acquire_obligation(
                config, obligation, restate=True, target_finality="official", manual_replay=True
            )
    assert len(calls) == 3

    clock[0] += timedelta(hours=1)
    turn = await producer.run_configuration(config)
    assert len(turn.revisions_committed) == 1
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert revision is not None and revision.finality == "official"
    assert len(calls) == 3
    observation = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert observation is not None
    persisted = observation.acquisition.request()
    sealed = SourceBatchManifestV1.model_validate_json(observation.manifest_json)
    assert persisted.identity == sealed.identity == calls[-1].identity
    assert persisted.period == sealed.period == calls[-1].period
    assert revision.finalized_at == clock[0]


@pytest.mark.parametrize("stale", ["observed_at", "batch", "constituent", "metric"])
async def test_contractual_proof_requires_post_cutoff_observation_and_full_watermarks(
    stale: str,
) -> None:
    policy = ReportingContractualCutoffPolicy("cutoff", "PT2H")
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    clock[0] = END + timedelta(hours=2)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(
        request,
        observed_at=clock[0] - timedelta(seconds=1) if stale == "observed_at" else clock[0],
        data_through=END - timedelta(seconds=1) if stale == "batch" else END,
    )
    raw = manifest.model_dump()
    raw["acquired_at"] = clock[0]
    if stale == "constituent":
        raw["coverage"]["constituents"][0]["data_through"] = END - timedelta(seconds=1)
    if stale == "metric":
        raw["metric_availability"][0]["data_through"] = END - timedelta(seconds=1)
    manifest = SourceBatchManifestV1.model_validate(raw)
    with pytest.raises(LedgerConflictError) as error:
        manifest_finality_proof(obligation, manifest, clock[0])
    assert error.value.code == "FINALITY_POLICY_UNPROVEN"


@pytest.mark.parametrize("entry", ["acquire", "commit"])
@pytest.mark.parametrize("different", ["namespace", "scope", "build", "constituents", "metrics"])
async def test_new_official_binds_provider_and_denominator(
    entry: str, different: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ReportingSourceFinalPolicy("selected", "source-evidence-redacted")
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    other = request
    if different == "namespace":
        other = request.model_copy(update={"publication_namespace": "reporting-source:alternate"})
    elif different == "scope":
        other = request.model_copy(
            update={
                "identity": request.identity.model_copy(
                    update={"source_scope": {"provider": "other"}}
                )
            }
        )
    elif different == "build":
        other = request.model_copy(
            update={
                "adapter_build": request.adapter_build.model_copy(
                    update={"adapter_version": "other"}
                )
            }
        )
    elif different == "constituents":
        members = [request.coverage.constituents[0].model_copy(update={"product_id": "other"})]
        other = request.model_copy(
            update={
                "coverage": request.coverage.model_copy(
                    update={
                        "constituents": members,
                        "denominator_fingerprint": coverage_denominator_fingerprint_v1(members),
                    }
                )
            }
        )
    else:
        other = request.model_copy(update={"requested_metrics": ["impressions"]})
    result, manifest, _ = redacted_completed_result(other, page_bodies=[PROVIDER_ROW])
    _, _, producer._source.reader = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])

    async def execute(*args: Any, **kwargs: Any) -> ReportingSourceExecutorResult:
        return result

    monkeypatch.setattr(producer._source, "execute", execute)
    with pytest.raises(LedgerConflictError) as error:
        if entry == "acquire":
            await producer.acquire_obligation(config, obligation)
        else:
            await producer.commit_revision_from_manifest(
                obligation,
                manifest,
                rows=ROWS,
                finality="official",
                finality_proof=source_finality_proof(obligation, manifest, policy),
            )
    assert error.value.code == "MANIFEST_MISMATCH"
    assert not await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )


@pytest.mark.parametrize("after_reservation", [timedelta(hours=1), timedelta(hours=3)])
async def test_manual_cutoff_finishes_reserved_snapshot_first(
    monkeypatch: pytest.MonkeyPatch, after_reservation: timedelta
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("three-hours", "PT3H"),
    )
    clock[0] = END + timedelta(hours=1)

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted snapshot")

    with monkeypatch.context() as patch:
        patch.setattr(producer._source, "read", interrupted)
        with pytest.raises(OSError, match="interrupted snapshot"):
            await producer.run_configuration(config)
    frozen = producer._source.requests[-1]
    clock[0] += after_reservation
    if after_reservation == timedelta(hours=3):
        direct = producer._build_slice(
            config, obligation, SNAPSHOT_OFFERING_ID, finality="official", now=clock[0]
        )
        _, manifest, producer._source.reader = redacted_completed_result(
            direct, page_bodies=[PROVIDER_ROW]
        )
        with pytest.raises(LedgerConflictError) as conflict:
            await producer.commit_revision_from_manifest(
                obligation,
                manifest,
                rows=ROWS,
                finality="official",
                finality_proof=manifest_finality_proof(obligation, manifest, clock[0]),
            )
        assert conflict.value.code == "OBSERVATION_CONFLICT"
    revision = await producer.acquire_obligation(
        config, obligation, restate=True, target_finality="official", manual_replay=True
    )
    assert revision is not None and revision.finality == "snapshot"
    latest = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert latest is not None and latest.acquisition.request().period == frozen.period


@pytest.mark.parametrize("entry", ["acquire", "commit"])
@pytest.mark.parametrize("reader", ["absent", "mismatched", "verified"])
async def test_official_zero_rows_require_staged_verification(
    entry: str, reader: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ReportingSourceFinalPolicy("selected", "source-evidence-redacted")
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    result, manifest, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])

    async def execute(*args: Any, **kwargs: Any) -> ReportingSourceExecutorResult:
        return result

    monkeypatch.setattr(producer._source, "execute", execute)
    reads = []

    async def read(**kwargs: Any) -> bytes:
        reads.append(kwargs["object_ref"])
        return b"" if reader == "verified" else b"other"

    if reader == "absent":
        producer._object_reader = None
    else:
        monkeypatch.setattr(producer._source, "read", read)
    publish = (
        producer.acquire_obligation(config, obligation)
        if entry == "acquire"
        else producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=[],
            finality="official",
            finality_proof=source_finality_proof(obligation, manifest, policy),
        )
    )
    if reader == "verified":
        revision = await publish
        assert revision is not None and revision.row_count == 0 and revision.finality == "official"
    else:
        with pytest.raises(LedgerConflictError, match="staged"):
            await publish
    assert reads == ([] if reader == "absent" else [manifest.objects[0].object_ref])


@pytest.mark.parametrize("competing_snapshot", [False, True])
@pytest.mark.parametrize("authoritative", [False, True])
async def test_direct_cutoff_honors_the_winning_reservation_during_staged_verification(
    monkeypatch: pytest.MonkeyPatch,
    competing_snapshot: bool,
    authoritative: bool,
) -> None:
    policy = (
        ReportingSourceFinalPolicy("provider", "source-evidence-redacted")
        if authoritative
        else ReportingContractualCutoffPolicy("contract", "PT2H")
    )
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(), finality_policy=policy
    )
    clock[0] = END + timedelta(hours=3)
    snapshot = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0])
    close = producer._build_slice(
        config,
        obligation,
        OFFICIAL_OFFERING_ID if authoritative else SNAPSHOT_OFFERING_ID,
        now=clock[0],
        finality="official",
        trigger="manual_replay",
    )
    _, manifest, producer._source.reader = redacted_completed_result(
        close, page_bodies=[PROVIDER_ROW]
    )
    original = producer._source.read

    async def competing_reservation(**kwargs: Any) -> bytes:
        winner = await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                snapshot.model_dump_json(),
                0,
                ProvisionalPolicy(timedelta(hours=12), timedelta(hours=1), timedelta(hours=2)),
            )
        )
        assert winner.request().identity == snapshot.identity
        return await original(**kwargs)

    if competing_snapshot:
        monkeypatch.setattr(producer._source, "read", competing_reservation)
    publication = producer.commit_revision_from_manifest(
        obligation,
        manifest,
        rows=ROWS,
        finality="official",
        finality_proof=manifest_finality_proof(obligation, manifest, clock[0]),
    )
    if competing_snapshot:
        with pytest.raises(LedgerConflictError) as conflict:
            await publication
        assert conflict.value.code == "OBSERVATION_CONFLICT"
        assert not await store.list_revisions(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
    else:
        official = await publication
        observation = await store.get_provisional_observation(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        assert observation is not None and observation.revision_id == official.reporting_revision_id
        assert observation.acquisition.request() == close
        if authoritative:
            assert observation.acquisition.request().publication_class == "AUTHORITATIVE"


async def test_direct_official_commit_checks_staged_size() -> None:
    policy = ReportingSourceFinalPolicy("selected", "source-evidence-redacted")
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, producer._source.reader = redacted_completed_result(
        request, page_bodies=[""], row_counts=[0]
    )
    objects = [manifest.objects[0].model_copy(update={"byte_count": 1})]
    draft = manifest.model_copy(
        update={
            "byte_count": 1,
            "objects": objects,
            "object_set_sha256": source_batch_object_set_sha256_v1(objects),
        }
    )
    manifest = SourceBatchManifestV1.model_validate(
        {**draft.model_dump(), "content_fingerprint": publication_content_fingerprint_v1(draft)}
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=[],
            finality="official",
            finality_proof=source_finality_proof(obligation, manifest, policy),
        )
    assert conflict.value.code == "STAGED_OBJECT_MISMATCH"


async def test_policyless_official_does_not_dispatch() -> None:
    producer, _, config, obligation, calls, _ = await _harness(official=True)
    with pytest.raises(LedgerConflictError) as error:
        await producer.acquire_obligation(config, obligation)
    assert error.value.code == "FINALITY_POLICY_UNPROVEN"
    assert not calls


@pytest.mark.parametrize("pending_close", [False, True])
async def test_contractual_cutoff_restores_frozen_output_on_restart(
    monkeypatch: pytest.MonkeyPatch, pending_close: bool, store_factory: Any
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        store_factory,
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("scheduled-cutoff", "PT3H"),
    )
    producer._offerings = replace(producer._offerings, official_offering_id=None)
    clock[0] = END + timedelta(hours=1)
    if pending_close:
        assert len((await producer.run_configuration(config)).revisions_committed) == 1
        clock[0] = END + timedelta(hours=3)

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted staged read")

    with monkeypatch.context() as patch:
        patch.setattr(producer._source, "read", interrupted)
        with pytest.raises(OSError, match="interrupted staged read"):
            await producer.run_configuration(config)
    request = producer._source.requests[-1]
    reserved = await store.get_provisional_acquisition(
        account_id=config.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        ordinal=int(pending_close),
    )
    assert reserved is not None
    wire = reserved.to_wire()
    if pending_close:
        assert wire["output_finality"] == "official"
        clock[0] = END + timedelta(hours=2)
        calls_before_replay = len(producer._source.requests)
        assert (
            await producer.acquire_obligation(
                config, obligation, restate=True, target_finality="snapshot", track_settling=True
            )
            is None
        )
        assert len(producer._source.requests) == calls_before_replay
    else:
        assert "output_finality" not in wire
    assert ProvisionalAcquisition.from_wire(wire) == reserved
    restarted = ReportingProducer(
        source=producer._source,
        object_reader=producer._source,
        store=store,
        offerings=producer._offerings,
        clock=lambda: clock[0],
        read_jitter_window=timedelta(0),
    )
    clock[0] = END + timedelta(hours=4)
    turn = await restarted.run_configuration(config)
    assert producer._source.requests[-1].period == request.period
    assert len(turn.revisions_committed) == 1
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert revision is not None
    assert revision.finality == ("official" if pending_close else "snapshot")
    assert revision.finality_policy_id == ("scheduled-cutoff" if pending_close else None)
    if not pending_close:
        closed = await restarted.run_configuration(config)
        assert len(closed.revisions_committed) == 1
        official = await store.get_revision(
            account_id=config.account_id, reporting_revision_id=closed.revisions_committed[0]
        )
        assert official is not None and official.finality == "official"


async def test_official_construction_requires_proof_on_new_writes() -> None:
    producer, _, config, obligation, _, clock = await _harness(
        official=True,
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "source-evidence-redacted"),
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=ROWS, finality="official"
        )
    assert conflict.value.code == "FINALITY_POLICY_UNPROVEN"


@pytest.mark.parametrize("offset", [timedelta(microseconds=-1), timedelta(seconds=31)])
async def test_official_proof_time_must_be_within_publication_bounds(offset: timedelta) -> None:
    producer, _, config, obligation, _, clock = await _harness(
        official=True,
        finality_policy=ReportingSourceFinalPolicy("delivery-v1", "source-evidence-redacted"),
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="official",
            finality_proof=FinalityProof("delivery-v1", "source_final", END + offset),
        )
    assert conflict.value.code == "PUBLICATION_TIME_INVALID"


async def test_contractual_cutoff_refuses_pre_cutoff_source_publication() -> None:
    producer, _, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingContractualCutoffPolicy("cutoff-v1", "PT2H")
    )
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="snapshot", now=clock[0]
    )
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    clock[0] = END + timedelta(hours=3)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="official",
            finality_proof=FinalityProof("cutoff-v1", "contractual_cutoff", clock[0]),
        )
    assert conflict.value.code == "FINALITY_POLICY_UNPROVEN"


async def test_official_construction_rejects_proof_for_another_policy() -> None:
    producer, _, config, obligation, _, clock = await _harness(
        official=True,
        finality_policy=ReportingSourceFinalPolicy("selected", "source-evidence-redacted"),
    )
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="official",
            finality_proof=FinalityProof("unselected", "source_final", clock[0]),
        )
    assert conflict.value.code == "FINALITY_POLICY_UNPROVEN"


async def test_retained_official_replay_preserves_original_policy_fields() -> None:
    producer, store, config, obligation, _, clock = await _harness(official=True)
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    revision_id = f"rpr_{manifest.publication_id[4:44]}"
    totals = (("impressions", "10"),)
    managed_totals = (ReportingControlTotalRecord("impressions", "10", "integer"),)
    historical = ReportingRevisionRecord(
        reporting_revision_id=revision_id,
        account_id=config.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality="official",
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=1,
            control_totals=totals,
            reporting_rows=ROWS,
            control_total_evidence=managed_totals,
        ),
        row_count=1,
        control_totals=totals,
        managed_control_totals=managed_totals,
        observed_at=manifest.observed_at,
        data_through=manifest.data_through,
        created_at=clock[0],
        finality_policy_id="archived-policy",
        finality_basis="stabilized",
        finalized_at=END,
        source_publication_id=manifest.publication_id,
        source_manifest_sha256=manifest.content_fingerprint.split(":", 1)[1],
    )
    historical = await store.commit_revision(historical, ROWS)
    result = await ReportingStatusHandler(store).handle(
        {"view": "revision", "reporting_revision_id": revision_id},
        caller=ReportingStatusCaller(config.account_id, config.consumer_id),
    )
    assert result["revision"]["finality"] == "official"
    assert result["revision"]["finality_policy_id"] == "archived-policy"
    clock[0] += timedelta(hours=1)
    assert await producer.acquire_obligation(config, obligation) is None
    assert (
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=ROWS, finality="official"
        )
        == historical
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=[dict(ROWS[0], campaign_id="other")], finality="official"
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"
