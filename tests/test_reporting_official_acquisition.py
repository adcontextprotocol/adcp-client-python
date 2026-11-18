"""Recoverable acquisition, source authority, and immutable official publication."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from functools import partial
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
)
from adcp.reporting.ledger.finality import manifest_finality_proof
from adcp.reporting.ledger.provisional import ProvisionalAcquisition, ProvisionalPolicy
from adcp.reporting.source import (
    ReportingSourceExecutorResult,
    SourceBatchManifestV1,
    SourceFormatV1,
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


@pytest.fixture(params=["memory", "postgres", "core", "progress"])
async def cutoff_backend(request: Any) -> AsyncIterator[Any]:
    if request.param == "postgres":
        async with isolated_reporting_pool() as pool:
            yield partial(PgReportingLedgerStore, pool=pool), False
    else:
        yield (
            ProgressStore if request.param == "progress" else InMemoryReportingLedgerStore
        ), request.param == "core"


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
async def test_manual_snapshot_obeys_winning_close(collision: str, monkeypatch: Any) -> None:
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
    revision = await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", restate=True, manual_replay=True
    )
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


async def test_satisfied_manual_snapshot_recovers_close_without_lookup(monkeypatch: Any) -> None:
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
    revision = await producer.acquire_obligation(
        config, obligation, target_finality="snapshot", manual_replay=True
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
        assert [o.acquisition.request().trigger for o in history] == ["scheduled_poll"] + [
            "manual_replay"
        ] * 3


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
