from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adcp.compat.proposal_evidence import (
    ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION,
    EstablishedProposalEvidence,
    EstablishedProposalMutationReplayStore,
    InMemoryEstablishedProposalEvidenceStore,
    ProposalEvidenceChangedError,
    ProposalMutationConflictError,
    ProposalMutationKind,
    ProposalStoreCapacityError,
)

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)
SCOPE = {
    "principal_id": "principal-acme",
    "target_binding": "seller-session-acme",
    "account_identity": '{"account_id":"account-acme"}',
    "source_adcp_version": "3.0.18",
}


def evidence(
    proposal_id: str = "proposal-1",
    *,
    expires_at: datetime | None = None,
    name: str = "Original",
) -> EstablishedProposalEvidence:
    proposal: dict[str, object] = {"proposal_id": proposal_id, "name": name}
    if expires_at is not None:
        proposal["expires_at"] = expires_at.isoformat().replace("+00:00", "Z")
    return EstablishedProposalEvidence.capture(
        proposal,
        **SCOPE,
        observed_request={"brief": "test"},
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_reservation_uses_store_clock_to_reject_expired_evidence() -> None:
    store_now = [NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    row = evidence(expires_at=NOW + timedelta(seconds=1))
    await store.put(row)
    store_now[0] += timedelta(seconds=1)

    with pytest.raises(ProposalEvidenceChangedError, match="expired before reservation"):
        await store.reserve_acceptance(
            row,
            idempotency_key="accept-1",
            request={"proposal_id": row.proposal_id},
            created_at=NOW,
        )
    with pytest.raises(ProposalEvidenceChangedError, match="expired before reservation"):
        await store.reserve_mutation(
            [row],
            operation=ProposalMutationKind.REFINE,
            idempotency_key="refine-1",
            request={"proposal_id": row.proposal_id},
            created_at=NOW,
        )


@pytest.mark.asyncio
async def test_acceptance_reserves_terminal_bytes_before_dispatch() -> None:
    row = evidence()
    baseline = InMemoryEstablishedProposalEvidenceStore(max_terminal_result_bytes=64)
    await baseline.put(row)
    reserved = await baseline.reserve_acceptance(
        row,
        idempotency_key="accept-1",
        request={"proposal_id": row.proposal_id},
        created_at=NOW,
    )
    assert reserved.record.reserved_completion_bytes == 64
    charged_size = baseline._evidence_size(row) + baseline._acceptance_size(reserved.record)

    limited = InMemoryEstablishedProposalEvidenceStore(
        max_bytes=charged_size - 1,
        max_terminal_result_bytes=64,
    )
    await limited.put(row)
    with pytest.raises(ProposalStoreCapacityError):
        await limited.reserve_acceptance(
            row,
            idempotency_key="accept-1",
            request={"proposal_id": row.proposal_id},
            created_at=NOW,
        )

    with pytest.raises(ProposalStoreCapacityError, match="reserved completion capacity"):
        await baseline.complete_acceptance(
            reserved.record,
            {"success": True, "payload": "x" * 64},
        )


@pytest.mark.asyncio
async def test_mutation_reserves_result_and_successor_capacity() -> None:
    row = evidence()
    store = InMemoryEstablishedProposalEvidenceStore(max_terminal_result_bytes=64)
    await store.put(row)

    reservation = await store.reserve_mutation(
        [row],
        operation=ProposalMutationKind.REFINE,
        idempotency_key="refine-1",
        request={"proposal_id": row.proposal_id},
        created_at=NOW,
    )

    assert reservation.record.reserved_completion_bytes > 64
    assert reservation.record.reserved_completion_records == 1
    successor = evidence("proposal-2", name="Successor")
    completed = await store.complete_mutation(
        reservation.record,
        {"success": True},
        replacements=[successor],
    )
    assert completed.reserved_completion_bytes == 0
    assert completed.reserved_completion_records == 0


@pytest.mark.asyncio
async def test_successful_acceptance_prunes_to_compact_permanent_fence() -> None:
    store_now = [NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    row = evidence()
    await store.put(row)
    reservation = await store.reserve_acceptance(
        row,
        idempotency_key="accept-1",
        request={"proposal_id": row.proposal_id},
        created_at=NOW,
    )
    await store.complete_acceptance(reservation.record, {"success": True})

    store_now[0] += ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION
    assert await store.prune_completion_tombstones() == 1
    assert await store.get(row.proposal_id, **SCOPE) is None
    with pytest.raises(ProposalMutationConflictError, match="reserved proposal evidence"):
        await store.put(row)


@pytest.mark.asyncio
async def test_failed_acceptance_pruning_releases_proposal_for_new_attempt() -> None:
    store_now = [NOW]
    store = InMemoryEstablishedProposalEvidenceStore(clock=lambda: store_now[0])
    row = evidence()
    await store.put(row)
    reservation = await store.reserve_acceptance(
        row,
        idempotency_key="accept-1",
        request={"proposal_id": row.proposal_id},
        created_at=NOW,
    )
    await store.complete_acceptance(reservation.record, {"success": False})

    store_now[0] += ESTABLISHED_PROPOSAL_COMPLETION_TOMBSTONE_RETENTION
    assert await store.prune_completion_tombstones() == 1
    retried = await store.reserve_acceptance(
        row,
        idempotency_key="accept-2",
        request={"proposal_id": row.proposal_id},
        created_at=store_now[0],
    )
    assert retried.created


@pytest.mark.asyncio
async def test_completed_mutation_lookup_retains_original_same_id_generation() -> None:
    store = InMemoryEstablishedProposalEvidenceStore()
    assert isinstance(store, EstablishedProposalMutationReplayStore)
    original = evidence(name="Original")
    successor = evidence(name="Successor")
    await store.put(original)
    reservation = await store.reserve_mutation(
        [original],
        operation=ProposalMutationKind.REFINE,
        idempotency_key="refine-1",
        request={"proposal_id": original.proposal_id},
        created_at=NOW,
    )
    await store.complete_mutation(
        reservation.record,
        {"success": True},
        replacements=[successor],
    )

    replay = await store.find_completed_mutation_by_idempotency_key(
        "refine-1",
        **SCOPE,
    )
    assert replay is not None
    assert replay.source_proposals[0]["name"] == "Original"
    assert (await store.get(original.proposal_id, **SCOPE)).proposal["name"] == "Successor"  # type: ignore[union-attr]
    assert (
        await store.find_completed_mutation_by_idempotency_key(
            "refine-1",
            **{**SCOPE, "principal_id": "different-principal"},
        )
        is None
    )
