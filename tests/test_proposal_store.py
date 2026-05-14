"""Tests for ProposalStore Protocol + InMemoryProposalStore reference impl.

Covers:

* Protocol conformance — ``isinstance(InMemoryProposalStore(), ProposalStore)``
* State-machine guards — DRAFT → COMMITTED → CONSUMED transitions, with
  invalid transitions raising INTERNAL_ERROR.
* put_draft idempotency on refine iterations — same proposal_id
  overwrites, draft-creation timestamp preserved.
* commit idempotency on equal payload + expires_at; raise on diverging
  values.
* mark_consumed records media_buy_id back-reference.
* get_by_media_buy_id round-trips after consume.
* Cross-tenant safety — get / get_by_media_buy_id with mismatched
  expected_account_id return None, not the raw record.
* Eviction — drafts older than draft_ttl, committed older than
  expires_at + committed_grace.
* discard idempotency — discarding unknown id is a no-op.
* create_dev_proposal_store warns on construction.
* is_durable class var is False for the in-memory ref.

The Protocol contract is tested via the public API surface, not by
poking at internal storage. Mirrors the test posture in
``tests/test_decisioning_task_registry.py``.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from adcp.decisioning import (
    AdcpError,
    InMemoryProposalStore,
    ProposalRecord,
    ProposalState,
    ProposalStore,
    Recipe,
    create_dev_proposal_store,
)


class _DemoRecipe(Recipe):
    """Minimal Recipe subclass for tests — demonstrates the discriminator
    + arbitrary typed field pattern."""

    recipe_kind: str = "demo"
    line_item_id: str = "li_demo"


def _utc(dt_str: str) -> datetime:
    """Build a tz-aware datetime from an ISO string for clock-pinned tests."""
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


@pytest.fixture
def store() -> InMemoryProposalStore:
    return InMemoryProposalStore()


@pytest.fixture
def fixed_clock() -> Any:
    """Pinned clock for eviction tests — first call returns t0, subsequent
    calls advance by the test's manipulation. Single mutable cell.
    """
    state = {"now": _utc("2026-01-01T00:00:00")}

    def advance(delta: timedelta) -> None:
        state["now"] = state["now"] + delta

    def now() -> datetime:
        return state["now"]

    now.advance = advance  # type: ignore[attr-defined]
    return now


# ---------------------------------------------------------------------------
# Protocol + class invariants
# ---------------------------------------------------------------------------


def test_in_memory_store_satisfies_protocol() -> None:
    assert isinstance(InMemoryProposalStore(), ProposalStore)


def test_in_memory_store_is_not_durable() -> None:
    """is_durable=False drives the production-mode gate — must be the
    hard-coded class var, not a constructor flag."""
    assert InMemoryProposalStore.is_durable is False


def test_create_dev_proposal_store_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        store = create_dev_proposal_store()
        assert isinstance(store, InMemoryProposalStore)
        assert any("do NOT use in production" in str(w.message) for w in caught)


# ---------------------------------------------------------------------------
# put_draft + get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_draft_then_get_round_trips(store: InMemoryProposalStore) -> None:
    recipes = {"prod_1": _DemoRecipe(line_item_id="li_1")}
    payload = {"proposal_id": "p1", "products": []}
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes=recipes,
        proposal_payload=payload,
    )
    record = await store.get("p1")
    assert record is not None
    assert record.proposal_id == "p1"
    assert record.account_id == "acct_a"
    assert record.state == ProposalState.DRAFT
    assert record.recipes["prod_1"].line_item_id == "li_1"  # type: ignore[attr-defined]
    assert record.proposal_payload == payload
    assert record.expires_at is None
    assert record.media_buy_id is None
    assert record.recipe_schema_version == 1


@pytest.mark.asyncio
async def test_get_unknown_proposal_returns_none(store: InMemoryProposalStore) -> None:
    assert await store.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_put_draft_overwrites_existing_draft(store: InMemoryProposalStore) -> None:
    """Refine iterations call put_draft with the same proposal_id."""
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={"prod_1": _DemoRecipe(line_item_id="li_1")},
        proposal_payload={"v": 1},
    )
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={"prod_1": _DemoRecipe(line_item_id="li_2")},
        proposal_payload={"v": 2},
    )
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.DRAFT
    assert record.recipes["prod_1"].line_item_id == "li_2"  # type: ignore[attr-defined]
    assert record.proposal_payload == {"v": 2}


@pytest.mark.asyncio
async def test_put_draft_rejects_overwrite_of_committed(store: InMemoryProposalStore) -> None:
    """Once committed, the proposal_id is immutable — refine must not
    overwrite. The state-machine guard raises INTERNAL_ERROR (framework
    bug, not buyer bug)."""
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={"committed": True},
        expected_account_id="acct_a",
    )
    with pytest.raises(AdcpError) as exc:
        await store.put_draft(
            proposal_id="p1",
            account_id="acct_a",
            recipes={},
            proposal_payload={},
        )
    assert exc.value.code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_promotes_draft_to_committed(store: InMemoryProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={"v": 1},
    )
    expires = _utc("2099-01-02T00:00:00")
    await store.commit(
        "p1", expires_at=expires, proposal_payload={"committed": True}, expected_account_id="acct_a"
    )
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.COMMITTED
    assert record.expires_at == expires
    assert record.proposal_payload == {"committed": True}


@pytest.mark.asyncio
async def test_commit_idempotent_on_equal_payload(store: InMemoryProposalStore) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = _utc("2099-01-02T00:00:00")
    await store.commit(
        "p1", expires_at=expires, proposal_payload={"x": 1}, expected_account_id="acct_a"
    )
    # Same args → no-op.
    await store.commit(
        "p1", expires_at=expires, proposal_payload={"x": 1}, expected_account_id="acct_a"
    )
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.COMMITTED


@pytest.mark.asyncio
async def test_commit_rejects_diverging_payload(store: InMemoryProposalStore) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = _utc("2099-01-02T00:00:00")
    await store.commit(
        "p1", expires_at=expires, proposal_payload={"x": 1}, expected_account_id="acct_a"
    )
    with pytest.raises(AdcpError) as exc:
        await store.commit(
            "p1", expires_at=expires, proposal_payload={"x": 2}, expected_account_id="acct_a"
        )
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_commit_unknown_proposal_raises(store: InMemoryProposalStore) -> None:
    with pytest.raises(AdcpError) as exc:
        await store.commit(
            "p-missing",
            expires_at=_utc("2099-01-02T00:00:00"),
            proposal_payload={},
            expected_account_id="acct_a",
        )
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_commit_from_consumed_raises(store: InMemoryProposalStore) -> None:
    """Once consumed, a proposal cannot transition back to committed."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p1", media_buy_id="mb_1", expected_account_id="acct_a")
    with pytest.raises(AdcpError) as exc:
        await store.commit(
            "p1",
            expires_at=_utc("2099-01-02T00:00:00"),
            proposal_payload={},
            expected_account_id="acct_a",
        )
    assert exc.value.code == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# mark_consumed + get_by_media_buy_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_consumed_records_media_buy_back_reference(
    store: InMemoryProposalStore,
) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p1", media_buy_id="mb_42", expected_account_id="acct_a")
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == "mb_42"

    # Reverse-index lookup hydrates the same record.
    by_buy = await store.get_by_media_buy_id("mb_42", expected_account_id="acct_a")
    assert by_buy is not None
    assert by_buy.proposal_id == "p1"
    assert by_buy.recipes is not None


@pytest.mark.asyncio
async def test_mark_consumed_idempotent_on_same_media_buy_id(
    store: InMemoryProposalStore,
) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p1", media_buy_id="mb_42", expected_account_id="acct_a")
    # Same media_buy_id → no-op.
    await store.mark_consumed("p1", media_buy_id="mb_42", expected_account_id="acct_a")


@pytest.mark.asyncio
async def test_mark_consumed_different_media_buy_id_raises(
    store: InMemoryProposalStore,
) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p1", media_buy_id="mb_42", expected_account_id="acct_a")
    with pytest.raises(AdcpError) as exc:
        await store.mark_consumed("p1", media_buy_id="mb_99", expected_account_id="acct_a")
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_mark_consumed_from_draft_raises(store: InMemoryProposalStore) -> None:
    """The state-machine guard requires COMMITTED — adopters must finalize
    before marking consumed."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    with pytest.raises(AdcpError) as exc:
        await store.mark_consumed("p1", media_buy_id="mb_1", expected_account_id="acct_a")
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_get_by_media_buy_id_unknown_returns_none(
    store: InMemoryProposalStore,
) -> None:
    assert await store.get_by_media_buy_id("mb_unknown", expected_account_id="acct_a") is None


# ---------------------------------------------------------------------------
# Cross-tenant safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none(store: InMemoryProposalStore) -> None:
    """Probe with a mismatched account_id returns None — never the raw
    record. Critical for principal-enumeration defense."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    assert await store.get("p1", expected_account_id="acct_b") is None
    # Same-account probe still works.
    assert await store.get("p1", expected_account_id="acct_a") is not None


@pytest.mark.asyncio
async def test_get_by_media_buy_id_cross_tenant_returns_none(
    store: InMemoryProposalStore,
) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p1", media_buy_id="mb_42", expected_account_id="acct_a")
    assert await store.get_by_media_buy_id("mb_42", expected_account_id="other") is None
    assert await store.get_by_media_buy_id("mb_42", expected_account_id="acct_a") is not None


# ---------------------------------------------------------------------------
# Two-phase commit — try_reserve / finalize / release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_reserve_consumption_transitions_to_consuming(
    store: InMemoryProposalStore,
) -> None:
    """Atomic CAS COMMITTED → CONSUMING; record returned, state visible."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )

    reserved = await store.try_reserve_consumption("p1", expected_account_id="acct_a")
    assert reserved.state == ProposalState.CONSUMING

    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None and record.state == ProposalState.CONSUMING


@pytest.mark.asyncio
async def test_try_reserve_consumption_concurrent_only_one_wins(
    store: InMemoryProposalStore,
) -> None:
    """The race the two-phase commit is built to prevent. Two callers
    invoke try_reserve_consumption concurrently; exactly one returns the
    reserved record, the other raises PROPOSAL_NOT_COMMITTED. Without
    the atomic CAS, both check-then-act would pass and the inventory
    hold would be double-spent."""
    import asyncio

    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )

    async def _try() -> str:
        try:
            await store.try_reserve_consumption("p1", expected_account_id="acct_a")
            return "ok"
        except AdcpError as exc:
            return exc.code

    results = await asyncio.gather(_try(), _try())
    # One success; one PROPOSAL_NOT_COMMITTED. The asyncio.Lock guarantees
    # serial execution; the second caller observes the first's transition.
    assert sorted(results) == ["PROPOSAL_NOT_COMMITTED", "ok"]


@pytest.mark.asyncio
async def test_release_consumption_rolls_back_to_committed(
    store: InMemoryProposalStore,
) -> None:
    """Rollback path for adapter failure: CONSUMING → COMMITTED. The
    buyer can retry without PROPOSAL_NOT_COMMITTED blocking them."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.try_reserve_consumption("p1", expected_account_id="acct_a")

    await store.release_consumption("p1", expected_account_id="acct_a")
    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None and record.state == ProposalState.COMMITTED

    # After rollback, a fresh reserve succeeds — exactly the buyer-retry
    # behaviour the rollback exists to preserve.
    reserved = await store.try_reserve_consumption("p1", expected_account_id="acct_a")
    assert reserved.state == ProposalState.CONSUMING


@pytest.mark.asyncio
async def test_release_consumption_idempotent_on_committed(
    store: InMemoryProposalStore,
) -> None:
    """Releasing a record already in COMMITTED is a no-op (not an error).
    Lets the adapter-failure rollback path be unconditional even when
    something else has already rolled back."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    # No reserve — just release. Should not raise.
    await store.release_consumption("p1", expected_account_id="acct_a")
    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None and record.state == ProposalState.COMMITTED


@pytest.mark.asyncio
async def test_finalize_consumption_promotes_to_consumed(
    store: InMemoryProposalStore,
) -> None:
    """Happy path: reserve + finalize_consumption transitions to
    CONSUMED with the media_buy_id back-reference."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.try_reserve_consumption("p1", expected_account_id="acct_a")
    await store.finalize_consumption("p1", media_buy_id="mb_42", expected_account_id="acct_a")
    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == "mb_42"

    # Reverse-index lookup hydrates correctly.
    by_buy = await store.get_by_media_buy_id("mb_42", expected_account_id="acct_a")
    assert by_buy is not None and by_buy.proposal_id == "p1"


@pytest.mark.asyncio
async def test_finalize_consumption_from_committed_raises(
    store: InMemoryProposalStore,
) -> None:
    """Calling finalize_consumption without first reserving is a
    framework bug and surfaces as INTERNAL_ERROR."""
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    with pytest.raises(AdcpError) as exc:
        await store.finalize_consumption("p1", media_buy_id="mb_1", expected_account_id="acct_a")
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_media_buy_id_collision_across_tenants_does_not_clobber(
    store: InMemoryProposalStore,
) -> None:
    """Adopter-controlled media_buy_ids can collide across tenants
    (sequential IDs, deterministic test fixtures, etc.). The reverse
    index must be keyed by (account_id, media_buy_id) so tenant A's
    entry is preserved when tenant B writes the same id."""
    # Tenant A consumes proposal p_a with media_buy_id "mb_001".
    await store.put_draft(proposal_id="p_a", account_id="acct_a", recipes={}, proposal_payload={})
    await store.commit(
        "p_a",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.mark_consumed("p_a", media_buy_id="mb_001", expected_account_id="acct_a")

    # Tenant B consumes proposal p_b with the SAME media_buy_id "mb_001".
    await store.put_draft(proposal_id="p_b", account_id="acct_b", recipes={}, proposal_payload={})
    await store.commit(
        "p_b",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_b",
    )
    await store.mark_consumed("p_b", media_buy_id="mb_001", expected_account_id="acct_b")

    # Both reverse-index lookups still resolve correctly under their
    # authenticated tenant. Without the tuple key, tenant A would lose
    # its mapping the moment tenant B wrote the same media_buy_id.
    a_record = await store.get_by_media_buy_id("mb_001", expected_account_id="acct_a")
    b_record = await store.get_by_media_buy_id("mb_001", expected_account_id="acct_b")
    assert a_record is not None and a_record.proposal_id == "p_a"
    assert b_record is not None and b_record.proposal_id == "p_b"


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_removes_record(store: InMemoryProposalStore) -> None:
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    await store.discard("p1", expected_account_id="acct_a")
    assert await store.get("p1") is None


@pytest.mark.asyncio
async def test_discard_unknown_is_noop(store: InMemoryProposalStore) -> None:
    """Mirrors TaskRegistry.discard — discarding an unknown id is a no-op."""
    await store.discard("never-existed", expected_account_id="acct_a")  # no raise


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_evicted_after_ttl(fixed_clock: Any) -> None:
    """A 24h-old draft is evicted on the next operation."""
    store = InMemoryProposalStore(
        draft_ttl=timedelta(hours=24),
        clock=fixed_clock,
    )
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    # Just after creation — record still present.
    assert await store.get("p1") is not None
    fixed_clock.advance(timedelta(hours=23))
    assert await store.get("p1") is not None
    fixed_clock.advance(timedelta(hours=2))  # 25h total
    # Eviction runs on the next get.
    assert await store.get("p1") is None


@pytest.mark.asyncio
async def test_committed_evicted_past_grace(fixed_clock: Any) -> None:
    store = InMemoryProposalStore(
        committed_grace=timedelta(days=7),
        clock=fixed_clock,
    )
    await store.put_draft(proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={})
    expires = fixed_clock() + timedelta(hours=1)
    await store.commit("p1", expires_at=expires, proposal_payload={}, expected_account_id="acct_a")
    # 1h past commit — committed window not even reached.
    fixed_clock.advance(timedelta(hours=2))
    assert await store.get("p1") is not None
    # 8d past expires — beyond grace.
    fixed_clock.advance(timedelta(days=8))
    assert await store.get("p1") is None


@pytest.mark.asyncio
async def test_refine_iteration_preserves_creation_time(fixed_clock: Any) -> None:
    """Refine iterations on the same proposal_id MUST NOT reset the TTL
    anchor; otherwise a buyer in a long refine session keeps their
    draft alive past the eviction window the framework promised."""
    store = InMemoryProposalStore(
        draft_ttl=timedelta(hours=24),
        clock=fixed_clock,
    )
    await store.put_draft(
        proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={"v": 1}
    )
    fixed_clock.advance(timedelta(hours=20))
    await store.put_draft(
        proposal_id="p1", account_id="acct_a", recipes={}, proposal_payload={"v": 2}
    )
    fixed_clock.advance(timedelta(hours=5))  # 25h since FIRST put_draft
    # Even though we just refined, the original put_draft was 25h ago.
    assert await store.get("p1") is None


# ---------------------------------------------------------------------------
# Sync-method support — adopter Stores returning plain values, not coros
# ---------------------------------------------------------------------------


class _SyncStubStore:
    """Minimal sync ProposalStore — exercises the MaybeAsync contract.

    Adopters returning plain dicts (sync DB driver, simple in-memory)
    instead of coroutines must round-trip cleanly through the framework's
    _await_maybe helper.
    """

    is_durable = False

    def __init__(self) -> None:
        self._records: dict[str, ProposalRecord] = {}

    def put_draft(self, *, proposal_id, account_id, recipes, proposal_payload) -> None:
        self._records[proposal_id] = ProposalRecord(
            proposal_id=proposal_id,
            account_id=account_id,
            state=ProposalState.DRAFT,
            recipes=recipes,
            proposal_payload=proposal_payload,
        )

    def get(self, proposal_id, *, expected_account_id=None):
        record = self._records.get(proposal_id)
        if record is None:
            return None
        if expected_account_id is not None and record.account_id != expected_account_id:
            return None
        return record

    def commit(self, proposal_id, *, expires_at, proposal_payload, expected_account_id):
        return None

    def try_reserve_consumption(self, proposal_id, *, expected_account_id):
        record = self._records.get(proposal_id)
        if record is None or record.account_id != expected_account_id:
            raise AdcpError("PROPOSAL_NOT_FOUND", message="not found", recovery="terminal")
        return record

    def finalize_consumption(self, proposal_id, *, media_buy_id, expected_account_id):
        return None

    def release_consumption(self, proposal_id, *, expected_account_id):
        return None

    def mark_consumed(self, proposal_id, *, media_buy_id, expected_account_id):
        return None

    def discard(self, proposal_id, *, expected_account_id):
        return None

    def get_by_media_buy_id(self, media_buy_id, *, expected_account_id):
        return None


def test_sync_store_satisfies_protocol() -> None:
    """A sync impl satisfies the runtime_checkable Protocol — methods are
    sync OR async per MaybeAsync contract."""
    assert isinstance(_SyncStubStore(), ProposalStore)
