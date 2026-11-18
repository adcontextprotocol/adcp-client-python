"""Official publication binds an exact policy to its qualifying source acquisition."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
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
    revision_content_sha256,
)
from adcp.reporting.ledger.finality import FinalityProof, source_finality_proof
from adcp.reporting.ledger.provisional import ProvisionalAcquisition
from adcp.reporting.source import ReportingFinalityEvidenceV1
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_source_availability import (
    END,
    PROVIDER_ROW,
    _closing_capabilities,
    _harness,
)

ROWS = [{"campaign_id": "campaign-redacted-1", "impressions": 10}]


@pytest.fixture(params=["memory", "postgres"])
async def store_factory(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield InMemoryReportingLedgerStore
    else:
        async with isolated_reporting_pool() as pool:
            yield partial(PgReportingLedgerStore, pool=pool)


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
) -> None:
    scheduled, store, config, obligation, _, clock = await _harness(
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

    turn = await producer.run_configuration(config)
    assert len(turn.revisions_committed) == 1
    revision = await store.get_revision(
        account_id=config.account_id, reporting_revision_id=turn.revisions_committed[0]
    )
    assert revision is not None and revision.finality == "official"
    assert len(calls) == 3
    assert calls[-1].period.source_read_cutoff_at == revision.finalized_at == clock[0]


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
    clock[0] += timedelta(hours=1)
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
