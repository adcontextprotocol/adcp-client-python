"""Recoverable acquisition, source authority, and immutable official publication."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from adcp_reporting_storage import PgReportingLedgerStore

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
    ReportingContractualCutoffPolicy,
    ReportingProducer,
    ReportingRevisionRecord,
    ReportingSourceFinalPolicy,
    ReportingStabilizedPolicy,
    WorkerTurn,
    revision_content_sha256,
)
from adcp.reporting.ledger.finality import manifest_finality_proof
from adcp.reporting.ledger.provisional import (
    ProvisionalAcquisition,
    ProvisionalObservation,
    ProvisionalPolicy,
)
from adcp.reporting.source import (
    ReportingFinalityEvidenceV1,
    ReportingSourceExecutorResult,
    SourceBatchManifestV1,
    SourceFormatV1,
    coverage_denominator_fingerprint_v1,
    encode_source_batch_manifest_v1,
    publication_content_fingerprint_v1,
    source_batch_manifest_reference_v1,
    source_batch_object_set_sha256_v1,
)
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_official_finality import ROWS
from tests.test_reporting_source_availability import (
    END,
    PROVIDER_ROW,
    EvidenceSource,
    ProgressStore,
    _closing_capabilities,
    _harness,
)


class CoreStore:
    """Expose the base ledger and retry schedule without optional observation extensions."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def __getattr__(self, name: str) -> Any:
        if "provisional" in name or "restatement" in name:
            raise AttributeError(name)
        return getattr(self.store, name)

    def source_publication(self, account_id: str, **kwargs: Any) -> Any:
        return self.store.source_publication(account_id, **kwargs)


@pytest.mark.parametrize(
    "policy",
    [
        ReportingSourceFinalPolicy("provider", "delivery.is_final"),
        ReportingContractualCutoffPolicy("cutoff", "PT0S"),
    ],
)
async def test_standalone_official_admission_requires_account_lock(policy: Any) -> None:
    producer, store, config, obligation, calls, _ = await _harness(
        official=True, finality_policy=policy
    )

    class UnlockedCore(CoreStore):
        source_publication = None

    producer._store = UnlockedCore(store)
    changed = replace(config, delivery_config_version=2)
    await store.put_configuration(changed)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.close_elapsed_periods(changed)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.acquire_obligation(config, obligation)
    assert not calls


@pytest.mark.parametrize("finality", ["official", "snapshot"])
async def test_direct_official_work_requires_lock_before_reserving(
    monkeypatch: pytest.MonkeyPatch, finality: str
) -> None:
    policy = ReportingSourceFinalPolicy("provider", "source-evidence-redacted")
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=policy
    )
    offering = SNAPSHOT_OFFERING_ID if finality == "snapshot" else OFFICIAL_OFFERING_ID
    request = producer._build_slice(config, obligation, offering, finality=finality, now=clock[0])
    _, manifest, producer._object_reader = redacted_completed_result(
        request, page_bodies=[PROVIDER_ROW]
    )
    monkeypatch.setattr(store, "source_publication", None)
    with pytest.raises(LedgerConflictError, match="publication lock"):
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality=finality,
            finality_proof=(
                manifest_finality_proof(obligation, manifest, clock[0])
                if finality == "official"
                else None
            ),
        )
    assert (
        await store.get_provisional_acquisition(
            account_id=config.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            ordinal=0,
        )
        is None
    )


@pytest.mark.parametrize("changed", ["signal", "provider_time"])
async def test_core_source_final_replay_requires_retained_signal_and_timestamp(
    changed: str,
) -> None:
    producer, store, config, obligation, _, _ = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("provider", "delivery.is_final")
    )
    producer._store = CoreStore(store)
    first = await producer.acquire_obligation(config, obligation)
    manifest = producer._source.manifest
    updates = (
        {"evidence_ref": "another-signal"}
        if changed == "signal"
        else {"observed_at": first.finalized_at - timedelta(seconds=1)}
    )
    changed_manifest = manifest.model_copy(
        update={"finality_evidence": manifest.finality_evidence.model_copy(update=updates)}
    )
    with pytest.raises(LedgerConflictError) as error:
        await producer.commit_revision_from_manifest(
            obligation, changed_manifest, rows=ROWS, finality="official"
        )
    assert error.value.code == "REVISION_IMMUTABLE"
    assert (
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=ROWS, finality="official"
        )
        == first
    )


@pytest.mark.parametrize("change", ["prefix", "suffix"])
@pytest.mark.parametrize("origin_lookup", [True, False])
@pytest.mark.parametrize(
    ("archived", "change_stored_revision"), [(False, False), (True, False), (True, True)]
)
async def test_unchanged_snapshot_replay_checks_the_referenced_publication(
    change: str,
    origin_lookup: bool,
    archived: bool,
    change_stored_revision: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("later", "P2D")
    )
    first = await producer.acquire_obligation(config, obligation, track_settling=True)
    clock[0] += timedelta(hours=1)
    assert first == await producer.acquire_obligation(
        config, obligation, restate=True, track_settling=True
    )
    observation = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    if archived:
        observation = replace(observation, revision_publication_id=None)
        store._provisional_observations[(config.account_id, obligation.reporting_obligation_id)][
            observation.acquisition.ordinal
        ] = observation
    if not origin_lookup:
        monkeypatch.setattr(store, "verify_snapshot_origin", None)
    manifest = SourceBatchManifestV1.model_validate_json(observation.manifest_json)
    assert first == await producer.commit_revision_from_manifest(
        obligation, manifest, rows=ROWS, finality="snapshot", acquisition=observation.acquisition
    )
    get_revision = store.get_revision

    async def different_origin(**kwargs: Any) -> Any:
        revision = await get_revision(**kwargs)
        offset = 4 if change == "prefix" else 44
        identifier = revision.source_publication_id
        return replace(
            revision,
            source_publication_id=identifier[:offset]
            + ("a" if identifier[offset] != "a" else "b")
            + identifier[offset + 1 :],
        )

    if change_stored_revision:
        store._revisions[first.reporting_revision_id] = await different_origin(
            account_id=config.account_id, reporting_revision_id=first.reporting_revision_id
        )
    else:
        monkeypatch.setattr(store, "get_revision", different_origin)
    with pytest.raises(LedgerConflictError) as error:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="snapshot",
            acquisition=observation.acquisition,
        )
    assert error.value.code in {"REVISION_IMMUTABLE", "STABILIZATION_ORIGIN_CONFLICT"}


@pytest.fixture(params=["memory", "postgres", "core", "progress"])
async def cutoff_backend(request: Any) -> AsyncIterator[Any]:
    if request.param == "postgres":
        async with isolated_reporting_pool() as pool:
            yield partial(PgReportingLedgerStore, pool=pool), False
    else:
        yield (
            ProgressStore if request.param == "progress" else InMemoryReportingLedgerStore
        ), request.param == "core"


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("witness", [True, False])
@pytest.mark.parametrize("atomic", [True, False])
async def test_historical_unchanged_replay_validates_stored_observation_origin(
    cutoff_backend: Any,
    witness: bool,
    atomic: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(
        factory, finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    first = await producer.acquire_obligation(config, obligation, track_settling=True)
    for _ in range(2):
        clock[0] += timedelta(hours=1)
        assert first == await producer.acquire_obligation(
            config, obligation, restate=True, track_settling=True
        )
    _, middle, latest = await store.list_provisional_observations(**owner, limit=3)
    assert middle.acquisition.ordinal == 1 and latest.acquisition.ordinal == 2
    assert middle.revision_publication_id == first.source_publication_id
    manifest = SourceBatchManifestV1.model_validate_json(middle.manifest_json)
    stored_id = first.source_publication_id
    wrong_id = stored_id[:-1] + ("0" if stored_id[-1] != "0" else "1")
    if isinstance(store, PgReportingLedgerStore):
        async with store.storage_identity.connection() as connection, connection.transaction():
            # Simulate an already inconsistent historical row in this fixture's private schema.
            await connection.execute(
                "ALTER TABLE reporting_provisional_observations DISABLE TRIGGER "
                "reporting_provisional_observation_immutable"
            )
            await connection.execute(
                "UPDATE reporting_provisional_observations SET payload = "
                "jsonb_set(payload, '{revision_publication_id}', to_jsonb(%s::text)) "
                "WHERE account_id=%s AND reporting_obligation_id=%s AND ordinal=1",
                (wrong_id, config.account_id, obligation.reporting_obligation_id),
            )
            await connection.execute(
                "ALTER TABLE reporting_provisional_observations ENABLE TRIGGER "
                "reporting_provisional_observation_immutable"
            )
    else:
        store._provisional_observations[(config.account_id, obligation.reporting_obligation_id)][
            1
        ] = replace(middle, revision_publication_id=wrong_id)
    if not witness:
        monkeypatch.setattr(store, "verify_snapshot_origin", None)
    with pytest.raises(LedgerConflictError) as error:
        if atomic:
            await store.commit_provisional_observation(middle, first, ROWS)
        else:
            await producer.commit_revision_from_manifest(
                obligation, manifest, rows=ROWS, finality="snapshot", acquisition=middle.acquisition
            )
    assert error.value.code in {"REVISION_IMMUTABLE", "STABILIZATION_ORIGIN_CONFLICT"}
    assert await store.get_provisional_observation(**owner) == latest


async def test_stale_cutoff_seal_retries_with_fresh_source_work(
    cutoff_backend: Any, monkeypatch: Any
) -> None:
    factory, core = cutoff_backend
    base, store, config, obligation, _, clock = await _harness(
        factory,
        official=True,
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H"),
    )
    if core:
        store = CoreStore(store)
    calls = []
    staging = InMemoryStagingStore()

    def fetch(request: Any) -> InlineFetchResult:
        calls.append(request)
        return InlineFetchResult(
            rows=[{"media_buy_id": "media-buy-redacted", "impressions": 10, "spend": "1.25"}],
            data_through=END - timedelta(seconds=1) if len(calls) == 1 else END,
        )

    source = InlineReportingSource(
        capabilities=base._source.capabilities,
        fetch=fetch,
        staging=staging,
        seals=InMemorySealStore(),
        clock=lambda: clock[0],
    )

    def restart() -> ReportingProducer:
        return ReportingProducer(
            store=store,
            retry_store=base._retry_store,
            source=source,
            offerings=base._offerings,
            object_reader=staging,
            clock=lambda: clock[0],
            read_jitter_window=timedelta(0),
        )

    clock[0] = END + timedelta(hours=2)
    first = await restart().run_configuration(config)
    assert first.earliest_retry_at is not None and first.revisions_committed == []
    assert len(calls) == 1
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert not await store.list_revisions(**owner)
    if not core:
        assert not await store.list_provisional_observations(**owner, limit=2)
        checkpoint = await store.get_restatement_checkpoint(**owner)
        assert checkpoint.next_observation == 1
    clock[0] = first.earliest_retry_at - timedelta(microseconds=1)
    assert not (await restart().run_configuration(config)).revisions_committed
    assert len(calls) == 1
    clock[0] = first.earliest_retry_at

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted verified read")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "read", interrupted)
        with pytest.raises(OSError, match="interrupted verified read"):
            await restart().run_configuration(config)
    assert len(calls) == 2
    assert calls[0].identity.source_execution_key != calls[1].identity.source_execution_key
    if not core:
        reserved = await store.get_provisional_acquisition(**owner, ordinal=1)
        assert reserved.request().identity == calls[1].identity
    clock[0] += timedelta(seconds=1)
    turn = await restart().run_configuration(config)
    assert len(calls) == 2 and len(turn.revisions_committed) == 1
    official = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert official.finality == "official" and official.finalized_at == clock[0]


async def test_unproven_stability_does_not_retire_a_cutoff_attempt() -> None:
    producer, store, config, obligation, _, _ = await _harness(
        official=True, finality_policy=ReportingStabilizedPolicy("stable", "PT2H", "PT1H")
    )
    try:
        await producer.acquire_obligation(config, obligation)
    except LedgerConflictError as conflict:
        assert conflict.code == "FINALITY_POLICY_UNPROVEN"
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    history = await store.list_provisional_observations(**owner, limit=1)
    checkpoint = await store.get_restatement_checkpoint(**owner)
    if history:
        assert checkpoint.next_observation == history[-1].acquisition.ordinal + 1
    else:
        assert checkpoint is None
    assert (
        await store.get_retry_schedule(
            scope_key=f"cutoff:{config.account_id}:{obligation.reporting_obligation_id}"
        )
        is None
    )


async def test_stale_cutoff_with_a_future_acquisition_retains_its_reservation(
    monkeypatch: Any,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H")
    )
    clock[0] = END + timedelta(hours=2)

    async def future_source(request: Any, **kwargs: Any) -> Any:
        result, _, producer._source.reader = redacted_completed_result(
            request,
            page_bodies=[PROVIDER_ROW],
            observed_at=clock[0] + timedelta(hours=1),
            data_through=END - timedelta(seconds=1),
        )
        return result

    monkeypatch.setattr(producer._source, "execute", future_source)
    with pytest.raises(LedgerConflictError) as error:
        await producer.acquire_obligation(config, obligation)
    assert error.value.code == "PUBLICATION_TIME_INVALID"
    assert (
        await store.get_restatement_checkpoint(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )


@pytest.mark.parametrize(
    "different",
    [
        "capability_class",
        "capability_scope",
        "capability_namespace",
        "response",
        "build_during_fetch",
    ],
)
async def test_contradictory_official_source_authority_is_rejected(
    different: str, monkeypatch: Any
) -> None:
    producer, _, config, obligation, _, _ = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final")
    )
    if different == "capability_class":
        producer._offerings = replace(
            producer._offerings, official_offering_id=SNAPSHOT_OFFERING_ID
        )
    elif different == "capability_scope":
        producer._offerings = replace(producer._offerings, source_scope={"another": "account"})
    elif different == "capability_namespace":
        producer._offerings = replace(producer._offerings, publication_namespace="another-source")
    else:
        original = producer._source.execute

        async def wrong_response(request: Any, **kwargs: Any) -> Any:
            if different == "build_during_fetch":
                build = request.adapter_build.model_copy(update={"adapter_version": "other"})
                producer._source.capabilities = producer._source.capabilities.model_copy(
                    update={"adapter_build": build}
                )
                return await original(request.model_copy(update={"adapter_build": build}), **kwargs)
            result = await original(request, **kwargs)
            wrong = request.identity.model_copy(update={"account_id": "other"})
            return result.model_copy(
                update={"response": result.response.model_copy(update={"identity": wrong})}
            )

        monkeypatch.setattr(producer._source, "execute", wrong_response)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation)
    assert conflict.value.code == "MANIFEST_MISMATCH"


async def test_explicit_snapshot_resumes_a_reserved_cutoff(monkeypatch: Any) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT3H"),
    )
    clock[0] = END + timedelta(hours=3)

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted close")

    with monkeypatch.context() as patch:
        patch.setattr(producer._source, "read", interrupted)
        with pytest.raises(OSError):
            await producer.acquire_obligation(
                config, obligation, restate=True, target_finality="official"
            )
    frozen = producer._source.requests[-1]
    clock[0] += timedelta(minutes=5)
    resumed = await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", manual_replay=True
    )
    assert resumed.finality == "official"
    assert producer._source.requests[-1].identity == frozen.identity
    latest = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert latest.acquisition.request().identity == frozen.identity


async def test_manual_cutoff_on_core_store() -> None:
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H")
    )
    producer._store = CoreStore(store)
    clock[0] = END + timedelta(hours=3)
    revision = await producer.acquire_obligation(config, obligation, manual_replay=True)
    assert revision.finality == "official"


async def test_retained_official_replay_leaves_an_unrelated_reservation_alone() -> None:
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final")
    )
    original = await producer.acquire_obligation(config, obligation)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    previous_observation = await store.get_provisional_observation(**owner)
    previous_checkpoint = await store.get_restatement_checkpoint(**owner)
    manifest = producer._source.manifest
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, finality="snapshot", now=clock[0], observation=1
    )
    reserved = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(),
            1,
            ProvisionalPolicy(timedelta(hours=2), timedelta(hours=1), None),
        )
    )
    turn = WorkerTurn()
    replayed = await producer.commit_revision_from_manifest(
        obligation, manifest, rows=ROWS, finality="official", acquisition=reserved, turn=turn
    )
    assert replayed == original
    assert (
        await store.get_provisional_observation(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        == previous_observation
    )
    assert (
        await store.get_restatement_checkpoint(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        == previous_checkpoint
    )
    assert not turn.did_work and not turn.revisions_committed


@pytest.mark.parametrize(
    "change", ["caller_rows", "caller_manifest", "publication_clock", "explicit_clock"]
)
async def test_direct_cutoff_publishes_its_verified_rows_and_completion_time(
    change: str, monkeypatch: Any
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        official=True, finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H")
    )
    start = clock[0] = END + timedelta(hours=2)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=start)
    _, manifest, producer._source.reader = redacted_completed_result(
        request, page_bodies=[PROVIDER_ROW]
    )
    caller = [dict(row) for row in ROWS]
    original = store.reserve_provisional_acquisition

    async def reserve(acquisition: Any) -> Any:
        if change == "caller_rows":
            caller[0]["campaign_id"] = "caller-mutated"
        if change == "caller_manifest":
            manifest.objects.clear()
        return await original(acquisition)

    monkeypatch.setattr(store, "reserve_provisional_acquisition", reserve)
    producer._source.read_delay = timedelta(minutes=5)
    revision = await producer.commit_revision_from_manifest(
        obligation,
        manifest,
        rows=caller,
        finality="official",
        finality_proof=manifest_finality_proof(obligation, manifest, start),
        now=start if change == "explicit_clock" else None,
    )
    page = await store.read_revision_rows(
        account_id=config.account_id, reporting_revision_id=revision.reporting_revision_id
    )
    assert list(page.rows) == ROWS
    stored = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert len(SourceBatchManifestV1.model_validate_json(stored.manifest_json).objects) == 1
    anchor = start if change == "explicit_clock" else clock[0]
    assert revision.finalized_at == revision.created_at == anchor


@pytest.mark.parametrize("encoding", ["gzip", "json", "corrupt_gzip", "unsupported"])
async def test_official_source_format_is_decoded_or_refused_before_fetch(
    encoding: str, monkeypatch: Any
) -> None:
    producer, _, config, obligation, calls, clock = await _harness(
        official=True,
        finality_policy=ReportingSourceFinalPolicy("source", "source-evidence-redacted"),
    )
    caps = producer._source.capabilities
    offering = caps.offering(OFFICIAL_OFFERING_ID)
    fmt = SourceFormatV1(media_type="application/x-ndjson", compression="gzip")
    if encoding == "json":
        fmt = SourceFormatV1(media_type="application/json")
    elif encoding == "unsupported":
        fmt = SourceFormatV1(media_type="application/vnd.apache.parquet")
    producer._source.capabilities = caps.model_copy(
        update={
            "offerings": [
                item.model_copy(update={"formats": [fmt]}) if item == offering else item
                for item in caps.offerings
            ]
        }
    )
    if encoding == "unsupported":
        with pytest.raises(LedgerConflictError) as conflict:
            await producer.acquire_obligation(config, obligation)
        assert conflict.value.code == "UNSUPPORTED_SOURCE_FORMAT" and not calls
        return
    request = producer._build_slice(config, obligation, OFFICIAL_OFFERING_ID, now=clock[0])
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    payload = gzip.compress(PROVIDER_ROW.encode())
    if encoding == "json":
        payload = f"[{PROVIDER_ROW}]".encode()
    elif encoding == "corrupt_gzip":
        payload = b"not a gzip object"
    objects = [
        manifest.objects[0].model_copy(
            update={
                **fmt.model_dump(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_count": len(payload),
            }
        )
    ]
    draft = manifest.model_copy(
        update={
            "objects": objects,
            "byte_count": len(payload),
            "object_set_sha256": source_batch_object_set_sha256_v1(objects),
        }
    )
    manifest = SourceBatchManifestV1.model_validate(
        {**draft.model_dump(), "content_fingerprint": publication_content_fingerprint_v1(draft)}
    )

    async def read(**kwargs: Any) -> bytes:
        return payload

    monkeypatch.setattr(producer._source, "read", read)
    publication = producer.commit_revision_from_manifest(
        obligation,
        manifest,
        rows=ROWS,
        finality="official",
        finality_proof=manifest_finality_proof(obligation, manifest, clock[0]),
    )
    if encoding == "corrupt_gzip":
        with pytest.raises(LedgerConflictError) as conflict:
            await publication
        assert conflict.value.code == "STAGED_OBJECT_MISMATCH"
        return
    official = await publication
    assert official.finality == "official" and official.row_count == 1


@pytest.mark.parametrize(
    "mismatch", ["product", "kind", "timezone", "window", "metric", "mutated_request"]
)
async def test_official_acquisition_verifies_full_frozen_capability(
    mismatch: str, monkeypatch: Any
) -> None:
    producer, _, config, obligation, calls, _ = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final")
    )
    caps = producer._source.capabilities
    offering = caps.offering(OFFICIAL_OFFERING_ID)
    if mismatch == "product":
        offering = offering.model_copy(update={"product_ids": ["forbidden"]})
    elif mismatch == "kind":
        offering = offering.model_copy(update={"constituent_kinds": ["package_item"]})
    elif mismatch == "timezone":
        offering = offering.model_copy(update={"source_timezone": "Asia/Tokyo"})
    elif mismatch == "window":
        offering = offering.model_copy(
            update={"windowing": offering.windowing.model_copy(update={"minimum_window": "P2D"})}
        )
    elif mismatch == "metric":
        offering = offering.model_copy(
            update={"metrics": [row for row in offering.metrics if row.name != "spend"]}
        )
    else:
        original = producer._source.execute

        async def remove_metric(request: Any, **kwargs: Any) -> Any:
            request.requested_metrics.remove("spend")
            return await original(request, **kwargs)

        monkeypatch.setattr(producer._source, "execute", remove_metric)
    producer._source.capabilities = caps.model_copy(
        update={
            "offerings": [
                offering if item.offering_id == offering.offering_id else item
                for item in caps.offerings
            ]
        }
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation)
    assert conflict.value.code == "MANIFEST_MISMATCH"
    if mismatch != "mutated_request":
        assert not calls


@pytest.mark.parametrize("collision", ["concurrent", "lookup_unavailable"])
async def test_manual_snapshot_cannot_overtake_a_hidden_winning_close(
    collision: str, monkeypatch: Any
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H"),
    )
    clock[0] = END + timedelta(hours=3)
    close = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], finality="official"
    )
    reservation = ProvisionalAcquisition(
        close.model_dump_json(),
        0,
        ProvisionalPolicy(timedelta(hours=2), timedelta(hours=1)),
        output_finality="official",
    )
    if collision == "lookup_unavailable":
        await store.reserve_provisional_acquisition(reservation)
        monkeypatch.setattr(store, "get_provisional_acquisition", None)
    else:

        async def raced_lookup(**kwargs: Any) -> None:
            await store.reserve_provisional_acquisition(reservation)

        monkeypatch.setattr(store, "get_provisional_acquisition", raced_lookup)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", restate=True, manual_replay=True
        )
    assert conflict.value.code == "OBSERVATION_CONFLICT"
    assert not await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    revision = await producer.acquire_obligation(config, obligation, track_settling=True)
    assert revision.finality == "official"
    assert producer._source.requests[-1].identity == close.identity


@pytest.mark.parametrize("supply_reservation", [False, True])
async def test_direct_snapshot_cannot_consume_official_reservation(
    supply_reservation: bool, monkeypatch: Any
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H"),
    )
    clock[0] = END + timedelta(hours=3)
    close = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], finality="official"
    )
    reservation = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            close.model_dump_json(),
            0,
            ProvisionalPolicy(timedelta(hours=2), timedelta(hours=1)),
            output_finality="official",
        )
    )
    _, manifest, _ = redacted_completed_result(close, page_bodies=[PROVIDER_ROW])
    monkeypatch.setattr(store, "get_provisional_acquisition", None)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=ROWS,
            finality="snapshot",
            acquisition=reservation if supply_reservation else None,
        )
    assert conflict.value.code == "OBSERVATION_CONFLICT"
    assert not await store.list_provisional_observations(
        account_id=config.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        limit=1,
    )


async def test_satisfied_manual_snapshot_preserves_a_hidden_close_for_scheduled_recovery(
    monkeypatch: Any,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H"),
    )
    await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", track_settling=True
    )
    clock[0] = END + timedelta(hours=3)
    close = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], finality="official", observation=1
    )
    reservation = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            close.model_dump_json(),
            1,
            ProvisionalPolicy(timedelta(hours=2), timedelta(hours=1)),
            output_finality="official",
        )
    )
    monkeypatch.setattr(store, "get_provisional_acquisition", None)
    assert (
        await producer.acquire_obligation(
            config, obligation, target_finality="snapshot", manual_replay=True
        )
        is None
    )
    revision = await producer.acquire_obligation(
        config, obligation, restate=True, track_settling=True
    )
    assert revision is not None and revision.finality == "official"
    assert producer._source.requests[-1].identity == reservation.request().identity


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("reserved_close", [False, True])
async def test_bare_revision_cannot_overtake_an_observation(
    cutoff_backend: Any, reserved_close: bool
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(factory)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0])
    acquisition = ProvisionalAcquisition(
        request.model_dump_json(),
        0,
        ProvisionalPolicy(timedelta(hours=2), timedelta(hours=1)),
        output_finality="official" if reserved_close else None,
    )
    await store.reserve_provisional_acquisition(acquisition)
    revision = ReportingRevisionRecord(
        reporting_revision_id="rpr_competing",
        reporting_obligation_id=obligation.reporting_obligation_id,
        account_id=config.account_id,
        finality="snapshot",
        revision_content_sha256="a" * 64,
        row_count=0,
        control_totals=(),
        observed_at=clock[0],
        data_through=END,
        created_at=clock[0],
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await store.commit_revision(revision, ())
    assert conflict.value.code == "OBSERVATION_CONFLICT"
    assert not await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )


@pytest.mark.parametrize("core", [False, True])
async def test_manual_restatements_fetch_current_content(core: bool, monkeypatch: Any) -> None:
    producer, store, config, obligation, _, clock = await _harness()
    if core:
        producer._store = CoreStore(store)
    calls = []
    values = iter([1, 99, 1, 2, 3] if core else [1, 1, 2, 3])

    def fetch(request: Any) -> list[dict[str, Any]]:
        calls.append(request)
        return [{"media_buy_id": "media-buy-redacted", "impressions": next(values), "spend": "0"}]

    monkeypatch.setattr(producer._source, "_fetch", fetch)
    first = await producer.acquire_obligation(config, obligation, track_settling=not core)
    if core:

        async def interrupted(**kwargs: Any) -> bytes:
            raise OSError("interrupted manual read")

        with monkeypatch.context() as patch:
            patch.setattr(producer._object_reader, "read", interrupted)
            with pytest.raises(OSError):
                await producer.acquire_obligation(config, obligation, restate=True)
    winners = [
        await producer.acquire_obligation(config, obligation, restate=True) for _ in range(3)
    ]
    assert len(calls) == len(set(req.identity.source_execution_key for req in calls)) == 4 + core
    assert all(req.trigger == "manual_replay" for req in calls[1:])
    assert len({r.reporting_revision_id for r in winners}) == 3
    assert winners[0] == first
    if not core:
        history = await store.list_provisional_observations(
            account_id=config.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            limit=5,
        )
        assert [o.acquisition.request().trigger for o in history] == ["scheduled_poll"]


async def test_contractual_workflow_reads_gzipped_prerequisite_then_closes(
    monkeypatch: Any,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        capabilities=_closing_capabilities(),
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H"),
    )
    caps = producer._source.capabilities
    snapshot = caps.offering(SNAPSHOT_OFFERING_ID)
    fmt = SourceFormatV1(media_type="application/x-ndjson", compression="gzip")
    producer._source.capabilities = caps.model_copy(
        update={
            "offerings": [
                snapshot.model_copy(update={"formats": [fmt]}) if item == snapshot else item
                for item in caps.offerings
            ]
        }
    )
    payload = gzip.compress(PROVIDER_ROW.encode())

    async def fetch(request: Any, **kwargs: Any) -> Any:
        _, old, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
        objects = [
            old.objects[0].model_copy(
                update={
                    **fmt.model_dump(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_count": len(payload),
                }
            )
        ]
        draft = old.model_copy(
            update={
                "objects": objects,
                "byte_count": len(payload),
                "object_set_sha256": source_batch_object_set_sha256_v1(objects),
            }
        )
        raw = encode_source_batch_manifest_v1(
            draft.model_copy(
                update={"content_fingerprint": publication_content_fingerprint_v1(draft)}
            )
        )
        return ReportingSourceExecutorResult.completed(
            request=request,
            manifest=source_batch_manifest_reference_v1("sealed", raw),
            manifest_bytes=raw,
        )

    async def read(**kwargs: Any) -> bytes:
        return payload

    monkeypatch.setattr(producer._source, "execute", fetch)
    monkeypatch.setattr(producer._source, "read", read)
    clock[0] = END + timedelta(hours=1)
    assert len((await producer.run_configuration(config)).revisions_committed) == 1
    clock[0] += timedelta(hours=2)
    assert len((await producer.run_configuration(config)).revisions_committed) == 1
    revisions = await store.list_revisions(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert {r.finality for r in revisions} == {"snapshot", "official"}


@pytest.mark.parametrize("route", ["source_final", "cutoff_prerequisite", "bound_snapshot"])
async def test_invalid_source_namespace_leaves_first_reservation_available(route: str) -> None:
    producer, store, config, obligation, calls, _ = await _harness(
        official=route == "source_final",
        finality_policy=(
            ReportingSourceFinalPolicy("source", "delivery.is_final")
            if route == "source_final"
            else (
                ReportingContractualCutoffPolicy("cutoff", "PT3H")
                if route == "cutoff_prerequisite"
                else None
            )
        ),
    )
    if route == "bound_snapshot":
        from adcp.reporting.production.contracts import ReportingProductionSourceBinding

        producer._source.configuration_binding = (
            lambda configuration: ReportingProductionSourceBinding.for_configuration(
                configuration,
                capabilities_sha256=producer._source.capabilities.capabilities_sha256,
                media_buy_products=(("media-buy-redacted", "fixture-product"),),
            )
        )
    correct = producer._offerings
    producer._offerings = replace(correct, publication_namespace="other-namespace")
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation)
    assert conflict.value.code == "MANIFEST_MISMATCH" and not calls
    assert (
        await store.get_provisional_acquisition(
            account_id=config.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            ordinal=0,
        )
        is None
    )
    producer._offerings = correct
    revision = await producer.acquire_obligation(config, obligation)
    assert revision.finality == obligation.required_finality and len(calls) == 1


async def test_mismatched_definition_is_rejected_before_reservation_and_dispatch() -> None:
    producer, store, config, obligation, calls, _ = await _harness(
        official=True, finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final")
    )
    original = producer._source.capabilities
    official = original.offering(OFFICIAL_OFFERING_ID)
    incompatible = official.model_copy(
        update={
            "contract": official.contract.model_copy(update={"report_definition_sha256": "f" * 64})
        }
    )
    producer._source.capabilities = original.model_copy(
        update={
            "offerings": [incompatible if item == official else item for item in original.offerings]
        }
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation)
    assert conflict.value.code == "REPORT_DEFINITION_MISMATCH"
    assert not calls
    assert (
        await store.get_provisional_acquisition(
            account_id=config.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            ordinal=0,
        )
        is None
    )
    producer._source.capabilities = original
    assert (await producer.acquire_obligation(config, obligation)).finality == "official"


async def test_scheduled_policy_snapshot_requires_verified_empty_staging(monkeypatch: Any) -> None:
    producer, store, config, obligation, _, _ = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "PT2H")
    )

    async def empty(request: Any, **kwargs: Any) -> ReportingSourceExecutorResult:
        result, _, _ = redacted_completed_result(request, page_bodies=[""], row_counts=[0])
        return result

    monkeypatch.setattr(producer._source, "execute", empty)
    producer._object_reader = None
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation, track_settling=True)
    assert conflict.value.code == "STAGED_OBJECT_UNAVAILABLE"
    assert (
        await store.get_provisional_observation(
            account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
async def test_source_final_restart_resumes_authoritative_seal_before_first_reservation(
    cutoff_backend: Any,
    monkeypatch: Any,
) -> None:
    factory, _ = cutoff_backend
    base, store, config, obligation, _, clock = await _harness(
        factory,
        official=True,
        finality_policy=ReportingSourceFinalPolicy("source", "delivery.is_final"),
    )
    staging = InMemoryStagingStore()
    fetched = []

    def fetch(request: Any) -> InlineFetchResult:
        fetched.append(request)
        return InlineFetchResult(
            rows=[{"media_buy_id": "media-buy-redacted", "impressions": 10, "spend": "1.25"}],
            finality_evidence=ReportingFinalityEvidenceV1(
                basis="provider_declared",
                evidence_ref="delivery.is_final",
                observed_at=clock[0],
            ),
        )

    source = InlineReportingSource(
        capabilities=base._source.capabilities,
        staging=staging,
        seals=InMemorySealStore(),
        fetch=fetch,
        clock=lambda: clock[0],
    )

    def restart(ledger: Any) -> ReportingProducer:
        return ReportingProducer(
            store=ledger,
            retry_store=base._retry_store,
            source=source,
            object_reader=staging,
            offerings=base._offerings,
            clock=lambda: clock[0],
        )

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted staged read")

    with monkeypatch.context() as patch:
        patch.setattr(staging, "read", interrupted)
        with pytest.raises(OSError, match="interrupted staged read"):
            await restart(CoreStore(store)).acquire_obligation(config, obligation)
    first_dispatch = clock[0]
    clock[0] += timedelta(minutes=5)
    revision = await restart(store).acquire_obligation(config, obligation)
    assert revision is not None and revision.finality == "official"
    assert revision.finalized_at == first_dispatch and len(fetched) == 1


async def test_manual_restatement_preserves_scheduled_due_time_and_evidence(
    monkeypatch: Any,
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        restatement_window="PT6H", restatement_cadence="PT1H"
    )
    requests = []

    def fetch(request: Any) -> list[dict[str, Any]]:
        requests.append(request)
        return [{"media_buy_id": "media-buy-redacted", "impressions": len(requests), "spend": "0"}]

    monkeypatch.setattr(producer._source, "_fetch", fetch)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    clock[0] = END + timedelta(hours=1)
    assert len((await producer.run_configuration(config)).revisions_committed) == 1
    first = await store.get_provisional_observation(**owner)
    assert first.next_due_at == END + timedelta(hours=2)
    clock[0] = first.next_due_at - timedelta(minutes=1)
    for _ in range(2):
        assert await producer.acquire_obligation(config, obligation, restate=True) is not None
    assert await store.get_provisional_observation(**owner) == first
    assert await producer.acquire_obligation(config, obligation, manual_replay=True) is None
    clock[0] = first.next_due_at
    assert len((await producer.run_configuration(config)).revisions_committed) == 1
    assert [req.trigger for req in requests] == [
        "scheduled_poll",
        "manual_replay",
        "manual_replay",
        "scheduled_poll",
    ]
    assert len({req.identity.source_execution_key for req in requests}) == len(requests)
    history = await store.list_provisional_observations(**owner, limit=5)
    assert [o.acquisition.ordinal for o in history] == [0, 1]
    assert [o.acquisition.request().trigger for o in history] == [
        "scheduled_poll",
        "scheduled_poll",
    ]


async def test_manual_replay_of_satisfied_snapshot_skips_source() -> None:
    producer, _, config, obligation, calls, _ = await _harness()
    assert await producer.acquire_obligation(config, obligation) is not None
    assert (
        await producer.acquire_obligation(config, obligation, manual_replay=True, restate=False)
        is None
    )
    assert len(calls) == 1


async def test_manual_snapshot_before_first_poll_preserves_the_scheduled_source_window() -> None:
    producer, store, config, obligation, calls, clock = await _harness(
        restatement_window="PT6H", restatement_cadence="PT1H"
    )
    clock[0] = END + timedelta(minutes=59)
    await producer.acquire_obligation(config, obligation, restate=True)
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    assert calls == [END + timedelta(minutes=59), END + timedelta(hours=1)]
    observation = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert observation.acquisition.request().trigger == "scheduled_poll"
    assert observation.next_due_at == END + timedelta(hours=2)


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
async def test_initial_manual_replay_keeps_the_first_scheduled_poll(
    cutoff_backend: Any, monkeypatch: Any
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, calls, clock = await _harness(
        factory,
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D"),
        restatement_window="PT6H",
        restatement_cadence="PT1H",
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    clock[0] = END + timedelta(minutes=59)

    async def interrupted(**kwargs: Any) -> bytes:
        raise OSError("interrupted manual read")

    with monkeypatch.context() as patch:
        patch.setattr(producer._object_reader, "read", interrupted)
        with pytest.raises(OSError, match="interrupted manual read"):
            await producer.acquire_obligation(config, obligation, manual_replay=True)
    manual = await producer.acquire_obligation(config, obligation, manual_replay=True)
    assert manual is not None and manual.finality == "snapshot"
    assert await store.get_restatement_checkpoint(**owner) is None
    assert not await store.list_provisional_observations(**owner, limit=5)
    assert await producer.acquire_obligation(config, obligation, manual_replay=True) is None
    await producer.run_configuration(config)
    clock[0] = END + timedelta(hours=1)
    await producer.run_configuration(config)
    assert calls == [END + timedelta(minutes=59)] * 2 + [END + timedelta(hours=1)]
    requests = producer._source.requests
    assert [r.trigger for r in requests] == ["manual_replay", "manual_replay", "scheduled_poll"]
    assert len({r.identity.source_execution_key for r in requests}) == 3
    (scheduled,) = await store.list_provisional_observations(**owner, limit=5)
    assert scheduled.checked_at == END + timedelta(hours=1)
    assert scheduled.next_due_at == END + timedelta(hours=2)


async def test_manual_completion_dispatches_the_scheduled_request_in_history() -> None:
    producer, store, config, obligation, _, clock = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], finality="snapshot"
    )
    await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(), 0, ProvisionalPolicy(timedelta(days=1), timedelta(hours=1))
        )
    )
    clock[0] += timedelta(minutes=10)
    revision = await producer.acquire_obligation(
        config, obligation, restate=True, manual_replay=True
    )
    assert revision.finality == "snapshot"
    history = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    (dispatched,) = producer._source.requests
    assert dispatched == history.acquisition.request(deadline_at=dispatched.deadline_at)
    assert dispatched.trigger == "scheduled_poll"


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
async def test_concurrent_scheduled_reservation_blocks_manual_snapshot(
    cutoff_backend: Any, monkeypatch: Any
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(factory)
    first = await producer.acquire_obligation(config, obligation)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    initial = await store.get_provisional_observation(**owner)
    clock[0] += timedelta(hours=1)
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], observation=1
    )
    scheduled = ProvisionalAcquisition(
        request.model_dump_json(),
        1,
        initial.acquisition.policy,
        first.reporting_revision_id,
        initial.provisional_until,
    )
    read = producer._object_reader.read

    async def reserve_during_read(**kwargs: Any) -> bytes:
        await store.reserve_provisional_acquisition(scheduled)
        return await read(**kwargs)

    def changed(request: Any) -> list[dict[str, Any]]:
        return [{"media_buy_id": "media-buy-redacted", "impressions": 99, "spend": "1.25"}]

    monkeypatch.setattr(producer._object_reader, "read", reserve_during_read)
    monkeypatch.setattr(producer._source, "_fetch", changed)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.acquire_obligation(config, obligation, restate=True)
    assert conflict.value.code == "OBSERVATION_CONFLICT"
    assert await store.get_provisional_observation(**owner) == initial
    assert (await store.get_provisional_acquisition(**owner, ordinal=1)).request() == request
    assert [r.reporting_revision_id for r in await store.list_revisions(**owner)] == [
        first.reporting_revision_id
    ]


@pytest.mark.parametrize(
    "change", ["namespace", "metric", "constituent", "rows", "caller_mutation"]
)
async def test_direct_reserved_snapshot_verifies_authority_and_staged_content(
    change: str, monkeypatch: Any
) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    request = producer._build_slice(
        config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], finality="snapshot"
    )
    reserved = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(), 0, ProvisionalPolicy(timedelta(days=1), timedelta(hours=1))
        )
    )
    if change == "namespace":
        request = request.model_copy(update={"publication_namespace": "another-source"})
    elif change == "metric":
        request = request.model_copy(update={"requested_metrics": ["impressions"]})
    elif change == "constituent":
        constituents = [
            request.coverage.constituents[0].model_copy(update={"product_id": "other-product"})
        ]
        request = request.model_copy(
            update={
                "coverage": request.coverage.model_copy(
                    update={
                        "constituents": constituents,
                        "denominator_fingerprint": coverage_denominator_fingerprint_v1(
                            constituents
                        ),
                    }
                )
            }
        )
    _, manifest, producer._source.reader = redacted_completed_result(
        request, page_bodies=[PROVIDER_ROW]
    )
    caller = [dict(row) for row in ROWS]
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    if change == "caller_mutation":
        commit = store.commit_provisional_observation

        async def alter_caller(*args: Any) -> Any:
            caller[0]["campaign_id"] = "caller-mutated"
            return await commit(*args)

        monkeypatch.setattr(store, "commit_provisional_observation", alter_caller)
        revision = await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=caller,
            acquisition=reserved,
            finality="snapshot",
        )
        page = await store.read_revision_rows(
            account_id=config.account_id, reporting_revision_id=revision.reporting_revision_id
        )
        assert list(page.rows) == ROWS
    else:
        if change == "rows":
            caller[0]["campaign_id"] = "other-campaign"
        with pytest.raises(LedgerConflictError) as conflict:
            await producer.commit_revision_from_manifest(
                obligation,
                manifest,
                rows=caller,
                acquisition=reserved,
                finality="snapshot",
            )
        assert conflict.value.code == (
            "STAGED_OBJECT_MISMATCH" if change == "rows" else "MANIFEST_MISMATCH"
        )
        assert not await store.list_revisions(**owner)
        assert await store.get_provisional_acquisition(**owner, ordinal=0) == reserved


@pytest.mark.parametrize("explicit", [False, True])
async def test_direct_reserved_snapshot_anchors_the_next_poll_after_staging(explicit: bool) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    start = clock[0] = END + timedelta(hours=2)
    request = producer._build_slice(config, obligation, SNAPSHOT_OFFERING_ID, now=start)
    acquisition = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(), 0, ProvisionalPolicy(timedelta(days=1), timedelta(hours=1))
        )
    )
    _, manifest, producer._source.reader = redacted_completed_result(
        request, page_bodies=[PROVIDER_ROW]
    )
    producer._source.read_delay = timedelta(minutes=15)
    revision = await producer.commit_revision_from_manifest(
        obligation,
        manifest,
        rows=ROWS,
        finality="snapshot",
        acquisition=acquisition,
        now=start if explicit else None,
    )
    observation = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert clock[0] == END + timedelta(hours=2, minutes=15)
    anchor = start if explicit else END + timedelta(hours=2, minutes=15)
    assert revision.created_at == observation.checked_at == anchor
    assert observation.next_due_at == anchor + timedelta(hours=1)


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("unavailable", ["reader", "object", "publication_store"])
async def test_retained_snapshot_replay_survives_source_staging_expiry(
    cutoff_backend: Any, unavailable: str
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(
        factory, finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    first = await producer.acquire_obligation(config, obligation, track_settling=True)
    clock[0] += timedelta(hours=1)
    assert first == await producer.acquire_obligation(
        config, obligation, restate=True, track_settling=True
    )
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    history = await store.list_provisional_observations(**owner, limit=5)
    assert len(history) == 2
    checkpoint = await store.get_restatement_checkpoint(**owner)
    page = await store.read_revision_rows(
        account_id=config.account_id, reporting_revision_id=first.reporting_revision_id
    )
    if unavailable == "object":
        producer._source.reader._objects.clear()
    else:
        producer._object_reader = None
        if unavailable == "publication_store":
            producer._store = SimpleNamespace(
                get_obligation=store.get_obligation,
                list_revisions=store.list_revisions,
                get_revision=store.get_revision,
                reserve_provisional_acquisition=store.reserve_provisional_acquisition,
                get_provisional_observation=store.get_provisional_observation,
                commit_provisional_observation=store.commit_provisional_observation,
            )
    turn = WorkerTurn()
    for observation in history:
        manifest = SourceBatchManifestV1.model_validate_json(observation.manifest_json)
        replayed = await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=page.rows,
            finality="snapshot",
            acquisition=observation.acquisition,
            turn=turn,
        )
        assert replayed == first
    assert not turn.did_work
    assert await store.list_provisional_observations(**owner, limit=5) == history
    assert await store.get_restatement_checkpoint(**owner) == checkpoint


@pytest.mark.parametrize("change", ["rows", "manifest", "acquisition", "new_acquisition"])
async def test_retained_snapshot_replay_requires_the_exact_ledger_binding(change: str) -> None:
    producer, store, config, obligation, _, clock = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    first = await producer.acquire_obligation(config, obligation, track_settling=True)
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    stored = await store.get_provisional_observation(**owner)
    checkpoint = await store.get_restatement_checkpoint(**owner)
    acquisition = stored.acquisition
    manifest = SourceBatchManifestV1.model_validate_json(stored.manifest_json)
    rows = [dict(row) for row in ROWS]
    if change == "rows":
        rows[0]["impressions"] = 11
    elif change == "manifest":
        evidence = manifest.finality_evidence.model_copy(update={"evidence_ref": "another"})
        manifest = manifest.model_copy(update={"finality_evidence": evidence})
    elif change == "acquisition":
        acquisition = replace(
            acquisition, policy=ProvisionalPolicy(timedelta(days=2), timedelta(hours=2))
        )
    else:
        request = producer._build_slice(
            config, obligation, SNAPSHOT_OFFERING_ID, now=clock[0], observation=1
        )
        acquisition = await store.reserve_provisional_acquisition(
            ProvisionalAcquisition(
                request.model_dump_json(),
                1,
                stored.acquisition.policy,
                first.reporting_revision_id,
                stored.provisional_until,
            )
        )
        _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    producer._object_reader = None
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation, manifest, rows=rows, acquisition=acquisition, finality="snapshot"
        )
    if change == "rows":
        assert conflict.value.code == "REVISION_IMMUTABLE"
    else:
        assert conflict.value.code == "STAGED_OBJECT_UNAVAILABLE"
    if change in {"manifest", "acquisition"}:
        producer._object_reader = producer._source
        with pytest.raises(LedgerConflictError) as mismatch:
            await producer.commit_revision_from_manifest(
                obligation, manifest, rows=rows, acquisition=acquisition, finality="snapshot"
            )
        assert mismatch.value.code == "OBSERVATION_CONFLICT"
    assert await store.list_provisional_observations(**owner, limit=5) == (stored,)
    assert await store.get_restatement_checkpoint(**owner) == checkpoint
    assert await store.list_revisions(**owner) == (first,)


@pytest.mark.parametrize("different", ["source_publication", "control_totals", "typed_totals"])
async def test_retained_snapshot_replay_checks_the_revision_source_publication(
    different: str,
    monkeypatch: Any,
) -> None:
    producer, store, config, obligation, _, _ = await _harness(
        finality_policy=ReportingContractualCutoffPolicy("cutoff", "P2D")
    )
    commit = store.commit_provisional_observation

    async def inconsistent(observation: Any, revision: Any, rows: Any) -> Any:
        if different == "source_publication":
            revision = replace(revision, source_publication_id="unrelated")
        else:
            totals = (
                (("impressions", "11"),)
                if different == "control_totals"
                else revision.control_totals
            )
            typed = (
                (replace(revision.managed_control_totals[0], unit="other"),)
                if different == "typed_totals"
                else None
            )
            revision = replace(
                revision,
                control_totals=totals,
                managed_control_totals=typed,
                revision_content_sha256=revision_content_sha256(
                    reporting_revision_id=revision.reporting_revision_id,
                    row_count=revision.row_count,
                    control_totals=totals,
                    reporting_rows=rows,
                    control_total_evidence=typed,
                ),
            )
        return await commit(observation, revision, rows)

    with monkeypatch.context() as patch:
        patch.setattr(store, "commit_provisional_observation", inconsistent)
        await producer.acquire_obligation(config, obligation, track_settling=True)
    retained = await store.get_provisional_observation(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    producer._object_reader = None
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            SourceBatchManifestV1.model_validate_json(retained.manifest_json),
            rows=ROWS,
            finality="snapshot",
            acquisition=retained.acquisition,
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("route", ["snapshot", "authoritative", "cutoff"])
async def test_store_rejects_revision_outside_reserved_output_finality(
    cutoff_backend: Any, route: str
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(factory)
    source_final = route == "authoritative"
    request = producer._build_slice(
        config,
        obligation,
        OFFICIAL_OFFERING_ID if source_final else SNAPSHOT_OFFERING_ID,
        now=clock[0],
        finality="official" if source_final else "snapshot",
    )
    acquisition = await store.reserve_provisional_acquisition(
        ProvisionalAcquisition(
            request.model_dump_json(),
            0,
            ProvisionalPolicy(timedelta(days=1), timedelta(hours=1)),
            output_finality="official" if route == "cutoff" else None,
        )
    )
    _, manifest, _ = redacted_completed_result(request, page_bodies=[PROVIDER_ROW])
    wrong_official = route == "snapshot"
    revision = ReportingRevisionRecord(
        reporting_revision_id="rpr_wrong_finality",
        account_id=config.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality="official" if wrong_official else "snapshot",
        finality_policy_id="selected" if wrong_official else None,
        finality_basis="source_final" if wrong_official else None,
        finalized_at=clock[0] if wrong_official else None,
        revision_content_sha256="a" * 64,
        row_count=1,
        control_totals=(),
        observed_at=clock[0],
        data_through=END,
        created_at=clock[0],
    )
    observation = ProvisionalObservation(
        acquisition,
        revision.reporting_revision_id,
        clock[0],
        END + timedelta(days=1),
        None,
        manifest.model_dump_json(),
    )
    with pytest.raises(LedgerConflictError) as conflict:
        await store.commit_provisional_observation(observation, revision, ROWS)
    assert conflict.value.code == "OBSERVATION_CONFLICT"
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert not await store.list_revisions(**owner)
    assert await store.get_provisional_observation(**owner) is None
    assert await store.get_restatement_checkpoint(**owner) is None
    assert await store.get_provisional_acquisition(**owner, ordinal=0) == acquisition


@pytest.mark.parametrize("cutoff_backend", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("prior_visible", [False, True])
@pytest.mark.parametrize(
    ("basis", "change", "core_only"),
    [
        ("source_final", "identical", False),
        ("source_final", "finalized_at", False),
        ("source_final", "provider_basis", False),
        ("source_final", "provider_basis", True),
        ("contractual_cutoff", "identical", False),
        ("contractual_cutoff", "evidence_ref", False),
    ],
)
async def test_concurrent_official_acquisitions_require_the_same_source_evidence(
    cutoff_backend: Any,
    prior_visible: bool,
    basis: str,
    change: str,
    core_only: bool,
    monkeypatch: Any,
) -> None:
    factory, _ = cutoff_backend
    producer, store, config, obligation, _, clock = await _harness(
        factory,
        official=True,
        finality_policy=(
            ReportingSourceFinalPolicy("source", "delivery.is_final")
            if basis == "source_final"
            else ReportingContractualCutoffPolicy("cutoff", "PT2H")
        ),
    )
    clock[0] = END + timedelta(hours=2)
    if core_only:
        producer._store = CoreStore(store)
    other_source = EvidenceSource(producer._source.capabilities, clock, [])
    if change == "provider_basis":
        other_source.basis = "provider_job_terminal"
    elif change in {"finalized_at", "evidence_ref"}:
        execute = other_source.execute

        async def changed_evidence(request: Any, **kwargs: Any) -> Any:
            await execute(request, **kwargs)
            manifest = other_source.manifest
            evidence = manifest.finality_evidence.model_copy(
                update=(
                    {"observed_at": clock[0] - timedelta(seconds=1)}
                    if change == "finalized_at"
                    else {"evidence_ref": "other-observation"}
                )
            )
            raw = encode_source_batch_manifest_v1(
                manifest.model_copy(update={"finality_evidence": evidence})
            )
            return ReportingSourceExecutorResult.completed(
                request=request,
                manifest=source_batch_manifest_reference_v1("manifest-redacted-v1", raw),
                manifest_bytes=raw,
            )

        monkeypatch.setattr(other_source, "execute", changed_evidence)
    other = ReportingProducer(
        store=producer._store,
        retry_store=producer._retry_store,
        source=other_source,
        object_reader=other_source,
        offerings=producer._offerings,
        clock=lambda: clock[0] + timedelta(minutes=1),
    )
    turns = [WorkerTurn(), WorkerTurn()]
    both_staged, first_published = asyncio.Event(), asyncio.Event()
    hook = "_commit_revision_from_manifest" if prior_visible else "_commit_publication"
    first_commit, second_commit = getattr(producer, hook), getattr(other, hook)

    async def publish_first(*args: Any, **kwargs: Any) -> Any:
        await asyncio.wait_for(both_staged.wait(), timeout=5)
        try:
            return await first_commit(*args, **kwargs)
        finally:
            first_published.set()

    async def publish_second(*args: Any, **kwargs: Any) -> Any:
        both_staged.set()
        await asyncio.wait_for(first_published.wait(), timeout=5)
        return await second_commit(*args, **kwargs)

    monkeypatch.setattr(producer, hook, publish_first)
    monkeypatch.setattr(other, hook, publish_second)
    winner, contender = await asyncio.gather(
        producer.acquire_obligation(config, obligation, turn=turns[0], now=clock[0]),
        other.acquire_obligation(config, obligation, turn=turns[1], now=clock[0]),
        return_exceptions=True,
    )
    assert isinstance(winner, ReportingRevisionRecord)
    assert other_source.requests[0].identity == producer._source.requests[0].identity
    if change == "identical":
        assert contender == winner
    else:
        assert isinstance(contender, LedgerConflictError)
        assert contender.code in {"REVISION_IMMUTABLE", "OBSERVATION_CONFLICT"}
    owner = dict(
        account_id=config.account_id, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert await store.list_revisions(**owner) == (winner,)
    history = await store.list_provisional_observations(**owner, limit=5)
    if core_only:
        assert not history
        assert await producer.acquire_obligation(config, obligation) is None
        assert (
            await producer.commit_revision_from_manifest(
                obligation, producer._source.manifest, rows=ROWS, finality="official"
            )
            == winner
        )
        with pytest.raises(LedgerConflictError) as direct_conflict:
            await producer.commit_revision_from_manifest(
                obligation,
                other_source.manifest,
                rows=ROWS,
                finality="official",
                finality_proof=manifest_finality_proof(obligation, other_source.manifest, clock[0]),
            )
        assert direct_conflict.value.code == "REVISION_IMMUTABLE"
    else:
        (observation,) = history
        assert (
            SourceBatchManifestV1.model_validate_json(observation.manifest_json)
            == producer._source.manifest
        )
    assert sum(len(turn.revisions_committed) for turn in turns) == 1
